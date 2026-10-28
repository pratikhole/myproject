import gzip
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup


# ============================================================
# 3RI TECHNOLOGIES WEBSITE MONITOR
# ============================================================

BASE_URL = "https://www.3ritechnologies.com/"
DOMAIN = "www.3ritechnologies.com"

DATA_DIR = "data"

SNAPSHOT_FILE = os.path.join(DATA_DIR, "snapshot.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
LATEST_DIFFS_FILE = os.path.join(DATA_DIR, "latest_diffs.json")

CONTENT_FILE = os.path.join(DATA_DIR, "content.json.gz")
DETAILS_FILE = os.path.join(DATA_DIR, "details.json.gz")

DIFF_DETAILS_DIR = os.path.join(DATA_DIR, "diff_details")

# Safety limit. Increase later if required.
MAX_URLS = 5000

# Polite delay between page requests.
REQUEST_DELAY = 1

TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; WebsiteMonitor/1.0; "
        "+https://github.com/)"
    )
}


# ============================================================
# SETUP
# ============================================================

session = requests.Session()
session.headers.update(HEADERS)

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(DIFF_DETAILS_DIR, exist_ok=True)


def current_time():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def save_gzip_json(path, data):
    with gzip.open(path, "wt", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False)


def normalize_url(url):
    """Normalize URLs and remove fragments."""

    if not url:
        return ""

    url = url.strip()

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return ""

    # Remove URL fragments.
    url = parsed._replace(fragment="").geturl()

    # Remove trailing slash except for root.
    parsed = urlparse(url)

    if parsed.path != "/" and parsed.path.endswith("/"):
        url = parsed._replace(path=parsed.path.rstrip("/")).geturl()

    return url


def is_valid_domain(url):
    """Only monitor URLs belonging to 3RI Technologies."""

    try:
        hostname = urlparse(url).hostname or ""
        hostname = hostname.lower()

        return hostname == DOMAIN or hostname == "3ritechnologies.com"

    except Exception:
        return False


# ============================================================
# SITEMAP DISCOVERY
# ============================================================

def get_sitemap_urls():
    """Find sitemap URLs using robots.txt and common sitemap paths."""

    sitemap_candidates = []

    robots_url = urljoin(BASE_URL, "robots.txt")

    try:
        response = session.get(robots_url, timeout=TIMEOUT)

        if response.status_code == 200:
            for line in response.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    sitemap_candidates.append(sitemap_url)

    except requests.RequestException as error:
        print(f"Could not read robots.txt: {error}")

    sitemap_candidates.extend([
        urljoin(BASE_URL, "sitemap.xml"),
        urljoin(BASE_URL, "sitemap_index.xml"),
        urljoin(BASE_URL, "sitemap-index.xml"),
    ])

    # Remove duplicates while preserving order.
    sitemap_candidates = list(dict.fromkeys(sitemap_candidates))

    return sitemap_candidates


def parse_sitemap(sitemap_url, discovered_urls, visited_sitemaps):
    """Parse sitemap files, including sitemap index files."""

    if sitemap_url in visited_sitemaps:
        return

    visited_sitemaps.add(sitemap_url)

    try:
        response = session.get(sitemap_url, timeout=TIMEOUT)

        if response.status_code != 200:
            print(
                f"Sitemap unavailable: {sitemap_url} "
                f"(HTTP {response.status_code})"
            )
            return

        root = ET.fromstring(response.content)

        # Handle XML namespaces.
        for element in root.iter():
            tag = element.tag.split("}")[-1].lower()

            if tag == "sitemap":
                loc = element.find(".//{*}loc")

                if loc is not None and loc.text:
                    parse_sitemap(
                        loc.text.strip(),
                        discovered_urls,
                        visited_sitemaps
                    )

            elif tag == "url":
                loc = element.find(".//{*}loc")

                if loc is not None and loc.text:
                    url = normalize_url(loc.text.strip())

                    if url and is_valid_domain(url):
                        discovered_urls.add(url)

    except (requests.RequestException, ET.ParseError) as error:
        print(f"Error parsing sitemap {sitemap_url}: {error}")


