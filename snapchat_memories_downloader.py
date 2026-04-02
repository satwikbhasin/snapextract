#!/usr/bin/env python3
"""
Snapchat Memories Downloader & Metadata Fixer

Parses Snapchat's memories_history.html export, downloads all media,
handles ZIP bundles, and writes correct capture time + GPS into EXIF/XMP metadata.

Usage:
    python snapchat_memories_downloader.py -m /path/to/memories_history.html -d /path/to/downloads
    python snapchat_memories_downloader.py -m /path/to/memories_history.html -d /path/to/downloads --workers 8
"""

import argparse
import logging
import os
import re
import shutil
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import StringIO
from typing import Optional

import subprocess

import pandas as pd
import requests
from bs4 import BeautifulSoup
from exiftool import ExifToolHelper
from PIL import Image

# ─── Config ──────────────────────────────────────────────────────────────────

MAX_RETRIES = 3
RETRY_DELAY_SEC = 2
DEFAULT_WORKERS = 4
CHUNK_SIZE = 8192

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("snapmem")


# ─── HTML Parsing ────────────────────────────────────────────────────────────

def parse_html(html_path: str) -> pd.DataFrame:
    """Parse memories_history.html into a structured DataFrame."""
    log.info(f"Parsing {html_path}")

    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    # Extract table data via pandas
    tables = pd.read_html(StringIO(html_content))
    if not tables:
        log.error("No tables found in HTML file.")
        sys.exit(1)

    df = tables[0]
    df = df.rename(columns={
        df.columns[0]: "timestamp_str",
        df.columns[1]: "media_type",
        df.columns[2]: "coordinates",
        df.columns[3]: "_link_placeholder",
    })

    # Parse timestamps
    df["timestamp_ms"] = df["timestamp_str"].apply(
        lambda ts: int(pd.to_datetime(ts, utc=True).timestamp() * 1000)
    )

    # Normalize media type
    df["media_type"] = df["media_type"].str.strip().str.replace(" ", "_").str.lower()

    # Extract lat/lon
    df["lat"] = None
    df["lon"] = None
    for i, row in df.iterrows():
        lat, lon = _extract_coords(row["coordinates"])
        df.at[i, "lat"] = lat
        df.at[i, "lon"] = lon

    # Extract download links + request method from onclick JS
    soup = BeautifulSoup(html_content, "html.parser")
    data_rows = soup.select("table tbody tr")[1:]  # skip header row

    pattern = r"downloadMemories\('([^']+)',\s*this,\s*(true|false)\)"
    links = []
    is_get = []

    for row in data_rows:
        a_tag = row.find("a", onclick=True)
        if a_tag and "onclick" in a_tag.attrs:
            match = re.search(pattern, a_tag["onclick"])
            if match:
                links.append(match.group(1))
                is_get.append(match.group(2) == "true")
                continue
        links.append(None)
        is_get.append(None)

    df["download_link"] = links
    df["is_get_request"] = is_get

    # Derived columns
    df["file_name"] = df.apply(lambda r: f"{r['timestamp_ms']}_{r['media_type']}", axis=1)
    df["file_path"] = None
    df["is_zip"] = False
    df["is_extracted"] = False

    valid = df["download_link"].notna().sum()
    log.info(f"Found {len(df)} entries ({valid} with valid download links)")
    return df


def _extract_coords(coord_str) -> tuple[Optional[float], Optional[float]]:
    """Extract latitude and longitude from 'Latitude, Longitude: XX.XX, -XX.XX'."""
    if pd.isna(coord_str) or not isinstance(coord_str, str):
        return None, None
    match = re.search(r"(-?\d+\.?\d*),\s*(-?\d+\.?\d*)", coord_str)
    if match:
        return float(match.group(1)), float(match.group(2))
    return None, None


# ─── Downloading ─────────────────────────────────────────────────────────────

