#!/usr/bin/env python3
"""
Snapchat Memories Extractor & Metadata Fixer

Parses Snapchat's memories.html export (div-based format with local media files),
composites overlays onto base images/videos, copies results to an output directory,
and writes correct capture dates into EXIF/XMP metadata.

Usage:
    python snapchat_memories_downloader.py -m /path/to/memories/memories.html -d /path/to/output
    python snapchat_memories_downloader.py -m /path/to/memories/memories.html -d /path/to/output --skip-metadata
"""

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime

from bs4 import BeautifulSoup
from exiftool import ExifToolHelper
from PIL import Image

# ─── Config ──────────────────────────────────────────────────────────────────

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("snapmem")


# ─── HTML Parsing ────────────────────────────────────────────────────────────

def parse_html(html_path: str) -> list[dict]:
    """Parse memories.html into a list of memory entries.

    Each entry has: date, main_file (optional), overlay_file (optional).
    Files sharing the same UUID are grouped together.
    """
    log.info(f"Parsing {html_path}")
    html_dir = os.path.dirname(os.path.abspath(html_path))

    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    containers = soup.select(".image-container")
    log.info(f"Found {len(containers)} containers in HTML")

    # Parse each container into raw entries
    uuid_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})_([A-Fa-f0-9-]+)-(main|overlay)\.(\w+)")
    uuid_map = {}  # uuid -> {date, main_path, overlay_path}

    for container in containers:
        date_el = container.select_one(".text-line")
        date_str = date_el.text.strip() if date_el else None

        media_tags = container.find_all(["img", "video"])
        for tag in media_tags:
            src = tag.get("src", "")
            # Normalize path: remove leading ./
            filename = src.lstrip("./")
            match = uuid_pattern.match(filename)
            if not match:
                continue

            date, uuid, kind, ext = match.groups()
            full_path = os.path.join(html_dir, filename)

            if uuid not in uuid_map:
                uuid_map[uuid] = {"date": date_str or date}

            if kind == "main":
                uuid_map[uuid]["main_file"] = full_path
            elif kind == "overlay":
                uuid_map[uuid]["overlay_file"] = full_path

    entries = list(uuid_map.values())
    main_count = sum(1 for e in entries if "main_file" in e)
    overlay_count = sum(1 for e in entries if "overlay_file" in e)
    both_count = sum(1 for e in entries if "main_file" in e and "overlay_file" in e)
    log.info(f"Found {len(entries)} unique memories: {main_count} with main, {overlay_count} with overlay, {both_count} with both")
    return entries


# ─── Overlay Compositing ────────────────────────────────────────────────────

def _composite_image(base_path: str, overlay_path: str, output_path: str):
    """Paste overlay PNG onto base image using alpha compositing."""
    with Image.open(base_path) as base:
        base = base.convert("RGBA")
        with Image.open(overlay_path) as overlay:
            overlay = overlay.convert("RGBA")
            if overlay.size != base.size:
                overlay = overlay.resize(base.size, Image.LANCZOS)
            composite = Image.alpha_composite(base, overlay)

        base_ext = os.path.splitext(base_path)[1].lower()
        if base_ext in (".jpg", ".jpeg"):
            composite = composite.convert("RGB")
            composite.save(output_path, "JPEG", quality=95)
        else:
            composite.save(output_path, "PNG")


def _composite_video(base_path: str, overlay_path: str, output_path: str) -> bool:
    """Overlay a PNG onto a video using ffmpeg. Returns True on success."""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", base_path,
                "-i", overlay_path,
                "-filter_complex",
                "[1:v]scale=iw:ih[ovr];[0:v][ovr]overlay=0:0:shortest=1",
                "-c:a", "copy",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                output_path,
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True
        else:
            log.warning(f"    ffmpeg failed: {result.stderr[:200]}")
            return False
    except FileNotFoundError:
        log.warning("    ffmpeg not found — skipping video overlay compositing")
        return False
    except subprocess.TimeoutExpired:
        log.warning(f"    ffmpeg timed out on {os.path.basename(base_path)}")
        return False


