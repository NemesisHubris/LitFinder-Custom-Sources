"""
OverDrive / Libby audiobook source for LitFinder.

Searches your library's OverDrive catalog, borrows available titles,
and downloads them as M4B files with full chapter metadata.

Requirements:
    pip install playwright requests
    playwright install chromium
    ffmpeg in PATH

Drop this file in:  $CONFIG_DIR/custom_sources/overdrive_source.py
Then restart LitFinder → Settings → Release Sources → Custom Sources
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import subprocess
import time
from pathlib import Path
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

SOURCE_NAME = "overdrive"

# ── JS hook injected before Libby's player scripts run ───────────────────────
# Intercepts JSON.parse to capture per-part signed auth tokens (cmpt params)
# and polls for the BIF (Book Information Format) object.
_INTERCEPT_JS = r"""
(function() {
    if (window.__od_hooked) return;
    window.__od_hooked = true;
    window.__od_params = null;
    window.__od_bif    = null;

    const _orig = JSON.parse;
    JSON.parse = function(...args) {
        const ret = _orig.apply(this, args);
        try {
            if (ret && typeof ret === 'object' &&
                ret['b'] && ret['b']['-odread-cmpt-params']) {
                window.__od_params = Array.from(ret['b']['-odread-cmpt-params']);
            }
        } catch(_) {}
        return ret;
    };

    const _t = setInterval(function() {
        if (window.BIF) {
            window.__od_bif = window.BIF;
            clearInterval(_t);
        }
    }, 100);
})();
"""

# Extracts all signed MP3 URLs + full metadata once BIF + params are ready
_EXTRACT_JS = r"""
() => {
    const bif    = window.__od_bif;
    const params = window.__od_params;
    if (!bif || !params) return null;

    const origin     = location.origin;
    const components = bif.objects.spool.components;
    const spineMap   = bif.map.spine;

    // Build path → index lookup for chapter spine mapping
    const pathToIdx = {};
    spineMap.forEach(function(x, i) {
        pathToIdx[x['-odread-original-path'] || x.path || ''] = i;
    });

    const urls = components.map(function(spine) {
        const idx = spine.meta['-odread-spine-position'];
        return {
            url:      origin + '/' + spine.meta.path + '?' + params[idx],
            index:    idx,
            duration: spine.meta['audio-duration'] || 0,
            size:     spine.meta['-odread-file-bytes'] || 0,
            path:     spine.meta.path,
        };
    });

    const spine_meta = spineMap.map(function(x) {
        return {
            duration: x['audio-duration'] || 0,
            bitrate:  x['audio-bitrate']  || 0,
        };
    });

    const creator = (bif.map.creator || []).map(function(c) {
        return { name: c.name, role: c.role };
    });

    let chapters = [];
    if (bif.map.nav && bif.map.nav.toc) {
        chapters = bif.map.nav.toc.map(function(ch) {
            const parts    = ch.path.split('#');
            const spinePath = parts[0];
            const offset   = parseFloat((parts[1] || '').replace(/^t=/, '')) || 0;
            return {
                title:  ch.title,
                spine:  pathToIdx.hasOwnProperty(spinePath) ? pathToIdx[spinePath] : 0,
                offset: offset,
            };
        });
    }

    // Cover URL — may be relative (/_d/cover/...) so we always include the origin
    let coverUrl = null;
    try {
        const imgs = bif.root ? bif.root.querySelectorAll('image') : [];
        if (imgs.length) {
            const href = imgs[0].getAttribute('href') || '';
            coverUrl = href.startsWith('http') ? href : (origin + href);
        }
    } catch(_) {}
    // Fallback: standard OverDrive cover endpoint
    if (!coverUrl) {
        coverUrl = origin + '/_d/cover/buid/' + (bif.deviceId || '') + '/big.jpg';
    }

    return {
        title:    bif.map.title.main,
        creator:  creator,
        spine:    spine_meta,
        chapters: chapters,
        coverUrl: coverUrl,
        origin:   origin,
        urls:     urls,
    };
}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_filename(s: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", s).strip()


def _fetch_language_http(detail_url: str, cookies: dict) -> str:
    """Fetch language from an OverDrive detail page via HTTP (SSR, no browser needed)."""
    try:
        r = requests.get(
            detail_url, cookies=cookies, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120"},
        )
        if not r.ok:
            return ""
        # SSR HTML: <div id="languages-panel" ...><ul ...><li>English</li></ul></div>
        m = re.search(r'id="languages-panel"[^>]*>.*?<li[^>]*>\s*(.*?)\s*</li>', r.text, re.DOTALL)
        return m.group(1).strip().lower() if m else ""
    except Exception:
        return ""


def _parse_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() not in ("false", "0", "no", "")


def _get_config():
    from shelfmark.core.settings_registry import load_config_file
    cfg = load_config_file("custom_overdrive_source")

    # New: OVERDRIVE_ACCOUNTS is a list of {library, card, pin} dicts from TableField
    accounts = cfg.get("OVERDRIVE_ACCOUNTS") or []
    accounts = [a for a in accounts if a.get("card") and a.get("pin") and a.get("library")]

    # Backward compat: if no accounts table, fall back to old single-account keys
    if not accounts:
        library = cfg.get("OVERDRIVE_LIBRARY", "").strip()
        card    = cfg.get("OVERDRIVE_CARD",    "").strip()
        pin     = cfg.get("OVERDRIVE_PIN",     "").strip()
        if card and pin:
            accounts = [{"library": library or "multcolib", "card": card, "pin": pin}]

    return {
        "accounts":        accounts,
        "headless":        _parse_bool(cfg.get("OVERDRIVE_HEADLESS", True)),
        "filter_language": cfg.get("OVERDRIVE_FILTER_LANGUAGE", "").strip().lower(),
    }


def _make_browser(pw, headless: bool = True):
    return pw.chromium.launch(
        headless=headless,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    ).new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120",
        viewport={"width": 1280, "height": 800},
    ).new_page()


