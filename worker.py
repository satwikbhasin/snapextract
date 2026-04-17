#!/usr/bin/env python3
"""
Snapchat Memories Extractor & Metadata Fixer

Supports two Snapchat export formats:
  1. memories_history.html — table-based with CDN download links, full timestamps, GPS
  2. memories.html         — div-based with local media files, date-only

Usage:
    python worker.py -i /path/to/export -d /path/to/output
    python worker.py -i /path/to/export -d /path/to/output --skip-metadata
    python worker.py -i /path/to/export -d /path/to/output --workers 10

Requirements:
    pip install beautifulsoup4 pyexiftool Pillow requests
    System: exiftool (brew install exiftool)
    Optional: ffmpeg (for video overlay compositing)
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from exiftool import ExifToolHelper
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Constants ────────────────────────────────────────────────────────────────

IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"})
VIDEO_EXTS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"})

DEFAULT_WORKERS = 15
MIN_WORKERS = 1
MAX_WORKERS_LIMIT = 50

# ─── Logging ─────────────────────────────────────────────────────────────────

# Shared lock — both the log handler and the progress bar must hold this
# before writing to stderr so they never interleave.
_print_lock = threading.Lock()

# The last progress bar string, so the handler can redraw it after a log line.
_current_bar: str = ""


class _ColoredFormatter(logging.Formatter):
    _COLORS = {
        "DEBUG":    "\033[36m",
        "INFO":     "\033[32m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[35m",
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self._COLORS.get(record.levelname, "")
        record.colored_levelname = (
            f"{color}{record.levelname:<7}{self._RESET}" if color else record.levelname
        )
        return super().format(record)


class _BarAwareHandler(logging.StreamHandler):
    """
    A stderr log handler that is aware of the progress bar.

    Before emitting a log line it:
      1. Erases the current progress bar line (\r + \033[2K).
      2. Prints the log line normally (with newline).
      3. Redraws the last known progress bar so it stays at the bottom.

    All three steps happen under _print_lock so no thread can interleave.
    """

    def emit(self, record: logging.LogRecord) -> None:
        global _current_bar
        try:
            msg = self.format(record)
            with _print_lock:
                # 1. Erase bar
                sys.stderr.write("\r\033[2K")
                # 2. Log line
                sys.stderr.write(msg + "\n")
                # 3. Redraw bar (if one exists)
                if _current_bar:
                    sys.stderr.write(_current_bar)
                sys.stderr.flush()
        except Exception:
            self.handleError(record)


def _build_logger() -> logging.Logger:
    handler = _BarAwareHandler(sys.stderr)
    handler.setFormatter(
        _ColoredFormatter(
            "%(asctime)s │ %(colored_levelname)s │ %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger = logging.getLogger("snapmem")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


log = _build_logger()


# ─── Thread-safe progress tracker ────────────────────────────────────────────


@dataclass
class _Progress:
    """All counters are mutated exclusively through locked methods."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # Download
    downloaded: int = 0
    failed_downloads: int = 0
    skipped_downloads: int = 0
    images_downloaded: int = 0
    videos_downloaded: int = 0
    zip_downloads: int = 0
    direct_downloads: int = 0
    rate_limited: int = 0

    # Metadata
    metadata_ok: int = 0
    metadata_errors: int = 0
    metadata_updated: int = 0
    metadata_skipped: int = 0

    # Totals / timing
    total: int = 0
    phase: str = "download"
    start_time: Optional[float] = None
    download_end_time: Optional[float] = None
    end_time: Optional[float] = None

    # Structured failure / event logs (bounded)
    failed_entries: list = field(default_factory=list)
    rate_limit_events: list = field(default_factory=list)

    _MAX_FAILED_LOG = 500
    _MAX_RATE_EVENTS = 200

    # ── Mutation helpers ──────────────────────────────────────────────────────

    def inc(self, attr: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, attr, getattr(self, attr) + amount)

    def set(self, attr: str, value) -> None:
        with self._lock:
            setattr(self, attr, value)

    def log_failure(self, entry: dict) -> None:
        with self._lock:
            if len(self.failed_entries) < self._MAX_FAILED_LOG:
                self.failed_entries.append(entry)

    def log_rate_event(self, event: dict) -> None:
        with self._lock:
            if len(self.rate_limit_events) < self._MAX_RATE_EVENTS:
                self.rate_limit_events.append(event)

    def snapshot(self) -> dict:
        with self._lock:
            return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}


# ─── Shared rate limiter ──────────────────────────────────────────────────────


