# LitFinder Custom Sources

Extra search sources for [LitFinder](https://github.com/NemesisHubris/litfinder). Drop a file in your config folder and restart — that's it.

---

## Available Sources

| File | Finds | Format |
|---|---|---|
| `project_gutenberg.py` | ~75,000 free public-domain books | EPUB |
| `librivox.py` | Free public-domain audiobooks | M4B / MP3 / Torrent |
| `libby_source.py` | Your borrowed Libby / OverDrive audiobooks | M4B |

---

## Installation

1. Go to your LitFinder config folder (the folder you mounted as `/config` in Docker).

2. Create a `custom_sources` folder inside it if it doesn't exist.

3. Download the `.py` file(s) you want from this repo and put them in that folder.

4. Restart LitFinder.

The sources will show up in **Settings → Release Sources → Custom Sources** where you can configure them.

---

## Notes

**Project Gutenberg** — You may see multiple results for the same book. That's normal — Project Gutenberg has separate entries for different editions (e.g. with or without images, different base texts). Use the *Max Editions* setting to control how many show up.

**LibriVox** — Downloads the pre-built M4B by default (single file with chapters, works great with Audiobookshelf). You can switch to MP3 ZIP or Torrent in settings. Torrent requires a download client configured in LitFinder.

**Libby** — Requires a Libby / OverDrive account. Enter your library's OverDrive website ID, library card number, and PIN in settings — no browser setup or manual login needed. Search results show the full OverDrive catalog with availability (borrowed, available to borrow, or holdable). Downloads as a single M4B with chapter markers — ffmpeg and Playwright must be installed (see settings for instructions).
