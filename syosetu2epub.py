import argparse
import base64
import html
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from random import uniform
from typing import Mapping, Optional, TypedDict

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_10_1) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/39.0.2171.95 Safari/537.36"
)

DEFAULT_JOBS = 10
DEFAULT_DELAY = 0.1
DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT = 60
DEFAULT_SKIP_ERRORS = False
SEPARATOR_LINE = "-" * 32
CONFIG_PATH = Path.home() / ".syosetu2epub.json"
CONFIG_OUTPUT_DIR_KEY = "output_dir"
MAX_FILENAME_LEN = 120

MARK_PREFACE = "__PREFACE__"
MARK_PREFACE_END = "__PREFACE_END__"
MARK_AFTERWORD = "__AFTERWORD__"
MARK_AFTERWORD_END = "__AFTERWORD_END__"
MARK_SEPARATOR = "__SEPARATOR__"

_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_JP_PUNCT_MAP = {
    "!": "！",
    "?": "？",
    ":": "：",
    ";": "；",
    ",": "，",
    ".": "．",
    "(": "（",
    ")": "）",
    "[": "［",
    "]": "］",
    "{": "｛",
    "}": "｝",
    '"': "＂",
    "'": "＇",
}
_JP_DIGIT_MAP = {
    "0": "０",
    "1": "１",
    "2": "２",
    "3": "３",
    "4": "４",
    "5": "５",
    "6": "６",
    "7": "７",
    "8": "８",
    "9": "９",
}
_JP_TEXT_TRANSLATION = str.maketrans({**_JP_PUNCT_MAP, **_JP_DIGIT_MAP})
_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_IMG_TAG_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))[^>]*>",
    re.IGNORECASE,
)
_IMG_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
}
_SEPARATOR_CHARS = "-_－＿—–―ｰー─━"
_SEPARATOR_SYMBOLS = "*＊"


class Chapter(TypedDict):
    title: str
    paragraphs: list[str]
    url: str


class ImageItem(TypedDict):
    href: str
    media_type: str
    data: bytes


class Volume(TypedDict):
    title: str
    chapters: list[str]


def normalize_japanese_punct(text: str) -> str:
    if not text:
        return text
    if "http://" not in text and "https://" not in text:
        return translate_japanese_punct(text)
    parts: list[str] = []
    last = 0
    for match in _URL_RE.finditer(text):
        if match.start() > last:
            parts.append(translate_japanese_punct(text[last : match.start()]))
        parts.append(match.group(0))
        last = match.end()
    if last < len(text):
        parts.append(translate_japanese_punct(text[last:]))
    return "".join(parts)


def translate_japanese_punct(text: str) -> str:
    if "." not in text:
        return text.translate(_JP_TEXT_TRANSLATION)
    out: list[str] = []
    text_len = len(text)
    for i, ch in enumerate(text):
        if ch == ".":
            prev = text[i - 1] if i > 0 else ""
            next_ch = text[i + 1] if i + 1 < text_len else ""
            if prev.isdigit() and next_ch.isdigit():
                out.append(".")
            else:
                out.append(_JP_PUNCT_MAP["."])
            continue
        mapped = _JP_PUNCT_MAP.get(ch)
        if mapped is None:
            mapped = _JP_DIGIT_MAP.get(ch)
        out.append(mapped if mapped is not None else ch)
    return "".join(out)


def is_separator_line(text: str) -> bool:
    compact = "".join(ch for ch in text if not ch.isspace())
    if not compact:
        return False
    if all(ch in _SEPARATOR_CHARS for ch in compact):
        return len(compact) >= 4
    if all(ch in _SEPARATOR_SYMBOLS for ch in compact):
        return True
    return False


def normalize_separator_spacing(paragraphs: list[str]) -> list[str]:
    if not paragraphs:
        return paragraphs
    markers = {
        MARK_PREFACE,
        MARK_PREFACE_END,
        MARK_AFTERWORD,
        MARK_AFTERWORD_END,
        MARK_SEPARATOR,
    }

    def is_blank_para(para: str) -> bool:
        if para == "":
            return True
        if para in markers:
            return False
        if "<img" in para:
            return False
        text = html.unescape(html_to_text(para))
        return not text.strip()

    out: list[str] = []
    i = 0
    total = len(paragraphs)
    while i < total:
        para = paragraphs[i]
        if para != MARK_SEPARATOR:
            out.append(para)
            i += 1
            continue
        while out and is_blank_para(out[-1]):
            out.pop()
        if out:
            out.append("")
        out.append(para)
        i += 1
        while i < total and is_blank_para(paragraphs[i]):
            i += 1
        if i < total:
            out.append("")
        continue
    return out


def apply_separator_handling(paragraphs: list[str]) -> list[str]:
    if not paragraphs:
        return paragraphs
    markers = {
        MARK_PREFACE,
        MARK_PREFACE_END,
        MARK_AFTERWORD,
        MARK_AFTERWORD_END,
        MARK_SEPARATOR,
    }
    out: list[str] = []
    for para in paragraphs:
        if para in markers or para == "":
            out.append(para)
            continue
        text = html.unescape(html_to_text(para))
        if is_separator_line(text):
            out.append(MARK_SEPARATOR)
        else:
            out.append(para)
    return normalize_separator_spacing(out)


def apply_separator_handling_to_chapters(chapters: list[Chapter]) -> list[Chapter]:
    out: list[Chapter] = []
    for chap in chapters:
        paragraphs = apply_separator_handling(chap.get("paragraphs") or [])
        out.append(
            {
                "title": chap.get("title") or "",
                "paragraphs": paragraphs,
                "url": chap.get("url") or "",
            }
        )
    return out


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_time = (
            time.monotonic() + self.min_interval if self.min_interval > 0 else 0.0
        )

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if now < self._next_time:
                sleep_for = self._next_time - now
                self._next_time += self.min_interval
            else:
                sleep_for = 0.0
                self._next_time = now + self.min_interval
        if sleep_for > 0:
            time.sleep(sleep_for)


def _fetch_url(
    url: str,
    timeout: int,
    delay: float,
    retries: int,
    user_agent: str,
    limiter: Optional[RateLimiter] = None,
) -> tuple[bytes, Mapping[str, str]]:
    last_err: Optional[BaseException] = None
    for attempt in range(retries + 1):
        if limiter is not None:
            limiter.wait()
        elif delay > 0:
            time.sleep(delay)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": user_agent,
                "Cookie": "over18=yes",
            },
        )
        ctx = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                backoff = min(8.0, (2**attempt)) + uniform(0, 0.25)
                time.sleep(backoff)
                continue
            raise
        except (urllib.error.URLError, socket.timeout, ssl.SSLError) as e:
            last_err = e
            if attempt < retries:
                backoff = min(8.0, (2**attempt)) + uniform(0, 0.25)
                time.sleep(backoff)
                continue
            raise
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def get_page(
    url: str,
    timeout: int,
    delay: float,
    retries: int,
    user_agent: str,
    limiter: Optional[RateLimiter] = None,
) -> str:
    data, _headers = _fetch_url(url, timeout, delay, retries, user_agent, limiter=limiter)
    return data.decode("utf-8", errors="replace")


