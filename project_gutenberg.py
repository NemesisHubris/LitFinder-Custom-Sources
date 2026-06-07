"""
Project Gutenberg custom source for Shelfmark.

Uses the Gutendex API (gutendex.com) — a free, open REST interface for the
Project Gutenberg catalogue. No API key required. All books are public domain.

Why are there multiple results for the same book?
Project Gutenberg maintains separate catalog entries for different preparations
of the same work — different volunteer editors, different base texts (e.g. 1818
vs 1831 edition of Frankenstein), with-images vs no-images editions. These are
genuinely distinct files with different Gutenberg IDs. Use the Max Editions
setting to control how many appear per search.

Drop this file in $CONFIG_DIR/custom_sources/ and restart Shelfmark.
"""

from __future__ import annotations

import re
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

SOURCE_NAME = "project_gutenberg"

_GUTENDEX_API = "https://gutendex.com/books/"
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Shelfmark/1.0 (custom-source; gutenberg)"


def _pick_epub_url(formats: dict[str, str], prefer_images: bool) -> str | None:
    """Return the best available EPUB URL from a Gutendex formats dict."""
    for mime in ("application/epub+zip", "application/epub"):
        url = formats.get(mime)
        if not url:
            continue

        has_noimages = "noimages" in url
        if prefer_images and has_noimages:
            images_url = url.replace(".noimages", ".images")
            if "gutenberg.org" in images_url:
                return images_url
        elif not prefer_images and not has_noimages:
            # Try to get the noimages version
            noimages_url = re.sub(r"(\.epub)", r".noimages\1", url)
            if "gutenberg.org" in noimages_url:
                return noimages_url
        return url
    return None


def _author_names(authors: list[dict]) -> str:
    parts = []
    for a in authors:
        name = a.get("name", "")
        # Gutendex returns "Surname, Firstname" — flip it
        if "," in name:
            surname, _, given = name.partition(",")
            name = f"{given.strip()} {surname.strip()}"
        parts.append(name)
    return ", ".join(parts)


def _human_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n_bytes < 1024:
            return f"{n_bytes:.0f} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} GB"


def _fetch_size(url: str) -> tuple[int | None, str | None]:
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
class ProjectGutenbergSource(ReleaseSource):
    name = SOURCE_NAME
    display_name = "Project Gutenberg"
    supported_content_types: list[str] = ["ebook"]  # noqa: RUF012
    can_be_default: bool = True

    def is_available(self) -> bool:
        return True

    def search(
        self,
        book: BookMetadata,
        plan: ReleaseSearchPlan,
        *,
        expand_search: bool = False,
        content_type: str = "ebook",
    ) -> list[Release]:
        if content_type != "ebook":
            return []

        from shelfmark.core.config import config

        prefer_images = config.get("GUTENBERG_PREFER_IMAGES", False)
        max_editions = int(config.get("GUTENBERG_MAX_EDITIONS", 5))
        lang_filter = (config.get("GUTENBERG_LANGUAGE") or "").strip().lower()

        query = book.search_title or book.title
        if book.search_author and not expand_search:
            query = f"{query} {book.search_author}"

        params: dict[str, str] = {"search": query}
        if lang_filter:
            params["languages"] = lang_filter

        try:
            resp = _SESSION.get(_GUTENDEX_API, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise SourceUnavailableError(f"Gutendex API error: {exc}") from exc

        results = data.get("results", [])
        releases: list[Release] = []

        for item in results[:max_editions]:
            formats = item.get("formats", {})
            epub_url = _pick_epub_url(formats, prefer_images)
            if not epub_url:
                continue

            book_id = str(item.get("id", ""))
            title = item.get("title", "Unknown")
            authors = item.get("authors", [])
            languages = item.get("languages", [])
            language = languages[0].upper() if languages else None
            info_url = f"https://www.gutenberg.org/ebooks/{book_id}"
            download_count = item.get("download_count")

            # Label editions to help users distinguish multiple results for the same work
            edition_label = ""
            if "noimages" in epub_url:
                edition_label = " (no images)"
            elif ".images" in epub_url:
                edition_label = " (with images)"

            size_bytes, size_human = _fetch_size(epub_url)

            releases.append(
                Release(
                    source=SOURCE_NAME,
                    source_id=book_id,
                    title=(
                        f"{title}{edition_label} — {_author_names(authors)}"
                        if authors
                        else f"{title}{edition_label}"
                    ),
                    format="epub",
                    language=language,
                    size=size_human,
                    size_bytes=size_bytes,
                    download_url=epub_url,
                    info_url=info_url,
                    protocol=ReleaseProtocol.HTTP,
                    indexer=self.display_name,
                    content_type="ebook",
                    extra={
                        "gutenberg_id": book_id,
                        "download_count": download_count,
                        "author_display": _author_names(authors),
                        "has_images": ".images" in epub_url and "noimages" not in epub_url,
                    },
                )
            )

        return releases

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
                    width="70px",
                    color_hint=ColumnColorHint(type="map", value="format"),
                    uppercase=True,
                ),
                ColumnSchema(
                    key="size",
                    label="Size",
                    render_type=ColumnRenderType.SIZE,
                    align=ColumnAlign.CENTER,
                    width="70px",
                ),
            ],
            grid_template="minmax(0,2fr) 60px 70px 70px",
            supported_filters=["format", "language"],
        )