class _TokenBucketRateLimiter:
    """
    Leaky-bucket rate limiter shared across all download threads.

    - Enforces a steady inter-request delay (rps).
    - Supports a global backoff gate: when any thread hits 429 it broadcasts
      a pause to all threads for `backoff_seconds`.
    - Automatically reduces rps on repeated 429s.
    """

    def __init__(self, rps: float = 10.0) -> None:
        self._lock = threading.Lock()
        self._rps = rps
        self._min_delay = 1.0 / rps
        self._last_call = 0.0
        # Backoff gate: cleared = all threads block, set = threads may proceed
        self._gate = threading.Event()
        self._gate.set()
        self._consecutive_429s = 0

    def acquire(self) -> None:
        """Block until a request slot is available."""
        self._gate.wait()  # honour global backoff
        with self._lock:
            now = time.monotonic()
            gap = self._min_delay - (now - self._last_call)
            if gap > 0:
                time.sleep(gap)
            self._last_call = time.monotonic()

    def on_429(self, retry_after: Optional[int] = None, index: int = -1) -> float:
        """
        Called by any thread upon receiving a 429.
        Closes the gate for all threads, schedules re-open, reduces rps.
        Returns the actual wait time used.
        """
        with self._lock:
            self._consecutive_429s += 1
            # Exponential rps reduction: halve rps each time, floor at 1 req/s
            new_rps = max(1.0, self._rps / (2 ** min(self._consecutive_429s, 4)))
            if new_rps != self._rps:
                self._rps = new_rps
                self._min_delay = 1.0 / new_rps
                log.warning(f"  Rate limiter: throttling to {new_rps:.1f} rps after {self._consecutive_429s} consecutive 429(s)")

        wait = retry_after if retry_after and 1 <= retry_after <= 300 else min(
            10 * (2 ** min(self._consecutive_429s - 1, 4)), 120
        )
        wait += random.uniform(0, 3)  # jitter

        # Only the first thread to close the gate does the actual pause
        if self._gate.is_set():
            self._gate.clear()
            log.warning(f"  Global backoff: pausing all threads for {wait:.1f}s (entry #{index})")
            threading.Timer(wait, self._reopen_gate).start()

        return wait

    def on_success(self) -> None:
        with self._lock:
            self._consecutive_429s = max(0, self._consecutive_429s - 1)

    def _reopen_gate(self) -> None:
        self._gate.set()
        log.info("  Global backoff lifted — resuming downloads")


# ─── Progress display ────────────────────────────────────────────────────────


def _elapsed_str(start: Optional[float]) -> str:
    if start is None:
        return "0s"
    elapsed = time.time() - start
    if elapsed < 60:
        return f"{elapsed:.1f}s"
    elif elapsed < 3600:
        return f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
    return f"{int(elapsed // 3600)}h {int((elapsed % 3600) // 60)}m"


def _print_progress(prog: _Progress) -> None:
    global _current_bar
    snap = prog.snapshot()
    total = snap["total"]
    if total == 0:
        return
    bar_width = 40
    elapsed = _elapsed_str(snap.get("start_time"))
    phase = snap["phase"]

    if phase == "download":
        current = snap["downloaded"] + snap["failed_downloads"] + snap["skipped_downloads"]
        filled = int(bar_width * current / total)
        bar = "█" * filled + "░" * (bar_width - filled)
        pct = int(100 * current / total)
        rate_str = f" | RL: {snap['rate_limited']}" if snap["rate_limited"] else ""
        skip_str = f" | Skip: {snap['skipped_downloads']}" if snap["skipped_downloads"] else ""
        msg = (
            f"\r\033[2KDownloading: |{bar}| {pct}% "
            f"({current}/{total}) Fail:{snap['failed_downloads']}"
            f"{skip_str}{rate_str} [{elapsed}]"
        )
    else:
        current = snap["metadata_ok"] + snap["metadata_errors"]
        filled = int(bar_width * current / total)
        bar = "█" * filled + "░" * (bar_width - filled)
        pct = int(100 * current / total)
        msg = (
            f"\r\033[2KMetadata: |{bar}| {pct}% "
            f"({current}/{total}) Err:{snap['metadata_errors']} [{elapsed}]"
        )

    with _print_lock:
        _current_bar = msg
        sys.stderr.write(msg)
        sys.stderr.flush()


def _clear_bar() -> None:
    """Erase the progress bar — call before printing a final blank line."""
    global _current_bar
    with _print_lock:
        _current_bar = ""
        sys.stderr.write("\r\033[2K")
        sys.stderr.flush()


# ─── Session factory ──────────────────────────────────────────────────────────