# ─── Process Entries ─────────────────────────────────────────────────────────

def process_entries(entries: list[dict], output_dir: str) -> list[dict]:
    """Copy/composite all entries into the output directory.

    Returns list of output records: {date, file_path}.
    """
    total = len(entries)
    log.info(f"Processing {total} memories...")
    outputs = []
    composited = 0
    copied = 0
    skipped = 0

    for i, entry in enumerate(entries):
        date = entry["date"]
        main_file = entry.get("main_file")
        overlay_file = entry.get("overlay_file")

        if main_file and overlay_file and os.path.exists(main_file) and os.path.exists(overlay_file):
            # Composite overlay onto main
            base_ext = os.path.splitext(main_file)[1].lower()
            is_video = base_ext in VIDEO_EXTS
            base_name = os.path.splitext(os.path.basename(main_file))[0].replace("-main", "")

            if is_video:
                out_path = os.path.join(output_dir, f"{base_name}{base_ext}")
                success = _composite_video(main_file, overlay_file, out_path)
                if success:
                    composited += 1
                    outputs.append({"date": date, "file_path": out_path})
                else:
                    # Fallback: just copy the main file
                    out_path = os.path.join(output_dir, os.path.basename(main_file))
                    shutil.copy2(main_file, out_path)
                    copied += 1
                    outputs.append({"date": date, "file_path": out_path})
            else:
                if base_ext in (".jpg", ".jpeg"):
                    out_path = os.path.join(output_dir, f"{base_name}.jpg")
                else:
                    out_path = os.path.join(output_dir, f"{base_name}.png")
                try:
                    _composite_image(main_file, overlay_file, out_path)
                    composited += 1
                    outputs.append({"date": date, "file_path": out_path})
                except Exception as e:
                    log.warning(f"  Composite failed for {base_name}: {e}")
                    out_path = os.path.join(output_dir, os.path.basename(main_file))
                    shutil.copy2(main_file, out_path)
                    copied += 1
                    outputs.append({"date": date, "file_path": out_path})

        elif main_file and os.path.exists(main_file):
            # Just copy the main file
            out_path = os.path.join(output_dir, os.path.basename(main_file))
            shutil.copy2(main_file, out_path)
            copied += 1
            outputs.append({"date": date, "file_path": out_path})

        elif overlay_file and os.path.exists(overlay_file):
            # Overlay-only (no main file) — copy as-is
            out_path = os.path.join(output_dir, os.path.basename(overlay_file))
            shutil.copy2(overlay_file, out_path)
            copied += 1
            outputs.append({"date": date, "file_path": out_path})

        else:
            skipped += 1
            log.warning(f"  Skipped entry (missing files): date={date}")

        done = i + 1
        if done % 50 == 0 or done == total:
            log.info(f"  [{done}/{total}] processed ({composited} composited, {copied} copied)")

    log.info(f"Processing complete: {composited} composited, {copied} copied, {skipped} skipped")
    return outputs


# ─── Metadata Fixing ────────────────────────────────────────────────────────

def update_metadata(outputs: list[dict]):
    """Write correct capture date into media EXIF/XMP and OS file times."""
    total = len(outputs)
    if total == 0:
        log.info("No files to update metadata for.")
        return

    log.info(f"Updating metadata on {total} files...")
    updated = 0
    errors = 0

    with ExifToolHelper() as et:
        for entry in outputs:
            fpath = entry["file_path"]
            date_str = entry["date"]

            if not fpath or not os.path.exists(fpath):
                continue

            try:
                # Date is YYYY-MM-DD, set time to noon
                dt_obj = datetime.strptime(date_str, "%Y-%m-%d")
                exif_dt = dt_obj.strftime("%Y:%m:%d 12:00:00")

                tags = [
                    "-overwrite_original",
                    f"-XMP:DateTimeOriginal={exif_dt}",
                    f"-XMP:CreateDate={exif_dt}",
                    f"-DateTimeOriginal={exif_dt}",
                    f"-CreateDate={exif_dt}",
                    f"-ModifyDate={exif_dt}",
                ]

                et.execute(*tags, fpath)

                # Set OS file times
                unix_ts = dt_obj.timestamp()
                os.utime(fpath, (unix_ts, unix_ts))

                updated += 1
                if updated % 50 == 0 or updated == total:
                    log.info(f"  [{updated}/{total}] metadata updated")

            except Exception as e:
                errors += 1
                log.warning(f"  Metadata error on {os.path.basename(fpath)}: {e}")

    log.info(f"Metadata complete: {updated} ok, {errors} errors")


