#!/usr/bin/env python3
"""
libby_dl.py — Download a borrowed Libby audiobook.

How it works:
  1. Opens Libby in a real browser window (you stay logged in via a saved profile)
  2. You click Listen on any book you have borrowed
  3. The script captures the per-chapter auth tokens Libby loads into the page
  4. Downloads each chapter and merges everything into a single M4B with chapters

Requirements:
  pip install playwright
  playwright install chromium
  ffmpeg must be in PATH

Usage:
  python libby_dl.py                        # interactive — point browser at your book
  python libby_dl.py --out ~/Audiobooks     # custom output folder
  python libby_dl.py --mp3                  # keep individual MP3s instead of M4B
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("playwright not installed. Run:  pip install playwright && playwright install chromium")

# ── JS injected into the listen page before any of Libby's scripts run ───────
# Hooks JSON.parse to intercept the per-chapter signed auth tokens
# (odreadCmptParams) that Libby fetches from its API on book load.
_HOOK_JS = """
if (!window.__ld_hooked) {
    window.__ld_hooked = true;
    window.__ld_params = null;

    const _orig = JSON.parse;
    JSON.parse = function(...args) {
        const ret = _orig(...args);
        try {
            if (ret && typeof ret === 'object' &&
                ret['b'] && ret['b']['-odread-cmpt-params']) {
                window.__ld_params = Array.from(ret['b']['-odread-cmpt-params']);
            }
        } catch (_) {}
        return ret;
    };
}
"""

# Extracts book metadata + chapter list once params are available
_EXTRACT_JS = """
() => {
    const bif = window.BIF;
    if (!bif || !window.__ld_params) return null;

    const spool = bif?.objects?.spool;
    if (!spool) return null;

    const rotr  = bif?.objects?.rotr  || {};
    const cover = bif?.objects?.cover || {};

    const components = spool.components || [];
    const chapters = [];
    for (const spine of components) {
        const pos   = spine.spinePosition ?? chapters.length;
        const param = window.__ld_params[pos];
        if (!spine.meta?.path || !param) continue;
        chapters.push({
            index:  pos,
            title:  spine.title || `Chapter ${pos + 1}`,
            path:   spine.meta.path,
            param:  param,
        });
    }

    return {
        title:   rotr.title   || 'Unknown Title',
        author:  (rotr.creators || []).map(c => c.name).join(', ') || 'Unknown Author',
        cover:   cover.href   || null,
        origin:  location.origin,
        chapters: chapters,
    };
}
"""


def _safe_name(text: str, max_len: int = 80) -> str:
    """Convert arbitrary text to a safe filename."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r'[^\w\s\-.]', '', text).strip()
    text = re.sub(r'\s+', ' ', text)
    return text[:max_len]


