#!/usr/bin/env python3
"""
Snapchat Memories Extractor & Metadata Fixer

Supports two Snapchat export formats:
  1. memories_history.html — table-based with CDN download links, full timestamps, GPS
  2. memories.html — div-based with local media files, date-only

Usage:
    python snapchat_memories_downloader.py -i /path/to/export -d /path/to/output
    python snapchat_memories_downloader.py -i /path/to/export -d /path/to/output --skip-metadata
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
from urllib.parse import parse_qs, urlparse

import requests
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


# ─── CDN Download Format (memories_history.html) ───────────────────────────

def parse_history_html(html_path: str) -> list[dict]:
    """Parse table-based memories_history.html into a list of entries.

    Each entry has: date (full timestamp), media_type, lat, lon, cdn_url.
    """
    log.info(f"Parsing history file: {html_path}")

    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    table = soup.find("table")
    if not table:
        log.error("No table found in history HTML")
        return []

    rows = table.find_all("tr")[1:]  # skip header
    log.info(f"Found {len(rows)} entries in history table")

    entries = []
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 4:
            continue

        date_str = tds[0].text.strip()
        media_type = tds[1].text.strip().lower()
        location_str = tds[2].text.strip()

        # Extract CDN URL from onclick
        link = tds[3].find("a")
        if not link:
            continue
        onclick = link.get("onclick", "")
        url_match = re.search(r"downloadMemories\('([^']+)'", onclick)
        if not url_match:
            continue
        cdn_url = url_match.group(1).replace("&amp;", "&")

        # Parse GPS
        lat, lon = None, None
        gps_match = re.search(r"Latitude, Longitude:\s*([-\d.]+),\s*([-\d.]+)", location_str)
        if gps_match:
            lat = float(gps_match.group(1))
            lon = float(gps_match.group(2))

        entries.append({
            "date": date_str,
            "media_type": media_type,
            "lat": lat,
            "lon": lon,
            "cdn_url": cdn_url,
        })

    images = sum(1 for e in entries if e["media_type"] == "image")
    videos = sum(1 for e in entries if e["media_type"] == "video")
    gps_count = sum(1 for e in entries if e["lat"] is not None)
    log.info(f"Parsed {len(entries)} entries: {images} images, {videos} videos, {gps_count} with GPS")
    return entries


def download_from_cdn(entries: list[dict], output_dir: str) -> list[dict]:
    """Download all entries from CDN and return output records."""
    total = len(entries)
    log.info(f"Downloading {total} memories from Snapchat CDN...")

    outputs = []
    downloaded = 0
    errors = 0

    for i, entry in enumerate(entries):
        cdn_url = entry["cdn_url"]
        media_type = entry["media_type"]
        date_str = entry["date"]

        # Build filename from date + index
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S %Z")
        except ValueError:
            dt = datetime.strptime(date_str.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S")
        ts_str = dt.strftime("%Y-%m-%d_%H-%M-%S")

        # Extract SID for unique naming
        parsed = urlparse(cdn_url)
        params = parse_qs(parsed.query)
        sid = params.get("sid", [f"unknown_{i}"])[0]
        short_id = sid[:8]

        ext = ".jpg" if media_type == "image" else ".mp4"
        filename = f"{ts_str}_{short_id}{ext}"
        out_path = os.path.join(output_dir, filename)

        # Skip if already downloaded
        if os.path.exists(out_path):
            outputs.append({
                "date": date_str,
                "file_path": out_path,
                "lat": entry.get("lat"),
                "lon": entry.get("lon"),
            })
            downloaded += 1
            done = i + 1
            if done % 100 == 0 or done == total:
                log.info(f"  [{done}/{total}] {downloaded} downloaded, {errors} errors (skipped existing)")
            continue

        try:
            resp = requests.get(
                cdn_url,
                headers={"X-Snap-Route-Tag": "mem-dmd"},
                timeout=60,
            )
            resp.raise_for_status()

            # Detect actual content type and fix extension
            content_type = resp.headers.get("Content-Type", "")
            if "video" in content_type and ext != ".mp4":
                ext = ".mp4"
                filename = f"{ts_str}_{short_id}{ext}"
                out_path = os.path.join(output_dir, filename)
            elif "image" in content_type and ext == ".mp4":
                if "png" in content_type:
                    ext = ".png"
                else:
                    ext = ".jpg"
                filename = f"{ts_str}_{short_id}{ext}"
                out_path = os.path.join(output_dir, filename)

            with open(out_path, "wb") as f:
                f.write(resp.content)

            downloaded += 1
            outputs.append({
                "date": date_str,
                "file_path": out_path,
                "lat": entry.get("lat"),
                "lon": entry.get("lon"),
            })

        except Exception as e:
            errors += 1
            log.warning(f"  Download failed for {filename}: {e}")

        done = i + 1
        if done % 100 == 0 or done == total:
            log.info(f"  [{done}/{total}] {downloaded} downloaded, {errors} errors")

    log.info(f"Download complete: {downloaded} ok, {errors} errors")
    return outputs


# ─── Local File Format (memories.html) ─────────────────────────────────────

def parse_html(html_path: str) -> list[dict]:
    """Parse div-based memories.html into a list of memory entries."""
    log.info(f"Parsing {html_path}")
    html_dir = os.path.dirname(os.path.abspath(html_path))

    with open(html_path, "r", encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    containers = soup.select(".image-container")
    log.info(f"Found {len(containers)} containers in HTML")

    uuid_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})_([A-Fa-f0-9-]+)-(main|overlay)\.(\w+)")
    uuid_map = {}

    for container in containers:
        date_el = container.select_one(".text-line")
        date_str = date_el.text.strip() if date_el else None

        media_tags = container.find_all(["img", "video"])
        for tag in media_tags:
            src = tag.get("src", "")
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


def process_entries(entries: list[dict], output_dir: str) -> list[dict]:
    """Copy/composite local-file entries into the output directory."""
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
            out_path = os.path.join(output_dir, os.path.basename(main_file))
            shutil.copy2(main_file, out_path)
            copied += 1
            outputs.append({"date": date, "file_path": out_path})

        elif overlay_file and os.path.exists(overlay_file):
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
    """Write correct capture date and GPS into media EXIF/XMP and OS file times."""
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
            lat = entry.get("lat")
            lon = entry.get("lon")

            if not fpath or not os.path.exists(fpath):
                continue

            try:
                # Parse date — supports both "YYYY-MM-DD HH:MM:SS UTC" and "YYYY-MM-DD"
                if len(date_str) > 10:
                    dt_obj = datetime.strptime(date_str.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S")
                    exif_dt = dt_obj.strftime("%Y:%m:%d %H:%M:%S")
                else:
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

                # Add GPS if available
                if lat is not None and lon is not None:
                    lat_ref = "N" if lat >= 0 else "S"
                    lon_ref = "E" if lon >= 0 else "W"
                    tags.extend([
                        f"-GPSLatitude={abs(lat)}",
                        f"-GPSLatitudeRef={lat_ref}",
                        f"-GPSLongitude={abs(lon)}",
                        f"-GPSLongitudeRef={lon_ref}",
                    ])

                et.execute(*tags, fpath)

                # Set OS file times
                unix_ts = dt_obj.timestamp()
                os.utime(fpath, (unix_ts, unix_ts))

                updated += 1
                if updated % 100 == 0 or updated == total:
                    log.info(f"  [{updated}/{total}] metadata updated")

            except Exception as e:
                errors += 1
                log.warning(f"  Metadata error on {os.path.basename(fpath)}: {e}")

    log.info(f"Metadata complete: {updated} ok, {errors} errors")


# ─── File Discovery ─────────────────────────────────────────────────────────

def _extract_zips(input_path: str) -> list[str]:
    """Extract any .zip files in the input directory."""
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


def find_html_files(input_path: str) -> dict:
    """Find memories HTML files. Returns dict with 'history' and 'local' lists.

    'history' = table-based files (memories_history*.html) with CDN links
    'local' = div-based files (memories.html) with local media
    """
    result = {"history": [], "local": []}

    def _scan_dir(dirpath):
        for name in sorted(os.listdir(dirpath)):
            full = os.path.join(dirpath, name)
            if os.path.isfile(full) and name.endswith(".html"):
                if "memories_history" in name:
                    result["history"].append(full)
                elif name == "memories.html":
                    result["local"].append(full)

    if os.path.isfile(input_path):
        name = os.path.basename(input_path)
        if "memories_history" in name:
            result["history"].append(input_path)
        else:
            result["local"].append(input_path)
        return result

    if os.path.isdir(input_path):
        # Check this folder
        _scan_dir(input_path)

        # Extract zips
        zips = [f for f in os.listdir(input_path) if f.lower().endswith(".zip")]
        if zips:
            log.info(f"Found {len(zips)} zip file(s), extracting...")
            _extract_zips(input_path)

        # Scan subfolders
        for name in sorted(os.listdir(input_path)):
            sub = os.path.join(input_path, name)
            if os.path.isdir(sub):
                _scan_dir(sub)

    return result


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract Snapchat memories, composite overlays, and fix metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -i ./memories_folder -d ./output
  %(prog)s -i ./all_exports -d ./output
  %(prog)s -i ./zips_folder -d ./output
  %(prog)s -i ./memories/memories_history.html -d ./output
  %(prog)s -i ./all_exports -d ./output --skip-metadata

Requirements:
  pip install beautifulsoup4 pyexiftool Pillow requests
  System: exiftool (brew install exiftool)
  Optional: ffmpeg (for video overlay compositing)
        """,
    )
    parser.add_argument("-i", "--input", required=True,
                        help="Path to HTML file, export folder, or parent folder with subfolders/.zips")
    parser.add_argument("-d", "--download-dir", required=True, help="Output directory for processed media")
    parser.add_argument("--skip-metadata", action="store_true", help="Skip metadata update")

    args = parser.parse_args()

    html_files = find_html_files(args.input)
    history_files = html_files["history"]
    local_files = html_files["local"]

    if not history_files and not local_files:
        log.error(f"No memories HTML files found in: {args.input}")
        sys.exit(1)

    log.info(f"Found {len(history_files)} history file(s), {len(local_files)} local file(s)")
    os.makedirs(args.download_dir, exist_ok=True)

    all_outputs = []

    # Prefer history files (richer data: full timestamps + GPS + all memories)
    if history_files:
        for html_path in history_files:
            log.info(f"── Processing history file: {html_path}")
            entries = parse_history_html(html_path)
            outputs = download_from_cdn(entries, args.download_dir)
            all_outputs.extend(outputs)
    else:
        # Fall back to local div-based files
        for html_path in local_files:
            log.info(f"── Processing local file: {html_path}")
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