def _login(page, base_url: str, card: str, pin: str) -> None:
    from playwright.sync_api import TimeoutError as PWTimeout
    page.goto(f"{base_url}/account/ozone/sign-in?forward=/", wait_until="load")
    try:
        page.wait_for_selector('input[name="username"]', timeout=10000).fill(card)
        pwd = page.wait_for_selector('input[name="password"]', timeout=5000)
        pwd.fill(pin)
        pwd.press("Enter")
        # Wait for redirect away from sign-in page (load event, not networkidle)
        page.wait_for_url(lambda url: "sign-in" not in url, timeout=15000)
    except PWTimeout as e:
        raise SourceUnavailableError(f"OverDrive login timed out: {e}") from e
    if "sign-in" in page.url:
        raise SourceUnavailableError("OverDrive login failed — check card and PIN in settings.")


def _get_listen_url(page, base_url: str, media_id: str) -> str:
    """Navigate to the OverDrive download URL and follow redirect to listen player."""
    download_url = f"{base_url}/media/download/audiobook-overdrive/{media_id}"
    page.goto(download_url, wait_until="load", timeout=60000)
    final = page.url
    for _ in range(20):
        if "listen.overdrive.com" in final:
            return final
        time.sleep(0.5)
        final = page.url
    raise RuntimeError(f"Expected redirect to listen.overdrive.com, got: {final}")


