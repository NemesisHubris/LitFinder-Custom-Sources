# LitFinder Custom Sources

Custom source plugins for [LitFinder](https://github.com/NemesisHubris/litfinder) — a self-hosted book and audiobook search interface built on [Shelfmark](https://github.com/elfabitto/shelfmark).

Custom sources let you add search providers beyond the built-in ones. Drop a Python file into your config directory and LitFinder picks it up on the next restart — no forks, no rebuilds.

---

## Included Sources

| File | Type | What it searches |
|---|---|---|
| `project_gutenberg.py` | Ebook (EPUB) | ~75,000 free public-domain books via the [Gutendex API](https://gutendex.com) |
| `librivox.py` | Audiobook (M4B / MP3 / Torrent) | Free public-domain audiobooks via the [LibriVox API](https://librivox.org/api/info) and Internet Archive |

---

## Installation

1. Find your LitFinder config directory — it is the folder you mount as `/config` in your Docker container. By default this is wherever you set `CONFIG_DIR`.

2. Inside that folder, create a `custom_sources/` subdirectory if it does not already exist:
   ```
   mkdir -p /your/config/dir/custom_sources
   ```

3. Copy the `.py` file(s) you want into that directory:
   ```
   cp project_gutenberg.py /your/config/dir/custom_sources/
   cp librivox.py         /your/config/dir/custom_sources/
   ```

4. Restart your LitFinder container. The sources will appear in **Settings → Release Sources → Custom Sources**.

> **Requirements:** LitFinder must be running a build that includes custom source support. The `requests` library is available in the official Docker image — no extra packages needed for either plugin here.

---

## Plugin Reference

A custom source is a single Python file that registers a **source** (search logic) and a **handler** (download logic) using two decorators. It can also expose a `get_settings_fields()` function to add a settings panel in the UI.

### Minimal skeleton

```python
from shelfmark.release_sources import (
    DownloadHandler, Release, ReleaseProtocol,
    ReleaseSource, SourceUnavailableError,
    register_handler, register_source,
)

SOURCE_NAME = "my_source"

@register_source(SOURCE_NAME)
class MySource(ReleaseSource):
    name = SOURCE_NAME
    display_name = "My Source"
    supported_content_types = ["ebook"]   # or ["audiobook"], or both
    can_be_default = True

    def is_available(self) -> bool:
        return True   # return False to temporarily disable

    def search(self, book, plan, *, expand_search=False, content_type="ebook"):
        # Return a list[Release]
        ...

@register_handler(SOURCE_NAME)
class MyHandler(DownloadHandler):
    def download(self, task, cancel_flag, progress_callback, status_callback):
        # Download the file and return its local path, or None to cancel
        ...

    def cancel(self, task_id):
        return True
```

---

### `Release` fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `source` | `str` | Yes | Must match `SOURCE_NAME` |
| `source_id` | `str` | Yes | Unique ID within this source |
| `title` | `str` | Yes | Shown in the results list |
| `format` | `str` | Yes | e.g. `"epub"`, `"m4b"`, `"mp3"` |
| `download_url` | `str` | Yes | Direct URL or torrent URL |
| `protocol` | `ReleaseProtocol` | Yes | `HTTP`, `TORRENT`, `NZB`, or `DCC` |
| `indexer` | `str` | Yes | Display name of the source |
| `content_type` | `str` | Yes | `"ebook"` or `"audiobook"` |
| `language` | `str \| None` | No | ISO 639-1 code, e.g. `"EN"` |
| `size` | `str \| None` | No | Human-readable, e.g. `"4.2 MB"` |
| `size_bytes` | `int \| None` | No | Raw bytes for sorting |
| `info_url` | `str \| None` | No | Link shown as "more info" |
| `extra` | `dict \| None` | No | Arbitrary metadata passed to the handler |

---

### Handler callbacks

Inside `download()`:

| Callback | Signature | Purpose |
|---|---|---|
| `status_callback` | `(state: str, message: str \| None)` | Sets the displayed status. Common states: `"resolving"`, `"downloading"` |
| `progress_callback` | `(percent: float)` | Updates the progress bar (0–100) |
| `cancel_flag` | `threading.Event` | Check `.is_set()` in your download loop and return `None` to abort |

Return the **absolute local path** to the downloaded file as a string. Return `None` to signal cancellation. Raise `RuntimeError` on unrecoverable failure.

---

### Column config (optional)

Override `get_column_config()` on your source class to customise how results are displayed:

```python
from shelfmark.release_sources import (
    ColumnAlign, ColumnColorHint, ColumnRenderType,
    ColumnSchema, ReleaseColumnConfig,
)

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
```

`ColumnRenderType` values: `BADGE`, `SIZE`, `TEXT`.

---

### Settings fields (optional)

Export a `get_settings_fields()` function returning a list of field objects. These appear as a dedicated section under **Settings → Release Sources → Custom Sources → [Your Source]**.

```python
def get_settings_fields() -> list:
    from shelfmark.core.settings_registry import (
        CheckboxField, HeadingField, NumberField,
        PasswordField, SelectField, TextField,
    )
    return [...]
```

#### Available field types

**`HeadingField`** — informational block, no user input
```python
HeadingField(key="MY_HEADING", title="Section Title", description="Explanatory text.")
```

**`TextField`** — single-line text input
```python
TextField(key="MY_API_URL", label="API URL", description="...", default="", placeholder="https://")
```

**`PasswordField`** — masked text input, stored encrypted
```python
PasswordField(key="MY_API_KEY", label="API Key", description="...")
```

**`NumberField`** — integer input with min/max/step
```python
NumberField(key="MY_MAX_RESULTS", label="Max Results", description="...", default=10, min_value=1, max_value=50, step=1)
```

**`CheckboxField`** — boolean toggle
```python
CheckboxField(key="MY_FEATURE", label="Enable Feature", description="...", default=False)
```

**`SelectField`** — dropdown with fixed options
```python
SelectField(
    key="MY_MODE",
    label="Mode",
    description="...",
    default="fast",
    options=[
        {"value": "fast", "label": "Fast"},
        {"value": "thorough", "label": "Thorough"},
    ],
)
```

All fields support `show_when` to conditionally hide them based on another field's value:
```python
show_when={"MY_MODE": "thorough"}
```

Read settings at runtime:
```python
from shelfmark.core.config import config
value = config.get("MY_SETTING_KEY", default_value)
```

---

## Source-Specific Notes

### Project Gutenberg (`project_gutenberg.py`)

Searches the [Gutendex REST API](https://gutendex.com) — a free, open wrapper around the Project Gutenberg catalogue. No API key needed.

**Why multiple results for the same book?**  
Project Gutenberg maintains separate catalogue entries for different preparations of the same work — different volunteer editors, different base texts (e.g. the 1818 vs 1831 editions of *Frankenstein*), with-images vs. no-images versions. These are distinct files with different Gutenberg IDs. Use **Max Editions** to cap how many appear.

**Settings:**

| Key | Default | Description |
|---|---|---|
| `GUTENBERG_MAX_EDITIONS` | `5` | Maximum results per search (1–32) |
| `GUTENBERG_PREFER_IMAGES` | `false` | Prefer illustrated EPUB editions |
| `GUTENBERG_LANGUAGE` | *(empty)* | ISO 639-1 language code filter (e.g. `en`, `de`) |

---

### LibriVox (`librivox.py`)

Searches the [LibriVox API](https://librivox.org/api/info) and resolves each result to its [Internet Archive](https://archive.org) item to find the best available file format. No API key needed. All content is public domain.

**Formats:**
- **M4B** *(default)* — single file with embedded chapter markers, ready for Audiobookshelf, Apple Books, etc. Pre-built by LibriVox volunteers on archive.org. Falls back to MP3 ZIP if no M4B exists.
- **MP3 ZIP** — ZIP of individual chapter MP3 files (VBR or 64 kbps).
- **Torrent** — downloads the full archive.org item via BitTorrent. Requires a configured torrent client in LitFinder.

**Settings:**

| Key | Default | Description |
|---|---|---|
| `LIBRIVOX_AUDIO_FORMAT` | `m4b` | `m4b`, `mp3`, or `torrent` |
| `LIBRIVOX_MP3_QUALITY` | `64kb` | `64kb` or `vbr` — only applies when format is `mp3` |
| `LIBRIVOX_LANGUAGE` | *(empty)* | Full language name as used by LibriVox (e.g. `English`, `German`) |
| `LIBRIVOX_SOLO_ONLY` | `false` | Filter to prefer solo-reader recordings |

---

## Writing Your Own

See [`example_source.py`](example_source.py) for a fully commented skeleton showing all the hooks in one place.

The key things to know:
- The file is loaded at startup — keep module-level code fast (no blocking I/O at import time).
- Use a module-level `requests.Session` for connection pooling.
- `book.search_title`, `book.search_author`, and `book.title` come from LitFinder's metadata lookup for the card the user clicked.
- `expand_search=True` is passed on a second attempt when the first returned nothing — broaden your query accordingly.
- `SourceUnavailableError` is the right exception when your API is unreachable. Any other exception is treated as an unexpected error.

---

## Contributing

PRs welcome. If you've built a source for a service not covered here, open a pull request — keep one source per file and include a docstring explaining any API quirks.
