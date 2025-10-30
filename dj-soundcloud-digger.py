#!/usr/bin/env python3

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import webbrowser
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DOWNLOAD_KEYWORDS = {"download", "free download", "free d/l"}
LINK_KEYWORDS = DOWNLOAD_KEYWORDS | {"buy", "purchase", "premiere", "kup"}
STORE_DOMAINS = {
    "bandcamp": {"bandcamp.com"},
    "beatport": {"beatport.com"},
    "junodownload": {"junodownload.com", "juno.co.uk"},
    "hypeddit": {"hypeddit.com", "hypd.it"},
}
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
    "albums",
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

CLIENT_ID_PATTERNS = [
    re.compile(r"client_id\s*[:=]\s*\"([0-9a-zA-Z]{16,32})\""),
    re.compile(r"client_id\s*[:=]\s*'([0-9a-zA-Z]{16,32})'"),
    re.compile(r"client_id%22%3A%22([0-9a-zA-Z]{16,32})%22"),
    re.compile(r"client_id\\\":\\\"([0-9a-zA-Z]{16,32})\\\""),
]


_CLIENT_ID_CACHE: Optional[str] = None


@dataclass
class LinkRecord:
    category: str
    title: str
    track_url: str
    link_url: str
    link_text: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract download and store links from SoundCloud playlist HTML file or open links from existing JSON"
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        help="Path to HTML file saved from SoundCloud playlist page, or JSON file with existing results",
    )
    parser.add_argument(
        "--export",
        choices=["json", "yaml", "none"],
        help="Export format for categorized links. Defaults to interactive prompt (ignored when using --open with JSON)",
    )
    parser.add_argument(
        "--output",
        help="Optional path for the export file. Defaults to soundcloud_links.<ext>",
    )
    parser.add_argument(
        "--open",
        choices=["hypeddit", "bandcamp", "beatport", "junodownload", "others", "all"],
        help="Open links from specified category in browser (without loading pages). If input is JSON, skips HTML processing.",
    )
    return parser.parse_args()


def prompt_missing_arguments(args: argparse.Namespace) -> argparse.Namespace:
    if not args.input_file:
        args.input_file = input("Enter path to HTML file or JSON file: ").strip()

    # Jeśli używamy --open z JSON, nie potrzebujemy export
    if not args.open and not args.export:
        while True:
            choice = (
                input("Choose export format (json/yaml/none): ")
                .strip()
                .lower()
            )
            if choice in {"json", "yaml", "none"}:
                args.export = choice
                break
            print("Please enter 'json', 'yaml', or 'none'.")
    elif not args.export:
        args.export = "none"  # Default jeśli nie podano
    return args


