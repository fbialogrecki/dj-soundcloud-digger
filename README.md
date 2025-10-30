# SoundCloud Purchase Links Scraper

This repository contains a Python tool that automates the process of extracting download or store links from public SoundCloud playlists. Save the playlist page as HTML, let the tool analyse every track, and instantly open the discovered Bandcamp / Beatport / JunoDownload / Hypeedit links in your preferred browser.

## Features

- **Offline-friendly:** Works on HTML pages saved from any browser – ideal when Selenium is blocked or unreliable.
- **Smart categorisation:** Classifies store links into Bandcamp, Beatport, JunoDownload, Hypeedit, or `others` when no known store is detected.
- **Progress visibility:** A terminal progress bar keeps you informed while tracks are analysed, with retries and back-off for network hiccups.
- **Flexible output:** Export results to JSON or YAML, or reuse an existing JSON file without re-scraping.
- **One-click opening:** Instantly open store links (per category) in Chrome, Firefox, Edge, Opera, Safari or the system default browser. Opening all at once remains the default behaviour, with a flag to disable it.

## Requirements

- Python 3.9+
- [Requests](https://docs.python-requests.org/)
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)
- [PyYAML](https://pyyaml.org/) *(optional – only for YAML export)*
- [tqdm](https://tqdm.github.io/) for the terminal progress bar

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/yourusername/soundcloud-purchase-links-scraper.git
   cd soundcloud-purchase-links-scraper

   ```

2. Install the required dependencies using pip (a `requirements.txt` is provided):

   ```bash
   pip install -r requirements.txt

   ```

## Usage

### 1. Save the playlist HTML

1. Open the playlist in your browser.
2. Scroll to the very bottom so that every track is loaded.
3. Save the page as **Complete webpage (HTML)**, e.g. `SoundCloud_playlist.html`.

### 2. Scrape the saved HTML

```bash
python dj-soundcloud-digger.py scrape SoundCloud_playlist.html \
  --export json \
  --output soundcloud_links.json \
  --open-category all \
  --browser chrome
```

- `--export {json,yaml,none}` – choose export format (default: `json`).
- `--open-category {hypeddit,bandcamp,beatport,junodownload,others,all}` – which category to open afterwards (default: `all`).
- `--no-open` – skip opening links in the browser.
- `--browser {default,chrome,firefox,edge,safari,opera}` – browser controller to use (default: system default).
- `--delay` – delay between track requests (default: `0.5s`).
- `--timeout` – HTTP timeout per track (default: `20s`).
- `--max-tracks` – process only the first N tracks (useful for testing).

The command prints a progress bar while fetching each track, saves the summary (if `--export` is enabled) and opens the requested links when finished.

### 3. Re-open links from JSON

```bash
python dj-soundcloud-digger.py open soundcloud_links.json \
  --category bandcamp \
  --browser firefox \
  --skip 10 \
  --limit 20
```

- `--category` – which category to open (default: `all`).
- `--skip` / `--limit` – skip or limit the number of links to open.
- `--no-open` – only display the summary without opening links.

### Log level

Add `--log-level DEBUG` for verbose output (network retries, per-track details), or `--log-level WARNING` to silence informational logs.

## License

This project is licensed under the Apache License. See the LICENSE file for details.