# ─── Main ────────────────────────────────────────────────────────────────────

def _extract_zips(input_path: str) -> list[str]:
    """Extract any .zip files in the input directory and return paths to extracted folders."""
    if not os.path.isdir(input_path):
        return []

    extracted = []
    for name in sorted(os.listdir(input_path)):
        if not name.lower().endswith(".zip"):
            continue
        zip_path = os.path.join(input_path, name)
        dest = os.path.join(input_path, os.path.splitext(name)[0])
        if os.path.isdir(dest):
            log.info(f"  Already extracted: {name}")
            extracted.append(dest)
            continue
        log.info(f"  Extracting: {name}")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(dest)
            extracted.append(dest)
        except zipfile.BadZipFile:
            log.warning(f"  Bad zip file: {name}")
    return extracted


def find_memories_html(input_path: str) -> list[str]:
    """Find all memories.html files in the given path.

    Accepts either:
      - A direct path to a memories.html file
      - A folder containing memories.html
      - A parent folder with subfolders or .zip files containing memories.html
    """
    if os.path.isfile(input_path):
        return [input_path]

    if os.path.isdir(input_path):
        # Check if this folder itself has memories.html
        direct = os.path.join(input_path, "memories.html")
        if os.path.isfile(direct):
            return [direct]

        # Extract any zip files first
        zips = [f for f in os.listdir(input_path) if f.lower().endswith(".zip")]
        if zips:
            log.info(f"Found {len(zips)} zip file(s), extracting...")
            _extract_zips(input_path)

        # Scan subfolders for memories.html
        found = []
        for name in sorted(os.listdir(input_path)):
            sub = os.path.join(input_path, name)
            if os.path.isdir(sub):
                html = os.path.join(sub, "memories.html")
                if os.path.isfile(html):
                    found.append(html)
        return found

    return []


def main():
    parser = argparse.ArgumentParser(
        description="Extract Snapchat memories, composite overlays, and fix metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -i ./memories_folder -d ./output
  %(prog)s -i ./all_exports -d ./output          (scans subfolders for memories.html)
  %(prog)s -i ./zips_folder -d ./output           (auto-extracts .zip files first)
  %(prog)s -i ./memories/memories.html -d ./output
  %(prog)s -i ./all_exports -d ./output --skip-metadata

Requirements:
  pip install beautifulsoup4 pyexiftool Pillow
  System: exiftool (brew install exiftool)
  Optional: ffmpeg (for video overlay compositing)
        """,
    )
    parser.add_argument("-i", "--input", required=True,
                        help="Path to memories.html, a folder containing it, or a parent folder with subfolders/.zips")
    parser.add_argument("-d", "--download-dir", required=True, help="Output directory for processed media")
    parser.add_argument("--skip-metadata", action="store_true", help="Skip metadata update")

    args = parser.parse_args()

    html_files = find_memories_html(args.input)
    if not html_files:
        log.error(f"No memories.html found in: {args.input}")
        sys.exit(1)

    log.info(f"Found {len(html_files)} memories.html file(s) to process")
    os.makedirs(args.download_dir, exist_ok=True)

    all_outputs = []
    for html_path in html_files:
        log.info(f"── Processing: {html_path}")
        entries = parse_html(html_path)
        outputs = process_entries(entries, args.download_dir)
        all_outputs.extend(outputs)

    if not args.skip_metadata:
        update_metadata(all_outputs)
    else:
        log.info("Skipping metadata update (--skip-metadata)")

    log.info(f"Done. {len(all_outputs)} files in {args.download_dir}")


if __name__ == "__main__":
    main()
