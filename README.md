# SnapExtract

Extract and fix metadata on your Snapchat Memories export.

## What it does

- Downloads all memories from Snapchat's CDN using your export's `memories_history.html`
- Writes correct capture dates and GPS coordinates into EXIF/XMP metadata
- Sets OS file timestamps to the original capture date
- Falls back to local `memories.html` exports (composites overlay images/filters onto base photos and videos)

## Quick start

```bash
./run.sh
```

That's it. Two dialogs will pop up:

1. **Select your input** — pick the `memories_history.html` file directly, or cancel to pick a folder instead
2. **Select your output** — where you want the processed memories saved

Everything else (Python venv, dependencies, ffmpeg, exiftool) is installed automatically.

## Input formats

SnapExtract supports two Snapchat export formats:

| Format | File | What it has |
|---|---|---|
| **History export** (preferred) | `memories_history.html` | Full timestamps, GPS, CDN download links for all memories |
| **Local export** (fallback) | `memories.html` | Local media files with overlays, date only (no time/GPS) |

If both formats are found, the history export is used automatically since it has richer data.

### Input examples

| Input | Example |
|---|---|
| A history HTML file | `./run.sh -i ./memories_history.html -d ./output` |
| A single export folder | `./run.sh -i ./memories -d ./output` |
| A folder of multiple exports | `./run.sh -i ./all_exports -d ./output` |
| A folder of zip files | `./run.sh -i ./my_zips -d ./output` |

## Options

| Flag | Description |
|---|---|
| `-i`, `--input` | Input path (HTML file, folder, or folder of zips) |
| `-d`, `--download-dir` | Output directory |
| `--skip-metadata` | Skip EXIF/XMP metadata updates |

## Requirements

- macOS or Linux
- Python 3.10+

The following are installed automatically by `run.sh` (via Homebrew, apt, dnf, or pacman):

- **exiftool** — for writing EXIF/XMP metadata
- **ffmpeg** — for compositing overlays onto videos (local export only)
- **beautifulsoup4**, **pyexiftool**, **Pillow**, **requests** — Python libraries (see `requirements.txt`)
