#!/usr/bin/env python3

import argparse
import json
import logging
import re
import time
import webbrowser
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

DOWNLOAD_KEYWORDS = {"download", "free download", "free d/l"}
LINK_KEYWORDS = DOWNLOAD_KEYWORDS | {"buy", "purchase", "premiere", "kup"}
STORE_DOMAINS = {
    "bandcamp": {"bandcamp.com"},
    "beatport": {"beatport.com"},
    "junodownload": {"junodownload.com", "juno.co.uk"},
    "hypeddit": {"hypeddit.com", "hypd.it"},
}
BROWSER_CHOICES = ["default", "chrome", "firefox", "edge", "safari", "opera"]
BROWSER_ALIASES = {
    "chrome": "chrome",
    "firefox": "firefox",
    "edge": "edge",
    "safari": "safari",
    "opera": "opera",
}
LOGGER = logging.getLogger(__name__)
CATEGORY_NAMES = [
    "hypeddit",
    "bandcamp",
    "beatport",
    "junodownload",
    "others",
]
RESERVED_TRACK_SLUGS = {
    "sets",
    "albums",
    "tracks",
    "followers",
    "following",
    "likes",
    "reposts",
    "library",
    "popular-tracks",
    "groups",
    "comments",
    "events",
}
TRACK_URL_PATTERN = re.compile(
    r"^https://soundcloud.com/([^/]+)/([^/?#]+)(?:[/?#]|$)", re.IGNORECASE
)
RESERVED_FIRST_SEGMENTS = {
    "about",
    "contributors",
    "discover",
    "popular",
    "charts",
    "company",
    "jobs",
    "press",
    "legal",
    "advertisers",
    "terms-of-use",
    "privacy",
    "pages",
    "stream",
    "stations",
    "getstarted",
    "the-upload",
    "you",
}
RESERVED_SECOND_SEGMENTS = {
    "sets",
    "albums",
    "tracks",
    "followers",
    "following",
    "library",
    "likes",
    "comments",
    "reposts",
    "popular-tracks",
}

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}


def create_requests_session(max_retries: int = 5, backoff_factor: float = 0.5) -> requests.Session:
    """Return a requests session with retry/backoff configured."""

    retry_strategy = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        status=max_retries,
        allowed_methods=["HEAD", "GET", "OPTIONS"],
        status_forcelist=[429, 500, 502, 503, 504],
        backoff_factor=backoff_factor,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)

    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


@dataclass
class LinkRecord:
    category: str
    title: str
    track_url: str
    link_url: str
    link_text: str


LOG_LEVELS = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]
CATEGORY_CHOICES = CATEGORY_NAMES + ["all"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract download and store links from a SoundCloud playlist or open previously exported links."
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=LOG_LEVELS,
        help="Logging verbosity (default: INFO)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Dig command
    dig = subparsers.add_parser(
        "dig",
        help="Analyse a saved SoundCloud playlist HTML file and export the results.",
    )
    dig.add_argument("html_file", type=Path, help="Path to the saved SoundCloud playlist HTML file")
    dig.add_argument(
        "--export",
        choices=["json", "yaml", "none"],
        default="json",
        help="Export format for categorized links (default: json)",
    )
    dig.add_argument(
        "--output",
        type=Path,
        help="Optional path for the export file. Defaults to soundcloud_links.<ext>",
    )
    dig.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay between track requests in seconds (default: 0.5)",
    )
    dig.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP request timeout in seconds (default: 20)",
    )
    dig.add_argument(
        "--max-tracks",
        type=int,
        help="Optional limit on number of tracks to process (useful for testing)",
    )

    # Open command
    open_cmd = subparsers.add_parser(
        "open",
        help="Open links from a previously exported JSON summary.",
    )
    open_cmd.add_argument("summary_file", type=Path, help="Path to the JSON summary file")
    open_cmd.add_argument(
        "--category",
        choices=CATEGORY_CHOICES,
        help="Category to open (prompted if omitted)",
    )
    open_cmd.add_argument(
        "--browser",
        choices=BROWSER_CHOICES,
        default="default",
        help="Browser to use when opening links (default: system default)",
    )
    open_cmd.add_argument(
        "--no-open",
        action="store_true",
        help="Only display summary without opening any links",
    )
    open_cmd.add_argument(
        "--skip",
        type=int,
        default=0,
        help="Number of links to skip before opening (default: 0)",
    )
    open_cmd.add_argument(
        "--limit",
        type=int,
        help="Maximum number of links to open",
    )

    return parser