def discover_urls():
    """Discover all URLs from the website's sitemap."""

    print("\nDiscovering sitemap URLs...")

    discovered_urls = set()
    visited_sitemaps = set()

    sitemap_candidates = get_sitemap_urls()

    for sitemap_url in sitemap_candidates:
        parse_sitemap(
            sitemap_url,
            discovered_urls,
            visited_sitemaps
        )

    # Ensure homepage is monitored.
    discovered_urls.add(normalize_url(BASE_URL))

    urls = sorted(discovered_urls)

    if MAX_URLS and len(urls) > MAX_URLS:
        print(
            f"Found {len(urls)} URLs. "
            f"Limiting crawl to {MAX_URLS} URLs."
        )
        urls = urls[:MAX_URLS]

    print(f"Total URLs discovered: {len(urls)}")

    return urls


# ============================================================
# PAGE EXTRACTION
# ============================================================

def clean_text(text):
    """Clean whitespace from extracted page text."""

    if not text:
        return ""

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def extract_page_data(url):
    """Fetch a page and extract its SEO and content information."""

    try:
        response = session.get(url, timeout=TIMEOUT)

        status_code = response.status_code

        if status_code >= 400:
            print(f"Page failed: {url} - HTTP {status_code}")

            return {
                "url": url,
                "status_code": status_code,
                "success": False,
                "error": f"HTTP {status_code}",
            }

        soup = BeautifulSoup(response.text, "lxml")

        # Remove elements that are not useful for page content.
        for element in soup([
            "script",
            "style",
            "noscript",
            "svg",
            "iframe"
        ]):
            element.decompose()

        title = ""

        if soup.title:
            title = clean_text(soup.title.get_text(" ", strip=True))

        meta_description = ""

        description_tag = soup.find(
            "meta",
            attrs={"name": re.compile("^description$", re.I)}
        )

        if description_tag:
            meta_description = clean_text(
                description_tag.get("content", "")
            )

        canonical = ""

        canonical_tag = soup.find(
            "link",
            attrs={"rel": re.compile("canonical", re.I)}
        )

        if canonical_tag:
            canonical = canonical_tag.get("href", "").strip()

            if canonical:
                canonical = urljoin(url, canonical)

        robots_meta = ""

        robots_tag = soup.find(
            "meta",
            attrs={"name": re.compile("^robots$", re.I)}
        )

        if robots_tag:
            robots_meta = clean_text(
                robots_tag.get("content", "")
            )

        h1_tags = [
            clean_text(tag.get_text(" ", strip=True))
            for tag in soup.find_all("h1")
        ]

        h2_tags = [
            clean_text(tag.get_text(" ", strip=True))
            for tag in soup.find_all("h2")
        ]

        h3_tags = [
            clean_text(tag.get_text(" ", strip=True))
            for tag in soup.find_all("h3")
        ]

        body = soup.body or soup

        page_text = clean_text(
            body.get_text(" ", strip=True)
        )

        word_count = len(page_text.split())

        content_hash = hashlib.sha256(
            page_text.encode("utf-8")
        ).hexdigest()

        return {
            "url": url,
            "final_url": response.url,
            "status_code": status_code,
            "success": True,
            "title": title,
            "meta_description": meta_description,
            "canonical": canonical,
            "robots": robots_meta,
            "h1": h1_tags,
            "h2": h2_tags,
            "h3": h3_tags,
            "word_count": word_count,
            "content_hash": content_hash,
            "content": page_text,
            "checked_at": current_time(),
        }

    except requests.RequestException as error:
        print(f"Request failed: {url} - {error}")

        return {
            "url": url,
            "success": False,
            "error": str(error),
            "checked_at": current_time(),
        }

    except Exception as error:
        print(f"Unexpected error on {url}: {error}")

        return {
            "url": url,
            "success": False,
            "error": str(error),
            "checked_at": current_time(),
        }


# ============================================================
# CHANGE DETECTION
# ============================================================

TRACKED_FIELDS = [
    "status_code",
    "title",
    "meta_description",
    "canonical",
    "robots",
    "h1",
    "h2",
    "h3",
    "word_count",
    "content_hash",
]


