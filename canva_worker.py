#!/usr/bin/env python3
"""Drain the bot's Canva queue: capture each design, file the pages into
Synology Photos.

Runs on the Mac mini, NOT on the NAS. A capture peaks near 1 GB of Chromium and
the Synology has ~1.7 GB free, so bot.py only *queues* the links it finds in
relayed PowerSchool messages; this worker does the heavy rendering and writes
the finished pages back to the photo share.

    python3 canva_worker.py                 # drain the queue once
    python3 canva_worker.py --dry-run       # show what would be captured
    python3 canva_worker.py --once URL      # capture one link, ignore the queue

Everything reaches the NAS over SSH: this NAS rejects scp and rsync, so files
are piped through `ssh 'cat > path'` one at a time (see CLAUDE.md).
"""
import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import canva_fetch

BANGKOK_TZ = timezone(timedelta(hours=7))

# Env-overridable so a real capture can be pointed somewhere harmless when
# testing, without editing the file the scheduler runs.
NAS_HOST  = os.getenv("CANVA_NAS_HOST", "sixpsy@100.79.219.110")  # Tailscale; LAN IP times out
SSH_KEY   = os.getenv("CANVA_SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519_nas"))
QUEUE_REMOTE = os.getenv("CANVA_QUEUE_REMOTE", "/volume1/docker/BCISB-bot/canva_queue.json")
PHOTO_ROOT   = os.getenv("CANVA_PHOTO_ROOT", "/volume1/photo/BCISB Newsletters")
SSH_OPTS  = ["-o", "ConnectTimeout=20", "-o", "BatchMode=yes"]


# --------------------------------------------------------------------------
#  NAS access
# --------------------------------------------------------------------------
def _ssh(args, stdin=None, capture=True):
    cmd = ["ssh", *SSH_OPTS, "-i", SSH_KEY, NAS_HOST, *args]
    return subprocess.run(cmd, input=stdin,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE, check=True)


def nas_read(path: str) -> bytes:
    """Read a remote file; empty bytes if it does not exist."""
    try:
        return _ssh([f"cat {shlex.quote(path)} 2>/dev/null || true"]).stdout
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"cannot read {path}: {e.stderr.decode()[:200]}") from e


def nas_write(path: str, data: bytes) -> None:
    _ssh([f"cat > {shlex.quote(path)}"], stdin=data, capture=False)


def nas_mkdir(path: str) -> None:
    _ssh([f"mkdir -p {shlex.quote(path)}"])


def load_queue() -> list:
    raw = nas_read(QUEUE_REMOTE).strip()
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"queue file is not valid JSON: {e}") from e


def drop_from_queue(urls: set) -> int:
    """Remove finished entries, re-reading first so a link the bot queued
    while we were rendering is not lost."""
    if not urls:
        return 0
    queue = load_queue()
    kept  = [e for e in queue if e.get("url") not in urls]
    nas_write(QUEUE_REMOTE, json.dumps(kept, ensure_ascii=False, indent=2).encode())
    return len(queue) - len(kept)


# --------------------------------------------------------------------------
#  Naming
# --------------------------------------------------------------------------
def safe_folder(name: str) -> str:
    name = re.sub(r"^Copy of\s+", "", (name or "").strip(), flags=re.I)
    name = re.sub(r'[/\\:*?"<>|]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "Canva design")[:80]


def folder_name(title: str, posted_at: str) -> str:
    # posted_at is best-effort upstream and can be a bare portal label
    # ("5:00 PM"), so require a real date before it reaches a folder name.
    day = (posted_at or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        day = datetime.now(BANGKOK_TZ).date().strftime("%Y-%m-%d")
    return f"{day} {safe_folder(title)}"


# --------------------------------------------------------------------------
#  Dating
# --------------------------------------------------------------------------
def stamp_date(path: Path, day: str, clock: str = "09:00:00") -> None:
    """Write the newsletter's own date into the JPEG's EXIF.

    A Playwright screenshot carries no EXIF, so Synology Photos would file the
    page under its upload date and it would land at the wrong point in the
    timeline.

    piexif, not Pillow: re-saving through Pillow — even with quality="keep" —
    re-encodes, and the pixels came back measurably different. piexif rewrites
    only the APP1 header segment, leaving the compressed image data untouched
    (verified byte-identical, +1 KB). Pillow's `getexif().get_ifd()` also
    silently fails to persist DateTimeOriginal, which is the tag Synology
    actually reads."""
    import piexif
    ts = f"{day.replace('-', ':')} {clock}"
    piexif.insert(piexif.dump({
        "0th":  {piexif.ImageIFD.DateTime: ts},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: ts,
                 piexif.ExifIFD.DateTimeDigitized: ts},
    }), str(path))


def nas_touch(path: str, day: str, clock: str = "0900") -> None:
    """Match the file's mtime to the EXIF date — Synology Photos falls back to
    mtime for anything it cannot read EXIF from."""
    stamp = f"{day.replace('-', '')}{clock}"
    _ssh([f"touch -t {stamp} {shlex.quote(path)}"])


# --------------------------------------------------------------------------
#  Work
# --------------------------------------------------------------------------
async def capture_and_file(url: str, posted_at: str, preset: str) -> str:
    tmp = Path(tempfile.mkdtemp(prefix="canva_"))
    try:
        title, shots = await canva_fetch.capture(url, tmp, preset=preset)
        if not shots:
            raise RuntimeError("captured no pages")
        folder = folder_name(title, posted_at)
        day    = folder.split(" ", 1)[0]
        dest   = f"{PHOTO_ROOT}/{folder}"
        nas_mkdir(dest)
        for sh in shots:
            stamp_date(sh, day)
            remote = f"{dest}/{sh.name}"
            nas_write(remote, sh.read_bytes())
            nas_touch(remote, day)
        return f"{dest}  ({len(shots)} page(s), dated {day})"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", metavar="URL", help="capture one URL, ignore the queue")
    ap.add_argument("--posted-at", default="", help="date for --once (YYYY-MM-DD…)")
    ap.add_argument("--preset", choices=sorted(canva_fetch.PRESETS), default="max")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if a.once:
        print(f"[canva] {a.once}")
        print("  ->", await capture_and_file(a.once, a.posted_at, a.preset))
        return 0

    queue = load_queue()
    if not queue:
        print("[canva] queue empty")
        return 0
    print(f"[canva] {len(queue)} pending")

    if a.dry_run:
        for e in queue:
            # Only the date half is known ahead of the render; the title comes
            # from the design itself, so show it as a placeholder unsanitised.
            day = folder_name("", e.get("posted_at", "")).split(" ", 1)[0]
            print(f"  would capture {e.get('url')} -> {day} <title from page>")
        return 0

    done, failed = set(), 0
    for e in queue:
        url = e.get("url")
        if not url:
            continue
        print(f"[canva] {url}")
        try:
            print("  ->", await capture_and_file(url, e.get("posted_at", ""), a.preset))
            done.add(url)
        except Exception as exc:
            # Left in the queue so the next run retries it.
            failed += 1
            print(f"  !! failed: {type(exc).__name__}: {exc}")

    removed = drop_from_queue(done)
    print(f"[canva] captured {len(done)}, failed {failed}, removed {removed} from queue")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
