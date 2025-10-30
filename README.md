# SoundCloud Purchase Links Scraper

This repository contains a Python tool that automates the process of extracting download or store links from public SoundCloud playlists. The script dynamically loads the entire playlist using Selenium, then parses the page to extract individual track URLs. For each track, it retrieves the track page and searches for download or purchase links based on keywords (such as "download", "buy", "purchase", "premiere", or "kup").

## Features

- **Dynamic Loading:** Automatically scrolls through a SoundCloud playlist to load all tracks.
- **Track Extraction:** Uses BeautifulSoup to extract track URLs from the fully loaded playlist page.
- **Link Categorisation:** Fetches each track's page via requests and scans for download/store links, assigning them to categories such as Bandcamp, Beatport, JunoDownload, HypeEdit, SoundCloud Download, Other, or SoundCloud Only (no external links).
- **Flexible Exports:** Optionally export categorized results to JSON or YAML for later automation, or keep them in memory for immediate processing.

## Requirements

- Python 3.x
- [Selenium](https://www.selenium.dev/) (with a compatible WebDriver such as ChromeDriver)
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/bs4/doc/)
- [Requests](https://docs.python-requests.org/)
- [PyYAML](https://pyyaml.org/) *(optional, required only for YAML export)*

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

3. Ensure your WebDriver (e.g., ChromeDriver) is installed and properly set up in your PATH. For YAML export support the `pyyaml` package is included in `requirements.txt`.

## Usage

Run the script using Python:

```bash
python dj-soundcloud-digger.py [PLAYLIST_URL] [options]
```

or run it as bash script (if you work on a Linux operating system)

```bash
./dj-soundcloud-digger.py
```

When prompted (or via CLI flags), enter the URL of the public SoundCloud playlist. The script will process the playlist, print categorized results, and optionally produce an export file.

### Command-line options

```bash
python dj-soundcloud-digger.py \
  "https://soundcloud.com/your-playlist" \
  --export json \
  --output my_links.json \
  --pause-time 2 \
  --initial-wait 3 \
  --max-scrolls 20 \
  --show-browser
```

- `--export {json,yaml,none}` – choose export format. Omit to answer interactively.
- `--output PATH` – custom export file path (`soundcloud_links.<ext>` by default).
- `--pause-time` – delay between scrolls when loading the playlist (seconds).
- `--initial-wait` – delay after opening playlist before scrolling (seconds).
- `--max-scrolls` – limit the number of scroll attempts (unlimited if omitted).
- `--show-browser` – open Chrome in headed mode for debugging.

Tracks with only the SoundCloud page available are listed under the `soundcloud_only` category for manual follow-up.

## License

This project is licensed under the Apache License. See the LICENSE file for details.
