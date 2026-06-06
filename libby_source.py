"""
Libby / OverDrive audiobook source for LitFinder.

Returns borrowed audiobooks from the user's Libby account and downloads
them directly using the OverDrive Patron API — no browser required.

Chip auth flow (same as odmpy / the official Libby app):
  POST /chip                           → get anonymous device token
  POST /auth/link/{websiteId}          → link library card + PIN
  GET  /chip/sync                      → list all active loans
  GET  /open/audiobook/card/X/title/Y → get per-book openbook URL
  GET  {openbook_url}                  → signed chapter tokens + spine

The chip token is saved in CONFIG_DIR/plugins/libby_chip.json so your
library card link survives container restarts.

Requirements: ffmpeg in PATH (already required by LitFinder for audiobooks).
"""

from __future__ import annotations

import json
import re
import subprocess
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
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) (Dewey; V22; iOS; 6.3.0-160)"
)
_SESSION.headers["Accept"] = "application/json"


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


def _save_chip(chip: str, card_key: str | None = None, clone_code: str | None = None) -> None:
    f = _chip_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_chip_data()
    data: dict = {"chip": chip}
    data["card_key"] = card_key or existing.get("card_key")
    data["clone_code"] = clone_code or existing.get("clone_code")
    f.write_text(json.dumps({k: v for k, v in data.items() if v is not None}))


def _card_key(website_id: str, card_number: str) -> str:
    return f"{website_id}:{card_number}"


def _ensure_chip() -> str:
    """Return a valid identity token, creating one if needed."""
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
    # API returns "identity" field (was "chip" in older API versions)
    data = r.json()
    chip = data.get("identity") or data.get("chip") or ""
    if not chip:
        raise RuntimeError(f"Unexpected chip response: {list(data.keys())}")
    _save_chip(chip)
    return chip


def _clone_with_code(chip: str, code: str) -> bool:
    """Clone a Libby account into this chip using a code from the Libby app."""
    r = _SESSION.post(
        f"{_API}/chip/clone/code",
        json={"code": code.replace("-", "").replace(" ", "")},
        headers=_auth_headers(chip),
        timeout=15,
    )
    return r.ok


