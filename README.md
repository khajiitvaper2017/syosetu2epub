# syosetu2epub

Download novels from syosetu.com and package them as EPUB2 or TXT with optional volume splitting, chapter selection, and furigana removal. Single-file solution (`syosetu2epub.py`).

**Acknowledgements**

This project was inspired by and is somewhat based on https://github.com/cessen/syosetu2ebook/.
That script didn't work for me, as it's epubs didn't open in ttsu-reader properly.
So I asked Codex to convert it to python and then heavily modified it to my needs.

**Features**
- EPUB2 or TXT output.
- Supports multi-page TOCs and volume headings.
- Supports one-shot (single-page) novels without a chapter list.
- Select chapter ranges or specific volumes.
- Optional furigana (ruby) removal.
- Normalizes ASCII punctuation and digits to Japanese full-width forms in text.
- (EPUB only) Converts scene-break lines made of dashes or asterisks (e.g., `＊`) into separators and collapses extra blank lines around them.
- Preserves preface (前書き / maegaki) and afterword (後書き / atogaki) sections.
- Parallel chapter and image downloads with `--jobs`.
- Embeds images in EPUB and includes image placeholders in TXT.
- Adds the novel summary from the TOC page to the EPUB title page when available.

**Requirements**
- Python 3.9+
- Internet access to syosetu.com
- No external libraries required (standard library only).

**Usage**
```bash
python syosetu2epub.py <book_url> [options]
```
You can also set the default output folder without downloading by running:
```bash
python syosetu2epub.py --output-dir <folder>
```

**Options**
- `-o, --output` Output path. If a filename is provided, that name is used inside the novel's output folder. If a directory is provided, outputs are written under that directory.
- `--output-dir, --output-folder` Set the default base output folder and save it to config (used when `--output` is not provided). Can be used without `book_url`.
- `-f, --format` Output format: `epub`, `txt`, or `both` (default: `epub`).
- `-c, --chapters` Chapter selection: `N` or `N-M` (1-based, inclusive).
- `-v, --volume, --volumes` Volume selection such as `1,3-4` or `all` (when the TOC has volume headings).
- `--remove-furigana, --no-furigana` Remove ruby annotations from the output.
- `--no-separator` (EPUB only) Keep scene-break lines (dashes/asterisks) as-is instead of converting them to separators.
- `--vertical, --vertical-text` Render EPUB in vertical writing mode (tategaki).
- `--jobs` Parallel download jobs (default: 10).

**Examples**
```bash
python syosetu2epub.py https://ncode.syosetu.com/abcd1234/
```

```bash
python syosetu2epub.py https://ncode.syosetu.com/abcd1234/ -f txt -o downloads
```

```bash
python syosetu2epub.py https://ncode.syosetu.com/abcd1234/ -c 10-25
```

```bash
python syosetu2epub.py https://ncode.syosetu.com/abcd1234/ -v 1,3-4
```

```bash
python syosetu2epub.py https://ncode.syosetu.com/abcd1234/ --vertical
```

```bash
python syosetu2epub.py https://ncode.syosetu.com/abcd1234/12/
```

**Output Layout**
- Outputs are written under `<base>/<Title>/` where `<base>` is the current directory, the path provided via `--output`, or the saved config output folder.
- Filenames are derived from the novel title and volume/chapter titles, with safe characters for Windows.
- Very long filenames are truncated to reduce Windows path length issues.
- If a volume title is extremely long, its output filename falls back to the volume number.

**Notes**
- If the TOC contains volume headings, the script lists volumes and may prompt for selection when run in a terminal. In non-interactive runs it defaults to all volumes.
- At the volume selection prompt, pressing Enter selects all volumes.
- When multiple volumes are downloaded to EPUB, the script can optionally merge them into a single "Complete" EPUB in interactive mode.
- Passing a direct chapter URL (e.g., `.../12/`) auto-selects that chapter if `--chapters` is not provided.
- `--vertical` only affects EPUB output.
- Config is stored at `~/.syosetu2epub.json` and currently saves `output_dir`.

**Issues & Contributions**

Issue reports and pull requests are welcome.

**License**

GPL-3.0. See `LICENSE`.