def _borrow(page, base_url: str, media_id: str) -> None:
    """Borrow a title if not already borrowed. Verifies via loans page."""
    from playwright.sync_api import TimeoutError as PWTimeout

    # Check loans page first — if the book is already there, skip borrow
    page.goto(f"{base_url}/account/loans", wait_until="domcontentloaded", timeout=60000)
    time.sleep(1)
    if page.query_selector(f'a[href*="audiobook-overdrive/{media_id}"]'):
        return  # already borrowed

    # Go to detail page and click Borrow
    page.goto(f"{base_url}/media/{media_id}", wait_until="domcontentloaded", timeout=60000)
    time.sleep(2)

    btn = page.query_selector(".TitleActionButton")
    if not btn:
        return
    btn_text = btn.inner_text().strip().lower()
    if "borrow" not in btn_text:
        return  # already borrowed or unavailable

    btn.click()
    time.sleep(1.5)

    # Confirm borrow in modal — find any visible button with "borrow" text
    # that is NOT the original button (which is now hidden behind the modal)
    confirmed = page.evaluate("""
        () => {
            const btns = Array.from(document.querySelectorAll('button'));
            const confirm = btns.find(b =>
                b.offsetParent !== null &&
                b.textContent.trim().toLowerCase().includes('borrow') &&
                (b.closest('.reveal-modal, .modal, [role="dialog"]') || b.classList.contains('js-borrow-title'))
            );
            if (confirm) { confirm.click(); return true; }
            return false;
        }
    """)
    if not confirmed:
        # Fallback: any newly visible primary button
        for sel in ['button.primary:visible', '.modal button:visible', 'button[data-confirm]:visible']:
            try:
                fb = page.wait_for_selector(sel, timeout=2000)
                if fb and "borrow" in fb.inner_text().strip().lower():
                    fb.click()
                    break
            except PWTimeout:
                pass

    # Wait for borrow to register on OverDrive's side
    time.sleep(3)


def _place_hold(page, base_url: str, media_id: str) -> None:
    """Place a hold by clicking the Place a Hold button on the title page."""
    page.goto(f"{base_url}/media/{media_id}", wait_until="domcontentloaded", timeout=60000)
    time.sleep(2)
    btn = page.query_selector(".TitleActionButton")
    if not btn or "hold" not in btn.inner_text().strip().lower():
        return
    btn.click()
    time.sleep(2)


def _extract_book_data(page, listen_url: str) -> dict:
    """Navigate to listen player with hook pre-injected, return full book data."""
    page.add_init_script(_INTERCEPT_JS)
    page.goto(listen_url, wait_until="load", timeout=60000)

    # Wait for BIF + params (up to 45s)
    deadline = time.time() + 45
    while time.time() < deadline:
        ready = page.evaluate("() => !!window.__od_bif && !!window.__od_params")
        if ready:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Timed out waiting for Libby player to load book data.")

    data = page.evaluate(_EXTRACT_JS)
    if not data:
        raise RuntimeError("Could not extract book data from Libby player.")
    return data


def _download_parts(data: dict, cookies: dict, out_dir: Path,
                    cancel_flag, progress_callback, status_callback) -> list[Path]:
    """Download all MP3 parts, return sorted list of paths."""
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120"}
    urls = sorted(data["urls"], key=lambda x: x["index"])
    total = len(urls)
    paths = []

    for item in urls:
        if cancel_flag.is_set():
            return []
        num = item["index"] + 1
        path = out_dir / f"Part_{num:03d}.mp3"
        paths.append(path)

        if path.exists() and path.stat().st_size > 10_000:
            progress_callback(num / total * 60)
            continue

        status_callback("downloading", f"Part {num} of {total}")
        with requests.get(item["url"], cookies=cookies, headers=headers,
                          stream=True, timeout=90) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if cancel_flag.is_set():
                        path.unlink(missing_ok=True)
                        return []
                    f.write(chunk)

        progress_callback(num / total * 60)

    return paths