def _wait_for_params(page, timeout_s: int = 120) -> bool:
    """Poll until odreadCmptParams is captured or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ready = page.evaluate("() => !!window.__ld_params")
        if ready:
            return True
        time.sleep(1)
    return False


def _download_chapter(session, url: str, dest: Path) -> bool:
    """Download a single audio chunk, return True on success."""
    import urllib.request
    import urllib.error

    # Reuse cookies from Playwright's session storage if provided
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for name, value in session.items():
        req.add_header("Cookie", f"{name}={value}")

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
        return True
    except urllib.error.URLError as e:
        print(f"  ✗ Download failed: {e}", file=sys.stderr)
        return False


def _build_m4b(chapters: list[dict], tmp_dir: Path, mp3_files: list[Path],
               cover_path: Path | None, out_path: Path) -> None:
    """Merge MP3 chapters into a single M4B with chapter markers."""

    # Build ffmpeg concat list
    concat_file = tmp_dir / "concat.txt"
    concat_file.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in mp3_files)
    )

    # Build ffmpeg metadata with chapter timestamps
    meta_file = tmp_dir / "meta.txt"
    lines = [";FFMETADATA1\n"]

    # We need duration of each MP3 to calculate chapter start times
    cursor_ms = 0
    chapter_timestamps = []
    for mp3 in mp3_files:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(mp3)],
            capture_output=True, text=True,
        )
        try:
            dur_s = float(json.loads(result.stdout)["format"]["duration"])
        except Exception:
            dur_s = 0.0
        chapter_timestamps.append(cursor_ms)
        cursor_ms += int(dur_s * 1000)

    for i, (ch, start_ms) in enumerate(zip(chapters, chapter_timestamps)):
        end_ms = chapter_timestamps[i + 1] if i + 1 < len(chapter_timestamps) else cursor_ms
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        lines.append(f"title={ch['title']}\n")

    meta_file.write_text("\n".join(lines))

    # Merge
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-i", str(meta_file),
        "-map_metadata", "1",
    ]
    if cover_path and cover_path.exists():
        cmd += ["-i", str(cover_path), "-map", "0:a", "-map", "2:v",
                "-disposition:v", "attached_pic"]
    else:
        cmd += ["-map", "0:a"]

    cmd += ["-c:a", "copy", "-c:v", "copy", str(out_path)]

    print(f"\nBuilding M4B → {out_path.name}")
    subprocess.run(cmd, check=True, capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download a borrowed Libby audiobook")
    parser.add_argument("--out", type=Path, default=Path.home() / "Downloads",
                        help="Output folder (default: ~/Downloads)")
    parser.add_argument("--mp3", action="store_true",
                        help="Keep individual MP3 files instead of merging to M4B")
    parser.add_argument("--profile", type=Path,
                        default=Path.home() / ".libby_dl_profile",
                        help="Browser profile directory (saves your Libby login)")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    args.profile.mkdir(parents=True, exist_ok=True)

    print("libby_dl — Libby audiobook downloader")
    print("=" * 45)
    print(f"  Browser profile: {args.profile}")
    print(f"  Output folder:   {args.out}")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=str(args.profile),
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )

        # Inject the hook on every new page in the listen domain
        browser.add_init_script(_HOOK_JS)

        # Open Libby if no tab is already there
        page = browser.pages[0] if browser.pages else browser.new_page()

        if "libbyapp.com" not in page.url and "overdrive.com" not in page.url:
            page.goto("https://libbyapp.com")

        print("→ Navigate to a borrowed audiobook in the browser and click LISTEN.")
        print("  The script will wait until the book player loads.\n")

        # Wait for the user to land on the listen subdomain
        listen_page = None
        deadline = time.time() + 300  # 5-minute window
        while time.time() < deadline:
            for p in browser.pages:
                if "listen.libbyapp.com" in p.url or "listen.overdrive.com" in p.url:
                    listen_page = p
                    break
            if listen_page:
                break
            time.sleep(1)

        if not listen_page:
            sys.exit("Timed out waiting for a listen page. Did you click Listen on a borrowed book?")

        # Make sure the hook is present (the page may have loaded before our context hook)
        listen_page.evaluate(_HOOK_JS)

        print(f"  Found listen page: {listen_page.url}")
        print("  Waiting for chapter tokens to load...")

        if not _wait_for_params(listen_page, timeout_s=60):
            sys.exit("Timed out waiting for auth tokens. Try refreshing the book player.")

        book = listen_page.evaluate(_EXTRACT_JS)
        if not book or not book.get("chapters"):
            sys.exit("Could not extract chapter data from page. Is the book fully loaded?")

        title  = book["title"]
        author = book["author"]
        origin = book["origin"]
        chapters = book["chapters"]

        print(f"\n  Book:     {title}")
        print(f"  Author:   {author}")
        print(f"  Chapters: {len(chapters)}")

        # Get session cookies for the listen domain
        cookies = {c["name"]: c["value"] for c in listen_page.context.cookies()
                   if "libbyapp.com" in c.get("domain", "")
                   or "overdrive.com" in c.get("domain", "")}

        safe_title = _safe_name(f"{author} - {title}")
        out_dir = args.out / safe_title
        out_dir.mkdir(parents=True, exist_ok=True)

        print(f"\nDownloading to: {out_dir}\n")

        mp3_files: list[Path] = []
        failed = 0

        for ch in chapters:
            url = f"{origin}/{ch['path']}?{ch['param']}"
            dest = out_dir / f"{ch['index']:03d} - {_safe_name(ch['title'])}.mp3"

            if dest.exists():
                print(f"  [skip] {dest.name} (already downloaded)")
                mp3_files.append(dest)
                continue

            print(f"  [{ch['index'] + 1}/{len(chapters)}] {ch['title']}")

            if _download_chapter(cookies, url, dest):
                mp3_files.append(dest)
            else:
                failed += 1

        browser.close()

        if not mp3_files:
            sys.exit("No chapters downloaded.")

        if failed:
            print(f"\n  Warning: {failed} chapter(s) failed to download.")

        if args.mp3:
            print(f"\nDone. MP3 files saved to: {out_dir}")
            return

        # Download cover art
        cover_path = None
        if book.get("cover"):
            cover_path = out_dir / "cover.jpg"
            try:
                import urllib.request
                urllib.request.urlretrieve(book["cover"], cover_path)
            except Exception:
                cover_path = None

        # Merge to M4B
        m4b_path = args.out / f"{safe_title}.m4b"
        try:
            _build_m4b(chapters, out_dir, mp3_files, cover_path, m4b_path)
            print(f"\nDone. → {m4b_path}")
        except subprocess.CalledProcessError as e:
            print(f"\nffmpeg merge failed: {e.stderr.decode()}", file=sys.stderr)
            print(f"MP3 files are still in: {out_dir}")


if __name__ == "__main__":
    main()