def get_binary(
    url: str,
    timeout: int,
    delay: float,
    retries: int,
    user_agent: str,
    limiter: Optional[RateLimiter] = None,
) -> tuple[bytes, str]:
    data, headers = _fetch_url(url, timeout, delay, retries, user_agent, limiter=limiter)
    content_type = headers.get("Content-Type", "")
    return data, content_type


def parse_number_range(text: str) -> Optional[tuple[int, int]]:
    raw = text.strip()
    m = re.match(r"^(\d+)\s*-\s*(\d+)$", raw)
    if m:
        a = int(m.group(1))
        b = int(m.group(2))
        if a < 1 or b < 1 or b < a:
            return None
        return a, b
    m = re.match(r"^(\d+)$", raw)
    if not m:
        return None
    value = int(m.group(1))
    if value < 1:
        return None
    return value, value


def _sanitize_filename(name: str, default: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(" .")
    if not name:
        name = default
    if name.upper() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name


def _truncate_filename(name: str, max_length: int) -> str:
    if max_length > 0 and len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    return name


def safe_filename(name: str, default: str = "syosetu", max_length: int = MAX_FILENAME_LEN) -> str:
    name = _sanitize_filename(name, default)
    name = _truncate_filename(name, max_length)
    if not name:
        name = default
    if name.upper() in _WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name


def volume_label_for_filename(volume_title: str, vol_index: int) -> str:
    default_label = f"Volume {vol_index}"
    sanitized = _sanitize_filename(volume_title, default_label)
    if len(sanitized) > MAX_FILENAME_LEN:
        return safe_filename(default_label, default_label)
    return safe_filename(sanitized, default_label)


def expand_path(value: str) -> Path:
    expanded = os.path.expandvars(value)
    return Path(expanded).expanduser()


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"Warning: failed to read config {path}: {e}")
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Warning: failed to parse config {path}: {e}")
        return {}
    if not isinstance(data, dict):
        print(f"Warning: config file {path} is not a JSON object.")
        return {}
    return data


def save_config(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True),
            encoding="utf-8",
        )
        tmp_path.replace(path)
    except OSError as e:
        print(f"Warning: failed to write config {path}: {e}")


def resolve_output_base(
    output_path: Optional[str],
    default_dir: Optional[str],
    title: str,
) -> tuple[Path, str, Optional[str]]:
    base_name = safe_filename(title)
    base_out_dir: Optional[Path] = None
    output_name: Optional[str] = None

    if output_path:
        out_path = expand_path(output_path)
        seps = [os.sep]
        if os.altsep:
            seps.append(os.altsep)
        has_trailing_sep = str(output_path).endswith(tuple(seps))
        if has_trailing_sep or (out_path.exists() and out_path.is_dir()):
            base_out_dir = out_path
        elif out_path.suffix:
            output_name = out_path.name
            base_name = safe_filename(out_path.stem)
            if out_path.parent != Path("."):
                base_out_dir = out_path.parent
        else:
            base_out_dir = out_path

    if base_out_dir is None:
        base_out_dir = expand_path(default_dir) if default_dir else Path(".")

    return base_out_dir, base_name, output_name


def has_class(attrs: dict[str, Optional[str]], class_name: str) -> bool:
    cls = attrs.get("class")
    if not cls:
        return False
    return class_name in cls.split()