def _make_session(workers: int) -> requests.Session:
    session = requests.Session()
    # No built-in urllib3 retries — we handle retries ourselves for full control
    adapter = HTTPAdapter(
        pool_connections=workers * 2,
        pool_maxsize=workers * 2,
        max_retries=Retry(total=0),
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ─── CDN URL helpers ──────────────────────────────────────────────────────────


def _check_url_expiry(entries: list[dict]) -> None:
    for entry in entries[:1]:
        parsed = urlparse(entry.get("cdn_url", ""))
        params = parse_qs(parsed.query)
        ts_values = params.get("ts", [])
        if not ts_values:
            return
        try:
            url_ts = int(ts_values[0]) / 1000
            age_hours = (time.time() - url_ts) / 3600
            if age_hours > 12:
                log.warning(
                    f"CDN URLs are ~{age_hours:.0f}h old and may have expired. "
                    "If downloads fail with 403, re-request your export from Snapchat."
                )
        except (ValueError, IndexError):
            pass


# ─── Parse memories_history.html ─────────────────────────────────────────────


def parse_history_html(html_path: str) -> list[dict]:
    """Parse table-based memories_history.html. Returns deduplicated entries."""
    log.info(f"Parsing history file: {html_path}")
    with open(html_path, "r", encoding="utf-8") as fh:
        soup = BeautifulSoup(fh.read(), "html.parser")

    table = soup.find("table")
    if not table:
        log.error("No <table> found in history HTML")
        return []

    rows = table.find_all("tr")[1:]
    log.info(f"Found {len(rows)} rows in history table")

    seen_urls: set[str] = set()
    entries: list[dict] = []

    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 4:
            continue

        date_str    = tds[0].text.strip()
        media_type  = tds[1].text.strip().lower()
        location_str = tds[2].text.strip()

        link = tds[3].find("a")
        if not link:
            continue
        onclick = link.get("onclick", "")
        url_match = re.search(r"downloadMemories\('([^']+)'", onclick)
        if not url_match:
            continue
        cdn_url = url_match.group(1).replace("&amp;", "&")

        if cdn_url in seen_urls:
            continue
        seen_urls.add(cdn_url)

        lat = lon = None
        gps_match = re.search(r"Latitude, Longitude:\s*([-\d.]+),\s*([-\d.]+)", location_str)
        if gps_match:
            lat = float(gps_match.group(1))
            lon = float(gps_match.group(2))

        entries.append({
            "date":       date_str,
            "media_type": media_type,
            "lat":        lat,
            "lon":        lon,
            "cdn_url":    cdn_url,
        })

    images   = sum(1 for e in entries if e["media_type"] == "image")
    videos   = sum(1 for e in entries if e["media_type"] == "video")
    gps_cnt  = sum(1 for e in entries if e["lat"] is not None)
    log.info(f"Parsed {len(entries)} unique entries: {images} images, {videos} videos, {gps_cnt} with GPS")
    return entries


# ─── Parse memories.html (local files) ───────────────────────────────────────


def parse_html(html_path: str) -> list[dict]:
    """Parse div-based memories.html. Returns list of memory entries."""
    log.info(f"Parsing {html_path}")
    html_dir = os.path.dirname(os.path.abspath(html_path))

    with open(html_path, "r", encoding="utf-8") as fh:
        soup = BeautifulSoup(fh.read(), "html.parser")

    containers = soup.select(".image-container")
    log.info(f"Found {len(containers)} containers")

    uuid_pattern = re.compile(r"(\d{4}-\d{2}-\d{2})_([A-Fa-f0-9-]+)-(main|overlay)\.(\w+)")
    uuid_map: dict[str, dict] = {}

    for container in containers:
        date_el  = container.select_one(".text-line")
        date_str = date_el.text.strip() if date_el else None

        for tag in container.find_all(["img", "video"]):
            src      = tag.get("src", "")
            filename = src.lstrip("./")
            match    = uuid_pattern.match(filename)
            if not match:
                continue

            date, uuid, kind, _ = match.groups()
            full_path = os.path.join(html_dir, filename)

            if uuid not in uuid_map:
                uuid_map[uuid] = {"date": date_str or date}
            if kind == "main":
                uuid_map[uuid]["main_file"] = full_path
            elif kind == "overlay":
                uuid_map[uuid]["overlay_file"] = full_path

    entries   = list(uuid_map.values())
    both_cnt  = sum(1 for e in entries if "main_file" in e and "overlay_file" in e)
    log.info(f"{len(entries)} unique memories ({both_cnt} with overlay)")
    return entries


# ─── Overlay compositing ──────────────────────────────────────────────────────


def _composite_image(base_path: str, overlay_path: str, output_path: str) -> None:
    with Image.open(base_path) as base:
        base = base.convert("RGBA")
        with Image.open(overlay_path) as overlay:
            overlay = overlay.convert("RGBA")
            if overlay.size != base.size:
                overlay = overlay.resize(base.size, Image.LANCZOS)
            composite = Image.alpha_composite(base, overlay)

    ext = os.path.splitext(base_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        composite.convert("RGB").save(output_path, "JPEG", quality=95)
    else:
        composite.save(output_path, "PNG")


def _composite_video(base_path: str, overlay_path: str, output_path: str) -> bool:
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", base_path,
                "-i", overlay_path,
                "-filter_complex", "[1:v]scale=iw:ih[ovr];[0:v][ovr]overlay=0:0:shortest=1",
                "-c:a", "copy",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                output_path,
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            log.warning(f"ffmpeg error: {result.stderr[:300]}")
            return False
        return True
    except FileNotFoundError:
        log.warning("ffmpeg not found — skipping video overlay compositing")
        return False
    except subprocess.TimeoutExpired:
        log.warning(f"ffmpeg timed out: {os.path.basename(base_path)}")
        return False


# ─── Single CDN download ──────────────────────────────────────────────────────


def _download_one(
    session: requests.Session,
    entry: dict,
    index: int,
    output_dir: str,
    prog: _Progress,
    limiter: _TokenBucketRateLimiter,
) -> Optional[dict]:
    """
    Download a single CDN entry.

    Returns output record on success, None on permanent failure.
    Raises on transient failure so the caller can retry.
    """
    cdn_url    = entry["cdn_url"]
    media_type = entry["media_type"]
    date_str   = entry["date"]

    # Parse date
    try:
        dt = datetime.strptime(date_str.replace(" UTC", "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
    ts_str = dt.strftime("%Y-%m-%d_%H-%M-%S")

    parsed   = urlparse(cdn_url)
    params   = parse_qs(parsed.query)
    sid      = params.get("sid", [f"unknown_{index}"])[0]
    short_id = sid[:8]

    ext      = ".jpg" if media_type == "image" else ".mp4"
    filename = f"{ts_str}_{short_id}{ext}"
    out_path = os.path.join(output_dir, filename)

    # Already downloaded
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        prog.inc("skipped_downloads")
        return {
            "date": date_str,
            "file_path": out_path,
            "lat": entry.get("lat"),
            "lon": entry.get("lon"),
        }

    max_attempts = 5
    resp = None

    for attempt in range(max_attempts):
        limiter.acquire()
        try:
            resp = session.get(
                cdn_url,
                headers={"X-Snap-Route-Tag": "mem-dmd"},
                timeout=120,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt == max_attempts - 1:
                raise
            wait = (2 ** attempt) + random.uniform(0, 1)
            log.debug(f"  Connection error on #{index} (attempt {attempt + 1}): {exc} — retry in {wait:.1f}s")
            time.sleep(wait)
            continue

        if resp.status_code == 429:
            prog.inc("rate_limited")
            retry_after_hdr = resp.headers.get("Retry-After")
            retry_after = int(retry_after_hdr) if retry_after_hdr and retry_after_hdr.isdigit() else None
            wait = limiter.on_429(retry_after=retry_after, index=index)
            prog.log_rate_event({
                "index": index, "attempt": attempt + 1,
                "wait": round(wait, 1),
                "time": datetime.now(timezone.utc).isoformat(),
            })
            if attempt == max_attempts - 1:
                raise requests.exceptions.HTTPError(
                    f"429 after {max_attempts} attempts", response=resp
                )
            continue  # gate will block until backoff clears

        if resp.status_code == 403:
            # Permanent: expired token — no point retrying
            raise requests.exceptions.HTTPError("403 Forbidden (expired CDN token)", response=resp)

        try:
            resp.raise_for_status()
        except requests.exceptions.HTTPError:
            if attempt == max_attempts - 1:
                raise
            wait = (2 ** attempt) + random.uniform(0, 1)
            log.debug(f"  HTTP {resp.status_code} on #{index} — retry in {wait:.1f}s")
            time.sleep(wait)
            continue

        limiter.on_success()
        break
    else:
        # Should never reach here — loop always raises on final attempt
        raise RuntimeError(f"Exhausted retries for entry #{index}")

    # ── Process response body ──────────────────────────────────────────────

    content_type = resp.headers.get("Content-Type", "")
    is_zip = "zip" in content_type or resp.content[:4] == b"PK\x03\x04"

    if is_zip:
        prog.inc("zip_downloads")
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
        except zipfile.BadZipFile as exc:
            raise ValueError(f"Bad zip from CDN for entry #{index}") from exc

        names = [n for n in zf.namelist() if not n.startswith(("__MACOSX", "."))]
        if not names:
            raise ValueError(f"Empty zip for entry #{index}")

        main_name    = next((n for n in names if "-main"    in n.lower()), None) or names[0]
        overlay_name = next((n for n in names if "-overlay" in n.lower()), None)

        actual_ext = os.path.splitext(main_name)[1].lower() or ext
        filename   = f"{ts_str}_{short_id}{actual_ext}"
        out_path   = os.path.join(output_dir, filename)

        if overlay_name:
            main_tmp    = os.path.join(output_dir, f".tmp_main_{short_id}_{index}{actual_ext}")
            overlay_tmp = os.path.join(output_dir, f".tmp_overlay_{short_id}_{index}.png")
            try:
                main_tmp_path    = main_tmp
                overlay_tmp_path = overlay_tmp
                with open(main_tmp_path, "wb") as fh:
                    fh.write(zf.read(main_name))
                with open(overlay_tmp_path, "wb") as fh:
                    fh.write(zf.read(overlay_name))

                if actual_ext in VIDEO_EXTS:
                    if not _composite_video(main_tmp_path, overlay_tmp_path, out_path):
                        shutil.copy2(main_tmp_path, out_path)
                else:
                    try:
                        _composite_image(main_tmp_path, overlay_tmp_path, out_path)
                    except Exception as exc:
                        log.warning(f"  Composite failed #{index}: {exc} — using main only")
                        shutil.copy2(main_tmp_path, out_path)
            finally:
                for tmp in (main_tmp, overlay_tmp):
                    if os.path.exists(tmp):
                        os.remove(tmp)
        else:
            with open(out_path, "wb") as fh:
                fh.write(zf.read(main_name))

    else:
        prog.inc("direct_downloads")
        # Adjust extension based on actual content-type
        if "video" in content_type and not out_path.endswith(tuple(VIDEO_EXTS)):
            out_path = os.path.splitext(out_path)[0] + ".mp4"
        elif "image" in content_type and out_path.endswith(tuple(VIDEO_EXTS)):
            out_path = os.path.splitext(out_path)[0] + (
                ".png" if "png" in content_type else ".jpg"
            )
        with open(out_path, "wb") as fh:
            fh.write(resp.content)

    # Validate output exists and is non-empty
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"Output file missing or empty after download: {out_path}")

    final_ext = os.path.splitext(out_path)[1].lower()
    prog.inc("videos_downloaded" if final_ext in VIDEO_EXTS else "images_downloaded")

    return {
        "date": date_str,
        "file_path": out_path,
        "lat": entry.get("lat"),
        "lon": entry.get("lon"),
    }


# ─── Batch download orchestration ────────────────────────────────────────────


def _run_download_batch(
    session: requests.Session,
    entries: list[dict],
    output_dir: str,
    prog: _Progress,
    limiter: _TokenBucketRateLimiter,
    workers: int,
    batch_label: str = "initial",
) -> tuple[list[dict], list[dict]]:
    """Download a batch in parallel. Returns (outputs, retryable_failed_entries)."""
    total = len(entries)
    log.info(f"Batch '{batch_label}': {total} entries, {workers} workers")
    batch_start = time.time()
    outputs: list[dict] = []
    failed: list[dict]  = []
    ok = errors = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_download_one, session, entry, i, output_dir, prog, limiter): (i, entry)
            for i, entry in enumerate(entries)
        }

        for fut in as_completed(futures):
            i, entry = futures[fut]
            try:
                result = fut.result()
                if result:
                    outputs.append(result)
                    prog.inc("downloaded")
                    ok += 1
                    limiter.on_success()
            except requests.exceptions.HTTPError as exc:
                errors += 1
                prog.inc("failed_downloads")
                status  = exc.response.status_code if exc.response is not None else "?"
                reason  = f"HTTP {status}"
                permanent = status == 403
                prog.log_failure({
                    "index": i, "date": entry["date"],
                    "media_type": entry.get("media_type", "?"),
                    "reason": reason, "batch": batch_label,
                    "permanent": permanent,
                })
                if not permanent:
                    failed.append(entry)
                log.debug(f"  #{i} failed permanently: {reason}")
            except Exception as exc:
                errors += 1
                prog.inc("failed_downloads")
                reason = f"{type(exc).__name__}: {exc}"
                prog.log_failure({
                    "index": i, "date": entry["date"],
                    "media_type": entry.get("media_type", "?"),
                    "reason": reason, "batch": batch_label,
                    "permanent": False,
                })
                failed.append(entry)
                log.debug(f"  #{i} failed (retryable): {reason}")

            current = ok + errors
            if current % 250 == 0 and current > 0:
                elapsed = time.time() - batch_start
                rate    = ok / elapsed if elapsed > 0 else 0
                log.info(
                    f"  [{batch_label}] {ok} ok, {errors} err in "
                    f"{_elapsed_str(batch_start)} ({rate:.1f}/s)"
                )

            _print_progress(prog)

    elapsed  = time.time() - batch_start
    rate     = ok / elapsed if elapsed > 0 else 0
    log.info(
        f"Batch '{batch_label}' done: {ok} ok, {errors} failed in "
        f"{_elapsed_str(batch_start)} ({rate:.1f}/s)"
    )
    return outputs, failed


def download_from_cdn(
    entries: list[dict],
    output_dir: str,
    prog: _Progress,
    workers: int = DEFAULT_WORKERS,
) -> list[dict]:
    """Download all entries from CDN with global rate limiting and auto-retry."""
    total = len(entries)
    log.info(f"Starting CDN download: {total} entries, {workers} workers")

    images_total = sum(1 for e in entries if e["media_type"] == "image")
    videos_total = sum(1 for e in entries if e["media_type"] == "video")
    gps_total    = sum(1 for e in entries if e.get("lat") is not None)
    log.info(f"  {images_total} images, {videos_total} videos, {gps_total} with GPS")

    if total > 5000:
        log.warning(f"Large export ({total} files) — rate limiting may occur")

    _check_url_expiry(entries)

    prog.set("total", total)
    prog.set("phase", "download")
    prog.set("start_time", time.time())

    limiter = _TokenBucketRateLimiter(rps=float(workers))
    session = _make_session(workers)
    all_outputs, failed = _run_download_batch(
        session, entries, output_dir, prog, limiter, workers, batch_label="initial"
    )

    log.info(
        f"Initial pass: {len(all_outputs)} ok "
        f"({prog.skipped_downloads} skipped), {len(failed)} to retry"
    )

    for attempt in range(2, 5):
        if not failed:
            break
        wait = 15 * attempt
        log.info(f"{len(failed)} retryable failures. Waiting {wait}s before attempt {attempt}/4…")
        time.sleep(wait)
        new_outputs, failed = _run_download_batch(
            session, failed, output_dir, prog, limiter, workers,
            batch_label=f"retry-{attempt}",
        )
        all_outputs.extend(new_outputs)
        if new_outputs:
            log.info(f"  Recovered {len(new_outputs)} files on retry {attempt}")

    prog.set("download_end_time", time.time())

    if failed:
        failed_log = os.path.join(output_dir, "failed_downloads.txt")
        with open(failed_log, "w") as fh:
            for e in failed:
                fh.write(f"{e['date']} | {e['media_type']} | {e['cdn_url']}\n")
        log.warning(f"{len(failed)} files permanently failed — logged to {failed_log}")

    snap = prog.snapshot()
    log.info(f"Download phase complete in {_elapsed_str(snap['start_time'])}")
    log.info(
        f"  {snap['downloaded']} downloaded, {snap['failed_downloads']} failed, "
        f"{snap['skipped_downloads']} skipped"
    )
    log.info(f"  {snap['images_downloaded']} images, {snap['videos_downloaded']} videos")
    log.info(f"  {snap['zip_downloads']} zip bundles, {snap['direct_downloads']} direct")
    if snap["rate_limited"]:
        log.info(f"  429 hits: {snap['rate_limited']}")

    return all_outputs


# ─── Local file processing ────────────────────────────────────────────────────


def process_entries(entries: list[dict], output_dir: str) -> list[dict]:
    """Copy/composite local-file entries into output_dir."""
    total = len(entries)
    log.info(f"Processing {total} local memories…")
    outputs: list[dict] = []
    composited = copied = skipped = 0

    for i, entry in enumerate(entries, 1):
        date       = entry["date"]
        main_file  = entry.get("main_file")
        ovl_file   = entry.get("overlay_file")
        main_ok    = main_file  and os.path.exists(main_file)
        ovl_ok     = ovl_file   and os.path.exists(ovl_file)

        if main_ok and ovl_ok:
            base_ext  = os.path.splitext(main_file)[1].lower()
            is_video  = base_ext in VIDEO_EXTS
            base_name = os.path.splitext(os.path.basename(main_file))[0].replace("-main", "")

            if is_video:
                out_path = os.path.join(output_dir, f"{base_name}{base_ext}")
                if _composite_video(main_file, ovl_file, out_path):
                    composited += 1
                else:
                    out_path = os.path.join(output_dir, os.path.basename(main_file))
                    shutil.copy2(main_file, out_path)
                    copied += 1
            else:
                out_ext  = ".jpg" if base_ext in (".jpg", ".jpeg") else ".png"
                out_path = os.path.join(output_dir, f"{base_name}{out_ext}")
                try:
                    _composite_image(main_file, ovl_file, out_path)
                    composited += 1
                except Exception as exc:
                    log.warning(f"  Composite failed {base_name}: {exc}")
                    out_path = os.path.join(output_dir, os.path.basename(main_file))
                    shutil.copy2(main_file, out_path)
                    copied += 1

            outputs.append({"date": date, "file_path": out_path})

        elif main_ok:
            out_path = os.path.join(output_dir, os.path.basename(main_file))
            shutil.copy2(main_file, out_path)
            copied += 1
            outputs.append({"date": date, "file_path": out_path})

        elif ovl_ok:
            out_path = os.path.join(output_dir, os.path.basename(ovl_file))
            shutil.copy2(ovl_file, out_path)
            copied += 1
            outputs.append({"date": date, "file_path": out_path})

        else:
            skipped += 1
            log.warning(f"  Skipped entry (files not found): date={date}")

        if i % 50 == 0 or i == total:
            log.info(f"  [{i}/{total}] {composited} composited, {copied} copied")

    log.info(f"Done: {composited} composited, {copied} copied, {skipped} skipped")
    return outputs


# ─── Metadata ─────────────────────────────────────────────────────────────────


def _parse_exif_dt(date_str: str) -> tuple[str, datetime]:
    """
    Parse a Snapchat date string into (exif_timestamp_str, datetime_obj).
    Raises ValueError on failure.
    """
    clean = date_str.replace(" UTC", "").strip()
    if len(clean) > 10:
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
    else:
        dt = datetime.strptime(clean, "%Y-%m-%d").replace(hour=12)
    return dt.strftime("%Y:%m:%d %H:%M:%S"), dt


def _has_correct_metadata(
    et: ExifToolHelper,
    fpath: str,
    expected_exif_dt: str,
    lat: Optional[float],
    lon: Optional[float],
    is_video: bool,
) -> bool:
    """Return True if the file already carries the expected date (and GPS if provided)."""
    try:
        metadata = et.get_metadata(fpath)
        md = (metadata[0] if isinstance(metadata, list) else metadata) or {}

        date_tag = (
            md.get("QuickTime:CreateDate", "") if is_video
            else md.get("EXIF:DateTimeOriginal", md.get("EXIF:CreateDate", ""))
        )
        if expected_exif_dt not in str(date_tag):
            return False

        if lat is not None and lon is not None:
            if is_video:
                if not md.get("Keys:GPSCoordinates"):
                    return False
            else:
                if not md.get("GPSLatitude"):
                    return False

        return True
    except Exception:
        return False


def update_metadata(outputs: list[dict], prog: _Progress) -> None:
    """Write capture date + GPS into EXIF/QuickTime tags and OS file times."""
    total = len(outputs)
    if total == 0:
        log.info("No files to update metadata for.")
        return

    prog.set("total", total)
    prog.set("phase", "metadata")
    prog.set("metadata_ok", 0)
    prog.set("metadata_errors", 0)

    meta_start = time.time()
    log.info(f"Writing metadata on {total} files…")
    gps_cnt = sum(1 for e in outputs if e.get("lat") is not None)
    log.info(f"  {gps_cnt} files have GPS")

    updated = skipped = errors = missing = 0

    with ExifToolHelper() as et:
        for i, entry in enumerate(outputs):
            fpath    = entry.get("file_path", "")
            date_str = entry.get("date", "")
            lat      = entry.get("lat")
            lon      = entry.get("lon")

            if not fpath or not os.path.exists(fpath):
                missing += 1
                log.debug(f"  Missing file: {fpath}")
                continue

            try:
                exif_dt, _ = _parse_exif_dt(date_str)
            except ValueError as exc:
                errors += 1
                prog.inc("metadata_errors")
                log.warning(f"  Bad date '{date_str}' for {os.path.basename(fpath)}: {exc}")
                _print_progress(prog)
                continue

            file_ext = os.path.splitext(fpath)[1].lower()
            is_video = file_ext in VIDEO_EXTS

            if _has_correct_metadata(et, fpath, exif_dt, lat, lon, is_video):
                skipped += 1
                prog.inc("metadata_ok")
                _print_progress(prog)
                continue

            tags = ["-overwrite_original"]

            if is_video:
                tags.extend([
                    f"-QuickTime:CreateDate={exif_dt}",
                    f"-QuickTime:ModifyDate={exif_dt}",
                    f"-QuickTime:TrackCreateDate={exif_dt}",
                    f"-QuickTime:TrackModifyDate={exif_dt}",
                    f"-QuickTime:MediaCreateDate={exif_dt}",
                    f"-QuickTime:MediaModifyDate={exif_dt}",
                ])
            else:
                tags.extend([
                    f"-EXIF:DateTimeOriginal={exif_dt}",
                    f"-EXIF:CreateDate={exif_dt}",
                    f"-EXIF:ModifyDate={exif_dt}",
                    f"-XMP:DateTimeOriginal={exif_dt}",
                    f"-XMP:CreateDate={exif_dt}",
                ])

            if lat is not None and lon is not None:
                lat_ref = "N" if lat >= 0 else "S"
                lon_ref = "E" if lon >= 0 else "W"
                if is_video:
                    tags.append(f"-Keys:GPSCoordinates={lat} {lon}")
                else:
                    tags.extend([
                        f"-GPSLatitude={abs(lat)}",
                        f"-GPSLatitudeRef={lat_ref}",
                        f"-GPSLongitude={abs(lon)}",
                        f"-GPSLongitudeRef={lon_ref}",
                    ])

            try:
                et.execute(*tags, fpath)
                et.execute(
                    f"-FileCreateDate={exif_dt}",
                    f"-FileModifyDate={exif_dt}",
                    fpath,
                )
                updated += 1
                prog.inc("metadata_ok")
            except Exception as exc:
                errors += 1
                prog.inc("metadata_errors")
                log.warning(f"  Metadata error on {os.path.basename(fpath)}: {exc}")

            _print_progress(prog)

    _clear_bar()
    prog.set("metadata_updated", updated)
    prog.set("metadata_skipped", skipped)

    log.info(f"Metadata done in {_elapsed_str(meta_start)}")
    log.info(f"  Updated: {updated} | Already correct: {skipped} | Missing: {missing} | Errors: {errors}")


# ─── File / zip discovery ─────────────────────────────────────────────────────


def _safe_extract_zip(zip_path: str, dest_dir: str) -> None:
    """Extract a zip file, guarding against path traversal."""
    real_dest = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            member_path = os.path.realpath(os.path.join(dest_dir, member))
            if not member_path.startswith(real_dest + os.sep) and member_path != real_dest:
                log.warning(f"  Skipping suspicious zip entry: {member}")
                continue
            zf.extract(member, dest_dir)


def _extract_zips(input_path: str) -> list[str]:
    if not os.path.isdir(input_path):
        return []
    extracted = []
    for name in sorted(os.listdir(input_path)):
        if not name.lower().endswith(".zip"):
            continue
        zip_path = os.path.join(input_path, name)
        dest     = os.path.join(input_path, os.path.splitext(name)[0])
        if os.path.isdir(dest):
            log.info(f"  Already extracted: {name}")
            extracted.append(dest)
            continue
        log.info(f"  Extracting: {name}")
        try:
            _safe_extract_zip(zip_path, dest)
            extracted.append(dest)
        except zipfile.BadZipFile:
            log.warning(f"  Bad zip: {name}")
    return extracted


def find_html_files(input_path: str) -> dict:
    """Find memories HTML files. Returns {'history': [...], 'local': [...]}."""
    result: dict[str, list] = {"history": [], "local": []}
    seen: set[str] = set()

    def _scan(dirpath: str) -> None:
        for name in sorted(os.listdir(dirpath)):
            full = os.path.join(dirpath, name)
            real = os.path.realpath(full)
            if real in seen:
                continue
            seen.add(real)
            if os.path.isfile(full) and name.endswith(".html"):
                if "memories_history" in name:
                    result["history"].append(full)
                elif name == "memories.html":
                    result["local"].append(full)

    if os.path.isfile(input_path):
        name = os.path.basename(input_path)
        key  = "history" if "memories_history" in name else "local"
        result[key].append(input_path)
        return result

    if os.path.isdir(input_path):
        _scan(input_path)
        zips = [f for f in os.listdir(input_path) if f.lower().endswith(".zip")]
        if zips:
            log.info(f"Found {len(zips)} zip(s), extracting…")
            _extract_zips(input_path)
        for name in sorted(os.listdir(input_path)):
            sub = os.path.join(input_path, name)
            if os.path.isdir(sub):
                _scan(sub)

    return result


# ─── Report ───────────────────────────────────────────────────────────────────


def _generate_report(
    output_dir: str,
    input_path: str,
    all_outputs: list[dict],
    skip_metadata: bool,
    prog: _Progress,
) -> str:
    prog.set("end_time", time.time())
    snap  = prog.snapshot()
    start = snap.get("start_time") or snap["end_time"]
    dl_end = snap.get("download_end_time") or snap["end_time"]

    report = {
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "input_path":    os.path.abspath(input_path),
        "output_dir":    os.path.abspath(output_dir),
        "timing": {
            "total_seconds":    round(snap["end_time"] - start, 1),
            "total_human":      _elapsed_str(start),
            "download_seconds": round(dl_end - start, 1),
            "download_human":   _elapsed_str(start),
        },
        "downloads": {
            "total_entries":   snap["downloaded"] + snap["failed_downloads"] + snap["skipped_downloads"],
            "successful":      snap["downloaded"],
            "failed":          snap["failed_downloads"],
            "skipped_existing": snap["skipped_downloads"],
            "images":          snap["images_downloaded"],
            "videos":          snap["videos_downloaded"],
            "zip_bundles":     snap["zip_downloads"],
            "direct_files":    snap["direct_downloads"],
        },
        "rate_limiting": {
            "total_429_hits": snap["rate_limited"],
            "events":         snap["rate_limit_events"],
        },
        "metadata": {
            "skipped_by_user": skip_metadata,
            "updated":         snap["metadata_updated"],
            "already_correct": snap["metadata_skipped"],
            "errors":          snap["metadata_errors"],
        },
        "failed_entries": snap["failed_entries"],
        "output_files":   len(all_outputs),
    }

    report_path = os.path.join(output_dir, "extraction_report.json")
    with open(report_path, "w") as fh:
        json.dump(report, fh, indent=2)
    log.info(f"Report: {report_path}")
    return report_path


# ─── CLI / interactive ────────────────────────────────────────────────────────


def _interactive_prompt() -> argparse.Namespace:
    print()
    print("=" * 60)
    print("  SnapExtract — Snapchat Memories Extractor")
    print("=" * 60)
    print()

    input_path = input("  Path to Snapchat export folder or HTML file:\n  > ").strip().strip("'\"")
    if not input_path:
        print("No input path. Exiting.")
        sys.exit(1)

    output_path = input("\n  Output folder (Enter for ./output):\n  > ").strip().strip("'\"")
    if not output_path:
        output_path = os.path.join(os.path.dirname(input_path), "output")
        print(f"  Using: {output_path}")

    workers_str = input(f"\n  Parallel workers (Enter for {DEFAULT_WORKERS}):\n  > ").strip()
    try:
        workers = int(workers_str) if workers_str else DEFAULT_WORKERS
        workers = max(MIN_WORKERS, min(workers, MAX_WORKERS_LIMIT))
    except ValueError:
        workers = DEFAULT_WORKERS

    skip_meta = input("\n  Skip metadata writing? (y/N): ").strip().lower()
    return argparse.Namespace(
        input=input_path,
        download_dir=output_path,
        skip_metadata=skip_meta in ("y", "yes"),
        workers=workers,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Snapchat memories, composite overlays, and fix metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s -i ./memories_folder -d ./output
  %(prog)s -i ./all_exports -d ./output --workers 10
  %(prog)s -i ./memories_history.html -d ./output --skip-metadata
        """,
    )
    parser.add_argument("-i", "--input",        required=False, help="Export folder, HTML file, or zips folder")
    parser.add_argument("-d", "--download-dir", required=False, help="Output directory for processed media")
    parser.add_argument("--skip-metadata", action="store_true",  help="Skip metadata update")
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Parallel download workers (default: {DEFAULT_WORKERS}, max: {MAX_WORKERS_LIMIT})",
    )
    return parser


# ─── Entry point ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = _build_arg_parser()
    args   = parser.parse_args()

    if not args.input or not args.download_dir:
        args = _interactive_prompt()

    args.workers = max(MIN_WORKERS, min(args.workers, MAX_WORKERS_LIMIT))

    html_files    = find_html_files(args.input)
    history_files = html_files["history"]
    local_files   = html_files["local"]

    if not history_files and not local_files:
        log.error(f"No memories HTML files found in: {args.input}")
        sys.exit(1)

    log.info(f"Found {len(history_files)} history file(s), {len(local_files)} local file(s)")
    os.makedirs(args.download_dir, exist_ok=True)

    prog = _Progress()
    prog.set("start_time", time.time())

    all_outputs: list[dict] = []

    if history_files:
        for html_path in history_files:
            log.info(f"── {html_path}")
            entries = parse_history_html(html_path)
            outputs = download_from_cdn(entries, args.download_dir, prog, workers=args.workers)
            all_outputs.extend(outputs)
    else:
        for html_path in local_files:
            log.info(f"── {html_path}")
            entries = parse_html(html_path)
            outputs = process_entries(entries, args.download_dir)
            all_outputs.extend(outputs)
        prog.set("download_end_time", time.time())

    _clear_bar()

    if not args.skip_metadata:
        update_metadata(all_outputs, prog)
    else:
        log.info("Skipping metadata (--skip-metadata)")

    report_path = _generate_report(
        args.download_dir, args.input, all_outputs, args.skip_metadata, prog
    )

    snap = prog.snapshot()
    _clear_bar()
    log.info("═" * 60)
    log.info("EXTRACTION COMPLETE")
    log.info("═" * 60)
    log.info(f"  Downloads:  {snap['downloaded']} ok, {snap['failed_downloads']} failed, {snap['skipped_downloads']} skipped")
    log.info(f"  Media:      {snap['images_downloaded']} images, {snap['videos_downloaded']} videos")
    log.info(f"  Metadata:   {snap['metadata_ok']} ok, {snap['metadata_errors']} errors")
    if snap["rate_limited"]:
        log.info(f"  429 hits:   {snap['rate_limited']}")
    log.info(f"  Duration:   {_elapsed_str(snap['start_time'])}")
    log.info(f"  Output:     {args.download_dir}")
    log.info(f"  Report:     {report_path}")
    log.info("═" * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _clear_bar()
        log.warning("Interrupted by user.")
        sys.exit(130)
    except Exception as exc:
        log.critical(f"Fatal: {exc}", exc_info=True)
        sys.exit(1)
    finally:
        if not sys.argv[1:]:
            input("\nPress Enter to close…")