def _build_ffmetadata(data: dict) -> str:
    """Generate ffmetadata chapter file."""
    spine_durations = [s["duration"] for s in data.get("spine", [])]
    offsets = []
    acc = 0.0
    for d in spine_durations:
        offsets.append(acc)
        acc += d
    total_ms = int(acc * 1000)

    def esc(s):
        return re.sub(r'([=;#\\])', r'\\\1', str(s)).replace('\n', ' ')

    chapters = data.get("chapters", [])
    # Deduplicate consecutive same-title chapters
    deduped = []
    for ch in chapters:
        if not deduped or ch["title"] != deduped[-1]["title"]:
            deduped.append(ch)

    lines = [";FFMETADATA1\n"]
    chap_list = []
    for ch in deduped:
        si = min(ch.get("spine", 0), len(offsets) - 1) if offsets else 0
        base = offsets[si] if offsets else 0.0
        start_ms = int((base + ch.get("offset", 0.0)) * 1000)
        chap_list.append((start_ms, esc(ch["title"])))

    for i, (start_ms, title) in enumerate(chap_list):
        end_ms = chap_list[i + 1][0] if i + 1 < len(chap_list) else total_ms
        lines += [
            "[CHAPTER]\n",
            "TIMEBASE=1/1000\n",
            f"START={start_ms}\n",
            f"END={end_ms}\n",
            f"title={title}\n",
        ]
    return "".join(lines)


def _merge_to_m4b(parts: list[Path], data: dict, cover_path: Path | None,
                  out_path: Path, status_callback) -> Path:
    """Concat MP3s → embed chapters → AAC encode → .m4b"""
    tmp_dir = parts[0].parent
    title   = data.get("title", "Unknown")
    authors = [c["name"] for c in data.get("creator", []) if "author" in c.get("role", "").lower()]
    author  = ", ".join(authors) or "Unknown"

    concat_txt   = tmp_dir / "concat.txt"
    chapters_txt = tmp_dir / "chapters.txt"

    with open(concat_txt, "w", encoding="utf-8") as f:
        for p in sorted(parts):
            f.write(f"file '{p.resolve()}'\n")
    chapters_txt.write_text(_build_ffmetadata(data), encoding="utf-8")

    # Single-pass: concat MP3s → AAC encode → M4B with chapters + cover
    # All -i inputs must come before any output options.
    status_callback("processing", f"Encoding {len(parts)} parts to M4B…")
    has_cover = bool(cover_path and cover_path.exists())
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_txt),
        "-f", "ffmetadata", "-i", str(chapters_txt),
    ]
    if has_cover:
        cmd += ["-i", str(cover_path)]
    cmd += [
        "-map_metadata", "1", "-map_chapters", "1",
        "-map", "0:a",
        "-c:a", "aac", "-b:a", "128k",
        "-metadata", f"title={title}",
        "-metadata", f"artist={author}",
        "-f", "ipod",
    ]
    if has_cover:
        cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
    cmd.append(str(out_path))
    subprocess.run(cmd, check=True, capture_output=True)

    return out_path


