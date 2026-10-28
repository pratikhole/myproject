
import os
import re
import json
import gzip
import time
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# 3RI TECHNOLOGIES WEBSITE MONITOR
# ============================================================

WEBSITE = "https://www.3ritechnologies.com/"
DOMAIN = "www.3ritechnologies.com"

DATA_DIR = "data"
SNAPSHOT_FILE = os.path.join(DATA_DIR, "snapshot.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
LATEST_DIFFS_FILE = os.path.join(DATA_DIR, "latest_diffs.json")
CONTENT_FILE = os.path.join(DATA_DIR, "content.json.gz")
DETAILS_FILE = os.path.join(DATA_DIR, "details.json.gz")
DIFF_DETAILS_DIR = os.path.join(DATA_DIR, "diff_details")

MAX_WORKERS = 8
REQUEST_TIMEOUT = 30

USER_AGENT = (
    "Mozilla/5.0 (compatible; 3RIWebsiteMonitor/1.0; "
    "+https://www.3ritechnologies.com/)"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

print_lock = Lock()


# ============================================================
# HELPERS
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(DIFF_DETAILS_DIR, exist_ok=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logging.warning("Could not read %s: %s", path, exc)
        return default


def save_json(path, data):
    temp_path = path + ".tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    os.replace(temp_path, path)


def save_gzip_json(path, data):
    temp_path = path + ".tmp"

    with gzip.open(temp_path, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    os.replace(temp_path, path)


def normalize_url(url):
    if not url:
        return ""

    url = urldefrag(url.strip())[0]

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return ""

    if parsed.netloc.lower() not in (
        DOMAIN,
        "3ritechnologies.com",
    ):
        return ""

    # Remove common tracking parameters.
    query_parts = []

    for part in parsed.query.split("&"):
        if not part:
            continue

        key = part.split("=", 1)[0].lower()

        if key.startswith("utm_"):
            continue

        if key in ("gclid", "fbclid", "msclkid"):
            continue

        query_parts.append(part)

    query = "&".join(query_parts)

    path = parsed.path or "/"

    return (
        "https://"
        + DOMAIN
        + path
        + ("?" + query if query else "")
    )


def get_session():
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
    })

    return session


SESSION = get_session()


def get_text(element):
    if not element:
        return ""

    return " ".join(element.get_text(" ", strip=True).split())


def get_meta(soup, name):
    tag = soup.find(
        "meta",
        attrs={"name": re.compile(r"^" + re.escape(name) + r"$", re.I)}
    )

    if tag:
        return (tag.get("content") or "").strip()

    return ""


def get_meta_property(soup, prop):
    tag = soup.find(
        "meta",
        attrs={"property": re.compile(r"^" + re.escape(prop) + r"$", re.I)}
    )

    if tag:
        return (tag.get("content") or "").strip()

    return ""


def sha256_text(value):
    return hashlib.sha256(
        str(value or "").encode("utf-8")
    ).hexdigest()


# ============================================================
# SITEMAP DISCOVERY
# ============================================================

def get_robots_sitemaps():
    robots_url = urljoin(WEBSITE, "robots.txt")
    sitemap_urls = []

    try:
        response = SESSION.get(
            robots_url,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            for line in response.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()

                    if sitemap_url:
                        sitemap_urls.append(sitemap_url)

    except Exception as exc:
        logging.warning("Could not read robots.txt: %s", exc)

    return sitemap_urls


def parse_sitemap(sitemap_url, visited=None):
    if visited is None:
        visited = set()

    if sitemap_url in visited:
        return set()

    visited.add(sitemap_url)

    found_urls = set()

    try:
        response = SESSION.get(
            sitemap_url,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            logging.warning(
                "Sitemap unavailable: %s (HTTP %s)",
                sitemap_url,
                response.status_code,
            )
            return set()

        soup = BeautifulSoup(response.content, "xml")

        # Sitemap index may point to child sitemaps.
        sitemap_tags = soup.find_all("sitemap")

        if sitemap_tags:
            for sitemap_tag in sitemap_tags:
                loc = sitemap_tag.find("loc")

                if loc and loc.text.strip():
                    child_url = loc.text.strip()

                    found_urls.update(
                        parse_sitemap(child_url, visited)
                    )

            return found_urls

        # Regular sitemap contains URL entries.
        for url_tag in soup.find_all("url"):
            loc = url_tag.find("loc")

            if loc and loc.text.strip():
                normalized = normalize_url(loc.text.strip())

                if normalized:
                    found_urls.add(normalized)

        # Also handle a plain sitemap containing loc tags.
        if not found_urls:
            for loc in soup.find_all("loc"):
                normalized = normalize_url(loc.text.strip())

                if normalized:
                    found_urls.add(normalized)

    except Exception as exc:
        logging.warning(
            "Could not parse sitemap %s: %s",
            sitemap_url,
            exc,
        )

    return found_urls


def discover_urls():
    print("\nDiscovering sitemap URLs...")

    sitemap_candidates = get_robots_sitemaps()

    # Fallback candidates. A 404 candidate is skipped.
    sitemap_candidates.extend([
        urljoin(WEBSITE, "sitemap.xml"),
        urljoin(WEBSITE, "sitemap_index.xml"),
        urljoin(WEBSITE, "sitemap-index.xml"),
        urljoin(WEBSITE, "wp-sitemap.xml"),
    ])

    unique_candidates = []

    for sitemap_url in sitemap_candidates:
        if sitemap_url not in unique_candidates:
            unique_candidates.append(sitemap_url)

    urls = set()

    for sitemap_url in unique_candidates:
        found = parse_sitemap(sitemap_url)

        if found:
            print(
                f"Sitemap found: {sitemap_url} "
                f"({len(found)} URLs)"
            )

            urls.update(found)

    # Always include homepage.
    homepage = normalize_url(WEBSITE)

    if homepage:
        urls.add(homepage)

    print(f"Total URLs discovered: {len(urls)}")

    return sorted(urls)


# ============================================================
# PAGE EXTRACTION
# ============================================================

def extract_page(url):
    result = {
        "url": url,
        "success": False,
        "status_code": 0,
        "title": "",
        "meta_description": "",
        "h1": [],
        "content": "",
        "content_hash": "",
        "images": [],
        "internal_links": [],
        "canonical": "",
        "robots": "",
        "og_title": "",
        "og_description": "",
        "schema": [],
        "word_count": 0,
        "error": "",
        "checked_at": now_iso(),
    }

    try:
        response = SESSION.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )

        result["status_code"] = response.status_code
        result["final_url"] = normalize_url(response.url) or response.url

        if response.status_code >= 400:
            result["error"] = f"HTTP {response.status_code}"
            return result

        content_type = response.headers.get("Content-Type", "").lower()

        if "html" not in content_type and response.text.strip() == "":
            result["error"] = "Empty response"
            return result

        soup = BeautifulSoup(response.content, "lxml")

        # Title
        title_tag = soup.find("title")
        result["title"] = get_text(title_tag)

        # Meta description
        result["meta_description"] = get_meta(
            soup,
            "description",
        )

        # H1 headings
        result["h1"] = [
            get_text(tag)
            for tag in soup.find_all("h1")
            if get_text(tag)
        ]

        # Canonical
        canonical_tag = soup.find(
            "link",
            rel=lambda value: value and "canonical" in value
        )

        if canonical_tag:
            result["canonical"] = (
                canonical_tag.get("href") or ""
            ).strip()

        # Robots
        result["robots"] = get_meta(soup, "robots")

        # Open Graph
        result["og_title"] = get_meta_property(
            soup,
            "og:title",
        )

        result["og_description"] = get_meta_property(
            soup,
            "og:description",
        )

        # Page content extraction.
        content_soup = BeautifulSoup(
            response.content,
            "lxml",
        )

        for tag in content_soup([
            "script",
            "style",
            "noscript",
            "svg",
            "iframe",
            "header",
            "footer",
            "nav",
        ]):
            tag.decompose()

        main_content = (
            content_soup.find("main")
            or content_soup.find("article")
            or content_soup.body
            or content_soup
        )

        content = get_text(main_content)

        result["content"] = content
        result["content_hash"] = sha256_text(content)
        result["word_count"] = len(content.split())

        # Images: URL + alt + title.
        images = []

        for img in soup.find_all("img"):
            image_url = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-lazy-src")
                or ""
            ).strip()

            if image_url:
                image_url = urljoin(response.url, image_url)

            images.append({
                "src": image_url,
                "alt": (img.get("alt") or "").strip(),
                "title": (img.get("title") or "").strip(),
            })

        result["images"] = images

        # Internal links.
        internal_links = set()

        for anchor in soup.find_all("a", href=True):
            href = urljoin(response.url, anchor["href"])
            normalized = normalize_url(href)

            if normalized:
                internal_links.add(normalized)

        result["internal_links"] = sorted(internal_links)

        # Structured data.
        schemas = []

        for script in soup.find_all(
            "script",
            attrs={"type": "application/ld+json"},
        ):
            raw = script.string or script.get_text()

            if raw:
                try:
                    schemas.append(json.loads(raw))
                except Exception:
                    schemas.append(raw.strip())

        result["schema"] = schemas

        result["success"] = True

        return result

    except Exception as exc:
        result["error"] = str(exc)

        return result


# ============================================================
# FIELD-WISE OLD / NEW COMPARISON
# ============================================================

TRACKED_FIELDS = [
    "title",
    "meta_description",
    "h1",
    "content",
    "images",
    "internal_links",
    "canonical",
    "robots",
    "og_title",
    "og_description",
    "schema",
    "status_code",
]


def field_value(page, field):
    value = page.get(field)

    if value is None:
        return ""

    return value


def compare_pages(old_page, new_page):
    changed_fields = []
    old_values = {}
    new_values = {}

    for field in TRACKED_FIELDS:
        old_value = field_value(old_page, field)
        new_value = field_value(new_page, field)

        if old_value != new_value:
            changed_fields.append(field)
            old_values[field] = old_value
            new_values[field] = new_value

    return changed_fields, old_values, new_values


def determine_priority(fields):
    high_fields = {
        "title",
        "h1",
        "canonical",
        "robots",
        "status_code",
    }

    medium_fields = {
        "meta_description",
        "content",
        "images",
        "internal_links",
        "schema",
    }

    if any(field in high_fields for field in fields):
        return "HIGH"

    if any(field in medium_fields for field in fields):
        return "MEDIUM"

    return "LOW"


def compare_snapshots(old_pages, new_pages):
    changes = []

    old_urls = set(old_pages.keys())
    new_urls = set(new_pages.keys())

    # New pages.
    for url in sorted(new_urls - old_urls):
        page = new_pages[url]

        if not page.get("success"):
            continue

        changes.append({
            "url": url,
            "type": "new",
            "changed_fields": TRACKED_FIELDS,
            "old": {},
            "new": {
                field: field_value(page, field)
                for field in TRACKED_FIELDS
            },
            "priority": "MEDIUM",
        })

    # Changed existing pages.
    for url in sorted(old_urls & new_urls):
        old_page = old_pages[url]
        new_page = new_pages[url]

        # Do not treat failed fetches as page changes.
        if not new_page.get("success"):
            continue

        # If old record was not successful, do not compare it.
        if not old_page.get("success"):
            continue

        fields, old_values, new_values = compare_pages(
            old_page,
            new_page,
        )

        if fields:
            changes.append({
                "url": url,
                "type": "changed",
                "changed_fields": fields,
                "old": old_values,
                "new": new_values,
                "priority": determine_priority(fields),
            })

    # Removed pages.
    for url in sorted(old_urls - new_urls):
        old_page = old_pages[url]

        if not old_page.get("success"):
            continue

        changes.append({
            "url": url,
            "type": "removed",
            "changed_fields": TRACKED_FIELDS,
            "old": {
                field: field_value(old_page, field)
                for field in TRACKED_FIELDS
            },
            "new": {},
            "priority": "HIGH",
        })

    return changes


# ============================================================
# MAIN MONITOR
# ============================================================

def main():
    ensure_dirs()

    print("=" * 60)
    print("3RI TECHNOLOGIES WEBSITE MONITOR")
    print(f"Website: {WEBSITE}")
    print(f"Started: {now_iso()}")
    print("=" * 60)

    old_snapshot = load_json(
        SNAPSHOT_FILE,
        {},
    )

    old_pages = old_snapshot.get("pages", {})

    urls = discover_urls()

    if not urls:
        raise RuntimeError(
            "No URLs discovered. Stopping to protect existing baseline."
        )

    print(f"\nChecking {len(urls)} pages...")

    new_pages = {}
    failed_count = 0
    successful_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(extract_page, url): url
            for url in urls
        }

        completed = 0

        for future in as_completed(futures):
            url = futures[future]
            completed += 1

            try:
                page = future.result()
            except Exception as exc:
                page = {
                    "url": url,
                    "success": False,
                    "status_code": 0,
                    "error": str(exc),
                    "checked_at": now_iso(),
                }

            new_pages[url] = page

            if page.get("success"):
                successful_count += 1
            else:
                failed_count += 1

            with print_lock:
                print(
                    f"[{completed}/{len(urls)}] "
                    f"{'OK' if page.get('success') else 'FAILED'} "
                    f"{url}"
                )

    # If a large portion of the site failed, do not overwrite
    # a healthy baseline with a potentially incomplete crawl.
    success_ratio = (
        successful_count / len(urls)
        if urls else 0
    )

    if old_pages and success_ratio < 0.50:
        raise RuntimeError(
            "Less than 50% of pages were fetched successfully. "
            "Existing baseline has been preserved."
        )

    # Keep old page records for temporarily failed URLs.
    # This prevents failed fetches from looking like removed pages.
    for url, old_page in old_pages.items():
        if url not in new_pages:
            new_pages[url] = old_page

        elif not new_pages[url].get("success"):
            new_pages[url] = {
                **old_page,
                "last_fetch_error": new_pages[url].get("error", ""),
                "last_failed_at": now_iso(),
            }

    # First run is a baseline only.
    if not old_pages:
        changes = []

        print(
            "\nFirst run detected: creating baseline. "
            "No old/new changes will be reported."
        )

    else:
        changes = compare_snapshots(
            old_pages,
            new_pages,
        )

    checked_at = now_iso()

    snapshot = {
        "website": WEBSITE,
        "checked_at": checked_at,
        "total_urls": len(urls),
        "successful_pages": successful_count,
        "failed_pages": failed_count,
        "pages": new_pages,
    }

    latest_diffs = {
        "website": WEBSITE,
        "checked_at": checked_at,
        "total_changes": len(changes),
        "changes": changes,
    }

    # Load previous history and append this run.
    history = load_json(
        HISTORY_FILE,
        [],
    )

    if not isinstance(history, list):
        history = []

    history.append({
        "checked_at": checked_at,
        "total_urls": len(urls),
        "successful_pages": successful_count,
        "failed_pages": failed_count,
        "total_changes": len(changes),
    })

    # Save latest snapshot and detailed comparison.
    save_json(
        SNAPSHOT_FILE,
        snapshot,
    )

    save_json(
        LATEST_DIFFS_FILE,
        latest_diffs,
    )

    save_json(
        HISTORY_FILE,
        history,
    )

    # Content and details archives.
    content_data = {
        url: page.get("content", "")
        for url, page in new_pages.items()
    }

    details_data = {
        url: {
            field: page.get(field)
            for field in TRACKED_FIELDS
        }
        for url, page in new_pages.items()
    }

    save_gzip_json(
        CONTENT_FILE,
        content_data,
    )

    save_gzip_json(
        DETAILS_FILE,
        details_data,
    )

    # Save individual detail files for changed pages.
    for change in changes:
        safe_name = sha256_text(change["url"])[:20] + ".json"

        save_json(
            os.path.join(DIFF_DETAILS_DIR, safe_name),
            change,
        )

    print("\n" + "=" * 60)
    print("MONITORING COMPLETED")
    print("=" * 60)
    print(f"Total URLs: {len(urls)}")
    print(f"Successful Pages: {successful_count}")
    print(f"Failed Pages: {failed_count}")
    print(f"Latest Changes: {len(changes)}")
    print(f"Completed: {checked_at}")
    print("=" * 60)


if __name__ == "__main__":
    main()