def load_json_file(json_file_path: str) -> Dict[str, List[Dict[str, str]]]:
    """Load summary data from existing JSON file."""
    if not os.path.exists(json_file_path):
        raise FileNotFoundError(f"JSON file not found: {json_file_path}")
    
    print(f"Loading JSON file: {json_file_path}")
    
    try:
        with open(json_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Validate structure
        if not isinstance(data, dict):
            raise ValueError("JSON file must contain a dictionary")
        
        print(f"Loaded JSON with categories: {', '.join(data.keys())}")
        return data
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON file: {e}")
    except Exception as e:
        raise Exception(f"Error loading JSON file: {e}")


def load_tracks_from_html_file(html_file_path: str) -> Tuple[List[str], Optional[int]]:
    """Load track URLs from a saved HTML file."""
    
    if not os.path.exists(html_file_path):
        raise FileNotFoundError(f"HTML file not found: {html_file_path}")
    
    print(f"Loading HTML file: {html_file_path}")
    
    try:
        with open(html_file_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
    except UnicodeDecodeError:
        # Try with different encoding
        with open(html_file_path, 'r', encoding='latin-1') as f:
            html_content = f.read()
    
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


def fetch_track_page(track_url: str) -> Optional[BeautifulSoup]:
    try:
        response = requests.get(track_url, headers=REQUEST_HEADERS, timeout=20)
        if response.status_code >= 400:
            print(f"Could not retrieve track page ({response.status_code}): {track_url}")
            return None
        return BeautifulSoup(response.text, "html.parser")
    except Exception as exc:
        print(f"Error while retrieving {track_url}: {exc}")
        return None


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


def open_links_in_browser(summary: Dict[str, List[Dict[str, str]]], category: str) -> None:
    """Open shop_link from specified category in browser. Falls back to track_url if shop_link is missing or same as track_url."""
    if category == "all":
        categories_to_open = CATEGORY_NAMES
    else:
        if category not in summary:
            print(f"Category '{category}' not found in results.")
            return
        categories_to_open = [category]
    
    total_opened = 0
    shop_links_opened = 0
    track_links_opened = 0
    
    for cat in categories_to_open:
        if cat not in summary or not summary[cat]:
            continue
        
        links = summary[cat]
        print(f"\nProcessing {len(links)} items from '{cat}' category...")
        
        for item in links:
            shop_link = item.get("shop_link", "").strip()
            track_url = item.get("track_url", "").strip()
            
            # Określ który link otworzyć:
            # 1. Jeśli shop_link istnieje i jest różny od track_url → otwórz shop_link
            # 2. W przeciwnym razie → otwórz track_url jako fallback
            if shop_link and shop_link != track_url:
                # Mamy prawdziwy link do sklepu
                link_to_open = shop_link
                shop_links_opened += 1
            else:
                # Brak shop_link lub jest taki sam jak track_url → użyj track_url
                if not track_url:
                    print(f"Warning: No track_url or shop_link for item: {item.get('title', 'Unknown')}")
                    continue
                link_to_open = track_url
                track_links_opened += 1
            
            try:
                webbrowser.open_new_tab(link_to_open)
                total_opened += 1
                # Small delay to avoid overwhelming the browser
                time.sleep(0.1)
            except Exception as e:
                print(f"Failed to open {link_to_open}: {e}")
        
        if cat != categories_to_open[-1]:
            # Small delay between categories
            time.sleep(0.5)
    
    print(f"\nOpened {total_opened} links in browser:")
    print(f"  - {shop_links_opened} shop links")
    print(f"  - {track_links_opened} track links (fallback)")


def export_results(summary: Dict[str, List[Dict[str, str]]], export_format: str, output_path: Optional[str]) -> None:
    if export_format == "none":
        return

    filename = output_path
    if not filename:
        extension = "json" if export_format == "json" else "yaml"
        filename = f"soundcloud_links.{extension}"

    if export_format == "json":
        with open(filename, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        print(f"Saved categorized links to {filename}")
        return

    try:
        import yaml  # type: ignore

        with open(filename, "w", encoding="utf-8") as handle:
            yaml.safe_dump(summary, handle, sort_keys=False, allow_unicode=True)
        print(f"Saved categorized links to {filename}")
    except ModuleNotFoundError:
        print("PyYAML not installed. Install via 'pip install pyyaml' to export YAML.")


def collect_track_data(track_links: Iterable[str]) -> Dict[str, List[LinkRecord]]:
    categorized: Dict[str, List[LinkRecord]] = defaultdict(list)
    print("\nAttempting to retrieve links for tracks:")
    for track_url in track_links:
        soup = fetch_track_page(track_url)
        if soup is None:
            # Jeśli nie można pobrać strony, dodaj do "others"
            categorized["others"].append(
                LinkRecord("others", "Unknown title", track_url, track_url, "Could not fetch track page")
            )
            continue

        title = extract_title(soup)
        per_track = analyze_links(track_url, soup)
        if not per_track:
            # Jeśli analyze_links zwróciło pusty dict, dodaj do "others"
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
            print(f"{track_url} -> Store links: {stores}")
        else:
            print(f"{track_url} -> No store link")
    return categorized


def main():
    args = prompt_missing_arguments(parse_args())

    # Sprawdź czy plik to JSON czy HTML
    input_file = args.input_file
    is_json = input_file.lower().endswith('.json')
    
    # Jeśli mamy JSON i chcemy tylko otworzyć linki, pominąć przetwarzanie HTML
    if is_json and args.open:
        try:
            summary = load_json_file(input_file)
            
            print("\nSummary from JSON:")
            for category in CATEGORY_NAMES:
                count = len(summary.get(category, []))
                if count > 0:
                    print(f"{category}: {count}")
            
            # Otwórz linki
            open_links_in_browser(summary, args.open)
            return
        except Exception as e:
            print(f"Error: {e}")
            return
    
    # Jeśli to JSON ale nie ma --open, możemy go użyć jako źródła danych
    if is_json:
        try:
            summary = load_json_file(input_file)
            
            print("\nSummary from JSON:")
            for category in CATEGORY_NAMES:
                count = len(summary.get(category, []))
                if count > 0:
                    print(f"{category}: {count}")
            
            # Eksportuj jeśli potrzeba (może z inną nazwą)
            if args.export != "none":
                export_results(summary, args.export, args.output)
            
            # Otwórz linki jeśli potrzeba
            if args.open:
                open_links_in_browser(summary, args.open)
            return
        except Exception as e:
            print(f"Error loading JSON: {e}")
            print("Treating as HTML file...")
            # Fall through to HTML processing
    
    # Przetwarzanie HTML (oryginalna logika)
    print("Loading tracks from HTML file...")
    try:
        track_links, declared_count = load_tracks_from_html_file(input_file)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        return
    except Exception as e:
        print(f"Error loading HTML file: {e}")
        return

    if not track_links:
        print("No track links found in HTML file.")
        print("\nTip: Make sure you:")
        print("1. Fully scroll down the playlist page in your browser")
        print("2. Wait for all tracks to load")
        print("3. Save the complete page HTML (Ctrl+S or right-click -> Save As)")
        return

    if declared_count and len(track_links) != declared_count:
        print(
            f"Warning: found {len(track_links)} tracks, playlist declares {declared_count}."
        )

    print(f"\nFound {len(track_links)} tracks:")
    for i, track in enumerate(track_links, 1):
        print(f"{i}. {track}")

    print("\nAnalyzing each track for download/store links...")
    categorized = collect_track_data(track_links)

    summary = summarize_categories(categorized)
    export_results(summary, args.export, args.output)

    print("\nSummary:")
    for category in CATEGORY_NAMES:
        count = len(summary.get(category, []))
        if count > 0:
            print(f"{category}: {count}")
    
    # Open links in browser if requested
    if args.open:
        open_links_in_browser(summary, args.open)


if __name__ == "__main__":
    main()


