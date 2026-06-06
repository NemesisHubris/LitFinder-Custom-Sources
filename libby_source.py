"""
Libby / OverDrive audiobook source for LitFinder.

HOW IT WORKS
------------
Libby delivers signed per-chapter audio URLs by running its own JavaScript
in the browser — there is no public API that returns them. The approach that
actually works (confirmed by LibbyRip's author) is to hook JSON.parse inside
the Libby listen page and intercept the odreadCmptParams array before Libby
removes it from the page.

  search()  → Libby Patron API (sentry.libbyapp.com) to list your loans
  download() → Playwright headless browser + JSON.parse hook to capture
               signed chapter URLs, then sequential download + M4B merge

SETUP
-----
Playwright must be installed in the LitFinder Python environment:
  pip install playwright && playwright install chromium

First-time login: run the bundled libby_dl.py script once to open a real
browser and log in to Libby. It saves a browser profile that this plugin
then reuses headlessly.

  python libby_dl.py --profile /config/plugins/libby_profile

After that, set the profile path in settings and the plugin handles
everything automatically.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import requests

from shelfmark.release_sources import (
    ColumnAlign,
    ColumnRenderType,
    ColumnSchema,
    DownloadHandler,
    Release,
    ReleaseColumnConfig,
    ReleaseProtocol,
    ReleaseSource,
    SourceUnavailableError,
    register_handler,
    register_source,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from threading import Event

    from shelfmark.core.models import DownloadTask
    from shelfmark.core.search_plan import ReleaseSearchPlan
    from shelfmark.metadata_providers import BookMetadata

SOURCE_NAME = "libby"

# sentry-read.svc.overdrive.com was replaced by sentry.libbyapp.com in late 2024
_API = "https://sentry.libbyapp.com"
_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) (Dewey; V22; iOS; 6.3.0-160)"
)
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = _UA
_SESSION.headers["Accept"] = "application/json"

# Injected before any Libby JS runs — hooks JSON.parse to capture odreadCmptParams
_HOOK_JS = """
if (!window.__ld_hooked) {
    window.__ld_hooked = true;
    window.__ld_params = null;
    window.__ld_origin = null;

    const _orig = JSON.parse;
    JSON.parse = function(...args) {
        const ret = _orig(...args);
        try {
            if (ret && typeof ret === 'object' &&
                ret['b'] && ret['b']['-odread-cmpt-params']) {
                window.__ld_params = Array.from(ret['b']['-odread-cmpt-params']);
                window.__ld_origin = location.origin;
            }
        } catch (_) {}
        return ret;
    };
}
"""

# Extracts spine + metadata once __ld_params is populated
_EXTRACT_JS = """
() => {
    if (!window.__ld_params || !window.BIF) return null;
    const spool = window.BIF?.objects?.spool;
    if (!spool) return null;
    const rotr  = window.BIF?.objects?.rotr  || {};
    const cover = window.BIF?.objects?.cover || {};
    const components = (spool.components || []).filter(c => c.meta?.path);
    const chapters = components.map((c, i) => ({
        index:  c.spinePosition ?? i,
        title:  c.title || ('Chapter ' + (i + 1)),
        path:   c.meta.path,
        param:  window.__ld_params[c.spinePosition ?? i] || '',
    })).filter(c => c.param);
    return {
        title:    rotr.title   || 'Unknown Title',
        author:   (rotr.creators || []).map(c => c.name).join(', ') || 'Unknown Author',
        cover:    cover.href   || null,
        origin:   window.__ld_origin || location.origin,
        chapters: chapters,
    };
}
"""


# ── Chip token persistence ─────────────────────────────────────────────────────

def _chip_file() -> Path:
    from shelfmark.config.env import CONFIG_DIR
    return CONFIG_DIR / "plugins" / "libby_chip.json"


def _load_chip_data() -> dict:
    try:
        return json.loads(_chip_file().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _load_chip() -> str | None:
    return _load_chip_data().get("chip") or None


def _save_chip(chip: str, **extra) -> None:
    f = _chip_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    data = {**_load_chip_data(), "chip": chip, **{k: v for k, v in extra.items() if v is not None}}
    f.write_text(json.dumps(data))


def _card_key(website_id: str, card_number: str) -> str:
    return f"{website_id}:{card_number}"


# ── Patron API helpers ─────────────────────────────────────────────────────────

def _ensure_chip() -> str:
    """Return a valid identity token, creating a new chip if needed."""
    chip = _load_chip()
    if chip:
        try:
            r = _SESSION.get(f"{_API}/chip", headers={"Authorization": f"Bearer {chip}"}, timeout=10)
            if r.ok:
                return chip
        except requests.RequestException:
            pass

    r = _SESSION.post(f"{_API}/chip", params={"client": "dewey"}, timeout=15)
    r.raise_for_status()
    data = r.json()
    chip = data.get("identity") or data.get("chip") or ""
    if not chip:
        raise RuntimeError(f"Unexpected chip response keys: {list(data.keys())}")
    _save_chip(chip)
    return chip


def _clone_with_code(chip: str, code: str) -> bool:
    """Clone a full Libby account via the 8-digit code from Libby app → Copy My Libby."""
    r = _SESSION.post(
        f"{_API}/chip/clone/code",
        json={"code": re.sub(r"\D", "", code)},
        headers={"Authorization": f"Bearer {chip}"},
        timeout=15,
    )
    return r.ok


def _link_card(chip: str, website_id: str, card_number: str, pin: str) -> bool:
    r = _SESSION.post(
        f"{_API}/auth/link/{website_id}",
        json={"barcode": card_number, "pin": pin},
        headers={"Authorization": f"Bearer {chip}"},
        timeout=15,
    )
    return r.ok


def _sync(chip: str) -> dict:
    r = _SESSION.get(f"{_API}/chip/sync", headers={"Authorization": f"Bearer {chip}"}, timeout=20)
    r.raise_for_status()
    return r.json()


def _default_profile_dir() -> Path:
    from shelfmark.config.env import CONFIG_DIR
    return CONFIG_DIR / "plugins" / "libby_profile"


# ── Source ─────────────────────────────────────────────────────────────────────

@register_source(SOURCE_NAME)
class LibbySource(ReleaseSource):
    name = SOURCE_NAME
    display_name = "Libby"
    supported_content_types: list[str] = ["audiobook"]  # noqa: RUF012
    can_be_default: bool = True

    def is_available(self) -> bool:
        from shelfmark.core.config import config
        has_clone = bool(config.get("LIBBY_CLONE_CODE"))
        has_card = bool(config.get("LIBBY_WEBSITE_ID") and config.get("LIBBY_CARD_NUMBER"))
        has_profile = _default_profile_dir().exists()
        return has_clone or has_card or bool(_load_chip()) or has_profile

    def search(
        self,
        book: BookMetadata,
        plan: ReleaseSearchPlan,
        *,
        expand_search: bool = False,
        content_type: str = "audiobook",
    ) -> list[Release]:
        if content_type not in self.supported_content_types:
            return []

        from shelfmark.core.config import config
        website_id = str(config.get("LIBBY_WEBSITE_ID", "")).strip()
        card_number = str(config.get("LIBBY_CARD_NUMBER", "")).strip()
        pin = str(config.get("LIBBY_PIN", "")).strip()
        clone_code = re.sub(r"\D", "", str(config.get("LIBBY_CLONE_CODE", "")))

        try:
            chip = _ensure_chip()
        except requests.RequestException as e:
            raise SourceUnavailableError(f"Libby: chip auth failed — {e}") from e

        try:
            sync_data = _sync(chip)
        except requests.RequestException as e:
            raise SourceUnavailableError(f"Libby: sync failed — {e}") from e

        saved_clone = _load_chip_data().get("clone_code", "")
        if clone_code and clone_code != saved_clone:
            _clone_with_code(chip, clone_code)
            _save_chip(chip, clone_code=clone_code)
            try:
                sync_data = _sync(chip)
            except requests.RequestException as e:
                raise SourceUnavailableError(f"Libby: sync after clone failed — {e}") from e

        elif not sync_data.get("cards") and website_id and card_number:
            current_key = _card_key(website_id, card_number)
            if current_key != _load_chip_data().get("card_key"):
                _link_card(chip, website_id, card_number, pin)
                _save_chip(chip, card_key=current_key)
                try:
                    sync_data = _sync(chip)
                except requests.RequestException as e:
                    raise SourceUnavailableError(f"Libby: sync after card link failed — {e}") from e

        loans: list[dict] = sync_data.get("loans", [])
        cards: list[dict] = sync_data.get("cards", [])

        # Build cardId → advantageKey map for later use in URL construction
        card_advantage: dict[str, str] = {
            str(c.get("cardId", "")): c.get("advantageKey", "")
            for c in cards
        }

        # Filter to audiobooks
        loans = [
            l for l in loans
            if l.get("type", {}).get("id") == "audiobook"
            or any(
                str(f.get("id", "")).startswith("audiobook")
                for f in (l.get("formats") or [])
            )
        ]

        if not loans and not sync_data.get("cards"):
            raise SourceUnavailableError(
                "Libby: no library cards linked. Enter a Clone Code or card credentials in settings."
            )

        query_words = set(_words(book.search_title or book.title))
        author_words = set(_words(book.search_author or ""))

        releases: list[Release] = []
        for loan in loans:
            title_obj = loan.get("title", {})
            loan_title = title_obj.get("main", "") or loan.get("title", "")
            if isinstance(loan_title, dict):
                loan_title = loan_title.get("main", "")
            loan_sub = title_obj.get("subtitle", "") if isinstance(title_obj, dict) else ""
            full_title = f"{loan_title}: {loan_sub}" if loan_sub else loan_title

            if not expand_search:
                loan_words = set(_words(full_title))
                sig = {w for w in query_words if len(w) > 3}
                if sig and not sig.intersection(loan_words):
                    continue
                if author_words:
                    loan_author_words = set(_words(loan.get("firstCreatorName", "")))
                    sig_a = {w for w in author_words if len(w) > 3}
                    if sig_a and not sig_a.intersection(loan_author_words):
                        continue

            author = loan.get("firstCreatorName", "Unknown Author")
            card_id = str(loan.get("cardId", ""))
            title_id = str(loan.get("id") or loan.get("titleId", ""))
            advantage_key = card_advantage.get(card_id, "")

            dur_s = loan.get("type", {}).get("duration", 0) or 0
            if dur_s:
                h, m = divmod(int(dur_s) // 60, 60)
                duration_str = f"{h}h {m:02d}m" if h else f"{m}m"
            else:
                duration_str = None

            releases.append(
                Release(
                    source=SOURCE_NAME,
                    source_id=f"{card_id}:{title_id}",
                    title=f"{full_title} — {author}",
                    format="m4b",
                    size=duration_str,
                    download_url=f"libby://{card_id}/{title_id}",
                    protocol=ReleaseProtocol.HTTP,
                    indexer=self.display_name,
                    content_type=content_type,
                    extra={
                        "card_id": card_id,
                        "title_id": title_id,
                        "title": full_title,
                        "author": author,
                        "advantage_key": advantage_key,
                    },
                )
            )

        return releases

    def get_column_config(self) -> ReleaseColumnConfig:
        return ReleaseColumnConfig(
            columns=[
                ColumnSchema(
                    key="format",
                    label="Format",
                    render_type=ColumnRenderType.BADGE,
                    align=ColumnAlign.CENTER,
                    width="60px",
                    uppercase=True,
                ),
                ColumnSchema(
                    key="size",
                    label="Duration",
                    render_type=ColumnRenderType.TEXT,
                    align=ColumnAlign.CENTER,
                    width="80px",
                ),
            ],
            grid_template="minmax(0,2fr) 60px 80px",
        )


# ── Handler ────────────────────────────────────────────────────────────────────

@register_handler(SOURCE_NAME)
class LibbyHandler(DownloadHandler):
    def download(
        self,
        task: DownloadTask,
        cancel_flag: Event,
        progress_callback: Callable[[float], None],
        status_callback: Callable[[str, str | None], None],
    ) -> str | None:
        from shelfmark.config.env import TMP_DIR
        from shelfmark.core.config import config

        card_id = task.extra.get("card_id", "")
        title_id = task.extra.get("title_id", "")
        book_title = task.extra.get("title", "Unknown")
        book_author = task.extra.get("author", "Unknown")
        advantage_key = task.extra.get("advantage_key", "")

        if not title_id:
            raise RuntimeError("Missing title_id in task metadata")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is not installed. Run: "
                "pip install playwright && playwright install chromium"
            )

        profile_str = str(config.get("LIBBY_PROFILE_DIR", "")).strip()
        profile_dir = Path(profile_str) if profile_str else _default_profile_dir()

        if not profile_dir.exists():
            raise RuntimeError(
                f"Browser profile not found at {profile_dir}. "
                "Run 'python libby_dl.py --profile {profile_dir}' once to log in to Libby, "
                "then try downloading again."
            )

        safe_name = _safe_filename(f"{book_author} - {book_title}")
        tmp_dir = TMP_DIR / f"libby_{task.task_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        status_callback("resolving", "Opening Libby player…")

        book_data = None

        with sync_playwright() as pw:
            browser = pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            browser.add_init_script(_HOOK_JS)

            page = browser.new_page()
            page.set_extra_http_headers({"User-Agent": _UA})

            listen_page = None

            # Try direct listen URL if we have the advantage key
            if advantage_key:
                candidate = (
                    f"https://libbyapp.com/library/{advantage_key}"
                    f"/audiobooks/media/{title_id}/listen"
                )
                try:
                    page.goto(candidate, timeout=20000)
                    # Check if we landed on a listen page
                    if "listen.libbyapp.com" in page.url or "listen.overdrive.com" in page.url:
                        listen_page = page
                    elif "listen" in page.url.lower():
                        listen_page = page
                except Exception:
                    pass

            # Fall back: navigate to loans shelf and click Listen on the right book
            if not listen_page:
                try:
                    page.goto("https://libbyapp.com/shelf/loans", timeout=30000)
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    time.sleep(3)  # let the SPA render

                    # Look for a link/button near the book title
                    clicked = False
                    for selector in [
                        f'[data-media-id="{title_id}"] a[href*="listen"]',
                        f'a[href*="{title_id}"][href*="listen"]',
                    ]:
                        try:
                            el = page.locator(selector).first
                            if el.is_visible(timeout=2000):
                                el.click()
                                clicked = True
                                break
                        except Exception:
                            continue

                    if not clicked:
                        # Try clicking via title text match
                        try:
                            page.get_by_text(book_title[:30], exact=False).first.click(timeout=5000)
                            time.sleep(1)
                            # Click Listen if a dialog/panel appeared
                            for txt in ["Listen", "Open", "Play"]:
                                try:
                                    page.get_by_role("link", name=txt).first.click(timeout=3000)
                                    break
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    # Wait for listen.libbyapp.com to open
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        for p in browser.pages:
                            if "listen.libbyapp.com" in p.url or "listen.overdrive.com" in p.url:
                                listen_page = p
                                break
                        if listen_page:
                            break
                        # Also check if the current page navigated there
                        if "listen.libbyapp.com" in page.url or "listen.overdrive.com" in page.url:
                            listen_page = page
                            break
                        time.sleep(1)
                except Exception:
                    pass

            if not listen_page:
                browser.close()
                raise RuntimeError(
                    "Could not navigate to the Libby listen page. "
                    "Make sure you're logged in and have this book borrowed "
                    f"(title_id={title_id}, advantage_key={advantage_key}). "
                    "If the profile is outdated, re-run libby_dl.py to refresh the login."
                )

            # Make sure the hook is present (page may have loaded before context hook)
            try:
                listen_page.evaluate(_HOOK_JS)
            except Exception:
                pass

            status_callback("resolving", "Waiting for chapter URLs…")

            # Poll for params (up to 90s)
            deadline = time.time() + 90
            while time.time() < deadline:
                if cancel_flag.is_set():
                    browser.close()
                    return None
                ready = listen_page.evaluate("() => !!window.__ld_params")
                if ready:
                    break
                time.sleep(1)
            else:
                browser.close()
                raise RuntimeError(
                    "Timed out waiting for chapter tokens from Libby. "
                    "The book player may not have fully loaded."
                )

            book_data = listen_page.evaluate(_EXTRACT_JS)
            browser.close()

        if not book_data or not book_data.get("chapters"):
            raise RuntimeError(
                "Could not extract chapter data from Libby player. "
                "The book may use a format not yet supported."
            )

        origin = book_data["origin"]
        chapters = book_data["chapters"]

        if cancel_flag.is_set():
            return None

        status_callback("downloading", f"0/{len(chapters)} chapters")
        mp3_files: list[Path] = []

        for ch in chapters:
            if cancel_flag.is_set():
                return None

            chapter_url = f"{origin}/{ch['path']}?{ch['param']}"
            dest = tmp_dir / f"{ch['index']:03d} - {_safe_filename(ch['title'])}.mp3"

            if not dest.exists():
                try:
                    with _SESSION.get(chapter_url, stream=True, timeout=120) as r:
                        r.raise_for_status()
                        total = int(r.headers.get("content-length", 0))
                        downloaded = 0
                        with dest.open("wb") as fh:
                            for chunk in r.iter_content(chunk_size=65536):
                                if cancel_flag.is_set():
                                    dest.unlink(missing_ok=True)
                                    return None
                                fh.write(chunk)
                                downloaded += len(chunk)
                                if total:
                                    part_pct = downloaded / total
                                    overall = (ch["index"] + part_pct) / len(chapters) * 85
                                    progress_callback(overall)
                except requests.RequestException as e:
                    raise RuntimeError(f"Chapter {ch['index'] + 1} download failed: {e}") from e

            mp3_files.append(dest)
            progress_callback((ch["index"] + 1) / len(chapters) * 85)
            status_callback("downloading", f"{ch['index'] + 1}/{len(chapters)} chapters")

        if not mp3_files:
            raise RuntimeError("No chapters downloaded")

        # Cover art
        cover_path: Path | None = None
        if book_data.get("cover"):
            try:
                cover_path = tmp_dir / "cover.jpg"
                with _SESSION.get(book_data["cover"], timeout=30) as r:
                    cover_path.write_bytes(r.content)
            except Exception:
                cover_path = None

        status_callback("processing", "Merging chapters…")
        progress_callback(88)

        out_path = TMP_DIR / f"{safe_name}_{task.task_id}.m4b"
        try:
            _build_m4b(chapters, tmp_dir, mp3_files, cover_path, out_path)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
            raise RuntimeError(f"ffmpeg merge failed: {stderr}") from e

        progress_callback(100)
        return str(out_path)

    def cancel(self, task_id: str) -> bool:
        return True


# ── Utilities ──────────────────────────────────────────────────────────────────

def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _safe_filename(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s\-.]", "", text).strip()
    text = re.sub(r"\s+", "_", text)
    return text[:max_len]


def _build_m4b(
    chapters: list[dict],
    tmp_dir: Path,
    mp3_files: list[Path],
    cover_path: Path | None,
    out_path: Path,
) -> None:
    concat_file = tmp_dir / "concat.txt"
    concat_file.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in mp3_files)
    )

    meta_lines = [";FFMETADATA1\n"]
    cursor_ms = 0
    timestamps: list[int] = []

    for mp3 in mp3_files:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(mp3)],
            capture_output=True, text=True,
        )
        try:
            dur_s = float(json.loads(result.stdout)["format"]["duration"])
        except Exception:
            dur_s = 0.0
        timestamps.append(cursor_ms)
        cursor_ms += int(dur_s * 1000)

    for i, (ch, start_ms) in enumerate(zip(chapters, timestamps)):
        end_ms = timestamps[i + 1] if i + 1 < len(timestamps) else cursor_ms
        meta_lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title={ch['title']}\n",
        ]

    meta_file = tmp_dir / "meta.txt"
    meta_file.write_text("\n".join(meta_lines))

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

    subprocess.run(cmd, check=True, capture_output=True)


# ── Settings ───────────────────────────────────────────────────────────────────

def get_settings_fields() -> list:
    from shelfmark.core.settings_registry import (
        HeadingField,
        PasswordField,
        TextField,
    )

    return [
        HeadingField(
            key="LIBBY_HEADING",
            title="Libby",
            description=(
                "Downloads your borrowed Libby audiobooks using a headless browser "
                "(Playwright) to capture the signed chapter URLs — this is the only "
                "method that works with Libby's current DRM approach.\n\n"
                "REQUIREMENTS:\n"
                "• pip install playwright && playwright install chromium\n"
                "• One-time browser login (see Profile Path below)\n\n"
                "STEP 1 — Set up the browser profile:\n"
                "Run this once from the command line to log in to Libby and save the session:\n"
                "  python libby_dl.py --profile /config/plugins/libby_profile\n"
                "Navigate to a borrowed audiobook and click Listen, then the script will save "
                "your session and you can close it.\n\n"
                "STEP 2 — Account auth for search results (shows your borrowed books):\n"
                "Use the Clone Code method (recommended) or card credentials below."
            ),
        ),
        TextField(
            key="LIBBY_PROFILE_DIR",
            label="Browser Profile Path",
            description="Path to the saved Libby browser profile. Defaults to /config/plugins/libby_profile.",
            placeholder="/config/plugins/libby_profile",
        ),
        HeadingField(
            key="LIBBY_AUTH_HEADING",
            title="Account (for Search Results)",
            description="Links your Libby account so the plugin can list your loans.",
        ),
        TextField(
            key="LIBBY_CLONE_CODE",
            label="Clone Code (recommended)",
            description="From Libby app: tap the icon top-left → Copy My Libby → Get a code. Enter the 8 digits here.",
            placeholder="12345678",
        ),
        HeadingField(
            key="LIBBY_CARD_HEADING",
            title="— or card credentials —",
            description="Alternative to the clone code if you don't have the Libby app.",
        ),
        TextField(
            key="LIBBY_WEBSITE_ID",
            label="Library Website ID",
            description="Your library's OverDrive website ID (a number). Found in your library's OverDrive URL.",
            placeholder="48",
        ),
        TextField(
            key="LIBBY_CARD_NUMBER",
            label="Library Card Number",
            description="Your library card barcode number.",
        ),
        PasswordField(
            key="LIBBY_PIN",
            label="Library PIN",
            description="Your library PIN or password. Leave empty if not required.",
        ),
    ]