@register_handler(SOURCE_NAME)
class ProjectGutenbergHandler(DownloadHandler):
    def download(
        self,
        task: DownloadTask,
        cancel_flag: Event,
        progress_callback: Callable[[float], None],
        status_callback: Callable[[str, str | None], None],
    ) -> str | None:
        from shelfmark.config.env import TMP_DIR

        if not task.source_url:
            raise RuntimeError("No download URL for this Gutenberg book")

        status_callback("resolving", "Connecting to Project Gutenberg…")

        url = task.source_url
        dest = TMP_DIR / f"gutenberg_{task.task_id}.epub"
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            with _SESSION.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                status_callback("downloading", None)

                with dest.open("wb") as fh:
                    for chunk in r.iter_content(chunk_size=65536):
                        if cancel_flag.is_set():
                            dest.unlink(missing_ok=True)
                            return None
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            progress_callback(downloaded / total * 100)
                        else:
                            progress_callback(min(downloaded / 500_000 * 80, 90))

        except requests.RequestException as exc:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Download from Project Gutenberg failed: {exc}") from exc

        progress_callback(100)
        return str(dest)

    def cancel(self, task_id: str) -> bool:
        return True


def get_settings_fields() -> list:
    from shelfmark.core.settings_registry import (
        CheckboxField,
        HeadingField,
        NumberField,
        TextField,
    )

    return [
        HeadingField(
            key="GUTENBERG_INFO_HEADING",
            title="Project Gutenberg — ~75,000 Free Public Domain Ebooks",
            description=(
                "Free ebooks via the Gutendex API. Popular titles may have multiple "
                "editions prepared by different volunteers — use Max Editions to control "
                "how many appear per search."
            ),
        ),
        NumberField(
            key="GUTENBERG_MAX_EDITIONS",
            label="Max Editions per Search",
            description="Cap how many editions of the same title appear in results.",
            default=5,
            min_value=1,
            max_value=32,
            step=1,
        ),
        CheckboxField(
            key="GUTENBERG_PREFER_IMAGES",
            label="Prefer illustrated editions",
            description="Download the version with cover art and images when available. Typically 2–5× larger.",
            default=False,
        ),
        TextField(
            key="GUTENBERG_LANGUAGE",
            label="Language Filter",
            description="ISO 639-1 code to limit results (e.g. 'en', 'de', 'fr'). Leave blank for all.",
            default="",
            placeholder="e.g. en",
        ),
    ]
