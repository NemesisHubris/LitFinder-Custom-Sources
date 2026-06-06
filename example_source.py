"""
Example / skeleton custom source for LitFinder.

This file demonstrates every hook available to a custom source plugin.
Copy it, rename it (e.g. my_source.py), and drop it in:

    $CONFIG_DIR/custom_sources/my_source.py

Then restart LitFinder. The source will appear in:

    Settings → Release Sources → Custom Sources

Delete or comment out anything you don't need.
"""

from __future__ import annotations

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

# ── Identity ──────────────────────────────────────────────────────────────────
# This string is used as the key everywhere — must be unique across all sources.
SOURCE_NAME = "example_source"

# ── HTTP session ──────────────────────────────────────────────────────────────
# Use a module-level Session for connection pooling. Set a descriptive UA.
_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "LitFinder/1.0 (custom-source; example)"


# ── Source ────────────────────────────────────────────────────────────────────

@register_source(SOURCE_NAME)
class ExampleSource(ReleaseSource):
    # display_name appears in the UI and in Release.indexer
    name = SOURCE_NAME
    display_name = "Example Source"

    # Which content types this source handles. Options: "ebook", "audiobook".
    # The source's search() will only be called for types in this list.
    supported_content_types: list[str] = ["ebook"]  # noqa: RUF012

    # Whether this source can be set as a default source in settings.
    can_be_default: bool = True

    def is_available(self) -> bool:
        """
        Return False to temporarily disable the source without removing the file.
        Useful if you want to check a config key before enabling, e.g.:

            from shelfmark.core.config import config
            return bool(config.get("EXAMPLE_API_KEY"))
        """
        return True

    def search(
        self,
        book: BookMetadata,
        plan: ReleaseSearchPlan,
        *,
        expand_search: bool = False,
        content_type: str = "ebook",
    ) -> list[Release]:
        """
        Return a list of Release objects matching `book`.

        `book` has:
            book.title          — canonical title
            book.search_title   — cleaned title for querying (prefer this)
            book.search_author  — cleaned author surname/name for querying
            book.authors        — list of author dicts

        `expand_search=True` is passed on a second attempt when the first
        call returned nothing. Broaden your query (drop the author, fuzzy
        match, etc.).

        Raise SourceUnavailableError if your API is unreachable so LitFinder
        can show a clean "source unavailable" message instead of a crash.
        """
        if content_type not in self.supported_content_types:
            return []

        # Read your custom settings (defined in get_settings_fields below)
        from shelfmark.core.config import config
        api_url = config.get("EXAMPLE_API_URL", "https://example.com/api")
        max_results = int(config.get("EXAMPLE_MAX_RESULTS", 10))

        query = book.search_title or book.title
        if book.search_author and not expand_search:
            query = f"{query} {book.search_author}"

        try:
            resp = _SESSION.get(
                f"{api_url}/search",
                params={"q": query, "limit": max_results},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise SourceUnavailableError(f"Example API error: {exc}") from exc

        releases: list[Release] = []
        for item in data.get("results", []):
            releases.append(
                Release(
                    source=SOURCE_NAME,
                    source_id=str(item["id"]),
                    title=item["title"],
                    format=item.get("format", "epub"),
                    language=item.get("language"),        # ISO 639-1, e.g. "EN"
                    size=item.get("size_human"),          # e.g. "4.2 MB"
                    size_bytes=item.get("size_bytes"),    # raw int for sorting
                    download_url=item["download_url"],
                    info_url=item.get("info_url"),        # "more info" link
                    protocol=ReleaseProtocol.HTTP,        # or TORRENT / NZB / DCC
                    indexer=self.display_name,
                    content_type=content_type,
                    extra={
                        # Anything you want passed through to your handler
                        "example_id": item["id"],
                    },
                )
            )

        return releases

    def get_column_config(self) -> ReleaseColumnConfig:
        """
        Customise the results table columns. Optional — remove this method to
        use LitFinder's default column layout.

        `grid_template` is a CSS grid-template-columns value. The first
        column (title) uses minmax(0, Xfr). Remaining columns should match
        the widths set on each ColumnSchema.
        """
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


# ── Handler ───────────────────────────────────────────────────────────────────

@register_handler(SOURCE_NAME)
class ExampleHandler(DownloadHandler):
    def download(
        self,
        task: DownloadTask,
        cancel_flag: Event,
        progress_callback: Callable[[float], None],
        status_callback: Callable[[str, str | None], None],
    ) -> str | None:
        """
        Download the file and return its absolute local path as a string.
        Return None to signal cancellation.
        Raise RuntimeError on unrecoverable failure.

        task.source_url   — the download_url from your Release
        task.task_id      — unique string ID for this download
        task.extra        — the extra dict from your Release
        """
        from shelfmark.config.env import TMP_DIR

        if not task.source_url:
            raise RuntimeError("No download URL provided")

        # status_callback(state, message)
        # Common states: "resolving", "downloading", "processing"
        # message is shown as a subtitle — pass None to clear it
        status_callback("resolving", "Connecting…")

        url = task.source_url
        dest = TMP_DIR / f"example_{task.task_id}.epub"
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            with _SESSION.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                status_callback("downloading", None)

                with dest.open("wb") as fh:
                    for chunk in r.iter_content(chunk_size=65536):
                        # Always check cancel_flag in your download loop
                        if cancel_flag.is_set():
                            dest.unlink(missing_ok=True)
                            return None
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            progress_callback(downloaded / total * 100)
                        else:
                            # Unknown size — fake progress up to 90%
                            progress_callback(min(downloaded / 5_000_000 * 90, 90))

        except requests.RequestException as exc:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Download failed: {exc}") from exc

        progress_callback(100)
        return str(dest)

    def cancel(self, task_id: str) -> bool:
        """
        Called when the user cancels. The cancel_flag passed to download()
        will already be set — this method is for any additional cleanup
        (e.g. closing a torrent session). Return True if handled.
        """
        return True


# ── Settings ──────────────────────────────────────────────────────────────────

def get_settings_fields() -> list:
    """
    Return a list of settings fields. These appear in the LitFinder settings UI
    under Release Sources → Custom Sources → [Your Source].

    Read values at runtime with:
        from shelfmark.core.config import config
        value = config.get("MY_KEY", default)
    """
    from shelfmark.core.settings_registry import (
        CheckboxField,
        HeadingField,
        NumberField,
        PasswordField,
        SelectField,
        TextField,
    )

    return [
        # ── HeadingField ─────────────────────────────────────────────────────
        # Informational block — no user input. Use this to explain the source.
        HeadingField(
            key="EXAMPLE_HEADING",
            title="Example Source",
            description=(
                "This is an example source. Replace this description with "
                "details about what the source searches and any caveats the "
                "user should know (rate limits, API key required, etc.)."
            ),
        ),

        # ── TextField ────────────────────────────────────────────────────────
        # Single-line text. Good for URLs, usernames, language codes.
        TextField(
            key="EXAMPLE_API_URL",
            label="API Base URL",
            description="Base URL for the example API.",
            default="https://example.com/api",
            placeholder="https://example.com/api",
        ),

        # ── PasswordField ────────────────────────────────────────────────────
        # Same as TextField but masked in the UI and stored encrypted.
        PasswordField(
            key="EXAMPLE_API_KEY",
            label="API Key",
            description="Your API key. Leave empty if the API is public.",
        ),

        # ── NumberField ──────────────────────────────────────────────────────
        # Integer input with optional min/max/step constraints.
        NumberField(
            key="EXAMPLE_MAX_RESULTS",
            label="Max Results",
            description="Maximum number of results to return per search.",
            default=10,
            min_value=1,
            max_value=50,
            step=1,
        ),

        # ── CheckboxField ────────────────────────────────────────────────────
        # Boolean toggle.
        CheckboxField(
            key="EXAMPLE_ONLY_ENGLISH",
            label="English results only",
            description="Filter results to English language only.",
            default=False,
        ),

        # ── SelectField ──────────────────────────────────────────────────────
        # Dropdown with a fixed list of options.
        SelectField(
            key="EXAMPLE_QUALITY",
            label="Preferred Quality",
            description="Which quality tier to prefer when multiple are available.",
            default="standard",
            options=[
                {"value": "standard", "label": "Standard"},
                {"value": "high",     "label": "High Quality"},
            ],
        ),

        # ── show_when ────────────────────────────────────────────────────────
        # Any field can be hidden unless another field has a specific value.
        # This field only appears when EXAMPLE_QUALITY is "high".
        CheckboxField(
            key="EXAMPLE_LOSSLESS",
            label="Lossless only",
            description="Only show lossless files when High Quality is selected.",
            default=False,
            show_when={"EXAMPLE_QUALITY": "high"},
        ),
    ]
