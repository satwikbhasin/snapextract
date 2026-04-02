# SnapExtract

Extract, composite, and fix metadata on your Snapchat Memories export.

## What it does

- Parses Snapchat's `memories.html` export files
- Composites overlay images (filters/stickers) onto base photos
- Composites overlay images onto videos (requires ffmpeg)
- Sets correct capture dates in EXIF/XMP metadata and OS file timestamps
- Merges multiple exports into a single output folder

## Quick start

```bash
./run.sh
```

That's it. Two Finder windows will pop up:

1. **Select your input** — the folder containing your Snapchat export (or a folder of zip files)
2. **Select your output** — where you want the processed memories saved

Everything else (Python venv, dependencies, ffmpeg, exiftool) is installed automatically.

## Input formats

SnapExtract handles all of these:

| Input | Example |
|---|---|
| A single export folder | `./run.sh -i ./memories -d ./output` |
| A folder of multiple exports | `./run.sh -i ./all_exports -d ./output` |
| A folder of zip files | `./run.sh -i ./my_zips -d ./output` |
| A direct `memories.html` file | `./run.sh -i ./memories/memories.html -d ./output` |

## Options

| Flag | Description |
|---|---|
| `-i`, `--input` | Input path (folder, zip folder, or memories.html) |
| `-d`, `--download-dir` | Output directory |
| `--skip-metadata` | Skip EXIF/XMP metadata updates |

## Requirements

- macOS (uses Homebrew for system deps)
- Python 3.10+

The following are installed automatically by `run.sh`:

- **exiftool** — for writing EXIF/XMP metadata
- **ffmpeg** — for compositing overlays onto videos
- **beautifulsoup4**, **pyexiftool**, **Pillow** — Python libraries