def compare_snapshots(previous, current):
    """Compare successful page records only."""

    changes = []
    detailed_changes = {}

    previous_pages = previous.get("pages", {})
    current_pages = current.get("pages", {})

    all_urls = sorted(
        set(previous_pages.keys()) | set(current_pages.keys())
    )

    for url in all_urls:
        old_page = previous_pages.get(url)
        new_page = current_pages.get(url)

        # New URL.
        if old_page is None and new_page and new_page.get("success"):
            changes.append({
                "url": url,
                "type": "new",
                "changed_fields": ["page_added"],
            })

            detailed_changes[url] = {
                "type": "new",
                "old": None,
                "new": new_page,
            }

            continue

        # Removed URL from sitemap.
        if old_page and new_page is None:
            changes.append({
                "url": url,
                "type": "removed",
                "changed_fields": ["page_removed"],
            })

            detailed_changes[url] = {
                "type": "removed",
                "old": old_page,
                "new": None,
            }

            continue

        # Do not treat failed requests as content changes.
        if (
            not old_page
            or not new_page
            or not old_page.get("success")
            or not new_page.get("success")
        ):
            continue

        changed_fields = []

        for field in TRACKED_FIELDS:
            if old_page.get(field) != new_page.get(field):
                changed_fields.append(field)

        if changed_fields:
            changes.append({
                "url": url,
                "type": "changed",
                "changed_fields": changed_fields,
            })

            detailed_changes[url] = {
                "type": "changed",
                "changed_fields": changed_fields,
                "old": old_page,
                "new": new_page,
            }

    return changes, detailed_changes


# ============================================================
# MAIN MONITOR
# ============================================================

def main():
    start_time = time.time()

    print("=" * 60)
    print("3RI TECHNOLOGIES WEBSITE MONITOR")
    print(f"Website: {BASE_URL}")
    print(f"Started: {current_time()}")
    print("=" * 60)

    previous_snapshot = load_json(
        SNAPSHOT_FILE,
        {"pages": {}}
    )

    urls = discover_urls()

    current_pages = {}

    total = len(urls)

    for index, url in enumerate(urls, start=1):
        print(f"[{index}/{total}] Checking: {url}")

        page_data = extract_page_data(url)

        current_pages[url] = page_data

        time.sleep(REQUEST_DELAY)

    current_snapshot = {
        "website": BASE_URL,
        "domain": DOMAIN,
        "checked_at": current_time(),
        "total_urls": len(urls),
        "successful_pages": sum(
            1 for page in current_pages.values()
            if page.get("success")
        ),
        "failed_pages": sum(
            1 for page in current_pages.values()
            if not page.get("success")
        ),
        "pages": current_pages,
    }

    # Compare with previous snapshot.
    changes, detailed_changes = compare_snapshots(
        previous_snapshot,
        current_snapshot
    )

    # Save current snapshot.
    save_json(SNAPSHOT_FILE, current_snapshot)

    # Save latest changes.
    latest_diffs = {
        "website": BASE_URL,
        "checked_at": current_snapshot["checked_at"],
        "total_changes": len(changes),
        "changes": changes,
    }

    save_json(LATEST_DIFFS_FILE, latest_diffs)

    # Save detailed changes separately.
    for url, details in detailed_changes.items():
        url_hash = hashlib.sha256(
            url.encode("utf-8")
        ).hexdigest()

        detail_path = os.path.join(
            DIFF_DETAILS_DIR,
            f"{url_hash}.json"
        )

        save_json(detail_path, details)

    # Save full content and page details as compressed JSON.
    save_gzip_json(CONTENT_FILE, {
        url: page.get("content", "")
        for url, page in current_pages.items()
        if page.get("success")
    })

    save_gzip_json(DETAILS_FILE, {
        url: {
            key: value
            for key, value in page.items()
            if key != "content"
        }
        for url, page in current_pages.items()
    })

    # Update history.
    history = load_json(HISTORY_FILE, [])

    history.append({
        "checked_at": current_snapshot["checked_at"],
        "total_urls": current_snapshot["total_urls"],
        "successful_pages": current_snapshot["successful_pages"],
        "failed_pages": current_snapshot["failed_pages"],
        "total_changes": len(changes),
        "new_pages": sum(
            1 for change in changes
            if change["type"] == "new"
        ),
        "removed_pages": sum(
            1 for change in changes
            if change["type"] == "removed"
        ),
        "changed_pages": sum(
            1 for change in changes
            if change["type"] == "changed"
        ),
    })

    save_json(HISTORY_FILE, history)

    elapsed = round(time.time() - start_time, 2)

    print("\n" + "=" * 60)
    print("MONITORING COMPLETED")
    print(f"Total URLs: {current_snapshot['total_urls']}")
    print(f"Successful pages: {current_snapshot['successful_pages']}")
    print(f"Failed pages: {current_snapshot['failed_pages']}")
    print(f"Detected changes: {len(changes)}")
    print(f"Time taken: {elapsed} seconds")
    print("=" * 60)


if __name__ == "__main__":
    main()