class TocParser(HTMLParser):
    def __init__(self, remove_furigana: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.author_parts: list[str] = []
        self.summary_parts: list[str] = []
        self.items: list[dict] = []
        self.next_url: str = ""
        self._remove_furigana = remove_furigana
        self._ruby_skip_depth = 0
        self._in_title = False
        self._in_author = False
        self._eplist_depth = 0
        self._in_volume_title = False
        self._volume_parts: list[str] = []
        self._in_chapter_link = False
        self._chapter_parts: list[str] = []
        self._chapter_href: Optional[str] = None
        self._summary_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        if tag in ("rt", "rp"):
            if self._remove_furigana:
                self._ruby_skip_depth += 1
            return
        if tag == "div":
            if self._summary_depth > 0:
                self._summary_depth += 1
                return
            if attrs_dict.get("id") == "novel_ex" or has_class(attrs_dict, "p-novel__summary"):
                self._summary_depth = 1
                return
        if tag == "h1" and has_class(attrs_dict, "p-novel__title"):
            self._in_title = True
            return
        if tag == "div" and has_class(attrs_dict, "p-novel__author"):
            self._in_author = True
            return
        if tag == "a" and has_class(attrs_dict, "c-pager__item--next") and not self.next_url:
            href = attrs_dict.get("href")
            if href:
                self.next_url = href
            return
        if tag == "div":
            if self._eplist_depth > 0:
                self._eplist_depth += 1
            elif has_class(attrs_dict, "p-eplist"):
                self._eplist_depth = 1
            if self._eplist_depth > 0 and has_class(attrs_dict, "p-eplist__chapter-title"):
                self._in_volume_title = True
                self._volume_parts = []
            return
        if tag == "br" and self._summary_depth > 0:
            self.summary_parts.append("\n")
            return
        if (
            tag == "a"
            and self._eplist_depth > 0
            and has_class(attrs_dict, "p-eplist__subtitle")
        ):
            href = attrs_dict.get("href")
            if href:
                self._in_chapter_link = True
                self._chapter_parts = []
                self._chapter_href = href

    def handle_endtag(self, tag: str) -> None:
        if tag in ("rt", "rp") and self._remove_furigana:
            if self._ruby_skip_depth > 0:
                self._ruby_skip_depth -= 1
            return
        if tag == "div" and self._summary_depth > 0:
            self._summary_depth -= 1
            if self._summary_depth == 0:
                self.summary_parts.append("\n")
            return
        if tag == "p" and self._summary_depth > 0:
            self.summary_parts.append("\n")
            return
        if tag == "h1" and self._in_title:
            self._in_title = False
            return
        if tag == "div" and self._in_author:
            self._in_author = False
            return
        if tag == "div":
            if self._in_volume_title:
                volume_title = "".join(self._volume_parts).strip()
                if volume_title:
                    self.items.append({"type": "volume", "title": volume_title})
                self._in_volume_title = False
                self._volume_parts = []
            if self._eplist_depth > 0:
                self._eplist_depth -= 1
            return
        if tag == "a" and self._in_chapter_link:
            chapter_title = "".join(self._chapter_parts).strip()
            if self._chapter_href:
                self.items.append(
                    {
                        "type": "chapter",
                        "href": self._chapter_href,
                        "title": chapter_title,
                    }
                )
            self._in_chapter_link = False
            self._chapter_parts = []
            self._chapter_href = None

    def handle_data(self, data: str) -> None:
        if self._remove_furigana and self._ruby_skip_depth > 0:
            return
        if self._in_title:
            self.title_parts.append(normalize_japanese_punct(data))
        elif self._in_author:
            self.author_parts.append(normalize_japanese_punct(data))
        elif self._summary_depth > 0:
            self.summary_parts.append(normalize_japanese_punct(data))
        elif self._in_volume_title:
            self._volume_parts.append(normalize_japanese_punct(data))
        elif self._in_chapter_link:
            self._chapter_parts.append(normalize_japanese_punct(data))

    def get_title(self) -> str:
        return "".join(self.title_parts).strip()

    def get_author(self) -> str:
        raw = "".join(self.author_parts).strip()
        raw = re.sub(r"^作者：", "", raw).strip()
        return raw

    def get_summary(self) -> str:
        raw = "".join(self.summary_parts)
        lines = [line.strip() for line in raw.splitlines()]
        return "\n".join([line for line in lines if line])


class ChapterParser(HTMLParser):
    def __init__(self, remove_furigana: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.paragraphs: list[str] = []
        self._remove_furigana = remove_furigana
        self._ruby_skip_depth = 0
        self._in_title = False
        self._text_block_depth = 0
        self._in_p = False
        self._current_html_parts: list[str] = []
        self._current_text_parts: list[str] = []
        self._block_label_end: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "img" and self._text_block_depth > 0 and self._in_p:
            src = attrs_dict.get("src") or ""
            if src:
                alt = attrs_dict.get("alt") or ""
                alt_attr = f' alt="{html.escape(alt, quote=True)}"' if alt else ' alt=""'
                safe_src = html.escape(src, quote=True)
                self._current_html_parts.append(f'<img src="{safe_src}"{alt_attr} />')
            return
        if tag in ("rt", "rp"):
            if self._remove_furigana:
                self._ruby_skip_depth += 1
            elif self._text_block_depth > 0 and self._in_p:
                self._current_html_parts.append(f"<{tag}>")
            return
        if tag == "ruby":
            if not self._remove_furigana and self._text_block_depth > 0 and self._in_p:
                self._current_html_parts.append("<ruby>")
            return
        if tag == "h1" and has_class(attrs_dict, "p-novel__title"):
            self._in_title = True
            return
        if tag == "div":
            if self._text_block_depth > 0:
                self._text_block_depth += 1
                return
            is_preface = has_class(attrs_dict, "p-novel__text--preface")
            is_afterword = has_class(attrs_dict, "p-novel__text--afterword")
            is_text_block = has_class(attrs_dict, "p-novel__text") or is_preface or is_afterword
            if is_text_block:
                self._text_block_depth = 1
                if is_preface:
                    self._block_label_end = MARK_PREFACE_END
                    self.paragraphs.append(MARK_PREFACE)
                elif is_afterword:
                    self._block_label_end = MARK_AFTERWORD_END
                    self.paragraphs.append(MARK_AFTERWORD)
                else:
                    self._block_label_end = None
            return
        if self._text_block_depth > 0 and tag == "p":
            self._in_p = True
            self._current_html_parts = []
            self._current_text_parts = []
            return
        if self._text_block_depth > 0 and self._in_p:
            if tag == "br":
                self._current_html_parts.append("<br />")
                self._current_text_parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if self._text_block_depth > 0 and self._in_p:
            if tag == "br":
                self._current_html_parts.append("<br />")
                self._current_text_parts.append("\n")
                return
            if tag == "img":
                attrs_dict = dict(attrs)
                src = attrs_dict.get("src") or ""
                if src:
                    alt = attrs_dict.get("alt") or ""
                    alt_attr = f' alt="{html.escape(alt, quote=True)}"' if alt else ' alt=""'
                    safe_src = html.escape(src, quote=True)
                    self._current_html_parts.append(f'<img src="{safe_src}"{alt_attr} />')
                return

    def handle_endtag(self, tag: str) -> None:
        if tag in ("rt", "rp"):
            if self._remove_furigana:
                if self._ruby_skip_depth > 0:
                    self._ruby_skip_depth -= 1
            elif self._text_block_depth > 0 and self._in_p:
                self._current_html_parts.append(f"</{tag}>")
            return
        if tag == "ruby":
            if not self._remove_furigana and self._text_block_depth > 0 and self._in_p:
                self._current_html_parts.append("</ruby>")
            return
        if tag == "h1" and self._in_title:
            self._in_title = False
            return
        if tag == "div" and self._text_block_depth > 0:
            self._text_block_depth -= 1
            if self._text_block_depth == 0:
                if self._block_label_end:
                    self.paragraphs.append(self._block_label_end)
                    self._block_label_end = None
                if self.paragraphs and self.paragraphs[-1] != "":
                    self.paragraphs.append("")
            return
        if tag == "p" and self._in_p:
            html_content = "".join(self._current_html_parts)
            text_content = "".join(self._current_text_parts)
            if not text_content.strip() and not html_content.strip():
                self.paragraphs.append("")
            else:
                self.paragraphs.append(html_content)
            self._in_p = False
            self._current_html_parts = []
            self._current_text_parts = []
            return

    def handle_data(self, data: str) -> None:
        if self._remove_furigana and self._ruby_skip_depth > 0:
            return
        if self._in_title:
            self.title_parts.append(normalize_japanese_punct(data))
        elif self._text_block_depth > 0 and self._in_p:
            normalized = normalize_japanese_punct(data)
            self._current_html_parts.append(html.escape(normalized, quote=True))
            self._current_text_parts.append(normalized)

    def get_title(self) -> str:
        return "".join(self.title_parts).strip()


def parse_toc_page(
    page_html: str, remove_furigana: bool = False
) -> tuple[str, str, str, list[dict], str]:
    parser = TocParser(remove_furigana=remove_furigana)
    parser.feed(page_html)
    parser.close()
    return parser.get_title(), parser.get_author(), parser.get_summary(), parser.items, parser.next_url


def parse_chapter_page(page_html: str, remove_furigana: bool = False) -> tuple[str, list[str]]:
    parser = ChapterParser(remove_furigana=remove_furigana)
    parser.feed(page_html)
    parser.close()
    paragraphs = parser.paragraphs
    while paragraphs and paragraphs[-1] == "":
        paragraphs.pop()
    return parser.get_title(), paragraphs


def normalize_image_src(src: str, base_url: str) -> str:
    src = html.unescape(src)
    if src.startswith("data:"):
        return src
    return urllib.parse.urljoin(base_url, src)


def extract_image_sources(chapters: list[Chapter], base_url: str) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for chap in chapters:
        chap_base = chap.get("url") or base_url
        for para in chap.get("paragraphs") or []:
            if not para or para in (
                MARK_PREFACE,
                MARK_PREFACE_END,
                MARK_AFTERWORD,
                MARK_AFTERWORD_END,
            ):
                continue
            for match in _IMG_TAG_RE.finditer(para):
                raw_src = match.group(1) or match.group(2) or match.group(3) or ""
                if not raw_src:
                    continue
                norm = normalize_image_src(raw_src, chap_base)
                if norm in seen:
                    continue
                seen.add(norm)
                sources.append(norm)
    return sources


def _guess_media_type_from_url(url: str) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(".jpg") or path.endswith(".jpeg"):
        return "image/jpeg"
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".gif"):
        return "image/gif"
    if path.endswith(".webp"):
        return "image/webp"
    if path.endswith(".bmp"):
        return "image/bmp"
    if path.endswith(".svg"):
        return "image/svg+xml"
    return ""


def _ext_from_media_type(media_type: str) -> str:
    return _IMG_EXT_BY_MIME.get(media_type.lower(), "")


def _parse_data_url(src: str) -> tuple[bytes, str]:
    header, _, data = src.partition(",")
    meta = header[5:] if header.startswith("data:") else ""
    mime, _, enc = meta.partition(";")
    if enc.lower() == "base64":
        return base64.b64decode(data), mime or "application/octet-stream"
    return data.encode("utf-8"), mime or "application/octet-stream"


def download_images(
    chapters: list[Chapter],
    base_url: str,
    timeout: int,
    delay: float,
    retries: int,
    user_agent: str,
    jobs: int = 1,
    limiter: Optional[RateLimiter] = None,
) -> tuple[dict[str, str], list[ImageItem]]:
    sources = extract_image_sources(chapters, base_url)
    image_map: dict[str, str] = {}
    images: list[ImageItem] = []

    if not sources:
        return image_map, images

    total = len(sources)
    print(f"    Downloading {total} images...")

    def fetch_one(index: int, src: str) -> tuple[int, str, Optional[bytes], str, Optional[BaseException]]:
        try:
            if src.startswith("data:"):
                data, media_type = _parse_data_url(src)
            else:
                data, content_type = get_binary(
                    src, timeout, delay, retries, user_agent, limiter=limiter
                )
                media_type = content_type.split(";", 1)[0].strip() or _guess_media_type_from_url(
                    src
                )
            if not media_type:
                media_type = "application/octet-stream"
            return index, src, data, media_type, None
        except Exception as e:
            return index, src, None, "", e

    results: list[Optional[tuple[str, Optional[bytes], str, Optional[BaseException]]]] = [
        None
    ] * len(sources)
    completed = 0

    if jobs <= 1 or len(sources) <= 1:
        for idx, src in enumerate(sources):
            index, src, data, media_type, err = fetch_one(idx, src)
            results[index] = (src, data, media_type, err)
            completed += 1
            print(f"    Downloaded {completed}/{total} images")
    else:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            future_map = {
                ex.submit(fetch_one, idx, src): idx for idx, src in enumerate(sources)
            }
            for future in as_completed(future_map):
                index = future_map[future]
                try:
                    idx, src, data, media_type, err = future.result()
                except Exception as e:
                    results[index] = (sources[index], None, "", e)
                    completed += 1
                    print(f"    Downloaded {completed}/{total} images")
                    continue
                results[idx] = (src, data, media_type, err)
                completed += 1
                print(f"    Downloaded {completed}/{total} images")

    counter = 1
    for item in results:
        if not item:
            continue
        src, data, media_type, err = item
        if err or data is None:
            print(f"    Failed to download image: {src} -> {err}")
            continue
        ext = _ext_from_media_type(media_type) or ".bin"
        filename = f"image{counter:03d}{ext}"
        href = f"images/{filename}"
        image_map[src] = href
        images.append({"href": href, "media_type": media_type, "data": data})
        counter += 1

    return image_map, images


def replace_img_srcs(html_text: str, base_url: str, image_map: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        raw_src = match.group(1) or match.group(2) or match.group(3) or ""
        if not raw_src:
            return match.group(0)
        norm = normalize_image_src(raw_src, base_url)
        new_src = image_map.get(norm)
        if not new_src:
            return match.group(0)
        return re.sub(
            r"\bsrc\s*=\s*(?:\"[^\"]+\"|'[^']+'|[^\s>]+)",
            f'src="{new_src}"',
            match.group(0),
            count=1,
            flags=re.IGNORECASE,
        )

    return _IMG_TAG_RE.sub(repl, html_text)


def replace_img_tags_for_txt(html_text: str, base_url: str) -> str:
    def repl(match: re.Match[str]) -> str:
        raw_src = match.group(1) or match.group(2) or match.group(3) or ""
        raw_src = html.unescape(raw_src)
        if not raw_src:
            return ""
        if raw_src.startswith("data:"):
            return "\n[Image: embedded]\n"
        norm = normalize_image_src(raw_src, base_url)
        return f"\n[Image: {norm}]\n"

    return _IMG_TAG_RE.sub(repl, html_text)


def ensure_image_breaks(html_text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        return f"<br />{tag}<br />"

    return _IMG_TAG_RE.sub(repl, html_text)


def html_to_text(s: str) -> str:
    s = s.replace("<br />", "\n").replace("<br/>", "\n").replace("<br>", "\n")
    return re.sub(r"<[^>]+>", "", s)


def count_characters(chapters: list[Chapter]) -> int:
    total = 0
    for chap in chapters:
        title = chap.get("title") or ""
        total += len(title.replace("\n", ""))
        for para in chap.get("paragraphs") or []:
            if para in (MARK_PREFACE, MARK_PREFACE_END, MARK_AFTERWORD, MARK_AFTERWORD_END):
                continue
            if not para:
                continue
            text = html.unescape(html_to_text(para)).replace("\n", "")
            total += len(text)
    return total


def build_volumes(
    toc_items: list[dict], selected: Optional[set[str]]
) -> tuple[list[Volume], bool]:
    volumes: list[Volume] = []
    current: Optional[Volume] = None
    vol_index = 0
    found_volume = False
    for item in toc_items:
        if item.get("type") == "volume":
            found_volume = True
            vol_index += 1
            title = (item.get("title") or "").strip() or f"Volume {vol_index}"
            current = {"title": title, "chapters": []}
            volumes.append(current)
            continue
        if item.get("type") == "chapter":
            if current is None:
                vol_index += 1
                current = {"title": f"Volume {vol_index}", "chapters": []}
                volumes.append(current)
            href = item.get("href")
            if href and (selected is None or href in selected):
                current["chapters"].append(href)
    return volumes, found_volume


def build_volume_breaks(
    volumes: list[Volume], chapters: list[Chapter]
) -> list[tuple[str, int, int]]:
    index_map = {chap.get("url"): idx for idx, chap in enumerate(chapters)}
    breaks: list[tuple[str, int, int]] = []
    for vol_idx, volume in enumerate(volumes, start=1):
        volume_title = (volume.get("title") or "").strip() or f"Volume {vol_idx}"
        indices = [
            index_map[url]
            for url in (volume.get("chapters") or [])
            if url in index_map
        ]
        if not indices:
            continue
        breaks.append((volume_title, min(indices), max(indices)))
    return breaks


def parse_volume_selection(text: str, max_index: int) -> Optional[set[int]]:
    raw = text.strip().lower()
    if raw in ("", "all", "*", "a"):
        return set(range(1, max_index + 1))
    result: set[int] = set()
    for part in re.split(r"[,\s]+", raw):
        if not part:
            continue
        if "-" in part:
            a_str, b_str = part.split("-", 1)
            if not a_str or not b_str:
                return None
            try:
                a = int(a_str)
                b = int(b_str)
            except ValueError:
                return None
            if a < 1 or b < 1 or b < a:
                return None
            for n in range(a, b + 1):
                if n > max_index:
                    return None
                result.add(n)
        else:
            try:
                n = int(part)
            except ValueError:
                return None
            if n < 1 or n > max_index:
                return None
            result.add(n)
    return result if result else None


def prompt_volume_selection(max_index: int) -> set[int]:
    if not sys.stdin.isatty():
        return set(range(1, max_index + 1))
    while True:
        try:
            raw = input(
                "Select volumes to download (e.g., 1,3-4 or all) [all is default]: "
            )
        except EOFError:
            return set(range(1, max_index + 1))
        parsed = parse_volume_selection(raw, max_index)
        if parsed is None:
            print("Invalid selection. Try again.")
            continue
        return parsed


def download_chapters(
    links: list[str],
    timeout: int,
    delay: float,
    retries: int,
    jobs: int,
    skip_errors: bool,
    user_agent: str,
    remove_furigana: bool,
    limiter: Optional[RateLimiter] = None,
) -> list[Chapter]:
    chapters: list[Chapter] = []
    if jobs <= 1 or len(links) <= 1:
        for idx, url in enumerate(links, start=1):
            print(f"    Downloading chapter {idx}/{len(links)}")
            try:
                html_page = get_page(url, timeout, delay, retries, user_agent, limiter=limiter)
            except Exception as e:
                if skip_errors:
                    print(f"    Failed: {url} -> {e}")
                    continue
                raise
            chap_title, paragraphs = parse_chapter_page(
                html_page, remove_furigana=remove_furigana
            )
            chap_title = chap_title or f"Chapter {idx}"
            chapters.append(
                {
                    "title": chap_title,
                    "paragraphs": paragraphs,
                    "url": url,
                }
            )
        return chapters

    def fetch_one(index: int, url: str) -> tuple[int, Chapter]:
        html_page = get_page(url, timeout, delay, retries, user_agent, limiter=limiter)
        chap_title, paragraphs = parse_chapter_page(
            html_page, remove_furigana=remove_furigana
        )
        chap_title = chap_title or f"Chapter {index + 1}"
        return index, {
            "title": chap_title,
            "paragraphs": paragraphs,
            "url": url,
        }

    results: list[Optional[Chapter]] = [None] * len(links)
    errors: list[tuple[str, BaseException]] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        future_map = {
            ex.submit(fetch_one, idx, url): (idx, url) for idx, url in enumerate(links)
        }
        for future in as_completed(future_map):
            idx, url = future_map[future]
            try:
                index, chapter = future.result()
                results[index] = chapter
            except Exception as e:
                errors.append((url, e))
                if not skip_errors:
                    for f in future_map:
                        f.cancel()
                    raise RuntimeError(f"Failed to download {url}") from e
            completed += 1
            print(f"    Downloaded {completed}/{len(links)}")

    if errors:
        print(f"\n{len(errors)} chapters failed to download.")
        for url, err in errors:
            print(f"    {url} -> {err}")

    return [c for c in results if c is not None]


def write_txt(path: str, title: str, author: str, chapters: list[Chapter], book_url: str) -> None:
    def append_block(lines: list[str], block: tuple[str, ...]) -> None:
        if not block:
            return
        if block[0] == SEPARATOR_LINE:
            last = next((ln for ln in reversed(lines) if ln != ""), "")
            if last == SEPARATOR_LINE:
                block = block[1:]
                if not block:
                    return
        if lines and lines[-1] == "":
            while block and block[0] == "":
                block = block[1:]
            if not block:
                return
        if lines and lines[-1] != "" and block[0] != "":
            lines.append("")
        lines.extend(block)

    section_map = {
        MARK_PREFACE: (SEPARATOR_LINE, "", "前書き", ""),
        MARK_AFTERWORD: (SEPARATOR_LINE, "", "後書き", ""),
        MARK_PREFACE_END: (SEPARATOR_LINE,),
        MARK_AFTERWORD_END: (SEPARATOR_LINE,),
        MARK_SEPARATOR: (SEPARATOR_LINE,),
    }

    out_lines: list[str] = []
    out_lines.append(title)
    if author:
        out_lines.append(f"作者：{author}")
    out_lines.append("")
    for chap in chapters:
        out_lines.append(chap["title"])
        out_lines.append("")
        chap_base = chap.get("url") or book_url
        for para in chap["paragraphs"]:
            section = section_map.get(para)
            if section:
                append_block(out_lines, section)
                continue
            if para == "":
                out_lines.append("")
                continue
            clean = replace_img_tags_for_txt(para, chap_base)
            out_lines.append(html.unescape(html_to_text(clean)))
        out_lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines).strip() + "\n")


def build_epub2(
    path: str,
    title: str,
    author: str,
    summary: str,
    chapters: list[Chapter],
    book_url: str,
    volume_breaks: Optional[list[tuple[str, int, int]]] = None,
    image_map: Optional[dict[str, str]] = None,
    image_items: Optional[list[ImageItem]] = None,
    vertical_text: bool = False,
) -> None:
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, book_url)}"
    lang = "ja"
    image_map = image_map or {}
    image_items = image_items or []

    def esc(text: str) -> str:
        return html.escape(text, quote=True)

    def xhtml_doc(doc_title: str, body: str, body_class: str = "") -> str:
        classes: list[str] = []
        if body_class:
            classes.append(body_class)
        if vertical_text:
            classes.append("vertical")
        class_attr = f' class="{" ".join(classes)}"' if classes else ""
        return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"
  "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">