def parse_cli_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def prompt_category_selection() -> str:
    prompt = (
        "Open which category? Enter one of: "
        + ", ".join(CATEGORY_CHOICES)
        + " (default: all): "
    )
    while True:
        choice = input(prompt).strip().lower()
        if not choice:
            return "all"
        for option in CATEGORY_CHOICES:
            if option.lower() == choice:
                return option
        print("Please choose a valid category name.")


def load_json_file(json_file_path: str) -> Dict[str, List[Dict[str, str]]]:
    """Load summary data from existing JSON file."""
    path = Path(json_file_path)
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {json_file_path}")
    
    print(f"Loading JSON file: {path}")
    
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        
        # Validate structure
        if not isinstance(data, dict):
            raise ValueError("JSON file must contain a dictionary")
        
        # Validate that values are lists
        for category, items in data.items():
            if not isinstance(items, list):
                raise ValueError(f"Category '{category}' must contain a list")
            # Validate item structure
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError(f"Items in category '{category}' must be dictionaries")
                if "track_url" not in item:
                    raise ValueError(f"Items in category '{category}' must have 'track_url' field")
        
        print(f"Loaded JSON with categories: {', '.join(data.keys())}")
        total_items = sum(len(items) for items in data.values())
        print(f"Total items: {total_items}")
        return data
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON file: {e}")
    except Exception as e:
        raise Exception(f"Error loading JSON file: {e}")


def load_tracks_from_html_file(html_file_path: Path) -> Tuple[List[str], Optional[int]]:
    """Load track URLs from a saved HTML file."""
    path = Path(html_file_path)
    if not path.exists():
        raise FileNotFoundError(f"HTML file not found: {html_file_path}")

    print(f"Loading HTML file: {path}")

    try:
        html_content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        html_content = path.read_text(encoding="latin-1")

    print("Parsing HTML content...")
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Extract track links from HTML
    track_links = parse_track_links_from_html(html_content)
    
    # Try to extract from hydration data if present in HTML
    hydration_links = set()
    declared_count = None
    
    # Look for hydration data in script tags
    script_tags = soup.find_all('script')
    for script in script_tags:
        if script.string:
            # Try to find window.__sc_hydration or similar
            script_content = script.string
            if '__sc_hydration' in script_content or 'sc_hydration' in script_content:
                try:
                    # Try to extract JSON data
                    import json
                    # Look for JSON arrays/objects in the script
                    # This is a simple approach - might need refinement
                    if 'window.__sc_hydration' in script_content:
                        # Try to extract the array
                        start_idx = script_content.find('window.__sc_hydration')
                        if start_idx != -1:
                            # Try to find array start
                            array_start = script_content.find('[', start_idx)
                            if array_start != -1:
                                # Try to extract JSON (simplified)
                                try:
                                    # Find the closing bracket
                                    bracket_count = 0
                                    end_idx = array_start
                                    for i in range(array_start, min(array_start + 50000, len(script_content))):
                                        if script_content[i] == '[':
                                            bracket_count += 1
                                        elif script_content[i] == ']':
                                            bracket_count -= 1
                                            if bracket_count == 0:
                                                end_idx = i + 1
                                                break
                                    
                                    json_str = script_content[array_start:end_idx]
                                    hydration_data = json.loads(json_str)
                                    links, declared = extract_links_from_hydration_data(hydration_data)
                                    hydration_links.update(links)
                                    if declared_count is None:
                                        declared_count = declared
                                except Exception:
                                    pass
                except Exception:
                    pass
    
    # Combine both sources
    all_links = sorted(set(track_links) | hydration_links)
    
    # Extract declared count from HTML metadata
    if declared_count is None:
        declared_count = extract_declared_track_count(html_content)
    
    print(f"Found {len(all_links)} track links in HTML file")
    if declared_count:
        print(f"Playlist declares {declared_count} tracks")
    
    return all_links, declared_count


def clean_track_url(url: str) -> str:
    parsed = urlparse(url)
    cleaned_query = ""
    if parsed.query:
        params = [p for p in parsed.query.split("&") if not p.startswith("in=")]
        if params:
            cleaned_query = "?" + "&".join(params)
    cleaned = parsed._replace(query=cleaned_query, fragment="")
    return cleaned.geturl().rstrip("?")


def is_reserved_path(path_segments: List[str]) -> bool:
    if not path_segments:
        return True
    first_segment = path_segments[0].lower()
    if first_segment in RESERVED_FIRST_SEGMENTS:
        return True
    if len(path_segments) >= 2:
        second_segment = path_segments[1].lower()
        if second_segment in RESERVED_SECOND_SEGMENTS:
            return True
    if first_segment.startswith("pages") or first_segment in {"charts", "company", "getstarted"}:
        return True
    return False