def _fetch_response(url: str, is_get: bool) -> requests.Response:
    """Fetch media from Snapchat CDN with retry logic."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if is_get:
                headers = {"X-Snap-Route-Tag": "mem-dmd"}
                resp = requests.get(url, headers=headers, stream=True, timeout=60)
            else:
                parts = url.split("?", 1)
                base_url = parts[0]
                payload = parts[1] if len(parts) > 1 else ""
                headers = {"Content-type": "application/x-www-form-urlencoded"}
                resp = requests.post(base_url, data=payload, headers=headers, stream=True, timeout=60)
            resp.raise_for_status()
            return resp
        except (requests.RequestException, IOError) as e:
            if attempt == MAX_RETRIES:
                raise
            log.warning(f"  Retry {attempt}/{MAX_RETRIES}: {e}")
            time.sleep(RETRY_DELAY_SEC * attempt)


def _get_extension(resp: requests.Response) -> str:
    """Determine file extension from Content-Disposition header."""
    cd = resp.headers.get("Content-Disposition", "")
    match = re.search(r'filename="?([^"]+)"?', cd)
    if match:
        _, ext = os.path.splitext(match.group(1))
        return ext.lower() if ext else ".dat"
    ct = resp.headers.get("Content-Type", "")
    mapping = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "video/mp4": ".mp4", "video/quicktime": ".mov",
        "application/zip": ".zip",
    }
    for mime, ext in mapping.items():
        if mime in ct:
            return ext
    return ".dat"


def _download_single(idx: int, row: pd.Series, download_dir: str) -> dict:
    """Download a single memory. Returns update dict for the DataFrame."""
    url = row["download_link"]
    is_get = row["is_get_request"]
    file_name = row["file_name"]

    if pd.isna(url) or url is None:
        return {"idx": idx, "file_path": None, "is_zip": False, "error": "no link"}

    try:
        resp = _fetch_response(url, is_get)
        ext = _get_extension(resp)
        is_zip = ext == ".zip"
        full_path = os.path.join(download_dir, f"{file_name}{ext}")

        with open(full_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                f.write(chunk)

        return {"idx": idx, "file_path": full_path, "is_zip": is_zip, "error": None}
    except Exception as e:
        return {"idx": idx, "file_path": None, "is_zip": False, "error": str(e)}


def download_memories(df: pd.DataFrame, download_dir: str, workers: int):
    """Download all memories with concurrent workers."""
    to_download = df[df["download_link"].notna()]
    total = len(to_download)
    log.info(f"Downloading {total} memories ({workers} workers)...")

    done = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download_single, idx, row, download_dir): idx
            for idx, row in to_download.iterrows()
        }
        for future in as_completed(futures):
            result = future.result()
            idx = result["idx"]
            done += 1

            if result["error"]:
                failed += 1
                log.warning(f"  [{done}/{total}] FAILED row {idx}: {result['error']}")
            else:
                df.at[idx, "file_path"] = result["file_path"]
                df.at[idx, "is_zip"] = result["is_zip"]
                if done % 25 == 0 or done == total:
                    log.info(f"  [{done}/{total}] downloaded ({failed} failed)")

    log.info(f"Downloads complete: {done - failed} ok, {failed} failed")


# ─── ZIP Handling + Overlay Compositing ──────────────────────────────────────

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}


def _classify_zip_contents(files: list[str]) -> dict:
    """Classify extracted files into base media and overlay candidates.

    Snapchat ZIPs typically contain:
      - 1 base image/video (the original snap)
      - 1 overlay PNG (filter/text/sticker layer with transparency)
    Sometimes there's just media with no overlay, or multiple layers.
    """
    images = []
    videos = []
    overlays = []

    for f in files:
        ext = os.path.splitext(f)[1].lower()
        fname = os.path.basename(f).lower()

        if ext in VIDEO_EXTS:
            videos.append(f)
        elif ext in IMAGE_EXTS:
            # Heuristic: overlay PNGs tend to be named "overlay", have alpha,
            # or are the second PNG. We'll check alpha after loading.
            images.append(f)

    # If we have exactly 1 video + 1 image, the image is the overlay
    if len(videos) == 1 and len(images) == 1:
        return {"base": videos[0], "overlay": images[0], "extras": []}

    # If we have 2+ images and no video, figure out which is the overlay
    if len(images) >= 2 and len(videos) == 0:
        # Check which has alpha channel (transparency = overlay)
        base = None
        overlay = None
        for img_path in images:
            try:
                with Image.open(img_path) as im:
                    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                        overlay = img_path
                    else:
                        base = img_path
            except Exception:
                pass

        # Fallback: if no alpha detected, treat first as base, second as overlay
        if base is None and overlay is None:
            base, overlay = images[0], images[1]
        elif base is None:
            # All have alpha — pick largest file as base
            images_sorted = sorted(images, key=lambda f: os.path.getsize(f), reverse=True)
            base = images_sorted[0]
            overlay = images_sorted[1] if images_sorted[1] != base else (images_sorted[2] if len(images_sorted) > 2 else None)
        elif overlay is None:
            # None have alpha — no real overlay, just return all as separate files
            return {"base": images[0], "overlay": None, "extras": images[1:]}

        extras = [f for f in images if f not in (base, overlay)]
        return {"base": base, "overlay": overlay, "extras": extras}

    # Single file or unrecognized combo — no compositing needed
    all_files = videos + images
    if all_files:
        return {"base": all_files[0], "overlay": None, "extras": all_files[1:]}
    return {"base": None, "overlay": None, "extras": files}


def _composite_image(base_path: str, overlay_path: str, output_path: str):
    """Paste overlay PNG onto base image using alpha compositing."""
    with Image.open(base_path) as base:
        base = base.convert("RGBA")
        with Image.open(overlay_path) as overlay:
            overlay = overlay.convert("RGBA")
            # Resize overlay to match base if dimensions differ
            if overlay.size != base.size:
                overlay = overlay.resize(base.size, Image.LANCZOS)
            composite = Image.alpha_composite(base, overlay)

        # Save as JPEG if original was JPEG, else PNG
        base_ext = os.path.splitext(base_path)[1].lower()
        if base_ext in (".jpg", ".jpeg"):
            composite = composite.convert("RGB")
            composite.save(output_path, "JPEG", quality=95)
        else:
            composite.save(output_path, "PNG")

    log.info(f"    Composited overlay onto {os.path.basename(output_path)}")


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
            log.info(f"    Composited overlay onto video {os.path.basename(output_path)}")
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


def handle_zips(df: pd.DataFrame, download_dir: str):
    """Extract ZIPs, composite overlays onto base media, and append to DataFrame."""
    zips = df[df["is_zip"] == True]
    if zips.empty:
        log.info("No ZIP files to extract.")
        return

    log.info(f"Extracting {len(zips)} ZIP files (with overlay compositing)...")
    new_rows = []
    composited_count = 0

    for idx, row in zips.iterrows():
        zip_path = row["file_path"]
        if not zip_path or not os.path.exists(zip_path):
            continue

        zip_name = os.path.splitext(os.path.basename(zip_path))[0]
        temp_dir = os.path.join(download_dir, f"_tmp_{zip_name}")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                os.makedirs(temp_dir, exist_ok=True)
                zf.extractall(temp_dir)

            # Collect all extracted files
            extracted_files = []
            for root, _, files in os.walk(temp_dir):
                for fname in files:
                    if not fname.startswith("."):  # skip hidden files
                        extracted_files.append(os.path.join(root, fname))

            # Classify contents
            classified = _classify_zip_contents(extracted_files)
            base_file = classified["base"]
            overlay_file = classified["overlay"]
            extras = classified["extras"]

            output_files = []

            if base_file and overlay_file:
                # Composite overlay onto base
                base_ext = os.path.splitext(base_file)[1].lower()
                is_video = base_ext in VIDEO_EXTS

                if is_video:
                    out_ext = base_ext
                    out_name = f"{zip_name}_composited{out_ext}"
                    out_path = os.path.join(download_dir, out_name)
                    success = _composite_video(base_file, overlay_file, out_path)
                    if success:
                        composited_count += 1
                        output_files.append(out_path)
                    else:
                        # Fallback: keep both files separately
                        for i, src in enumerate([base_file, overlay_file]):
                            ext = os.path.splitext(src)[1]
                            dest = os.path.join(download_dir, f"{zip_name}_part_{i+1}{ext}")
                            shutil.move(src, dest)
                            output_files.append(dest)
                else:
                    # Image compositing
                    if base_ext in (".jpg", ".jpeg"):
                        out_name = f"{zip_name}_composited.jpg"
                    else:
                        out_name = f"{zip_name}_composited.png"
                    out_path = os.path.join(download_dir, out_name)
                    try:
                        _composite_image(base_file, overlay_file, out_path)
                        composited_count += 1
                        output_files.append(out_path)
                    except Exception as e:
                        log.warning(f"    Composite failed: {e}, keeping files separate")
                        for i, src in enumerate([base_file, overlay_file]):
                            ext = os.path.splitext(src)[1]
                            dest = os.path.join(download_dir, f"{zip_name}_part_{i+1}{ext}")
                            shutil.move(src, dest)
                            output_files.append(dest)
            elif base_file:
                # No overlay — just move the base file
                ext = os.path.splitext(base_file)[1]
                dest = os.path.join(download_dir, f"{zip_name}_extracted{ext}")
                shutil.move(base_file, dest)
                output_files.append(dest)

            # Handle any extra files
            for i, src in enumerate(extras):
                ext = os.path.splitext(src)[1]
                dest = os.path.join(download_dir, f"{zip_name}_extra_{i+1}{ext}")
                shutil.move(src, dest)
                output_files.append(dest)

            # Add all output files to DataFrame
            for out_path in output_files:
                new_row = row.copy()
                new_row["file_name"] = os.path.splitext(os.path.basename(out_path))[0]
                new_row["file_path"] = out_path
                new_row["is_zip"] = False
                new_row["is_extracted"] = True
                new_rows.append(new_row)

        except zipfile.BadZipFile:
            log.warning(f"  Bad ZIP: {zip_path}")
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

        # Remove original zip
        os.remove(zip_path)
        df.at[idx, "file_path"] = None

    for new_row in new_rows:
        df.loc[len(df)] = new_row

    log.info(f"Extracted {len(new_rows)} files from ZIPs ({composited_count} with overlays composited)")


# ─── Metadata Fixing ────────────────────────────────────────────────────────

def update_metadata(df: pd.DataFrame):
    """Write correct capture time and GPS coordinates into media EXIF/XMP."""
    eligible = df[(df["file_path"].notna()) & (df["is_zip"] == False)]
    total = len(eligible)
    if total == 0:
        log.info("No files to update metadata for.")
        return

    log.info(f"Updating metadata on {total} files...")
    updated = 0
    errors = 0

    with ExifToolHelper() as et:
        for idx, row in eligible.iterrows():
            fpath = row["file_path"]
            if not fpath or not os.path.exists(fpath):
                continue

            try:
                ts_str = row["timestamp_str"]
                dt_obj = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S UTC")
                exif_dt = dt_obj.strftime("%Y:%m:%d %H:%M:%S")

                tags = [
                    "-overwrite_original",
                    f"-XMP:DateTimeOriginal={exif_dt}",
                    f"-XMP:CreateDate={exif_dt}",
                    f"-DateTimeOriginal={exif_dt}",
                    f"-CreateDate={exif_dt}",
                    f"-ModifyDate={exif_dt}",
                ]

                lat, lon = row["lat"], row["lon"]
                if lat is not None and lon is not None:
                    lat, lon = float(lat), float(lon)
                    tags.extend([
                        f"-XMP:GPSLatitude={lat}",
                        f"-XMP:GPSLongitude={lon}",
                        f"-GPSLatitude={abs(lat)}",
                        f"-GPSLongitude={abs(lon)}",
                        f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                        f"-GPSLongitudeRef={'E' if lon >= 0 else 'W'}",
                    ])

                et.execute(*tags, fpath)

                # Also set OS file times
                unix_ts = pd.to_datetime(ts_str, utc=True).timestamp()
                os.utime(fpath, (unix_ts, unix_ts))

                updated += 1
                if updated % 25 == 0 or updated == total:
                    log.info(f"  [{updated}/{total}] metadata updated")

            except Exception as e:
                errors += 1
                log.warning(f"  Metadata error on {os.path.basename(fpath)}: {e}")

    log.info(f"Metadata complete: {updated} ok, {errors} errors")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Download Snapchat memories and fix their metadata (capture time + GPS).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -m ~/Downloads/memories_history.html -d ~/SnapMemories
  %(prog)s -m export/memories_history.html -d ./output --workers 8
  %(prog)s -m export/memories_history.html -d ./output --skip-download

Requirements:
  pip install beautifulsoup4 pandas requests pyexiftool
  System: exiftool (apt install libimage-exiftool-perl / brew install exiftool)
        """,
    )
    parser.add_argument("-m", "--memories-path", required=True, help="Path to memories_history.html")
    parser.add_argument("-d", "--download-dir", required=True, help="Output directory for downloaded media")
    parser.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS, help=f"Concurrent download threads (default: {DEFAULT_WORKERS})")
    parser.add_argument("--skip-download", action="store_true", help="Skip downloading, only fix metadata on existing files")
    parser.add_argument("--skip-metadata", action="store_true", help="Skip metadata update, only download files")

    args = parser.parse_args()

    if not os.path.exists(args.memories_path):
        log.error(f"File not found: {args.memories_path}")
        sys.exit(1)

    os.makedirs(args.download_dir, exist_ok=True)

    # 1. Parse
    df = parse_html(args.memories_path)

    # 2. Download
    if not args.skip_download:
        download_memories(df, args.download_dir, args.workers)
    else:
        log.info("Skipping download (--skip-download)")
        # Try to match existing files
        for idx, row in df.iterrows():
            pattern = os.path.join(args.download_dir, f"{row['file_name']}.*")
            import glob
            matches = glob.glob(pattern)
            if matches:
                df.at[idx, "file_path"] = matches[0]
                df.at[idx, "is_zip"] = matches[0].endswith(".zip")

    # 3. Handle ZIPs
    handle_zips(df, args.download_dir)

    # 4. Fix metadata
    if not args.skip_metadata:
        update_metadata(df)
    else:
        log.info("Skipping metadata update (--skip-metadata)")

    # Summary
    downloaded = df["file_path"].notna().sum()
    log.info(f"Done. {downloaded} files in {args.download_dir}")


if __name__ == "__main__":
    main()