<head>
  <title>{esc(doc_title)}</title>
  <link rel="stylesheet" type="text/css" href="style.css" />
</head>
<body{class_attr}>
{body}
</body>
</html>
"""

    style_css = (
        "body{font-family:serif;line-height:1.6;}"
        ".vertical{-epub-writing-mode:vertical-rl;writing-mode:vertical-rl;text-orientation:mixed;}"
        "h1{font-size:1.4em;margin:1.2em 0 0.6em 0;}"
        "p{margin:0 0 0.8em 0;}"
        "p.blank{margin:0 0 0.8em 0;}"
        "p.summary{margin:0 0 0.8em 0;}"
        "img{max-width:100%;height:auto;}"
        ".toc ol{list-style:none;padding-left:0;}"
        ".toc li{margin:0 0 0.4em 0;}"
        ".toc .toc-volume{margin:0.8em 0 0.3em 0;font-weight:bold;}"
        ".toc .toc-sublist{list-style:none;padding-left:1.2em;}"
        ".toc .toc-sublist li{margin:0 0 0.3em 0;font-weight:normal;}"
        ".toc a{text-decoration:none;color:inherit;}"
        ".volume h1{margin-top:2.2em;text-align:center;}"
        ".section-marker{border-top:4px solid #333;border-bottom:4px solid #333;"
        "padding:0.5em 0;text-align:center;letter-spacing:0.08em;}"
        "hr.separator{border:0;border-top:2px solid #333;margin:0.9em 0;}"
    )

    title_lines = [f"  <h1>{esc(title)}</h1>"]
    if author:
        title_lines.append(f"  <p>作者：{esc(author)}</p>")
    link_url = book_url.replace("http://", "https://", 1)
    title_lines.append(
        f'  <p>リンク：<a href="{esc(link_url)}">{esc(link_url)}</a></p>'
    )
    if summary:
        title_lines.append('  <p class="blank">&#160;</p>')
        for line in summary.splitlines():
            if not line.strip():
                continue
            title_lines.append(f'  <p class="summary">{esc(line.strip())}</p>')
    title_xhtml = xhtml_doc(title, "\n".join(title_lines))

    marker_map = {
        MARK_PREFACE: (
            '<p class="blank">&#160;</p>',
            '<p class="section-marker preface">前書き</p>',
            '<p class="blank">&#160;</p>',
        ),
        MARK_AFTERWORD: (
            '<p class="section-marker afterword">後書き</p>',
            '<p class="blank">&#160;</p>',
        ),
        MARK_PREFACE_END: ('<hr class="separator" />',),
        MARK_AFTERWORD_END: ('<hr class="separator" />',),
        MARK_SEPARATOR: ('<hr class="separator" />',),
    }

    chapter_files: list[tuple[str, str]] = []
    for idx, chap in enumerate(chapters, start=1):
        chap_title = chap["title"] or f"Chapter {idx}"
        anchor_id = f"ref-{idx:03d}"
        chap_base = chap.get("url") or book_url
        paras: list[str] = []
        for para in chap["paragraphs"]:
            marker = marker_map.get(para)
            if marker:
                paras.extend(marker)
                continue
            if para == "":
                paras.append('<p class="blank">&#160;</p>')
            else:
                text = para.replace("\n", "<br />")
                if image_map:
                    text = replace_img_srcs(text, chap_base, image_map)
                if "<img" in text:
                    text = ensure_image_breaks(text)
                paras.append(f"<p>{text}</p>")
        body_html = "\n  ".join(paras) if paras else "<p></p>"
        chap_body = f'  <h1 id="{anchor_id}">{esc(chap_title)}</h1>\n  {body_html}'
        chap_xhtml = xhtml_doc(chap_title, chap_body)
        filename = f"chapter{idx:03d}.xhtml"
        chapter_files.append((filename, chap_xhtml))

    toc_title = "目次"
    use_volume_groups = bool(volume_breaks) and len(volume_breaks) > 1
    volume_files: list[tuple[str, str]] = []
    volume_items: list[tuple[str, str, int]] = []
    if use_volume_groups and volume_breaks:
        for vol_idx, (vol_title, start_idx, _end_idx) in enumerate(volume_breaks, start=1):
            volume_label = (vol_title or "").strip() or f"Volume {vol_idx}"
            filename = f"volume{vol_idx:03d}.xhtml"
            anchor_id = f"vol-{vol_idx:03d}"
            volume_body = f'  <h1 id="{anchor_id}">{esc(volume_label)}</h1>'
            volume_xhtml = xhtml_doc(volume_label, volume_body, body_class="volume")
            volume_files.append((filename, volume_xhtml))
            item_id = f"vol{vol_idx:03d}"
            volume_items.append((item_id, filename, start_idx))
    toc_items_html: list[str] = []
    if use_volume_groups and volume_breaks:
        for vol_idx, (vol_title, start_idx, end_idx) in enumerate(volume_breaks, start=1):
            volume_label = (vol_title or "").strip() or f"Volume {vol_idx}"
            volume_filename = f"volume{vol_idx:03d}.xhtml"
            volume_label_html = (
                f'<a href="{volume_filename}#vol-{vol_idx:03d}">{esc(volume_label)}</a>'
            )
            volume_toc_items: list[str] = []
            for chap_index in range(start_idx, end_idx + 1):
                filename = chapter_files[chap_index][0]
                chap_title = chapters[chap_index]["title"] or f"Chapter {chap_index + 1}"
                volume_toc_items.append(
                    f'<li><a href="{filename}#ref-{chap_index + 1:03d}">{esc(chap_title)}</a></li>'
                )
            volume_list_html = "\n        ".join(volume_toc_items)
            toc_items_html.append(
                f"""<li class="toc-volume">{volume_label_html}
      <ol class="toc-sublist">
        {volume_list_html}
      </ol>
    </li>"""
            )
    else:
        for idx, (filename, _) in enumerate(chapter_files, start=1):
            chap_title = chapters[idx - 1]["title"] or f"Chapter {idx}"
            toc_items_html.append(
                f'<li><a href="{filename}#ref-{idx:03d}">{esc(chap_title)}</a></li>'
            )
    toc_list_html = "\n    ".join(toc_items_html)
    toc_body = f"  <h1>{esc(toc_title)}</h1>\n  <ol>\n    {toc_list_html}\n  </ol>"
    toc_xhtml = xhtml_doc(toc_title, toc_body, body_class="toc")
    include_toc = len(chapters) > 1

    manifest_items: list[str] = [
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="style" href="style.css" media-type="text/css"/>',
        '<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>',
    ]
    if include_toc:
        manifest_items.append(
            '<item id="toc" href="toc.xhtml" media-type="application/xhtml+xml"/>'
        )
    for idx, img in enumerate(image_items, start=1):
        media_type = img.get("media_type") or "application/octet-stream"
        href = img.get("href") or f"images/image{idx:03d}.bin"
        manifest_items.append(
            f'<item id="img{idx:03d}" href="{href}" media-type="{media_type}"/>'
        )
    for item_id, filename, _start_idx in volume_items:
        manifest_items.append(
            f'<item id="{item_id}" href="{filename}" media-type="application/xhtml+xml"/>'
        )

    chapter_item_ids: list[str] = []
    for idx, (filename, _) in enumerate(chapter_files, start=1):
        item_id = f"chap{idx:03d}"
        chapter_item_ids.append(item_id)
        manifest_items.append(
            f'<item id="{item_id}" href="{filename}" media-type="application/xhtml+xml"/>'
        )

    spine_items: list[str] = [
        '<itemref idref="title"/>',
    ]
    if include_toc:
        spine_items.append('<itemref idref="toc"/>')
    volume_insert_map: dict[int, list[str]] = {}
    for item_id, _filename, start_idx in volume_items:
        volume_insert_map.setdefault(start_idx, []).append(item_id)

    for idx, item_id in enumerate(chapter_item_ids):
        for vol_id in volume_insert_map.get(idx, []):
            spine_items.append(f'<itemref idref="{vol_id}"/>')
        spine_items.append(f'<itemref idref="{item_id}"/>')

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package version="2.0" unique-identifier="bookid" xmlns="http://www.idpf.org/2007/opf">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{esc(title)}</dc:title>
    {f'<dc:creator opf:role="aut">{esc(author)}</dc:creator>' if author else ''}
    <dc:language>{lang}</dc:language>
    <dc:identifier id="bookid">{esc(book_id)}</dc:identifier>
  </metadata>
  <manifest>
    {' '.join(manifest_items)}
  </manifest>
  <spine toc="ncx">
    {' '.join(spine_items)}
  </spine>
</package>
"""

    nav_points: list[str] = []
    nav_depth = "2" if use_volume_groups else "1"
    play_order = 1
    nav_points.append(
        f"""<navPoint id="navpoint-{play_order}" playOrder="{play_order}">
      <navLabel><text>{esc(title)}</text></navLabel>
      <content src="title.xhtml"/>
    </navPoint>"""
    )
    play_order += 1
    if include_toc:
        nav_points.append(
            f"""<navPoint id="navpoint-{play_order}" playOrder="{play_order}">
      <navLabel><text>{esc(toc_title)}</text></navLabel>
      <content src="toc.xhtml"/>
    </navPoint>"""
        )
        play_order += 1
    if use_volume_groups and volume_breaks:
        for vol_idx, (vol_title, start_idx, end_idx) in enumerate(volume_breaks, start=1):
            volume_label = (vol_title or "").strip() or f"Volume {vol_idx}"
            volume_filename = f"volume{vol_idx:03d}.xhtml"
            volume_play = play_order
            play_order += 1
            child_points: list[str] = []
            for chap_index in range(start_idx, end_idx + 1):
                chap_title = chapters[chap_index]["title"] or f"Chapter {chap_index + 1}"
                filename = chapter_files[chap_index][0]
                child_points.append(
                    f"""<navPoint id="navpoint-{play_order}" playOrder="{play_order}">
      <navLabel><text>{esc(chap_title)}</text></navLabel>
      <content src="{filename}#ref-{chap_index + 1:03d}"/>
    </navPoint>"""
                )
                play_order += 1
            children_block = "\n      ".join(child_points)
            nav_points.append(
                f"""<navPoint id="navpoint-{volume_play}" playOrder="{volume_play}">
      <navLabel><text>{esc(volume_label)}</text></navLabel>
      <content src="{volume_filename}#vol-{vol_idx:03d}"/>
      {children_block}
    </navPoint>"""
            )
    else:
        for idx, (filename, _) in enumerate(chapter_files, start=1):
            chap_title = chapters[idx - 1]["title"] or f"Chapter {idx}"
            nav_points.append(
                f"""<navPoint id="navpoint-{play_order}" playOrder="{play_order}">
      <navLabel><text>{esc(chap_title)}</text></navLabel>
      <content src="{filename}#ref-{idx:03d}"/>
    </navPoint>"""
            )
            play_order += 1

    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN"
  "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{esc(book_id)}"/>
    <meta name="dtb:depth" content="{nav_depth}"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{esc(title)}</text></docTitle>
  {f'<docAuthor><text>{esc(author)}</text></docAuthor>' if author else ''}
  <navMap>
    {' '.join(nav_points)}
  </navMap>
