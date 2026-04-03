# SnapExtract

Extract and fix metadata on your Snapchat Memories export.

## What it does

- Downloads all memories from Snapchat's CDN using your export's `memories_history.html`
- Composites Snapchat overlays (filters, text, stickers) onto base photos and videos
- Writes correct capture dates and GPS coordinates into EXIF/XMP/QuickTime metadata
- Sets OS file timestamps (created + modified) to the original capture date
- Downloads in parallel (8 workers) with automatic retry on failures
- Extracts zip files automatically from input folders
- Falls back to local `memories.html` exports when CDN links aren't available

## Quick start

```bash
./run.sh
```

That's it. Two dialogs will pop up (macOS):

1. **Select your input** — pick the `memories_history.html` file directly, or cancel to pick a folder instead
2. **Select your output** — where you want the processed memories saved

Everything else (Python venv, dependencies, ffmpeg, exiftool) is installed automatically.

> **Important:** CDN download links expire ~12-24 hours after export. Run SnapExtract soon after requesting your data from Snapchat.

## Input formats

SnapExtract supports two Snapchat export formats:

| Format | File | What it has |
|---|---|---|
| **History export** (preferred) | `memories_history.html` | Full timestamps, GPS, CDN download links, overlays |
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

## Error handling

- Failed downloads are automatically retried up to 4 times (5 total attempts) with exponential backoff
- Permanently failed downloads are saved to `failed_downloads.txt` in the output directory
- Already-downloaded files are skipped on re-runs, so you can safely re-run to pick up failures

## Requirements

- macOS or Linux
- Python 3.10+

The following are installed automatically by `run.sh` (via Homebrew, apt, dnf, or pacman):

- **exiftool** — for writing EXIF/XMP/QuickTime metadata
- **ffmpeg** — for compositing overlays onto videos
- **beautifulsoup4**, **pyexiftool**, **Pillow**, **requests** — Python libraries (see `requirements.txt`)
