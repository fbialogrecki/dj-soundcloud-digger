# SoundCloud Purchase Links Scraper

This repository contains a Python tool that automates the process of extracting download or store links from public SoundCloud playlists. Save the playlist page as HTML, let the tool analyse every track, and later open the discovered Bandcamp / Beatport / JunoDownload / Hypeedit links in your preferred browser.

## Features

- **Offline-friendly workflow:** Works entirely on HTML files saved from your browser – no live Selenium scraping required.
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

### 1. Save the playlist from SoundCloud

1. Open the playlist page in your browser (Chrome / Edge / Firefox / …).
2. Scroll all the way down until every track is visible (no “Load more” button).
3. Use `Ctrl+S` (`Cmd+S` on macOS) and choose **Webpage, Complete (HTML)**. Pick a descriptive name such as `SoundCloud_playlist.html`.
4. Verify that the browser created both the HTML file *and* the companion folder with resources. That confirms the save was complete.
5. Move the HTML file (and its resources folder) into your project directory or note the absolute path.

### 2. Run the `dig` command on the saved HTML

```bash
python dj-soundcloud-digger.py dig SoundCloud_playlist.html \
  --export json \
  --output soundcloud_links.json
```

- `--export {json,yaml,none}` – choose export format (default: `json`).
- `--delay` – delay between track requests (default: `0.5s`).
- `--timeout` – HTTP timeout per track (default: `20s`).
- `--max-tracks` – process only the first N tracks (useful for testing).

The command shows a progress bar while each track is fetched, generates the summary (unless `--export none` is specified) and then exits. The browser is **not** opened at this stage – that happens in the next step.

### 3. Open links from an existing JSON / YAML summary

```bash
python dj-soundcloud-digger.py open soundcloud_links.json \
  --browser firefox \
  --skip 10 \
  --limit 20
```

- `--category {hypeddit,bandcamp,beatport,junodownload,others,all}` – category to open. If omitted (and `--no-open` is not set) the tool will interactively ask whether to open all categories or a specific one.
- `--skip` / `--limit` – skip or limit the number of links to open.
- `--no-open` – only display the summary without opening links.
- `--browser {default,chrome,firefox,edge,safari,opera}` – browser controller to use (default: system browser). If the requested browser is not available, the script falls back to the default.

### Log level

Add `--log-level DEBUG` for verbose output (network retries, per-track details), or `--log-level WARNING` to silence informational logs.

## License

This project is licensed under the Apache License. See the LICENSE file for details.