</ncx>
"""

    container_xml = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        zf.writestr("OEBPS/style.css", style_css)
        zf.writestr("OEBPS/title.xhtml", title_xhtml)
        if include_toc:
            zf.writestr("OEBPS/toc.xhtml", toc_xhtml)
        for filename, content in volume_files + chapter_files:
            zf.writestr(f"OEBPS/{filename}", content)
        for img in image_items:
            href = img.get("href")
            data = img.get("data")
            if not href or data is None:
                continue
            zf.writestr(f"OEBPS/{href}", data)


def write_output(
    path: str,
    title: str,
    author: str,
    summary: str,
    chapters: list[Chapter],
    fmt: str,
    book_url: str,
    jobs: int,
    handle_separators: bool,
    limiter: Optional[RateLimiter] = None,
    volume_breaks: Optional[list[tuple[str, int, int]]] = None,
    vertical_text: bool = False,
) -> None:
    path_str = str(path)
    if fmt == "txt":
        write_txt(path_str, title, author, chapters, book_url)
    else:
        if handle_separators:
            chapters = apply_separator_handling_to_chapters(chapters)
        image_map, image_items = download_images(
            chapters,
            book_url,
            DEFAULT_TIMEOUT,
            DEFAULT_DELAY,
            DEFAULT_RETRIES,
            UA,
            jobs=jobs,
            limiter=limiter,
        )
        build_epub2(
            path_str,
            title,
            author,
            summary,
            chapters,
            book_url,
            volume_breaks=volume_breaks,
            image_map=image_map,
            image_items=image_items,
            vertical_text=vertical_text,
        )


def main() -> None:
    p = argparse.ArgumentParser(description="Download a syosetu novel to an EPUB2 file.")
    p.add_argument(
        "book_url",
        nargs="?",
        help="Full URL of the novel's main page on syosetu.com",
    )
    p.add_argument("-o", "--output", help="Output file path")
    p.add_argument(
        "--output-dir",
        "--output-folder",
        dest="output_dir",
        help="Default base output folder (saved in config).",
    )
    p.add_argument("-f", "--format", choices=("epub", "txt"), default="epub")
    p.add_argument("-c", "--chapters", help="Chapter range N-M (1-based)")
    p.add_argument(
        "-v",
        "--volume",
        "--volumes",
        dest="volume",
        help="Select volume numbers to download (e.g., 1,3-4 or all).",
    )
    p.add_argument(
        "--remove-furigana",
        "--no-furigana",
        dest="remove_furigana",
        action="store_true",
        help="Remove furigana (ruby annotations) from output.",
    )
    p.add_argument(
        "--no-separator",
        dest="handle_separators",
        action="store_false",
        help="EPUB only: keep separator lines as-is (do not convert to separators).",
    )
    p.add_argument(
        "--vertical",
        "--vertical-text",
        dest="vertical_text",
        action="store_true",
        help="Render EPUB in vertical writing mode (tategaki).",
    )
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help="Parallel download jobs")
    p.set_defaults(handle_separators=True)
    args = p.parse_args()
    rate_limiter = RateLimiter(DEFAULT_DELAY)

    config = load_config(CONFIG_PATH)
    config_output_dir: Optional[str] = None
    raw_output_dir = config.get(CONFIG_OUTPUT_DIR_KEY)
    if isinstance(raw_output_dir, str):
        raw_output_dir = raw_output_dir.strip()
        if raw_output_dir:
            config_output_dir = raw_output_dir

    if args.output_dir is not None:
        if not args.output_dir.strip():
            print("Invalid --output-dir value.")
            return
        expanded_output_dir = expand_path(args.output_dir)
        normalized_output_dir = os.path.abspath(str(expanded_output_dir))
        config[CONFIG_OUTPUT_DIR_KEY] = normalized_output_dir
        save_config(CONFIG_PATH, config)
        config_output_dir = normalized_output_dir

    if not args.book_url:
        if args.output_dir is not None:
            print(f"Saved default output folder: {config_output_dir}")
            return
        print("Missing book_url. Provide a URL or use --output-dir to set the default output folder.")
        return

    if args.vertical_text and args.format == "txt":
        print("Note: --vertical only applies to EPUB output.")

    input_url = args.book_url.rstrip("/")
    chapter_match = re.match(
        r"^https?://[^/]*syosetu\.com/([^/]+)/(\d+)/?$",
        input_url,
    )
    if chapter_match:
        novel_code = chapter_match.group(1)
        chapter_no = int(chapter_match.group(2))
        parsed = urllib.parse.urlparse(input_url)
        host = parsed.netloc
        main_url = f"{parsed.scheme or 'https'}://{host}/{novel_code}/"
        if not args.chapters:
            args.chapters = f"{chapter_no}-{chapter_no}"
    else:
        main_url = input_url

    if not main_url.endswith("/"):
        main_url = f"{main_url}/"
    book_url = main_url.replace("http://", "https://", 1)

    for name, value, min_value in (("jobs", args.jobs, 1),):
        if value < min_value:
            print(f"--{name} must be >= {min_value}")
            return

    print("Downloading table of contents...")
    next_url = book_url
    page_num = 1
    title = ""
    author = ""
    summary = ""
    toc_items: list[dict] = []
    seen_links: set[str] = set()
    seen_pages: set[str] = set()
    while next_url:
        if next_url in seen_pages:
            break
        seen_pages.add(next_url)
        print(f"    Page {page_num}...")
        page_url = next_url
        try:
            page = get_page(
                page_url,
                DEFAULT_TIMEOUT,
                DEFAULT_DELAY,
                DEFAULT_RETRIES,
                UA,
                limiter=rate_limiter,
            )
        except Exception as e:
            print(f"Failed to fetch TOC page: {e}")
            return
        page_title, page_author, page_summary, items, page_next = parse_toc_page(
            page, remove_furigana=args.remove_furigana
        )
        if not title and page_title:
            title = page_title
        if not author and page_author:
            author = page_author
        if not summary and page_summary:
            summary = page_summary
        for item in items:
            item_type = item.get("type")
            if item_type == "volume":
                volume_title = (item.get("title") or "").strip()
                if not volume_title:
                    continue
                if (
                    toc_items
                    and toc_items[-1].get("type") == "volume"
                    and toc_items[-1].get("title") == volume_title
                ):
                    continue
                toc_items.append({"type": "volume", "title": volume_title})
                continue
            if item_type == "chapter":
                href = item.get("href")
                if not href:
                    continue
                full = urllib.parse.urljoin(page_url, href)
                if full in seen_links:
                    continue
                seen_links.add(full)
                toc_items.append(
                    {
                        "type": "chapter",
                        "href": full,
                        "title": (item.get("title") or "").strip(),
                    }
                )
        next_url = urllib.parse.urljoin(page_url, page_next) if page_next else ""
        page_num += 1

    title = title or "syosetu"

    print(f"\nTitle: {title}")
    if author:
        print(f"作者：{author}")

    chapter_links = [item["href"] for item in toc_items if item.get("type") == "chapter"]
    if not chapter_links:
        print("No chapters found.")
        return

    if args.chapters:
        parsed_range = parse_number_range(args.chapters)
        if not parsed_range:
            print("Invalid chapter range.")
            return
        start, end = parsed_range
        if end > len(chapter_links):
            print("Invalid chapter range.")
            return
        indices = range(start - 1, end)
    else:
        indices = range(len(chapter_links))

    selected_links = [chapter_links[i] for i in indices]
    selected_set = set(selected_links)

    volumes, found_volume = build_volumes(toc_items, selected_set)
    split_volumes = found_volume

    if split_volumes:
        if not volumes:
            print("No volumes found.")
            return
        if args.volume and not found_volume:
            print("No volume headings found in TOC.")
            return

        display_volumes = [v for v in volumes if v.get("chapters")]
        if not display_volumes:
            print("No chapters matched the selection.")
            return

        print("\nVolumes:")
        for idx, vol in enumerate(display_volumes, start=1):
            count = len(vol.get("chapters") or [])
            print(f"  {idx}. {vol.get('title')} ({count} chapters)")

        if args.volume:
            selected_vol_indices = parse_volume_selection(args.volume, len(display_volumes))
            if not selected_vol_indices:
                print("Invalid volume selection.")
                return
        elif len(display_volumes) == 1:
            selected_vol_indices = {1}
        else:
            selected_vol_indices = prompt_volume_selection(len(display_volumes))

        selected_volumes = [(i, display_volumes[i - 1]) for i in sorted(selected_vol_indices)]

        base_out_dir, base_name, _output_name = resolve_output_base(
            args.output,
            config_output_dir,
            title,
        )
        out_dir = base_out_dir / safe_filename(title)
        out_dir.mkdir(parents=True, exist_ok=True)

        volume_stats: list[tuple[int, str, int]] = []
        merged_chapters: list[Chapter] = []
        volume_breaks: list[tuple[str, int, int]] = []

        for vol_index, volume in selected_volumes:
            volume_title = volume.get("title") or f"Volume {vol_index}"
            volume_links = volume.get("chapters") or []
            if not volume_links:
                continue
            chapters = download_chapters(
                volume_links,
                DEFAULT_TIMEOUT,
                DEFAULT_DELAY,
                DEFAULT_RETRIES,
                args.jobs,
                DEFAULT_SKIP_ERRORS,
                UA,
                args.remove_furigana,
                limiter=rate_limiter,
            )
            volume_stats.append((vol_index, volume_title, count_characters(chapters)))
            start_index = len(merged_chapters)
            merged_chapters.extend(chapters)
            end_index = len(merged_chapters) - 1
            if end_index >= start_index:
                volume_breaks.append((volume_title, start_index, end_index))
            display_title = title
            if found_volume or len(volumes) > 1:
                display_title = f"{title} - {volume_title}"
            volume_label = volume_label_for_filename(volume_title, vol_index)
            if len(chapters) == 1:
                chap_label = safe_filename(chapters[0].get("title") or "Chapter 1")
                filename = f"{base_name} - {volume_label} - {chap_label}.{args.format}"
            else:
                filename = f"{base_name} - {volume_label}.{args.format}"
            out_path = out_dir / filename
            write_output(
                out_path,
                display_title,
                author,
                summary,
                chapters,
                args.format,
                book_url,
                args.jobs,
                args.handle_separators,
                limiter=rate_limiter,
                vertical_text=args.vertical_text,
            )
            print(f"\nWrote {out_path}")

        if volume_stats:
            print("\nCharacter counts by volume:")
            for vol_index, volume_title, char_count in volume_stats:
                label = f"{vol_index:02d} - {volume_title}"
                print(f"  {label}: {char_count}")

        if args.format == "epub" and len(volume_breaks) > 1:
            if sys.stdin.isatty():
                print("\nMerge selected volumes into a single EPUB now? [y/N]")
                choice = input().strip().lower()
                if choice in {"y", "yes"}:
                    complete_title = f"{title} - Complete"
                    out_path = out_dir / f"{base_name} - Complete.epub"
                    write_output(
                        out_path,
                        complete_title,
                        author,
                        summary,
                        merged_chapters,
                        args.format,
                        book_url,
                        args.jobs,
                        args.handle_separators,
                        limiter=rate_limiter,
                        volume_breaks=volume_breaks,
                        vertical_text=args.vertical_text,
                    )
                    print(f"\nWrote {out_path}")
            else:
                print("\nRun this script in a terminal to merge volumes interactively.")
    else:
        if args.volume:
            print("Cannot use --volume without TOC volume headings.")
            return

        base_out_dir, base_name, output_name = resolve_output_base(
            args.output,
            config_output_dir,
            title,
        )
        out_dir = base_out_dir / safe_filename(title)
        out_dir.mkdir(parents=True, exist_ok=True)
        chapters = download_chapters(
            selected_links,
            DEFAULT_TIMEOUT,
            DEFAULT_DELAY,
            DEFAULT_RETRIES,
            args.jobs,
            DEFAULT_SKIP_ERRORS,
            UA,
            args.remove_furigana,
            limiter=rate_limiter,
        )
        if output_name:
            out_path = out_dir / output_name
        else:
            if len(chapters) == 1:
                chap_label = safe_filename(chapters[0].get("title") or "Chapter 1")
                filename = f"{base_name} - {chap_label}.{args.format}"
            else:
                filename = f"{base_name}.{args.format}"
            out_path = out_dir / filename
        volume_breaks = build_volume_breaks(volumes, chapters) if found_volume and args.format == "epub" else None
        write_output(
            out_path,
            title,
            author,
            summary,
            chapters,
            args.format,
            book_url,
            args.jobs,
            args.handle_separators,
            limiter=rate_limiter,
            volume_breaks=volume_breaks,
            vertical_text=args.vertical_text,
        )

        print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
