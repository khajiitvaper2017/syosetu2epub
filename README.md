# syosetu2epub

syosetu2epub is a single-file Python script (`syosetu2epub.py`) that downloads novels from syosetu.com and packages them as EPUB2 or TXT. It supports chapter ranges, volume grouping, optional furigana removal, and vertical writing mode for EPUB.

**Acknowledgements**

This project was inspired by and is somewhat based on https://github.com/cessen/syosetu2ebook/.
That script did not work for me because its EPUBs did not open properly in ttsu-reader.
I asked Codex to convert it to Python and then heavily modified it to my needs.

**Features**

EPUB2 or TXT output is supported, including multi-page TOCs and volume headings. You can select chapter ranges or specific volumes, preserve preface (前書き / maegaki) and afterword (後書き / atogaki), remove furigana (ruby annotations), and download chapters in parallel. Images are embedded in EPUB and shown as placeholders in TXT.

**Requirements**

Python 3.9+ and internet access to syosetu.com are required. There are no external library dependencies; everything uses the standard library.

**Usage**

```bash
python syosetu2epub.py <book_url> [options]
```

**Options**

`-o, --output` sets the output path. If you pass a filename, that name is used inside the novel's output folder. If you pass a directory, outputs are written under that directory.

`-f, --format` selects the output format: `epub` or `txt` (default: `epub`).

`-c, --chapters` downloads a chapter range in `N-M` (1-based, inclusive).

`-v, --volume, --volumes` selects volumes such as `1,3-4` or `all` when the TOC has volume headings.

`--remove-furigana, --no-furigana` removes ruby annotations from the output.

`--vertical, --vertical-text` renders EPUB in vertical writing mode (tategaki).

`--jobs` sets the number of parallel download jobs (default: 10).

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

Outputs are written under `<base>/<Title>/`, where `<base>` is the current directory or the path provided via `--output`. Filenames are derived from the novel title and volume/chapter titles, with safe characters for Windows.

**Notes**

If the TOC contains volume headings, the script lists volumes and may prompt for selection when run in a terminal; in non-interactive runs it defaults to all volumes. At the volume selection prompt, pressing Enter selects all volumes. When multiple volumes are downloaded to EPUB, the script can optionally merge them into a single "Complete" EPUB in interactive mode. Passing a direct chapter URL (e.g., `.../12/`) auto-selects that chapter if `--chapters` is not provided. The `--vertical` option only affects EPUB output.

**Issues & Contributions**

Issue reports and pull requests are welcome.

**License**

GPL-3.0. See `LICENSE`.