def parse_track_links_from_html(html: str) -> Set[str]:
    links: Set[str] = set()
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if href.startswith("/"):
            href = urljoin("https://soundcloud.com", href)
        match = TRACK_URL_PATTERN.match(href)
        if not match:
            continue
        slug = match.group(2).lower()
        if slug in RESERVED_TRACK_SLUGS:
            continue
        cleaned = clean_track_url(href)
        parsed = urlparse(cleaned)
        segments = [seg for seg in parsed.path.split("/") if seg]
        if len(segments) < 2:
            continue
        if is_reserved_path(segments):
            continue
        links.add(cleaned)
    return links


def extract_declared_track_count(html: str) -> Optional[int]:
    soup = BeautifulSoup(html, "html.parser")

    meta = soup.find("meta", attrs={"itemprop": "numTracks"})
    if meta:
        content = meta.get("content") or meta.get("value")
        if content:
            try:
                return int(content)
            except (TypeError, ValueError):
                pass

    text = soup.get_text(" ", strip=True)
    match = re.search(r"Contains tracks\s*(\d+)", text, flags=re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            pass

    inline_match = re.search(r"\b(\d{1,4})\s+tracks?\b", text, flags=re.IGNORECASE)
    if inline_match:
        try:
            return int(inline_match.group(1))
        except (TypeError, ValueError):
            pass

    return None




def extract_links_from_hydration_data(dataset: Any) -> Tuple[Set[str], Optional[int]]:
    links: Set[str] = set()
    declared: Optional[int] = None

    if not isinstance(dataset, list):
        return links, declared

    for entry in dataset:
        if not isinstance(entry, dict):
            continue
        if entry.get("hydratable") != "playlist":
            continue

        data = entry.get("data", {})
        track_count = data.get("track_count")
        if track_count is not None:
            try:
                declared = declared or int(track_count)
            except (TypeError, ValueError):
                pass

        tracks = data.get("tracks", [])
        if isinstance(tracks, list) and declared is None:
            declared = len(tracks)

        for track in tracks:
            if not isinstance(track, dict):
                continue
            permalink = track.get("permalink_url") or track.get("permalink")
            if not permalink:
                continue
            links.add(clean_track_url(permalink))

    return links, declared


def extract_title(soup: BeautifulSoup) -> str:
    if soup.title and soup.title.get_text():
        title = soup.title.get_text().strip()
        # Usuń różne warianty sufixów SoundCloud
        for suffix in [" | SoundCloud", " | Listen online for free on SoundCloud"]:
            if suffix in title:
                title = title.split(suffix)[0].strip()
                break
        return title
    return "Unknown title"


def normalize_link(track_url: str, href: str) -> str:
    if href.startswith("//"):
        parsed = urlparse(track_url)
        return f"{parsed.scheme}:{href}"
    if href.startswith("/"):
        parsed = urlparse(track_url)
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return urljoin(track_url, href)


def analyze_links(track_url: str, soup: BeautifulSoup) -> Dict[str, List[LinkRecord]]:
    categories: Dict[str, List[LinkRecord]] = defaultdict(list)
    known_store_found = False  # Czy znaleziono link do znanego sklepu
    unknown_store_links = []  # Linki do nieznanych sklepów (z keyword ale nie pasują do znanych)

    for a in soup.select("a[href]"):
        raw_href = a["href"].strip()
        if not raw_href:
            continue
        link_url = normalize_link(track_url, raw_href)
        text = a.get_text(strip=True)
        text_lower = text.lower()

        # Sprawdź czy to link do sklepu/download
        if any(keyword in text_lower for keyword in LINK_KEYWORDS):
            domain = urlparse(link_url).netloc.lower()
            matched_category = None
            for category, domains in STORE_DOMAINS.items():
                if any(target in domain for target in domains):
                    matched_category = category
                    known_store_found = True
                    break
            
            if matched_category:
                # Znaleziono link do znanego sklepu - dodaj do odpowiedniej kategorii
                categories[matched_category].append(
                    LinkRecord(matched_category, "", track_url, link_url, text)
                )
            else:
                # Link z keyword, ale nie pasuje do żadnego znanego sklepu
                # Zapisz go na później - dodamy do "others" tylko jeśli nie znaleziono znanego sklepu
                unknown_store_links.append((link_url, text))

    # Dodaj do "others" TYLKO jeśli nie znaleziono żadnego linku do znanego sklepu
    if not known_store_found:
        if unknown_store_links:
            # Znaleziono linki do nieznanych sklepów - dodaj je do "others"
            for link_url, text in unknown_store_links:
                categories["others"].append(
                    LinkRecord("others", "", track_url, link_url, text)
                )
        else:
            # Nie znaleziono żadnych linków do sklepów
            categories["others"].append(
                LinkRecord("others", "", track_url, track_url, "No store link found")
            )
    
    return categories


def fetch_track_page(
    track_url: str,
    session: requests.Session,
    timeout: float,
) -> Optional[BeautifulSoup]:
    """Retrieve a track page with retry-aware session and improved error handling."""

    try:
        response = session.get(track_url, timeout=timeout)
    except requests.RequestException as exc:
        logging.getLogger(__name__).warning("Request error for %s: %s", track_url, exc)
        return None

    if response.status_code >= 400:
        logging.getLogger(__name__).warning(
            "Could not retrieve track page (%s): %s", response.status_code, track_url
        )
        return None

    return BeautifulSoup(response.text, "html.parser")


def summarize_categories(categorized: Dict[str, List[LinkRecord]]) -> Dict[str, List[Dict[str, str]]]:
    summary = {category: [] for category in CATEGORY_NAMES}
    
    # Najpierw zbierz wszystkie utwory z kategorii innych niż "others"
    tracks_in_other_categories = set()
    for category, records in categorized.items():
        if category != "others" and category != "other":
            for record in records:
                tracks_in_other_categories.add(record.track_url)
    
    # Teraz dodaj do summary, pomijając utwory z "others" które są już w innych kategoriach
    for category, records in categorized.items():
        # Mapuj starą nazwę "other" na "others" jeśli potrzeba
        output_category = "others" if category == "other" else category
        if output_category not in CATEGORY_NAMES:
            output_category = "others"
        
        summary.setdefault(output_category, [])
        for record in records:
            # Jeśli to kategoria "others" i utwór jest już w innej kategorii, pomiń
            if output_category == "others" and record.track_url in tracks_in_other_categories:
                continue
            
            summary[output_category].append(
                {
                    "title": record.title,
                    "track_url": record.track_url,
                    "shop_link": record.link_url,  # Zmieniono z link_url na shop_link
                }
            )
    return summary


def log_summary(summary: Dict[str, List[Dict[str, str]]]) -> None:
    LOGGER.info("Summary:")
    for category in CATEGORY_NAMES:
        count = len(summary.get(category, []))
        LOGGER.info("  %s: %s", category, count)


def resolve_browser_controller(browser_name: str) -> webbrowser.BaseBrowser:
    target = BROWSER_ALIASES.get(browser_name, browser_name)
    try:
        return webbrowser.get(target if browser_name != "default" else None)
    except webbrowser.Error as exc:
        LOGGER.warning(
            "Could not resolve browser '%s' (%s). Falling back to system default.",
            browser_name,
            exc,
        )
        return webbrowser.get()


def open_links_in_browser(
    summary: Dict[str, List[Dict[str, str]]],
    category: str,
    *,
    browser: str = "default",
    skip: int = 0,
    limit: Optional[int] = None,
    disable_open: bool = False,
) -> None:
    """Open links in the requested browser controller with optional slicing."""

    if disable_open:
        LOGGER.info("Opening links skipped (--no-open).")
        return

    categories_to_open = CATEGORY_NAMES if category == "all" else [category]

    # Flatten records preserving category order
    records: List[Tuple[str, Dict[str, str]]] = []
    for cat in categories_to_open:
        items = summary.get(cat, [])
        if not items:
            continue
        records.extend((cat, item) for item in items)

    if not records:
        LOGGER.info("No links found for the requested category '%s'.", category)
        return

    if skip:
        records = records[skip:]
    if limit is not None:
        records = records[:limit]

    if not records:
        LOGGER.info("No links left to open after applying skip/limit filters.")
        return

    controller = resolve_browser_controller(browser)

    total_opened = 0
    shop_links_opened = 0
    track_links_opened = 0

    for cat, item in records:
        shop_link = (item.get("shop_link") or "").strip()
        track_url = (item.get("track_url") or "").strip()

        if shop_link and shop_link != track_url:
            link_to_open = shop_link
            shop_links_opened += 1
        else:
            if not track_url:
                LOGGER.warning(
                    "Skipping item without usable link in category '%s': %s",
                    cat,
                    item.get("title", "Unknown"),
                )
                continue
            link_to_open = track_url
            track_links_opened += 1

        try:
            controller.open_new_tab(link_to_open)
            total_opened += 1
            time.sleep(0.1)
        except Exception as exc:
            LOGGER.error("Failed to open %s: %s", link_to_open, exc)

    LOGGER.info(
        "Opened %s links in browser '%s' (%s shop, %s track fallbacks).",
        total_opened,
        browser,
        shop_links_opened,
        track_links_opened,
    )


def export_results(summary: Dict[str, List[Dict[str, str]]], export_format: str, output_path: Optional[str]) -> None:
    if export_format == "none":
        return

    if output_path:
        filename = Path(output_path)
    else:
        extension = "json" if export_format == "json" else "yaml"
        filename = Path(f"soundcloud_links.{extension}")

    filename.parent.mkdir(parents=True, exist_ok=True)

    if export_format == "json":
        with filename.open("w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        LOGGER.info("Saved categorized links to %s", filename)
        return

    try:
        import yaml  # type: ignore

        with filename.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(summary, handle, sort_keys=False, allow_unicode=True)
        LOGGER.info("Saved categorized links to %s", filename)
    except ModuleNotFoundError:
        LOGGER.error("PyYAML not installed. Install via 'pip install pyyaml' to export YAML.")


def collect_track_data(
    track_links: Iterable[str],
    session: requests.Session,
    delay: float,
    timeout: float,
) -> Dict[str, List[LinkRecord]]:
    categorized: Dict[str, List[LinkRecord]] = defaultdict(list)
    track_list = list(track_links)
    if not track_list:
        return categorized

    progress = tqdm(track_list, desc="Fetching tracks", unit="track")
    for track_url in progress:
        soup = fetch_track_page(track_url, session=session, timeout=timeout)
        if soup is None:
            categorized["others"].append(
                LinkRecord("others", "Unknown title", track_url, track_url, "Could not fetch track page")
            )
            tqdm.write(f"{track_url} -> Error: Could not fetch")
            if delay > 0:
                time.sleep(delay)
            continue

        title = extract_title(soup)
        per_track = analyze_links(track_url, soup)
        if not per_track:
            per_track = {
                "others": [
                    LinkRecord("others", title, track_url, track_url, "No links found")
                ]
            }

        for category, records in per_track.items():
            for record in records:
                record.title = title
            categorized[category].extend(records)

        store_categories = [
            key for key in per_track if key in STORE_DOMAINS or key == "others"
        ]
        if store_categories:
            stores = ", ".join(sorted(store_categories))
            tqdm.write(f"{track_url} -> Store links: {stores}")
        else:
            tqdm.write(f"{track_url} -> No store link")

        if delay > 0:
            time.sleep(delay)

    progress.close()
    return categorized


def handle_dig(args: argparse.Namespace) -> None:
    html_file: Path = args.html_file
    if not html_file.exists():
        raise FileNotFoundError(f"HTML file not found: {html_file}")

    track_links, declared_count = load_tracks_from_html_file(html_file)

    if args.max_tracks is not None and args.max_tracks >= 0:
        track_links = track_links[: args.max_tracks]
        LOGGER.info("Limiting processing to first %s tracks", len(track_links))

    if not track_links:
        LOGGER.warning("No track links found in HTML file '%s'.", html_file)
        LOGGER.info(
            "Tip: Ensure the playlist page is fully scrolled, all tracks are loaded, and the HTML is saved completely."
        )
        return

    if declared_count and len(track_links) != declared_count:
        LOGGER.warning(
            "Collected %s tracks but playlist declares %s.",
            len(track_links),
            declared_count,
        )
    else:
        LOGGER.info("Collected %s tracks from HTML file.", len(track_links))

    session = create_requests_session()
    try:
        categorized = collect_track_data(
            track_links,
            session=session,
            delay=args.delay,
            timeout=args.timeout,
        )
    finally:
        session.close()

    summary = summarize_categories(categorized)

    if args.export != "none":
        export_results(summary, args.export, args.output)

    log_summary(summary)


def handle_open(args: argparse.Namespace) -> None:
    summary_file: Path = args.summary_file
    summary = load_json_file(summary_file)

    log_summary(summary)

    category = args.category
    if category is None and not args.no_open:
        category = prompt_category_selection()
    elif category is None:
        category = "all"

    open_links_in_browser(
        summary,
        category,
        browser=args.browser,
        skip=max(0, args.skip),
        limit=args.limit,
        disable_open=args.no_open,
    )


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_cli_args(argv)

    log_level = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")
    LOGGER.setLevel(log_level)

    if args.command == "dig":
        handle_dig(args)
    elif args.command == "open":
        handle_open(args)


if __name__ == "__main__":
    main()