def _search_account(
    account: dict,
    query: str,
    *,
    headless: bool = True,
    filter_lang: str = "",
    content_type: str = "audiobook",
    display_name: str = "OverDrive / Libby",
) -> list[Release]:
    """Run a full search for one OverDrive account. Returns a list of Release objects."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    library  = account["library"]
    card     = account["card"]
    pin      = account["pin"]
    base_url = f"https://{library}.overdrive.com"

    try:
        with sync_playwright() as pw:
            page = _make_browser(pw, headless=headless)
            _login(page, base_url, card, pin)

            page.goto(
                f"{base_url}/search?query={query.replace(' ', '+')}",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            time.sleep(2)

            try:
                page.wait_for_selector(".js-titleCard", timeout=10000)
            except PWTimeout:
                page.context.browser.close()
                return []

            # Pass 1: scrape all card data from the search results page
            raw_cards = []
            for card_el in page.query_selector_all(".js-titleCard"):
                title_el  = card_el.query_selector('a[href*="/media/"]')
                format_el = card_el.query_selector(".title-format-badge")
                btn_el    = card_el.query_selector(".TitleActionButton")

                if not title_el:
                    continue
                fmt = (format_el.inner_text().strip() if format_el else "").lower()
                if "audio" not in fmt:
                    continue

                title = title_el.inner_text().strip()
                if not title:
                    lines = [ln.strip() for ln in card_el.inner_text().splitlines() if ln.strip()]
                    title = lines[0] if lines else "?"

                href     = title_el.get_attribute("href") or ""
                btn_text = (btn_el.inner_text().strip().lower() if btn_el else "")
                wait_el  = card_el.query_selector(".waitingText, [class*='waitingText']")
                inline_wait = wait_el.inner_text().strip() if wait_el else None

                author_el = (
                    card_el.query_selector(".title-author") or
                    card_el.query_selector(".subtitle-text") or
                    card_el.query_selector("[class*='author' i]") or
                    card_el.query_selector("[class*='Author']")
                )
                author = author_el.inner_text().strip() if author_el else ""
                if not author:
                    author = (card_el.get_attribute("data-author") or
                              card_el.get_attribute("data-creators") or "")
                author = re.sub(r"^[Bb]y\s+", "", author).strip()

                lang_el = (
                    card_el.query_selector("[data-language]") or
                    card_el.query_selector("[class*='language' i]")
                )
                if lang_el:
                    language = (lang_el.get_attribute("data-language") or
                                lang_el.inner_text().strip()).lower()
                else:
                    language = card_el.get_attribute("data-language") or ""
                language = language.lower().strip()

                raw_cards.append({
                    "title": title, "href": href,
                    "btn_text": btn_text, "inline_wait": inline_wait,
                    "author": author, "language": language,
                })

            # Pass 1.5: fetch language for all cards in parallel via HTTP
            browser_cookies = {c["name"]: c["value"] for c in page.context.cookies()}
            cards_needing_lang = [rc for rc in raw_cards if not rc["language"] and rc["href"]]
            if cards_needing_lang:
                urls = [
                    f"{base_url}{rc['href']}" if not rc["href"].startswith("http") else rc["href"]
                    for rc in cards_needing_lang
                ]
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                    langs = list(pool.map(
                        lambda url: _fetch_language_http(url, browser_cookies),
                        urls,
                    ))
                for rc, lang in zip(cards_needing_lang, langs):
                    rc["language"] = lang

            # Pass 2: browser detail-page visit for Hold titles missing wait time
            for rc in raw_cards:
                needs_wait = (
                    "borrow" not in rc["btn_text"] and
                    "loan" not in rc["btn_text"] and
                    "listen" not in rc["btn_text"] and
                    not rc["inline_wait"] and
                    rc["href"]
                )
                if not needs_wait:
                    continue
                detail_url = (
                    f"{base_url}{rc['href']}" if not rc["href"].startswith("http") else rc["href"]
                )
                try:
                    page.goto(detail_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(1)
                    w_el = page.query_selector(".waitingText, [class*='waitingText']")
                    c_el = page.query_selector(".CopiesAvailable, [class*='Copies']")
                    rc["inline_wait"] = (
                        (w_el.inner_text().strip() if w_el else None)
                        or (c_el.inner_text().strip() if c_el else None)
                        or "On hold"
                    )
                except Exception:
                    rc["inline_wait"] = "On hold"

            page.context.browser.close()

        # Pass 3: build Release objects
        _lang_map = {
            "english": "en", "french": "fr", "spanish": "es",
            "german": "de", "portuguese": "pt", "italian": "it",
            "dutch": "nl", "russian": "ru", "chinese": "zh",
            "japanese": "ja", "korean": "ko", "arabic": "ar",
        }
        releases: list[Release] = []
        for rc in raw_cards:
            href     = rc["href"]
            media_id = href.rstrip("/").split("/")[-1]
            info_url = f"{base_url}{href}" if not href.startswith("http") else href
            btn_text = rc["btn_text"]
            raw_lang = rc.get("language", "")
            language = _lang_map.get(raw_lang, raw_lang[:2] if len(raw_lang) > 2 else raw_lang)

            if filter_lang and language and language != filter_lang:
                continue

            if "borrow" in btn_text:
                availability, wait = "Available", None
            elif "loan" in btn_text or "listen" in btn_text:
                availability, wait = "Borrowed", None
            else:
                availability = "Hold"
                wait = rc["inline_wait"] or "On hold"

            releases.append(Release(
                source       = SOURCE_NAME,
                source_id    = media_id,
                title        = rc["title"],
                format       = "m4b",
                language     = language or None,
                size         = wait or availability,
                size_bytes   = None,
                download_url = info_url,
                info_url     = info_url,
                protocol     = ReleaseProtocol.HTTP,
                indexer      = display_name,
                content_type = content_type,
                extra        = {
                    "media_id":     media_id,
                    "base_url":     base_url,
                    "availability": availability,
                    "author":       rc.get("author", ""),
                },
            ))

        return releases

    except SourceUnavailableError:
        raise
    except Exception as exc:
        raise SourceUnavailableError(f"OverDrive search failed: {exc}") from exc


# ── Source ────────────────────────────────────────────────────────────────────

@register_source(SOURCE_NAME)
class OverDriveSource(ReleaseSource):
    name         = SOURCE_NAME
    display_name = "OverDrive / Libby"
    supported_content_types: list[str] = ["audiobook"]  # noqa: RUF012
    can_be_default: bool = True

    def is_available(self) -> bool:
        return bool(_get_config()["accounts"])

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

        cfg = _get_config()
        if not cfg["accounts"]:
            return []

        query = book.search_title or book.title
        query = " ".join(query.split()[:4])
        if book.search_author and not expand_search:
            author_first = (book.search_author.split()[0] if book.search_author else "")
            query = f"{query} {author_first}".strip()

        def search_one(account: dict) -> list[Release]:
            return _search_account(
                account=account,
                query=query,
                headless=cfg["headless"],
                filter_lang=cfg["filter_language"],
                content_type=content_type,
                display_name=self.display_name,
            )

        if len(cfg["accounts"]) == 1:
            return search_one(cfg["accounts"][0])

        # Multiple accounts — search all in parallel, combine results.
        # Deduplicate by (base_url, media_id): same title available from
        # two libraries = two distinct releases (different loans/holds).
        all_releases: list[Release] = []
        seen: set[tuple[str, str]] = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(cfg["accounts"])) as pool:
            futures = {pool.submit(search_one, acc): acc for acc in cfg["accounts"]}
            for future in concurrent.futures.as_completed(futures):
                try:
                    for r in future.result():
                        key = (r.extra.get("base_url", ""), r.extra.get("media_id", ""))
                        if key not in seen:
                            seen.add(key)
                            all_releases.append(r)
                except Exception:
                    pass  # one account failing shouldn't block others

        # Sort: Available first, then borrowed, then holds; within each group by title
        _order = {"Available": 0, "Borrowed": 1, "Hold": 2}
        all_releases.sort(key=lambda r: (
            _order.get(r.extra.get("availability", "Hold"), 2),
            r.title or "",
        ))
        return all_releases

    def get_column_config(self) -> ReleaseColumnConfig:
        return ReleaseColumnConfig(
            columns=[
                ColumnSchema(
                    key="extra.author",
                    label="Author",
                    render_type=ColumnRenderType.TEXT,
                    align=ColumnAlign.LEFT,
                    width="minmax(80px,1fr)",
                ),
                ColumnSchema(
                    key="language",
                    label="Language",
                    render_type=ColumnRenderType.BADGE,
                    align=ColumnAlign.CENTER,
                    width="70px",
                    color_hint=ColumnColorHint(type="map", value="language"),
                    uppercase=True,
                ),
                ColumnSchema(
                    key="format",
                    label="Format",
                    render_type=ColumnRenderType.BADGE,
                    align=ColumnAlign.CENTER,
                    width="55px",
                    color_hint=ColumnColorHint(type="map", value="format"),
                    uppercase=True,
                ),
                ColumnSchema(
                    key="size",
                    label="Status",
                    render_type=ColumnRenderType.TEXT,
                    align=ColumnAlign.LEFT,
                    width="160px",
                ),
            ],
            grid_template="minmax(0,2fr) minmax(80px,1fr) 70px 55px 160px",
            supported_filters=["format", "language"],
        )


# ── Handler ───────────────────────────────────────────────────────────────────

@register_handler(SOURCE_NAME)
class OverDriveHandler(DownloadHandler):
    def download(
        self,
        task: DownloadTask,
        cancel_flag: Event,
        progress_callback: Callable[[float], None],
        status_callback: Callable[[str, str | None], None],
    ) -> str | None:
        from playwright.sync_api import sync_playwright
        from shelfmark.config.env import TMP_DIR

        if cancel_flag.is_set():
            return None

        cfg = _get_config()
        # Parse base_url and media_id from source_url (e.g. https://multcolib.overdrive.com/media/12345)
        source_url = task.source_url or ""
        media_id   = source_url.rstrip("/").split("/")[-1]
        url_parts  = source_url.split("/media/")
        base_url   = url_parts[0] if len(url_parts) == 2 else ""

        # Find the account whose library subdomain matches the source URL
        library_subdomain = base_url.split("//")[-1].split(".")[0] if base_url else ""
        account = next(
            (a for a in cfg["accounts"] if a.get("library", "").lower() == library_subdomain.lower()),
            cfg["accounts"][0] if cfg["accounts"] else None,
        )
        if not account:
            raise SourceUnavailableError("No OverDrive account configured.")
        card = account["card"]
        pin  = account["pin"]
        if not base_url:
            base_url = f"https://{account['library']}.overdrive.com"

        title = task.title or f"overdrive_{media_id}"

        out_dir = TMP_DIR / f"overdrive_{task.task_id}"
        out_dir.mkdir(parents=True, exist_ok=True)

        out_m4b = TMP_DIR / f"{_safe_filename(title)}.m4b"

        status_callback("resolving", "Logging in to OverDrive…")

        try:
            with sync_playwright() as pw:
                page = _make_browser(pw, headless=cfg["headless"])
                _login(page, base_url, card, pin)

                if cancel_flag.is_set():
                    return None

                # Borrow if needed, then verify the book is actually in loans
                status_callback("resolving", "Checking loan status…")
                _borrow(page, base_url, media_id)

                if cancel_flag.is_set():
                    return None

                # Confirm the book is now borrowed before trying to stream it.
                # If it's not in loans it's on hold — place the hold and surface
                # a clear status message (not an error/crash).
                page.goto(f"{base_url}/account/loans", wait_until="domcontentloaded", timeout=30000)
                time.sleep(1)
                if not page.query_selector(f'a[href*="audiobook-overdrive/{media_id}"]'):
                    _place_hold(page, base_url, media_id)
                    page.context.browser.close()
                    status_callback("error", "Hold placed — come back to download once OverDrive notifies you it's available.")
                    return None

                if cancel_flag.is_set():
                    return None

                # Get listen URL
                status_callback("resolving", "Opening listen player…")
                listen_url = _get_listen_url(page, base_url, media_id)

                if cancel_flag.is_set():
                    return None

                # Extract all signed URLs + metadata from the player
                status_callback("resolving", "Extracting chapter URLs…")
                data = _extract_book_data(page, listen_url)
                progress_callback(5)

                if cancel_flag.is_set():
                    return None

                # Grab cookies from the browser for authenticated downloads
                cookies = {c["name"]: c["value"] for c in page.context.cookies()}

                # Download cover art — URL is already absolute (fixed in _EXTRACT_JS)
                cover_path = None
                if data.get("coverUrl"):
                    try:
                        r = requests.get(data["coverUrl"], cookies=cookies, timeout=30)
                        if r.ok and r.headers.get("content-type", "").startswith("image"):
                            cover_path = out_dir / "cover.jpg"
                            cover_path.write_bytes(r.content)
                    except Exception:
                        pass

                # Return the book now — signed URLs are self-authenticating,
                # no active loan needed for the actual MP3 downloads
                status_callback("resolving", "Returning book…")
                try:
                    page.goto(f"{base_url}/account/loans", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(1)
                    for card in page.query_selector_all(".account-loans-item"):
                        link = card.query_selector(f'a[href*="{media_id}"]')
                        if not link:
                            continue
                        # Found the right loan card — attempt return
                        ret_btn = card.query_selector(".js-show-return-modal")
                        if ret_btn:
                            ret_btn.click()
                            time.sleep(1)
                            confirm = page.query_selector(".js-return-title")
                            if confirm:
                                confirm.click()
                                time.sleep(1)
                        break  # always stop after matching card
                except Exception:
                    pass  # non-fatal — download proceeds regardless

                page.context.browser.close()

            if cancel_flag.is_set():
                return None

            # Download all MP3 parts
            parts = _download_parts(data, cookies, out_dir,
                                    cancel_flag, progress_callback, status_callback)
            if not parts:
                return None  # cancelled

            progress_callback(65)

            # Merge → M4B
            _merge_to_m4b(parts, data, cover_path, out_m4b, status_callback)
            progress_callback(100)

            return str(out_m4b)

        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"ffmpeg failed: {exc.stderr.decode()[-2000:]}") from exc
        except Exception as exc:
            raise RuntimeError(f"OverDrive download failed: {exc}") from exc

    def cancel(self, task_id: str) -> bool:
        return True


# ── Settings ──────────────────────────────────────────────────────────────────

def get_settings_fields() -> list:
    from shelfmark.core.settings_registry import (
        CheckboxField,
        HeadingField,
        TableField,
        TextField,
    )
    return [
        HeadingField(
            key="OVERDRIVE_HEADING",
            title="OverDrive / Libby",
            description=(
                "Download borrowed audiobooks from your library's OverDrive catalog. "
                "Searches, borrows, and downloads as M4B with full chapters. "
                "Add one row per library card — all cards are searched in parallel. "
                "Requires: pip install playwright requests && playwright install chromium && ffmpeg in PATH."
            ),
        ),
        TableField(
            key="OVERDRIVE_ACCOUNTS",
            label="Library Cards",
            description=(
                "Add one row per library card. All cards are searched in parallel and results are combined. "
                "Library Subdomain is the part before '.overdrive.com' — e.g. 'multcolib' for multcolib.overdrive.com."
            ),
            columns=[
                {
                    "key": "library",
                    "label": "Library Subdomain",
                    "type": "text",
                    "placeholder": "e.g. multcolib",
                },
                {
                    "key": "card",
                    "label": "Card Number",
                    "type": "text",
                    "placeholder": "e.g. 998774",
                },
                {
                    "key": "pin",
                    "label": "PIN / Password",
                    "type": "password",
                    "placeholder": "••••",
                },
            ],
            add_label="Add Library Card",
            empty_message="No library cards configured.",
            default=[],
        ),
        CheckboxField(
            key="OVERDRIVE_HEADLESS",
            label="Run browser headlessly",
            description="Hide the browser window during search and download. Recommended.",
            default=True,
        ),
        TextField(
            key="OVERDRIVE_FILTER_LANGUAGE",
            label="Language filter (optional)",
            description=(
                "Leave blank to show all languages (recommended). "
                "Set a 2-letter ISO code (e.g. 'en', 'fr', 'es') to hide results in other languages."
            ),
            default="",
            placeholder="e.g. en",
        ),
    ]