def _auth_headers(chip: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {chip}"}


# ── Libby API helpers ──────────────────────────────────────────────────────────

def _link_card(chip: str, website_id: str, card_number: str, pin: str) -> bool:
    """Link a library card to this chip. Returns True on success."""
    r = _SESSION.post(
        f"{_API}/auth/link/{website_id}",
        headers=_auth_headers(chip),
        json={"barcode": card_number, "pin": pin},
        timeout=15,
    )
    return r.ok


def _sync(chip: str) -> dict:
    r = _SESSION.get(f"{_API}/chip/sync", headers=_auth_headers(chip), timeout=20)
    r.raise_for_status()
    return r.json()


def _get_audiobook_meta(chip: str, card_id: str, title_id: str) -> dict:
    r = _SESSION.get(
        f"{_API}/open/audiobook/card/{card_id}/title/{title_id}",
        headers=_auth_headers(chip),
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def _fetch_openbook(url: str) -> dict:
    r = _SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r.json()


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
        return has_clone or has_card or bool(_load_chip())

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

        try:
            chip = _ensure_chip()
        except requests.RequestException as e:
            raise SourceUnavailableError(f"Libby: chip auth failed — {e}") from e

        # Clone account via code if provided and not already done
        clone_code = str(config.get("LIBBY_CLONE_CODE", "")).strip().replace("-", "").replace(" ", "")

        try:
            sync_data = _sync(chip)
        except requests.RequestException as e:
            raise SourceUnavailableError(f"Libby: sync failed — {e}") from e

        saved_clone = _load_chip_data().get("clone_code")
        if clone_code and clone_code != saved_clone:
            _clone_with_code(chip, clone_code)
            _save_chip(chip, card_key=_load_chip_data().get("card_key"), clone_code=clone_code)
            try:
                sync_data = _sync(chip)
            except requests.RequestException as e:
                raise SourceUnavailableError(f"Libby: sync after clone failed — {e}") from e

        # Fall back to card number + PIN link if clone code not used
        elif not sync_data.get("cards") and website_id and card_number:
            current_key = _card_key(website_id, card_number)
            saved_key = _load_chip_data().get("card_key")
            if not saved_key or current_key != saved_key:
                _link_card(chip, website_id, card_number, pin)
                _save_chip(chip, card_key=current_key)
                try:
                    sync_data = _sync(chip)
                except requests.RequestException as e:
                    raise SourceUnavailableError(f"Libby: sync after link failed — {e}") from e

        loans: list[dict] = sync_data.get("loans", [])
        # Only audiobooks — format field is "audiobook-mp3" or type.id is "audiobook"
        loans = [
            l for l in loans
            if (l.get("type", {}).get("id") == "audiobook"
                or str(l.get("formats", [{}])[0].get("id", "") if l.get("formats") else "").startswith("audiobook"))
        ]

        query_words = set(_words(book.search_title or book.title))
        author_words = set(_words(book.search_author or ""))

        releases: list[Release] = []
        for loan in loans:
            title_obj = loan.get("title", {})
            loan_title = title_obj.get("main", "")
            loan_sub = title_obj.get("subtitle", "")
            full_title = f"{loan_title}: {loan_sub}" if loan_sub else loan_title

            if not expand_search:
                loan_words = set(_words(full_title))
                # Require at least one significant word to match
                sig = {w for w in query_words if len(w) > 3}
                if sig and not sig.intersection(loan_words):
                    continue
                if author_words:
                    author_name = loan.get("firstCreatorName", "")
                    loan_author_words = set(_words(author_name))
                    sig_author = {w for w in author_words if len(w) > 3}
                    if sig_author and not sig_author.intersection(loan_author_words):
                        continue

            author = loan.get("firstCreatorName", "Unknown Author")
            card_id = str(loan.get("cardId", ""))
            # API used "titleId" historically; current format uses "id"
            title_id = str(loan.get("id") or loan.get("titleId", ""))

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

        card_id = task.extra.get("card_id", "")
        title_id = task.extra.get("title_id", "")
        book_title = task.extra.get("title", "Unknown")
        book_author = task.extra.get("author", "Unknown")

        if not card_id or not title_id:
            raise RuntimeError("Missing card_id or title_id in task metadata")

        status_callback("resolving", "Authenticating with Libby…")
        try:
            chip = _ensure_chip()
        except requests.RequestException as e:
            raise RuntimeError(f"Auth failed: {e}") from e

        status_callback("resolving", "Getting chapter URLs…")
        try:
            book_meta = _get_audiobook_meta(chip, card_id, title_id)
            openbook_url = book_meta["urls"]["openbook"]
            openbook = _fetch_openbook(openbook_url)
        except (requests.RequestException, KeyError) as e:
            raise RuntimeError(f"Failed to get audiobook data: {e}") from e

        # Parse the openbook JSON — signed tokens live in b["-odread-cmpt-params"]
        ob = openbook.get("b", {})
        cmpt_params: list[str] = ob.get("-odread-cmpt-params", [])
        spine: list[dict] = ob.get("spine", [])

        if not cmpt_params or not spine:
            raise RuntimeError(
                f"Unexpected openbook format. Keys at root: {list(openbook.keys())}. "
                f"Keys at 'b': {list(ob.keys()) if ob else '(missing)'}"
            )

        # Base URL for chapter files = scheme + host of the openbook URL
        parsed = urlparse(openbook_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if cancel_flag.is_set():
            return None

        safe_name = _safe_filename(f"{book_author} - {book_title}")
        tmp_dir = TMP_DIR / f"libby_{task.task_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        # Filter spine to audio entries only (skip nav docs, etc.)
        audio_spine = [
            (entry, cmpt_params[entry.get("-odread-spine-position", i)])
            for i, entry in enumerate(spine)
            if entry.get("path") and i < len(cmpt_params)
        ]

        if not audio_spine:
            raise RuntimeError("No audio chapters found in openbook spine")

        status_callback("downloading", f"0/{len(audio_spine)} chapters")
        mp3_files: list[Path] = []

        for seq, (entry, param) in enumerate(audio_spine):
            if cancel_flag.is_set():
                return None

            path = entry["path"]
            chapter_url = f"{origin}/{path}?{param}"
            dest = tmp_dir / f"{seq:03d}.mp3"

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
                                    overall = (seq + part_pct) / len(audio_spine) * 85
                                    progress_callback(overall)
                except requests.RequestException as e:
                    raise RuntimeError(f"Chapter {seq + 1} download failed: {e}") from e

            mp3_files.append(dest)
            progress_callback((seq + 1) / len(audio_spine) * 85)
            status_callback("downloading", f"{seq + 1}/{len(audio_spine)} chapters")

        if not mp3_files:
            raise RuntimeError("No chapters downloaded")

        # Cover art
        cover_path: Path | None = None
        cover_data = ob.get("cover", {})
        cover_url = cover_data.get("href") if isinstance(cover_data, dict) else None
        if cover_url:
            try:
                cover_path = tmp_dir / "cover.jpg"
                with _SESSION.get(cover_url, timeout=30) as r:
                    cover_path.write_bytes(r.content)
            except Exception:
                cover_path = None

        # Merge to M4B
        status_callback("processing", "Merging chapters…")
        progress_callback(88)

        out_path = TMP_DIR / f"{safe_name}_{task.task_id}.m4b"
        try:
            _build_m4b(audio_spine, tmp_dir, mp3_files, cover_path, out_path)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)
            raise RuntimeError(f"ffmpeg merge failed: {stderr}") from e

        progress_callback(100)
        return str(out_path)

    def cancel(self, task_id: str) -> bool:
        return True


# ── Utilities ──────────────────────────────────────────────────────────────────

def _words(text: str) -> list[str]:
    """Lowercase word tokens for fuzzy matching."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _safe_filename(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s\-.]", "", text).strip()
    text = re.sub(r"\s+", "_", text)
    return text[:max_len]


def _build_m4b(
    audio_spine: list[tuple[dict, str]],
    tmp_dir: Path,
    mp3_files: list[Path],
    cover_path: Path | None,
    out_path: Path,
) -> None:
    concat_file = tmp_dir / "concat.txt"
    concat_file.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in mp3_files)
    )

    # Probe each MP3 for duration to compute chapter timestamps
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

    for i, ((entry, _), start_ms) in enumerate(zip(audio_spine, timestamps)):
        end_ms = timestamps[i + 1] if i + 1 < len(timestamps) else cursor_ms
        ch_title = entry.get("title") or entry.get("path") or f"Chapter {i + 1}"
        meta_lines += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={end_ms}",
            f"title={ch_title}\n",
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
                "Downloads borrowed audiobooks from your Libby account using the Libby API. "
                "Search results show only titles you currently have borrowed. "
                "\n\n"
                "RECOMMENDED SETUP — Clone Code (easiest):\n"
                "1. Open the Libby app on your phone\n"
                "2. Tap the icon in the top-left corner\n"
                "3. Tap Copy My Libby → Get a code\n"
                "4. Enter the 8-digit code below\n"
                "\n"
                "ALTERNATIVE — Card credentials (if you don't have the app):\n"
                "Fill in Library Website ID + Card Number + PIN below instead."
            ),
        ),
        TextField(
            key="LIBBY_CLONE_CODE",
            label="Libby Clone Code",
            description="8-digit code from Libby app → Copy My Libby → Get a code. Used once to link your account.",
            placeholder="1234 5678",
        ),
        HeadingField(
            key="LIBBY_CARD_HEADING",
            title="— or use card credentials —",
            description="Fill these in if you don't have the Libby app for the clone code.",
        ),
        TextField(
            key="LIBBY_WEBSITE_ID",
            label="Library Website ID",
            description="Your library's OverDrive website ID (a number). Find it in your library's OverDrive URL.",
            placeholder="48",
        ),
        TextField(
            key="LIBBY_CARD_NUMBER",
            label="Library Card Number",
            description="Your library card barcode number.",
        ),
        PasswordField(
            key="LIBBY_PIN",
            label="Library PIN / Password",
            description="Your library PIN or password. Leave empty if your library doesn't require one.",
        ),
    ]
