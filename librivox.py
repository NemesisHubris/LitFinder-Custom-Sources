"""
LibriVox custom source for Shelfmark.

LibriVox offers free public-domain audiobooks read by volunteers. All content
is hosted on the Internet Archive (archive.org), which provides M4B (single-file
audiobook with embedded chapters), individual MP3 chapters, and BitTorrent.

This plugin prefers the pre-built M4B when available — a single file with chapter
markers baked in, ready for any audiobook player. For books without an M4B it
falls back to downloading all MP3 chapters and merging them with ffmpeg (available
in the Shelfmark Docker image).

No account or API key required.

Drop this file in $CONFIG_DIR/custom_sources/ and restart Shelfmark.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

import requests

from shelfmark.release_sources import (
    ColumnAlign,
    ColumnColorHint,
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

SOURCE_NAME = "librivox"

_API_BASE = "https://librivox.org/api/feed/audiobooks/"
_IA_METADATA_BASE = "https://archive.org/metadata"
_IA_DOWNLOAD_BASE = "https://archive.org/download"

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Shelfmark/1.0 (custom-source; librivox)"

_LANG_MAP: dict[str, str] = {
    "english": "en",
    "german": "de",
    "french": "fr",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
    "dutch": "nl",
    "russian": "ru",
    "chinese": "zh",
    "japanese": "ja",
    "latin": "la",
    "greek": "el",
    "polish": "pl",
    "czech": "cs",
    "swedish": "sv",
    "danish": "da",
    "norwegian": "no",
    "finnish": "fi",
    "hungarian": "hu",
    "romanian": "ro",
}


def _normalise_language(lang: str) -> str:
    return _LANG_MAP.get(lang.lower().strip(), lang[:2].upper())


def _author_display(authors: list[dict]) -> str:
    parts = []
    for a in authors:
        first = a.get("first_name", "").strip()
        last = a.get("last_name", "").strip()
        name = f"{first} {last}".strip() if first or last else ""
        if name:
            parts.append(name)
    return ", ".join(parts)


def _human_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n_bytes < 1024:
            return f"{n_bytes:.0f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} GB"


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _extract_ia_identifier(book: dict) -> str | None:
    """Extract the archive.org item identifier from a LibriVox API book record."""
    for field in ("url_zip_file",):
        url = book.get(field, "")
        m = re.search(r"archive\.org/(?:download|compress)/([^/?]+)", url)
        if m:
            return m.group(1)
    # Fall back to section listen_url
    for section in book.get("sections", []):
        listen = section.get("listen_url", "")
        m = re.search(r"archive\.org/download/([^/?]+)", listen)
        if m:
            return m.group(1)
    return None


def _fetch_ia_files(ia_id: str) -> list[dict]:
    """Return archive.org file list for an item, or [] on error."""
    try:
        r = _SESSION.get(f"{_IA_METADATA_BASE}/{ia_id}/files", timeout=6)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception:
        return []


def _pick_ia_m4b(ia_id: str, files: list[dict]) -> tuple[str | None, int | None, str | None]:
    """Return (url, bytes, human_size) for the best M4B in an IA file list."""
    for f in files:
        name = f.get("name", "")
        if name.lower().endswith(".m4b"):
            size_str = f.get("size", "")
            size_bytes = int(size_str) if size_str and size_str.isdigit() else None
            size_human = _human_size(size_bytes) if size_bytes else None
            url = f"{_IA_DOWNLOAD_BASE}/{ia_id}/{name}"
            return url, size_bytes, size_human
    return None, None, None


def _pick_ia_torrent(ia_id: str, files: list[dict]) -> str | None:
    """Return the archive.org torrent URL if one exists."""
    for f in files:
        name = f.get("name", "")
        if name.endswith("_archive.torrent"):
            return f"{_IA_DOWNLOAD_BASE}/{ia_id}/{name}"
    return None


def _pick_zip_url(book: dict, quality: str) -> str:
    """Build the archive.org ZIP URL for the requested MP3 quality."""
    ia_id = _extract_ia_identifier(book)
    if not ia_id:
        return book.get("url_zip_file", "").strip()

    fmt = "VBR MP3" if quality == "vbr" else "64Kbps MP3"
    return f"https://archive.org/compress/{ia_id}/formats={fmt}&file=/{ia_id}.zip"


def _head_size(url: str) -> tuple[int | None, str | None]:
    try:
        r = _SESSION.head(url, timeout=8, allow_redirects=True)
        cl = r.headers.get("content-length")
        if cl and cl.isdigit():
            n = int(cl)
            return n, _human_size(n)
    except requests.RequestException:
        pass
    return None, None


@register_source(SOURCE_NAME)
class LibriVoxSource(ReleaseSource):
    name = SOURCE_NAME
    display_name = "LibriVox"
    supported_content_types: list[str] = ["audiobook"]  # noqa: RUF012
    can_be_default: bool = True

    def is_available(self) -> bool:
        return True

    def search(
        self,
        book: BookMetadata,
        plan: ReleaseSearchPlan,
        *,
        expand_search: bool = False,
        content_type: str = "audiobook",
    ) -> list[Release]:
        if content_type != "audiobook":
            return []

        from shelfmark.core.config import config

        audio_format = config.get("LIBRIVOX_AUDIO_FORMAT", "m4b")
        mp3_quality = config.get("LIBRIVOX_MP3_QUALITY", "64kb")
        language_filter = (config.get("LIBRIVOX_LANGUAGE") or "").strip().lower()
        solo_only = config.get("LIBRIVOX_SOLO_ONLY", False)

        title = book.search_title or book.title
        author = book.search_author or ""

        results = self._search_api(title=title, author=author if not expand_search else "")
        if not results and not expand_search:
            results = self._search_api(title=title, author="")

        if language_filter:
            results = [
                r for r in results
                if _normalise_language(r.get("language", "")).lower() == language_filter
                or r.get("language", "").lower() == language_filter
            ]

        if solo_only:
            results = [r for r in results if r.get("num_sections") and int(r.get("num_sections", 0)) > 0]

        # Resolve archive.org metadata in parallel (for M4B + torrent discovery)
        ia_files_map: dict[str, list[dict]] = {}
        ia_ids = [_extract_ia_identifier(r) for r in results]

        if audio_format == "m4b" or audio_format == "torrent":
            with ThreadPoolExecutor(max_workers=8) as pool:
                future_to_id = {
                    pool.submit(_fetch_ia_files, ia_id): ia_id
                    for ia_id in ia_ids if ia_id
                }
                for future in as_completed(future_to_id):
                    ia_id = future_to_id[future]
                    ia_files_map[ia_id] = future.result()

        releases: list[Release] = []
        for item, ia_id in zip(results, ia_ids):
            zip_url = item.get("url_zip_file", "").strip()
            if not zip_url and not ia_id:
                continue

            book_id = str(item.get("id", ""))
            title_str = item.get("title", "Unknown")
            authors = item.get("authors", [])
            language = _normalise_language(item.get("language", ""))
            num_sections = item.get("num_sections", "?")
            info_url = item.get("url_librivox", "") or None
            author_str = _author_display(authors)
            display_title = f"{title_str} — {author_str}" if author_str else title_str

            ia_files = ia_files_map.get(ia_id, []) if ia_id else []

            if audio_format == "m4b":
                m4b_url, m4b_bytes, m4b_human = _pick_ia_m4b(ia_id, ia_files) if ia_id else (None, None, None)
                if m4b_url:
                    releases.append(Release(
                        source=SOURCE_NAME,
                        source_id=book_id,
                        title=display_title,
                        format="m4b",
                        language=language,
                        size=m4b_human,
                        size_bytes=m4b_bytes,
                        download_url=m4b_url,
                        info_url=info_url,
                        protocol=ReleaseProtocol.HTTP,
                        indexer=self.display_name,
                        content_type="audiobook",
                        extra={
                            "librivox_id": book_id,
                            "ia_identifier": ia_id,
                            "num_sections": num_sections,
                            "author_display": author_str,
                            "librivox_format": "m4b",
                        },
                    ))
                    continue  # M4B found — skip ZIP fallback for this book

                # No M4B — fall through to MP3 ZIP, but tag for ffmpeg merge
                effective_format = "mp3"
            elif audio_format == "torrent":
                torrent_url = _pick_ia_torrent(ia_id, ia_files) if ia_id else None
                if torrent_url:
                    releases.append(Release(
                        source=SOURCE_NAME,
                        source_id=book_id,
                        title=display_title,
                        format="mp3",
                        language=language,
                        size=None,
                        size_bytes=None,
                        download_url=torrent_url,
                        info_url=info_url,
                        protocol=ReleaseProtocol.TORRENT,
                        indexer=self.display_name,
                        content_type="audiobook",
                        extra={
                            "librivox_id": book_id,
                            "ia_identifier": ia_id,
                            "num_sections": num_sections,
                            "author_display": author_str,
                            "librivox_format": "torrent",
                        },
                    ))
                    continue
                effective_format = "mp3"
            else:
                effective_format = "mp3"

            # MP3 ZIP path
            built_zip_url = _pick_zip_url(item, mp3_quality)
            size_bytes, size_human = _head_size(built_zip_url)
            releases.append(Release(
                source=SOURCE_NAME,
                source_id=book_id,
                title=display_title,
                format="mp3",
                language=language,
                size=size_human,
                size_bytes=size_bytes,
                download_url=built_zip_url,
                info_url=info_url,
                protocol=ReleaseProtocol.HTTP,
                indexer=self.display_name,
                content_type="audiobook",
                extra={
                    "librivox_id": book_id,
                    "ia_identifier": ia_id,
                    "num_sections": num_sections,
                    "author_display": author_str,
                    "librivox_format": "zip",
                    "mp3_quality": mp3_quality,
                },
            ))

        return releases

    def _search_api(self, *, title: str, author: str) -> list[dict]:
        _STOPS = {"or", "the", "a", "an", "and", "of", "in", "to"}
        title_words = [w for w in title.split() if w.lower() not in _STOPS]
        search_title = title_words[0] if title_words else title.split()[0]

        params: dict[str, str] = {
            "format": "json",
            "extended": "1",
            "limit": "20",
            "title": f"^{search_title}",
        }
        if author:
            last = author.split()[-1] if author.split() else author
            params["author"] = last

        try:
            resp = _SESSION.get(_API_BASE, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise SourceUnavailableError(f"LibriVox API error: {exc}") from exc

        books = data.get("books", [])
        if not isinstance(books, list):
            return []
        return books

    def get_column_config(self) -> ReleaseColumnConfig:
        return ReleaseColumnConfig(
            columns=[
                ColumnSchema(
                    key="language",
                    label="Language",
                    render_type=ColumnRenderType.BADGE,
                    align=ColumnAlign.CENTER,
                    width="60px",
                    color_hint=ColumnColorHint(type="map", value="language"),
                    uppercase=True,
                ),
                ColumnSchema(
                    key="format",
                    label="Format",
                    render_type=ColumnRenderType.BADGE,
                    align=ColumnAlign.CENTER,
                    width="65px",
                    color_hint=ColumnColorHint(type="map", value="format"),
                    uppercase=True,
                ),
                ColumnSchema(
                    key="size",
                    label="Size",
                    render_type=ColumnRenderType.SIZE,
                    align=ColumnAlign.CENTER,
                    width="75px",
                ),
            ],
            grid_template="minmax(0,2fr) 60px 65px 75px",
            supported_filters=["language"],
        )


@register_handler(SOURCE_NAME)
class LibriVoxHandler(DownloadHandler):
    def download(
        self,
        task: DownloadTask,
        cancel_flag: Event,
        progress_callback: Callable[[float], None],
        status_callback: Callable[[str, str | None], None],
    ) -> str | None:
        from shelfmark.config.env import TMP_DIR

        if not task.source_url:
            raise RuntimeError("No download URL for this LibriVox audiobook")

        url = task.source_url
        fmt = (getattr(task, "extra", None) or {}).get("librivox_format", "zip")

        if fmt == "m4b" or url.lower().endswith(".m4b"):
            return self._download_m4b(url, task, cancel_flag, progress_callback, status_callback, TMP_DIR)
        elif fmt == "torrent":
            # Shelfmark handles the torrent protocol — return None and let it proceed
            return None
        else:
            return self._download_zip(url, task, cancel_flag, progress_callback, status_callback, TMP_DIR)

    def _download_m4b(self, url, task, cancel_flag, progress_callback, status_callback, TMP_DIR):
        status_callback("resolving", "Connecting to Internet Archive…")
        dest = TMP_DIR / f"librivox_{task.task_id}.m4b"
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            with _SESSION.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                status_callback("downloading", None)

                with dest.open("wb") as fh:
                    for chunk in r.iter_content(chunk_size=131072):
                        if cancel_flag.is_set():
                            dest.unlink(missing_ok=True)
                            return None
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            progress_callback(downloaded / total * 100)
                        else:
                            progress_callback(min(downloaded / 200_000_000 * 90, 90))

        except requests.RequestException as exc:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"LibriVox M4B download failed: {exc}") from exc

        progress_callback(100)
        return str(dest)

    def _download_zip(self, url, task, cancel_flag, progress_callback, status_callback, TMP_DIR):
        status_callback("resolving", "Connecting to Internet Archive…")
        dest = TMP_DIR / f"librivox_{task.task_id}.zip"
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            with _SESSION.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                status_callback("downloading", None)

                with dest.open("wb") as fh:
                    for chunk in r.iter_content(chunk_size=131072):
                        if cancel_flag.is_set():
                            dest.unlink(missing_ok=True)
                            return None
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            progress_callback(downloaded / total * 100)
                        else:
                            progress_callback(min(downloaded / 50_000_000 * 80, 90))

        except requests.RequestException as exc:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"LibriVox download failed: {exc}") from exc

        progress_callback(100)
        return str(dest)

    def cancel(self, task_id: str) -> bool:
        return True


def get_settings_fields() -> list:
    from shelfmark.core.settings_registry import (
        CheckboxField,
        HeadingField,
        SelectField,
        TextField,
    )

    return [
        HeadingField(
            key="LIBRIVOX_INFO_HEADING",
            title="LibriVox — Free Public Domain Audiobooks",
            description=(
                "Volunteer-recorded audiobooks, all public domain and free. "
                "Files are served by the Internet Archive."
            ),
        ),
        SelectField(
            key="LIBRIVOX_AUDIO_FORMAT",
            label="Download Format",
            description="M4B is recommended — single file with chapters, works in most audiobook apps.",
            default="m4b",
            options=[
                {"value": "m4b", "label": "M4B (single file, chapters)"},
                {"value": "mp3", "label": "MP3 ZIP (individual chapters)"},
                {"value": "torrent", "label": "Torrent (via archive.org)"},
            ],
        ),
        SelectField(
            key="LIBRIVOX_MP3_QUALITY",
            label="MP3 Quality",
            description="VBR is higher quality but ~2× the file size. Only applies to MP3 ZIP.",
            default="64kb",
            options=[
                {"value": "64kb", "label": "64 kbps (smaller)"},
                {"value": "vbr", "label": "VBR (higher quality)"},
            ],
            show_when={"LIBRIVOX_AUDIO_FORMAT": "mp3"},
        ),
        TextField(
            key="LIBRIVOX_LANGUAGE",
            label="Language Filter",
            description="Full English name as used by LibriVox (e.g. 'English', 'German'). Leave blank for all.",
            default="",
            placeholder="e.g. English",
        ),
        CheckboxField(
            key="LIBRIVOX_SOLO_ONLY",
            label="Prefer solo readers",
            description="Hide group-read projects when a solo version exists.",
            default=False,
        ),
    ]
