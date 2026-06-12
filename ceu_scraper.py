#!/usr/bin/env python3
"""
Western US CEU Course Scraper
=============================
Scrapes a curated list of physical therapy / strength & conditioning / massage
therapy / chiropractic / osteopathic CE provider sites and produces:

  - courses_current.md   — the current course list (organized by date)
  - courses_state.json   — machine-readable state for diff comparison
  - changes.md           — what's new / changed / dropped since last run

Designed to be run weekly via cron / launchd / Task Scheduler / Claude Code routines.

Defaults to California-only using a hard-coded keyword match against venue
names (cheap, reliable, no API key). Override the keyword list by editing
CALIFORNIA_KEYWORDS below.

USAGE
-----
    pip install -r requirements.txt
    python ceu_scraper.py                 # run once
    python ceu_scraper.py --notify        # also try to email/notify on changes
    python ceu_scraper.py --provider ucsf # run just one provider
    python ceu_scraper.py --list-providers

SCHEDULING WEEKLY
-----------------
macOS / Linux (cron, every Monday at 7am):
    0 7 * * 1 cd /path/to/ceu_scraper && /usr/bin/python3 ceu_scraper.py

macOS (launchd): see README.md
Windows: Task Scheduler, weekly trigger
Claude Code: `/schedule` and point it at this script
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Callable

import requests
from bs4 import BeautifulSoup

# Optional Playwright support for JavaScript-rendered pages (currently only
# FMS). Detected at import time; flag flips when user passes --use-playwright.
try:
    from playwright.sync_api import sync_playwright as _sync_playwright  # noqa: F401
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

# Defaults ON whenever the playwright package is importable, so the scheduled
# run uses the browser fallback automatically — no CLI flag needed. Pass
# --no-playwright to force it off (e.g. to debug static fetches).
USE_PLAYWRIGHT = _PLAYWRIGHT_AVAILABLE

# Debug dumping — when a problem provider parses 0 results from non-empty
# HTML, the raw response is written to ./debug/<provider>.html for
# inspection. Set to False to disable.
DEBUG_DUMP_ENABLED = True
DEBUG_DUMP_DIR = Path(__file__).parent / "debug"


def dump_html_for_debug(key: str, url: str, html: str) -> None:
    """Write received HTML to ./debug/<key>.html for diagnostic inspection.

    Used by problematic scrapers (DNS, Agile, AAMT, CPTA, FMS) when their
    parser returns 0 results from a non-empty HTML response. The user can
    then inspect the file and share it for parser tuning.
    """
    if not DEBUG_DUMP_ENABLED or not html:
        return
    try:
        DEBUG_DUMP_DIR.mkdir(exist_ok=True)
        path = DEBUG_DUMP_DIR / f"{key}.html"
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"<!-- URL: {url} -->\n")
            f.write(f"<!-- Captured: {datetime.now().isoformat(timespec='seconds')} -->\n")
            f.write(f"<!-- Length: {len(html):,} chars -->\n")
            f.write(html)
        log(f"  [DEBUG] dumped {len(html):,}-char response to {path}")
    except Exception as e:
        log(f"  [DEBUG] failed to dump {key}: {e}")


def render_with_playwright(url: str, wait_selector: str | None = None,
                            wait_ms: int = 8000,
                            post_load_actions: list[dict] | None = None) -> str | None:
    """Fetch the rendered HTML of a JS-heavy page using a headless browser.

    Returns None on any failure so callers can fall back to static fetch.

    Args:
        url: page to load
        wait_selector: CSS selector to wait for before grabbing HTML
        wait_ms: max wait for selector or networkidle event
        post_load_actions: optional list of {action: ..., ...} dicts to run
            after the page loads. Supported actions:
                {"action": "click", "selector": "..."}
                {"action": "fill", "selector": "...", "value": "..."}
                {"action": "select", "selector": "...", "value": "..."}
                {"action": "wait", "ms": 2000}
                {"action": "wait_selector", "selector": "...", "ms": 5000}
            This is the FMS escape hatch — its events page requires clicking
            a search button before events appear.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                if wait_selector:
                    try:
                        page.wait_for_selector(wait_selector, timeout=wait_ms)
                    except Exception:
                        pass
                else:
                    try:
                        page.wait_for_load_state("networkidle", timeout=wait_ms)
                    except Exception:
                        pass
                page.wait_for_timeout(1500)

                # Execute post-load actions (form fills, button clicks, etc.).
                if post_load_actions:
                    for step in post_load_actions:
                        action = step.get("action")
                        try:
                            if action == "click":
                                page.click(step["selector"], timeout=5000)
                            elif action == "fill":
                                page.fill(step["selector"], step["value"], timeout=5000)
                            elif action == "select":
                                page.select_option(step["selector"], step["value"], timeout=5000)
                            elif action == "wait":
                                page.wait_for_timeout(step.get("ms", 1000))
                            elif action == "wait_selector":
                                page.wait_for_selector(
                                    step["selector"],
                                    timeout=step.get("ms", 5000),
                                )
                        except Exception as e:
                            try:
                                log(f"  playwright action {action} failed: {e}")
                            except NameError:
                                pass

                html = page.content()
                return html
            finally:
                browser.close()
    except Exception as e:
        try:
            log(f"  playwright render failed for {url}: {e}")
        except NameError:
            pass
        return None

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
STATE_FILE = SCRIPT_DIR / "courses_state.json"
OUTPUT_MD = SCRIPT_DIR / "courses_current.md"
CHANGES_MD = SCRIPT_DIR / "changes.md"
LOG_FILE = SCRIPT_DIR / "scraper.log"

# Polite scraping: identify yourself, throttle, and respect 429s.
USER_AGENT = (
    "WesternUS-CEU-Scraper/1.1 (personal CE planning tool; "
    "contact: replace_with_your_email@example.com)"
)
REQUEST_TIMEOUT = 20  # seconds
DELAY_BETWEEN_REQUESTS = 2.0  # seconds, polite throttle

# Locations counted as "in California". State name and 2-letter abbrev with
# surrounding punctuation plus major metro cities so we still match when a
# provider lists only "Pasadena" without state. To broaden geographic scope
# (e.g. re-include OR/WA/NV/AZ/CO), append the relevant patterns.
CALIFORNIA_KEYWORDS = [
    # State name / abbreviation (with separators so we don't false-match "ca"
    # inside other words)
    "california", ", ca ", ", ca,", ", ca\n", " ca ", " ca,", " ca.",
    # SF Bay + Sacramento + Santa Cruz
    "san francisco", "daly city", "south san francisco",
    "san mateo", "burlingame", "millbrae", "redwood city", "palo alto",
    "menlo park", "mountain view", "sunnyvale", "santa clara", "san jose",
    "stanford, ca", "stanford, usa", "stanford university",
    "cupertino", "los altos", "los gatos", "campbell, ca", "milpitas", "fremont, ca",
    "san carlos", "belmont, ca", "foster city",
    "oakland, ca", "berkeley, ca", "emeryville", "alameda, ca", "hayward",
    "san leandro", "castro valley", "dublin, ca", "pleasanton", "livermore",
    "walnut creek", "concord, ca", "richmond, ca",
    "novato", "san rafael", "marin", "petaluma", "santa rosa", "napa,",
    "pacifica", "half moon bay",
    "sacramento", "davis, ca", "elk grove", "rocklin", "roseville, ca", "folsom",
    "santa cruz", "scotts valley", "capitola", "watsonville",
    # Greater LA + Inland Empire + Orange County + San Diego
    "los angeles", "burbank, ca", "glendale, ca", "glendora", "pasadena", "long beach",
    "torrance", "van nuys", "westlake village", "valencia, ca", "seal beach",
    "studio city", "culver city", "santa monica", "el segundo",
    "anaheim", "santa ana", "irvine, ca", "huntington beach", "newport beach",
    "fountain valley", "orange, ca", "costa mesa", "tustin", "fullerton",
    "laguna niguel", "laguna beach", "mission viejo", "dana point",
    "ontario, ca", "riverside, ca", "san bernardino", "rancho cucamonga",
    "san diego", "la jolla", "coronado", "encinitas", "carlsbad, ca", "oceanside",
    "chula vista", "del mar",
    # Central Valley / Inland CA
    "fresno", "modesto", "stockton, ca", "bakersfield", "visalia",
    "pleasant hill", "antioch, ca",
]

# Legacy aliases — older parts of the file reference these names. Keep them
# pointed at the CA-only set so all filters now use the same list.
WESTERN_US_KEYWORDS = CALIFORNIA_KEYWORDS
BAY_AREA_KEYWORDS = CALIFORNIA_KEYWORDS


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------

@dataclass
class Course:
    """A single course offering."""
    course_id: str           # stable hash used for diffing; built from provider+title+date+location
    provider: str
    title: str
    start_date: str          # ISO YYYY-MM-DD; "TBD" if unknown
    end_date: str = ""       # ISO YYYY-MM-DD; "" if single-day or unknown
    location: str = ""       # human-readable: "Embassy Suites Milpitas, CA"
    url: str = ""
    audience: str = ""       # "PT, DC, ATC" etc., free-text
    pt_ceus: str = "unknown" # "yes" | "no" | "via reciprocity" | "unknown"
    pt_attendable: bool = True  # False = course is for non-PT discipline only; gets filtered out
    notes: str = ""
    discovered_at: str = ""  # ISO timestamp when first seen

    def stable_key(self) -> str:
        """Used for diffing across runs."""
        return self.course_id


def make_course_id(provider: str, title: str, start_date: str, location: str) -> str:
    """Build a stable ID that survives small formatting changes."""
    import hashlib
    blob = "|".join([
        provider.lower().strip(),
        re.sub(r"\s+", " ", title.lower().strip()),
        start_date,
        # take just the city/venue prefix to ignore minor address tweaks
        location.lower().strip()[:40],
    ])
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


# ----------------------------------------------------------------------------
# HTTP helper
# ----------------------------------------------------------------------------

_session = None

def http_get(url: str, allow_playwright_fallback: bool = False) -> str | None:
    """GET a URL with polite throttle and standard error handling.

    Sends a browser-like header set so providers behind Cloudflare or with
    strict Accept-header filtering (Barbell Rehab, CPTA, Northeast Seminars,
    etc.) return real HTML instead of 403/415.

    If allow_playwright_fallback=True AND Playwright is loaded via
    --use-playwright AND the static fetch returns 403/415/429 or empty,
    we'll retry with the headless browser. Use sparingly; it adds ~5-10s
    per provider.

    Returns text or None.
    """
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": USER_AGENT,
            # Mimic a real browser. Many sites reject the bare python-requests
            # default Accept header (e.g. neseminars.com → 415).
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            # CRITICAL: do NOT include 'br' here. Python's `requests` library
            # auto-decompresses gzip and deflate, but NOT brotli unless the
            # `brotli` pip package is installed. If we advertise br support
            # and the server uses it, we'll receive raw compressed bytes that
            # look like binary garbage. Many sites (rehabps.cz, agilept.com,
            # spinalmanipulation.org) negotiate brotli when offered.
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Ch-Ua": '"Chromium";v="120", "Not?A_Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })
    try:
        time.sleep(DELAY_BETWEEN_REQUESTS)
        resp = _session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            log(f"  RATE LIMITED on {url}, backing off 30s")
            time.sleep(30)
            resp = _session.get(url, timeout=REQUEST_TIMEOUT)
        # Treat 403/415 as candidates for the Playwright fallback rather
        # than a hard error — these are often anti-bot blocks that a real
        # browser would clear.
        if resp.status_code in (403, 415, 429, 503) and allow_playwright_fallback and USE_PLAYWRIGHT:
            log(f"  static fetch got {resp.status_code} on {url}; "
                f"retrying with Playwright...")
            html = render_with_playwright(url, wait_ms=6000)
            if html:
                return html
            # If Playwright also fails, fall through to raise_for_status
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log(f"  FETCH FAILED {url}: {e}")
        # Last-ditch fallback: if user passed --use-playwright, try the
        # headless browser even on RequestException for sites flagged as
        # "Playwright-eligible".
        if allow_playwright_fallback and USE_PLAYWRIGHT:
            log(f"  retrying {url} via Playwright after static failure...")
            html = render_with_playwright(url, wait_ms=6000)
            if html:
                return html
        return None


# ----------------------------------------------------------------------------
# Location filter
# ----------------------------------------------------------------------------

def in_radius(location_text: str) -> bool:
    """Cheap keyword match — adequate for most provider listings.

    Each keyword is checked as a substring, BUT for keywords containing a
    state-code pattern (", ca"), the character immediately after must be a
    boundary (comma, space, or end-of-string). This prevents false matches
    like "Valencia, Carabobo, Venezuela" hitting the "valencia, ca" keyword.

    NOTE: Returns False for virtual/webinar/online events. Use
    keep_for_listing() if you also want to keep virtual events.
    """
    if not location_text:
        return False
    # Append sentinel space so end-of-string state codes (", CA") still match.
    lt = location_text.lower() + " "
    # Webinar / online events are excluded — only counts in-person CA.
    online_signals = ("webinar", "online", "virtual", "zoom", "livestream", "remote")
    if any(s in lt for s in online_signals):
        return False
    for kw in BAY_AREA_KEYWORDS:
        idx = lt.find(kw)
        if idx < 0:
            continue
        # If the keyword ends in a state code (e.g. "ca"), require a
        # non-letter follow-on character so we don't match "Carabobo".
        end = idx + len(kw)
        if end >= len(lt):
            return True
        nxt = lt[end]
        # Boundary: comma, space, period, end-of-string. NOT another letter.
        if nxt.isalpha() or nxt.isdigit():
            continue
        return True
    return False


_VIRTUAL_SIGNALS = (
    "webinar", "online", "virtual", "zoom",
    "livestream", "live stream", "remote",
    "tele-course", "telecourse", "teleseminar",
)


def is_virtual(location_text: str) -> bool:
    """True when the location text indicates a virtual / online / webinar
    event (not tied to a physical venue).

    Used to bucket events into a separate 'Virtual / Online / Webinar'
    section in the rendered output.
    """
    if not location_text:
        return False
    lt = location_text.lower()
    return any(s in lt for s in _VIRTUAL_SIGNALS)


def keep_for_listing(location_text: str) -> bool:
    """True if a course should appear in the listing at all.

    Policy: keep CA in-person events AND virtual/online/webinar events.
    Drop only out-of-region in-person events (e.g. 'Neptune, NJ', 'Boise, ID').
    """
    return in_radius(location_text) or is_virtual(location_text)


# ----------------------------------------------------------------------------
# PT-attendable filter
# ----------------------------------------------------------------------------
# Policy: include courses if EITHER
#   (a) the provider is PT-primary (UCSF DPT, CPTA, Agile PT, Herman & Wallace,
#       EIM, Great Lakes, Summit, Mulligan MCTA — all teach to PTs as primary
#       audience), OR
#   (b) the course is cross-disciplinary and PT is explicitly named in the
#       audience or CE approvals (ART, Barbell Rehab, Cup Therapy / MFD).
# Exclude courses where the audience is purely non-PT (DC-only CBCE seminars,
# LMT-only NCBTMB workshops, DO-only AOA CME, instructor-cert-only S&C events).

PT_PRIMARY_PROVIDERS = {
    "ucsf department of pt & rehab science",
    "agile physical therapy",
    "california physical therapy association",
    "california physical therapy association (cpta)",
    "herman & wallace",
    "herman & wallace pelvic rehabilitation institute",
    "evidence in motion",
    "great lakes seminars",
    "summit professional education",
    "mulligan mwm usa / mcta",
    "vestibularpt",
    "university of pittsburgh",
    "postural restoration institute",
    "pri",
    "institute of physical art",
    "ipa",
    "northeast seminars",     # Kevin Wilk live courses
    "kevin wilk",
    "wilk physical therapy institute",
    "johns hopkins clinical vestibular",
    "american institute of balance",
    "functional movement systems",
    "fms",
    "amsi",
    "amsi training",
    "american academy of manipulative therapy",
    "spinal manipulation institute",
    "aamt",
    "functional range systems",
    "frs",
    "functional anatomy seminars",
    "pain free training",
    "pain free training (ppsc)",
    "ppsc",
    "dns",
    "dns / prague school of rehabilitation",
    "prague school of rehabilitation",
    "rehab prague school",
}

NON_PT_KEYWORDS = (
    "dc-only", "dcs only", "chiropractors only",
    "lmts only", "massage therapists only",
    "dos only", "physicians only",
    "instructor certification only",
)


def is_pt_attendable(course: Course) -> bool:
    """Return False to exclude this course from the output."""
    if not course.pt_attendable:
        return False
    provider_norm = course.provider.lower().strip()
    if any(p in provider_norm for p in PT_PRIMARY_PROVIDERS):
        return True
    audience = course.audience.lower()
    # Cross-disciplinary courses must have PT (or PTA) explicitly in audience.
    if "pt" in audience or "physical therap" in audience:
        return True
    # PT CEUs explicitly offered counts as evidence PTs can attend.
    if course.pt_ceus.lower() in ("yes", "via reciprocity"):
        return True
    # Explicit exclusion markers
    if any(kw in audience for kw in NON_PT_KEYWORDS):
        return False
    # Default conservative: if we can't tell, exclude — better to miss a course
    # than to clutter the list with DC/LMT/DO-only events.
    return False


# ----------------------------------------------------------------------------
# Date parsing utilities
# ----------------------------------------------------------------------------

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

def parse_date_loose(s: str) -> str:
    """Try several common formats. Return ISO YYYY-MM-DD or '' if unparseable."""
    if not s:
        return ""
    s = s.strip()
    # ISO already?
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    # 2026-04-26 14:00:00 UTC — strip time
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    # "April 25-26, 2026" / "April 25, 2026" / "Sept. 19-20, 2026"
    m = re.search(
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z\.]*\s+(\d{1,2})(?:[-–]\d{1,2})?,?\s+(\d{4})",
        s.lower(),
    )
    if m:
        month = MONTHS.get(m.group(1).strip("."))
        day = int(m.group(2))
        year = int(m.group(3))
        if month:
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                return ""
    # "9/19/2026" or "9-19-2026"
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return ""
    return ""


def within_window(start_date_iso: str, today: date, days: int = 365) -> bool:
    """True if start_date is in [today, today + days]."""
    if not start_date_iso:
        return True  # keep TBD entries; user will see them as 'date TBD'
    try:
        d = date.fromisoformat(start_date_iso)
    except ValueError:
        return True
    return today <= d <= (today + timedelta(days=days))


# ----------------------------------------------------------------------------
# Provider scrapers
# Each function takes no args, returns list[Course].
# All scraping is best-effort: if a site changes layout, that provider is
# logged and skipped while the rest continue running.
# ----------------------------------------------------------------------------

def scrape_active_release() -> list[Course]:
    """Active Release Techniques workshop schedule."""
    url = "https://education.activerelease.com/workshops"
    html = http_get(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    courses: list[Course] = []

    # Each course is in an <h3> with a link, followed by location and date text.
    for h3 in soup.find_all("h3"):
        link = h3.find("a")
        if not link:
            continue
        title = link.get_text(strip=True)
        course_url = link.get("href", "")
        if course_url.startswith("/"):
            course_url = "https://education.activerelease.com" + course_url

        # Walk forward through siblings to find location + date block.
        location_text = ""
        date_text = ""
        node = h3
        for _ in range(6):
            node = node.find_next_sibling()
            if not node:
                break
            text = node.get_text(" ", strip=True)
            if not text:
                continue
            if not location_text and not re.match(r"\d{4}-\d{2}-\d{2}", text):
                location_text = text
            elif "UTC" in text or re.search(r"\d{4}-\d{2}-\d{2}", text):
                date_text = text
                break

        if not keep_for_listing(location_text):
            continue

        start_iso = parse_date_loose(date_text)
        all_dates = re.findall(r"\d{4}-\d{2}-\d{2}", date_text)
        end_iso = all_dates[-1] if len(all_dates) > 1 else ""

        courses.append(Course(
            course_id=make_course_id("ART", title, start_iso, location_text),
            provider="Active Release Techniques",
            title=title,
            start_date=start_iso,
            end_date=end_iso,
            location=location_text,
            url=course_url,
            audience="PT, DC, ATC, MD/DO",
            pt_ceus="yes",
            notes="CA is on ART's approved-state list; verify exact CE hours on registration page.",
        ))
    return courses


def scrape_barbell_rehab() -> list[Course]:
    """Barbell Rehab certification schedule.

    Source: https://barbellrehab.com/certification/

    The certification page lists 3 different live-cert tracks in the top nav:
      - Barbell Rehab Method (BRM) Certification — 15 CE hours, CERS-approved (CA PT board)
      - Weightlifting Certification (BRW)
      - Sports Performance Certification (BRS)

    Each track has an "upcoming events" table and corresponding nav links of
    the form "Sep 26-27: Sacramento, CA". We scrape all 3 tracks and tag the
    title with the appropriate certification name.
    """
    url = "https://barbellrehab.com/certification/"
    # Confirmed CA dates from the May 2026 scrape — kept as a fallback so this
    # provider still surfaces data when network is unavailable. Live fetch
    # below dedupes against these.
    SEED_BARBELL_CA = [
        ("2026-09-26", "2026-09-27",
         "Barbell Rehab Method Certification (BRM)", "Sacramento, CA"),
    ]
    courses: list[Course] = []
    seen: set[tuple] = set()

    def emit(start_iso, end_iso, title, location, href, sold_out=False):
        key = (title, start_iso, location.lower())
        if key in seen:
            return
        seen.add(key)
        notes = "15 contact hours by CERS (CA PT board); also NSCA Cat A 1.5 CEUs, NASM/AFAA/ACE/BOC 15.0."
        if sold_out:
            notes = "SOLD OUT — waitlist or future date. " + notes
        courses.append(Course(
            course_id=make_course_id("Barbell Rehab", title, start_iso, location),
            provider="Barbell Rehab",
            title=title,
            start_date=start_iso,
            end_date=end_iso,
            location=location,
            url=href,
            audience="PT, PTA, DC, ATC, S&C coaches, personal trainers, LMTs",
            pt_ceus="yes (CERS, CA PT board)",
            notes=notes,
        ))

    # Seed pass first so live fetch dedupes against confirmed entries.
    for iso_start, iso_end, title, loc in SEED_BARBELL_CA:
        emit(iso_start, iso_end, title, loc, url)

    html = http_get(url, allow_playwright_fallback=True)
    if not html:
        return courses
    soup = BeautifulSoup(html, "html.parser")

    # Per-link extraction: pattern "Sep 26-27: Sacramento, CA" or "Sep 26-27:
    # Sacramento, CA (SOLD OUT)". The href path tells us which track.
    link_re = re.compile(
        r"^(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z\.]*\s+"
        r"(\d{1,2})(?:[-–](\d{1,2}))?\s*:\s*(.+?)\s*(\(SOLD OUT\))?$",
        re.IGNORECASE,
    )
    today = date.today()

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        m = link_re.match(text)
        if not m:
            continue
        month = MONTHS.get(m.group(1).lower().strip("."))
        if not month:
            continue
        day_start = int(m.group(2))
        day_end = int(m.group(3)) if m.group(3) else day_start
        location_text = m.group(4).strip()
        sold_out = bool(m.group(5))
        if not keep_for_listing(location_text):
            continue
        year = today.year if month >= today.month else today.year + 1
        try:
            start_iso = date(year, month, day_start).isoformat()
            end_iso = date(year, month, day_end).isoformat() if day_end != day_start else ""
        except ValueError:
            continue

        href = a.get("href", "")
        # Identify which track based on the URL slug.
        if "-brw" in href:
            title = "Barbell Rehab Weightlifting Certification (BRW)"
        elif "-brs" in href:
            title = "Barbell Rehab Sports Performance Certification (BRS)"
        else:
            title = "Barbell Rehab Method Certification (BRM)"

        emit(start_iso, end_iso, title, location_text, href, sold_out=sold_out)

    return courses


def scrape_mulligan() -> list[Course]:
    """Mulligan MWM USA upcoming course list (NA-MCTA mirror)."""
    # mulliganmwmusa.com has per-event pages; the listing is on their courses page.
    url = "https://www.mulliganmwmusa.com/"
    html = http_get(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    courses: list[Course] = []
    # Look for course event cards/links containing city and date.
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        # Patterns like "Novato - March 21-22 2026" or "Oceanside-October 3-4 2026"
        m = re.search(
            r"([A-Za-z][A-Za-z\s\-]+?)\s*[-–]\s*"
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z\.]*\s+"
            r"(\d{1,2})(?:[-–](\d{1,2}))?\s+(\d{4})",
            text.lower(),
        )
        if not m:
            continue
        city = m.group(1).strip()
        month = MONTHS.get(m.group(2).strip("."))
        day_start = int(m.group(3))
        day_end = int(m.group(4)) if m.group(4) else day_start
        year = int(m.group(5))
        if not keep_for_listing(city):
            continue
        try:
            start_iso = date(year, month, day_start).isoformat()
            end_iso = date(year, month, day_end).isoformat() if day_end != day_start else ""
        except (ValueError, TypeError):
            continue
        courses.append(Course(
            course_id=make_course_id("Mulligan", text, start_iso, city),
            provider="Mulligan MWM USA / MCTA",
            title=text,
            start_date=start_iso,
            end_date=end_iso,
            location=city.title(),
            url=a.get("href", ""),
            audience="PT, OT",
            pt_ceus="yes",
            notes="Mulligan Concept courses are typically CA PT board approved; verify on event page.",
        ))
    return courses


def scrape_cup_therapy() -> list[Course]:
    """Cup Therapy / Integrated Movement Health course calendar.

    Source: https://www.cuptherapy.com/courses

    Each course is rendered as a product card whose link text follows one of
    these patterns:
      "May 16, 2026 Sacramento, CA Level 1 with Full MFD Kit"
      "May 16, 2026 Sacramento, CA Level 1 with Full MFD Kit - SOLD OUT"
      "August 6-7, 2026 SFGH: Fresh Cadaver Dissection ... w/ Carla Stecco"
      "Online Myofascial Decompression Level 1"  (no date — online; skipped)

    We pull dates from each link, filter by CA, and tag SOLD OUT entries.
    """
    url = "https://www.cuptherapy.com/courses"
    # Confirmed CA dates from the May 2026 scrape — fallback when network is
    # unavailable. Live fetch dedupes against these.
    SEED_CUP_CA = [
        ("2026-05-16", "",          "Level 1 with Full MFD Kit (SOLD OUT)",
         "Sacramento, CA",
         "https://www.cuptherapy.com/product-page/sacramento-ca-level-1-with-full-mfd-kit",
         True),
        ("2026-05-17", "",          "Level 2 Advanced with MFD Precision Pump",
         "Sacramento, CA",
         "https://www.cuptherapy.com/product-page/sacramento-ca-level-2-advanced-with-mfd-precision-pump",
         False),
        ("2026-06-13", "",          "Level 1 with Full MFD Kit",
         "San Diego, CA",
         "https://www.cuptherapy.com/product-page/san-diego-ca-level-1-with-full-mfd-kit",
         False),
        ("2026-06-14", "",          "Level 2 Advanced with MFD Precision Pump",
         "San Diego, CA",
         "https://www.cuptherapy.com/product-page/san-diego-level-2-advanced-with-mfd-precision-pump",
         False),
        ("2026-07-11", "",          "Level 1 with Full MFD Kit",
         "San Francisco, CA",
         "https://www.cuptherapy.com/product-page/san-francisco-ca-level-1-with-full-mfd-kit-1",
         False),
        ("2026-07-12", "",          "Level 2 Advanced with MFD Precision Pump",
         "San Francisco, CA",
         "https://www.cuptherapy.com/product-page/san-francisco-ca-level-2-advanced-with-mfd-precision-pump",
         False),
        ("2026-07-18", "",          "Sacramento, CA Blood Flow Restriction Certification",
         "Sacramento, CA",
         "https://www.cuptherapy.com/product-page/sacramento-ca-blood-flow-restriction-certification",
         False),
        ("2026-08-05", "",          "SFGH: Fresh Cadaver Investigation of the Upper Quarter and Trunk w/ Carla Stecco",
         "San Francisco, CA",
         "https://www.cuptherapy.com/product-page/sfgh-fresh-cadaver-investigation-of-the-upper-quarter-and-trunk-w-carla-stecco",
         False),
        ("2026-08-06", "2026-08-07","SFGH: Fresh Cadaver Dissection Lower Quarter and Lumbo-Pelvic w/ Carla Stecco",
         "San Francisco, CA",
         "https://www.cuptherapy.com/product-page/ucsf-fresh-cadaver-dissection-lower-extremity-amp-lower-trunk-pelvis",
         False),
        ("2026-11-14", "",          "Level 1 with Full MFD Kit",
         "Laguna Niguel, CA",
         "https://www.cuptherapy.com/product-page/laguna-niguel-ca-level-1-with-full-mfd-kit",
         False),
        ("2026-11-15", "",          "Level 2 Advanced with MFD Precision Pump",
         "Laguna Niguel, CA",
         "https://www.cuptherapy.com/product-page/laguna-niguel-ca-level-2-advanced-with-mfd-precision-pump",
         False),
    ]
    courses: list[Course] = []
    seen: set[tuple] = set()

    def emit_cup(start_iso, end_iso, title, location, href, sold_out=False):
        key = (title.lower(), start_iso, location.lower())
        if key in seen:
            return
        seen.add(key)
        notes = ("Provider lists CA PT board reciprocity (CERS-route); FSBPT-credentialed in "
                 "most states. Verify level (L1/L2/BFR) and hours on product page.")
        if sold_out:
            notes = "SOLD OUT — waitlist via product page. " + notes
        courses.append(Course(
            course_id=make_course_id("Cup Therapy", title, start_iso, location),
            provider="Integrated Movement Health / Cup Therapy",
            title=title,
            start_date=start_iso,
            end_date=end_iso,
            location=location,
            url=href,
            audience="PT, ATC, LMT, PTA, OT, MD, DO, LAc",
            pt_ceus="yes",
            notes=notes,
        ))

    # Seed pass first so live fetch dedupes against confirmed entries.
    for iso_start, iso_end, title, loc, href, sold_out in SEED_CUP_CA:
        emit_cup(iso_start, iso_end, title, loc, href, sold_out)

    html = http_get(url)
    if not html:
        return courses
    soup = BeautifulSoup(html, "html.parser")

    # Pattern 1: single-day "May 16, 2026 Location, ST Description"
    # Pattern 2: range "August 6-7, 2026 ..." or "May 16-17, 2026 ..."
    single_re = re.compile(
        r"^(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+"
        r"(\d{1,2}),\s+(20\d{2})\s+(.+)$",
        re.IGNORECASE,
    )
    range_re = re.compile(
        r"^(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+"
        r"(\d{1,2})[\-\u2013](\d{1,2}),\s+(20\d{2})\s+(.+)$",
        re.IGNORECASE,
    )

    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not text or "/product-page/" not in a.get("href", ""):
            continue
        if text.lower().startswith("online"):
            continue

        # Try range first, then single date.
        start_iso = end_iso = ""
        rest = ""
        rm = range_re.match(text)
        sm = single_re.match(text)
        if rm:
            month_name, d1, d2, year, rest = rm.groups()
            try:
                start = datetime.strptime(f"{month_name} {d1} {year}", "%B %d %Y").date()
                end = datetime.strptime(f"{month_name} {d2} {year}", "%B %d %Y").date()
            except ValueError:
                continue
            start_iso, end_iso = start.isoformat(), end.isoformat()
        elif sm:
            month_name, d1, year, rest = sm.groups()
            try:
                start = datetime.strptime(f"{month_name} {d1} {year}", "%B %d %Y").date()
            except ValueError:
                continue
            start_iso = start.isoformat()
            end_iso = ""
        else:
            continue

        # Strip "- SOLD OUT" suffix into a flag.
        sold_out = False
        if rest.upper().endswith("- SOLD OUT"):
            rest = rest[:-len("- SOLD OUT")].rstrip(" -")
            sold_out = True

        # Pull location out of "rest". For the standard product cards, the
        # location appears as the first chunk before the course descriptor:
        #   "Sacramento, CA Level 1 with Full MFD Kit"
        loc_match = re.match(
            r"^([A-Z][A-Za-z\.\-' ]+?,\s*[A-Z]{2})\s+(.+)$", rest
        )
        if loc_match:
            location, title = loc_match.group(1).strip(), loc_match.group(2).strip()
        else:
            # Special-case the SFGH/UCSF cadaver dissection cards which lead
            # with the venue acronym, not a city.
            if rest.upper().startswith("SFGH") or rest.upper().startswith("UCSF"):
                location = "San Francisco, CA"
                title = rest
            else:
                continue

        if not keep_for_listing(location):
            continue

        href = a["href"]
        if href.startswith("/"):
            href = "https://www.cuptherapy.com" + href

        emit_cup(start_iso, end_iso, title, location, href, sold_out)
    return courses


def scrape_agile_pt() -> list[Course]:
    """Agile Physical Therapy continuing-education page (Bay Area-based provider).

    Source: https://agilept.com/education/continuing-education/

    Agile runs an annual "Sports Movement Analysis Series" with 3 day-long
    in-person labs at their Palo Alto location, plus online lectures. The
    page format has been observed in two layouts:

        Day 1 - May 16, 2026: Cycling, Running, Lifting   (legacy)
        Day 1: Running, Cycling, Swimming (Lab: May 17) – $200   (current)

    We try both patterns so we don't break on a future format change.
    """
    url = "https://agilept.com/education/continuing-education/"
    html = http_get(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    courses: list[Course] = []
    text = soup.get_text("\n", strip=True)

    today = date.today()
    seen: set[tuple] = set()

    def emit(iso: str, topics: str):
        key = (iso, topics.lower())
        if key in seen:
            return
        seen.add(key)
        courses.append(Course(
            course_id=make_course_id("Agile PT", topics, iso, "Palo Alto"),
            provider="Agile Physical Therapy",
            title=f"Sports Movement Analysis: {topics}",
            start_date=iso,
            end_date=iso,
            location="Agile PT, 3825 El Camino Real, Palo Alto, CA 94306",
            url=url,
            audience="PT, sports rehab clinicians",
            pt_ceus="yes",
            notes="Confirm CERS approval at registration. Each day-package "
                  "bundles online lecture + in-person lab at Palo Alto.",
        ))

    # Format 1 (current, 2026): "Day N: topics (Lab: Month DD)"
    current_pattern = re.compile(
        r"day\s*\d+\s*:\s*"
        r"([^()\n]+?)\s*"                                  # 1: topics
        r"\(\s*lab\s*:\s*"
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z\.]*\s+"
        r"(\d{1,2})"                                       # 3: day
        r"(?:,\s*(\d{4}))?\s*\)",                          # 4: optional year
        re.IGNORECASE,
    )
    for m in current_pattern.finditer(text):
        topics = m.group(1).strip().rstrip(",;:- ")
        month = MONTHS.get(m.group(2).lower().strip("."))
        day = int(m.group(3))
        year_str = m.group(4)
        # Infer year if absent. Default current year, bump to next if the
        # date has already passed.
        try:
            year = int(year_str) if year_str else today.year
            candidate = date(year, month, day)
            if not year_str and candidate < today:
                candidate = date(year + 1, month, day)
        except (ValueError, TypeError):
            continue
        emit(candidate.isoformat(), topics)

    # Format 2 (legacy): "Day N - Month DD, YYYY: topics"
    legacy_pattern = re.compile(
        r"day\s*\d+\s*[-–]\s*"
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z\.]*\s+"
        r"(\d{1,2}),?\s+(\d{4})\s*:\s*(.+)",
        re.IGNORECASE,
    )
    for m in legacy_pattern.finditer(text):
        month = MONTHS.get(m.group(1).lower().strip("."))
        day = int(m.group(2))
        year = int(m.group(3))
        topics = m.group(4).split("\n")[0].strip()
        try:
            iso = date(year, month, day).isoformat()
        except (ValueError, TypeError):
            continue
        emit(iso, topics)

    log(f"  Agile PT: parsed {len(courses)} CA event(s)")
    if not courses and html:
        log("  Agile PT: 0 events parsed from non-empty HTML — dumping for "
             "inspection.")
        dump_html_for_debug("agile", url, html)
        snippet = re.sub(r"\s+", " ", html[:400])
        log(f"  Agile PT: response starts with: {snippet!r}")
    return courses


def scrape_cpta() -> list[Course]:
    """California Physical Therapy Association — courses & events.

    Sources:
      - https://www.ccapta.org/page/CPTACourses   (full course list)
      - https://www.ccapta.org/                    (homepage banner with annual conference)

    **Important**: As of May 2026, CPTA's site is behind a Cloudflare bot
    challenge ("Just a moment..." page) that detects both python-requests
    AND headless Chromium browsers. We cannot bypass this without a real
    user-agent browser (or services like ScrapingBee).

    Behavior:
      - If the response contains the Cloudflare challenge marker, log a
        clear message and return seed entries only.
      - If somehow we get past the challenge (network changes, IP rotation
        etc.), parse normally.

    To add a CPTA course manually: append a tuple (start_iso, end_iso, title,
    location) to SEED_CPTA_CA below after checking the site in your browser.
    """
    courses_url = "https://www.ccapta.org/page/CPTACourses"
    home_url = "https://www.ccapta.org/"
    courses: list[Course] = []
    seen: set[tuple] = set()
    today = date.today()

    # Manual seed list — append entries after manually browsing
    # https://www.ccapta.org/page/CPTACourses since the site blocks
    # automated scrapers. Tuple format: (iso_start, iso_end, title, location).
    # Both in-person CA events AND virtual/online/webinar events are kept
    # (virtual events appear in a separate section in courses_current.md).
    SEED_CPTA_CA: list[tuple] = [
        # CPTA Annual Conference 2026 — confirmed via public search snippets
        # of ccapta.org. Theme: "The Future of Movement". Eligible for
        # contact hours per CPTA Annual Conference page.
        ("2026-09-19", "2026-09-20",
         "CPTA Annual Conference 2026 — The Future of Movement",
         "DoubleTree by Hilton, San Jose, CA"),
    ]

    def is_cloudflare_challenge(html_text: str) -> bool:
        """Detect Cloudflare's 'Just a moment...' page."""
        if not html_text:
            return False
        head = html_text[:2000].lower()
        return ("just a moment" in head and "challenge" in head) or \
               "cf_chl_opt" in head or "cf-challenge" in head

    def emit(iso_start, iso_end, title, location, src_url, notes=""):
        loc_low = location.lower()
        if any(k in loc_low for k in ("zoom", "online", "webinar", "virtual")):
            title = f"{title} (Online via Zoom)"
        key = (title.lower(), iso_start, location.lower())
        if key in seen:
            return
        seen.add(key)
        courses.append(Course(
            course_id=make_course_id("CPTA", title, iso_start, location),
            provider="California Physical Therapy Association (CPTA)",
            title=title,
            start_date=iso_start,
            end_date=iso_end,
            location=location,
            url=src_url,
            audience="PT, PTA, students",
            pt_ceus="yes",
            notes=notes or "CPTA-provided CE — CERS-approved for CA PTs.",
        ))

    # --- Source 1: the dedicated courses page (most reliable) ---
    html = http_get(courses_url, allow_playwright_fallback=True)
    if html and is_cloudflare_challenge(html):
        log("  CPTA: Cloudflare challenge detected on /page/CPTACourses — "
            "site is bot-blocked even via Playwright. Skipping. Visit "
            "the URL in a browser to see the full course list; add "
            "confirmed CA dates to SEED_CPTA_CA in scrape_cpta() to track.")
        html = None  # don't try to parse the challenge page

    if html:
        soup = BeautifulSoup(html, "html.parser")
        for s in soup(["script", "style"]):
            s.decompose()
        text = soup.get_text("\n", strip=True)

        # Each course entry on CPTACourses looks like:
        #   March 28-29, 2026 Credentialed Clinical Instructor Program (CCIP) Level 1
        #   Online via ZOOM
        #   8:00 am - 5:30 pm (PT)
        #   Presenters: ...
        #   Course Number: ...
        #
        # Pattern: month + day(s) + year, then title on same/next line,
        # then location line.
        entry_re = re.compile(
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+"
            r"(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?,?\s+(20\d{2})\s+"
            r"([^\n]+)",
            re.IGNORECASE,
        )
        cpta_count = 0
        for m in entry_re.finditer(text):
            mo, d1, d2, yy, title = m.groups()
            try:
                start = datetime.strptime(f"{mo} {d1} {yy}", "%B %d %Y").date()
                end = (datetime.strptime(f"{mo} {d2 or d1} {yy}", "%B %d %Y")
                       .date())
            except ValueError:
                continue
            if start < today:
                continue
            title = title.strip()
            # Look ahead for the next line that names a location/format.
            tail = text[m.end():m.end() + 200]
            loc_line = ""
            for line in tail.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Stop when we hit a presenter or time-of-day line.
                if line.lower().startswith(("presenter", "course number",
                                            "8:", "9:", "10:", "11:", "12:",
                                            "1:", "2:", "3:", "4:", "5:", "6:", "7:")):
                    break
                loc_line = line
                break
            emit(start.isoformat(), end.isoformat(), title, loc_line or "Online via ZOOM",
                 courses_url)
            cpta_count += 1
        log(f"  CPTA: parsed {cpta_count} course entries from CPTACourses page")
        if cpta_count == 0:
            log("  CPTA: 0 entries parsed from courses page — dumping response.")
            dump_html_for_debug("cpta_courses", courses_url, html)
            snippet = re.sub(r"\s+", " ", html[:400])
            log(f"  CPTA courses: response starts with: {snippet!r}")
    else:
        log("  CPTA: courses page fetch failed (will still try homepage)")

    # --- Source 2: homepage annual-conference banner ---
    home_html = http_get(home_url, allow_playwright_fallback=True)
    if home_html and is_cloudflare_challenge(home_html):
        log("  CPTA: Cloudflare challenge on homepage too. Skipping.")
        home_html = None
    if home_html:
        body_text = BeautifulSoup(home_html, "html.parser").get_text(" ", strip=True)
        m = re.search(
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z\.]*\s+"
            r"(\d{1,2})(?:[-–](\d{1,2}))?\s+at\s+([^\.]+)",
            body_text.lower(),
        )
        if m:
            month = MONTHS.get(m.group(1).strip("."))
            day_start = int(m.group(2))
            day_end = int(m.group(3)) if m.group(3) else day_start
            venue = m.group(4).strip()
            year = today.year if month >= today.month else today.year + 1
            if in_radius(venue):
                try:
                    iso = date(year, month, day_start).isoformat()
                    iso_end = date(year, month, day_end).isoformat()
                    emit(iso, iso_end, "CPTA Annual Conference", venue.title(),
                         "https://www.ccapta.org/page/CPTAAnnualConference",
                         notes="Flagship CA PT event; sessions fill up.")
                except (ValueError, TypeError):
                    pass
        else:
            log("  CPTA: homepage banner pattern not found — dumping.")
            dump_html_for_debug("cpta_home", home_url, home_html)

    # --- Source 3: manual seed entries ---
    for iso_start, iso_end, title, loc in SEED_CPTA_CA:
        emit(iso_start, iso_end, title, loc,
             "https://www.ccapta.org/page/CPTACourses",
             notes="Manually seeded — CPTA site is bot-blocked.")

    return courses


def scrape_ucsf() -> list[Course]:
    """UCSF DPT continuing education page.
    Note: This page returned 403 in our testing — UCSF blocks some bots.
    The scraper falls back gracefully and the user should check the URL manually.
    """
    url = "https://ptrehab.ucsf.edu/continuing-education"
    html = http_get(url)
    if not html:
        log("  UCSF: no HTML returned (likely 403 — needs manual check)")
        return []
    soup = BeautifulSoup(html, "html.parser")
    courses: list[Course] = []
    # UCSF format: blocks of "Date/Time: April 25-26, 2026..." followed by "Location: ..."
    text = soup.get_text("\n", strip=True)
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        if "Date/Time" not in block:
            continue
        date_match = re.search(
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z\.]*\s+"
            r"(\d{1,2})(?:[-–](\d{1,2}))?,?\s+(\d{4})",
            block.lower(),
        )
        if not date_match:
            continue
        month = MONTHS.get(date_match.group(1).strip("."))
        day_start = int(date_match.group(2))
        year = int(date_match.group(4))
        try:
            iso = date(year, month, day_start).isoformat()
        except (ValueError, TypeError):
            continue
        loc_match = re.search(r"Location:\s*([^\n]+)", block)
        location = loc_match.group(1).strip() if loc_match else ""
        if not keep_for_listing(location):
            continue
        # Pull a title — first line of the block
        title = block.split("\n", 1)[0][:120]
        courses.append(Course(
            course_id=make_course_id("UCSF", title, iso, location),
            provider="UCSF Department of PT & Rehab Science",
            title=title,
            start_date=iso,
            end_date="",
            location=location,
            url=url,
            audience="PT and allied health",
            pt_ceus="yes",
            notes="UCSF DPT-hosted CE typically carries CA PT approval.",
        ))
    return courses


# ----------------------------------------------------------------------------
# Postural Restoration Institute (PRI)
# ----------------------------------------------------------------------------

def scrape_pri() -> list[Course]:
    """Postural Restoration Institute — 2026 in-person western-state courses.

    Source: https://www.posturalrestoration.com/courses/ (JS-rendered) and the
    2026 brochure PDF at
    https://cdn-space2.dbmgo.com/pri/wp-content/uploads/2025/12/2026-PRI-Brochure-FINAL.pdf

    Strategy: the live calendar is JS-heavy, so we seed a hard-coded list from
    the published 2026 brochure (extracted May 10, 2026). The scraper also
    attempts to fetch the live courses page; any future-dated additions found
    there will be merged in. Update SEED_PRI_2026 annually when the new
    brochure drops.
    """
    base_url = "https://www.posturalrestoration.com/courses/"
    # (start_iso, end_iso, title, city_state)
    SEED_PRI_2026 = [
        ("2026-09-26", "2026-09-27", "Pelvis Restoration",        "Valencia, CA"),
        ("2026-10-24", "2026-10-25", "Postural Respiration",      "Orange, CA"),
        ("2026-11-14", "2026-11-15", "Myokinematic Restoration",  "Valencia, CA"),
    ]
    courses: list[Course] = []
    for iso_start, iso_end, title, loc in SEED_PRI_2026:
        courses.append(Course(
            course_id=make_course_id("PRI", title, iso_start, loc),
            provider="Postural Restoration Institute (PRI)",
            title=title,
            start_date=iso_start,
            end_date=iso_end,
            location=loc,
            url=base_url,
            audience="PT, PTA, OT, DC, ATC, S&C, LMT, Pilates instructors",
            pt_ceus="via reciprocity",
            notes="PRI courses are PT board approved with reciprocity in AZ, CO, OR, WA "
                  "and many other states; CA requires self-submission. Verify per course.",
        ))
    # Optionally try the live page to pick up new dates added post-brochure.
    html = http_get(base_url)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        # The live page lists e.g. "May 15-16, 2026 (Seattle, WA)" — pick up any
        # such entries we don't already have. Cheap heuristic; safe to skip if
        # nothing matches.
        pattern = re.compile(
            r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z\.]*\s+"
            r"(\d{1,2})[-–](\d{1,2}),?\s+(\d{4})\s*\(([^)]+)\)",
            re.IGNORECASE,
        )
        for m in pattern.finditer(text):
            try:
                month = MONTHS.get(m.group(1).lower().strip("."))
                year = int(m.group(4))
                d1 = date(year, month, int(m.group(2)))
                d2 = date(year, month, int(m.group(3)))
                loc = m.group(5).strip()
                if not keep_for_listing(loc):
                    continue
                cid = make_course_id("PRI", "(scraped)", d1.isoformat(), loc)
                if any(c.course_id == cid for c in courses):
                    continue
                courses.append(Course(
                    course_id=cid,
                    provider="Postural Restoration Institute (PRI)",
                    title="PRI Course (verify on site)",
                    start_date=d1.isoformat(),
                    end_date=d2.isoformat(),
                    location=loc,
                    url=base_url,
                    audience="PT, PTA, OT, DC, ATC, S&C, LMT",
                    pt_ceus="via reciprocity",
                ))
            except (ValueError, TypeError):
                continue
    return courses


# ----------------------------------------------------------------------------
# Institute of Physical Art (IPA) — Functional Manual Therapy
# ----------------------------------------------------------------------------

def scrape_ipa() -> list[Course]:
    """Institute of Physical Art (Functional Manual Therapy, Gregg & Vicky Johnson).

    Source: https://instituteofphysicalart.com/scheduled-courses/
    This page is HTML-table-based and parses cleanly; we still seed a baseline
    from the May 10, 2026 scrape so the script returns real data even if the
    DOM changes upstream.
    """
    base_url = "https://instituteofphysicalart.com/scheduled-courses/"
    # (start_iso, end_iso, title, city_state)
    SEED_IPA_2026 = [
        # CFS: CoreFirst Strategies
        ("2026-06-12", "2026-06-14", "CFS: CoreFirst Strategies",                       "Glendora, CA"),
        ("2026-07-17", "2026-07-19", "CFS: CoreFirst Strategies",                       "Fremont, CA"),
        ("2026-10-09", "2026-10-11", "CFS: CoreFirst Strategies",                       "Coronado, CA"),
        # DFA
        ("2026-09-19", "2026-09-20", "DFA: Dynamic Foot and Ankle",                     "Encinitas, CA"),
        # FM I
        ("2026-09-25", "2026-09-27", "FM I: Functional Mobilization I",                 "Westlake Village, CA"),
        ("2026-10-23", "2026-10-25", "FM I: Functional Mobilization I",                 "Pleasant Hill, CA"),
        # FMLE
        ("2026-06-12", "2026-06-14", "FMLE: Functional Mobilization Lower Extremities", "Huntington Beach, CA"),
        ("2026-09-18", "2026-09-20", "FMLE: Functional Mobilization Lower Extremities", "Mountain View, CA"),
        # FMLT
        ("2026-10-09", "2026-10-11", "FMLT: Functional Mobilization Lower Trunk",       "Van Nuys, CA"),
        # FMUE
        ("2026-05-15", "2026-05-17", "FMUE: Functional Mobilization Upper Extremities", "San Francisco, CA"),
        ("2026-11-13", "2026-11-15", "FMUE: Functional Mobilization Upper Extremities", "Glendora, CA"),
        # FMUT
        ("2026-05-15", "2026-05-17", "FMUT: Functional Mobilization Upper Trunk",       "Fountain Valley, CA"),
        # GAIT
        ("2026-10-16", "2026-10-18", "GAIT: Functional Gait",                           "San Francisco, CA"),
        # KSC
        ("2026-06-27", "2026-06-28", "KSC: Kinetic Shoulder Complex",                   "Coronado, CA"),
        ("2026-09-19", "2026-09-20", "KSC: Kinetic Shoulder Complex",                   "Ontario, CA"),
        ("2026-11-07", "2026-11-08", "KSC: Kinetic Shoulder Complex",                   "Fremont, CA"),
        # PGP
        ("2026-06-13", "2026-06-14", "PGP: The Pelvic Girdle Puzzle",                   "Belmont, CA"),
        ("2026-10-03", "2026-10-04", "PGP: The Pelvic Girdle Puzzle",                   "Fresno, CA"),
        # PNF
        ("2026-05-15", "2026-05-17", "PNF: Functional Neuromuscular and Motor Control", "Westlake Village, CA"),
        ("2026-09-18", "2026-09-20", "PNF: Functional Neuromuscular and Motor Control", "Antioch, CA"),
        ("2026-11-06", "2026-11-08", "PNF: Functional Neuromuscular and Motor Control", "Fountain Valley, CA"),
        ("2026-11-06", "2026-11-08", "PNF: Functional Neuromuscular and Motor Control", "San Carlos, CA"),
        # REM
        ("2026-09-11", "2026-09-13", "REM: Resistance Enhanced Manipulation",           "Fresno, CA"),
        # SOP
        ("2026-07-18", "2026-07-19", "SOP: Strategies for Optimizing Performance",      "Mountain View, CA"),
        # VFM
        ("2026-07-24", "2026-07-26", "VFM: Visceral Functional Mobilization",           "Ontario, CA"),
        ("2026-08-07", "2026-08-09", "VFM: Visceral Functional Mobilization",           "San Francisco, CA"),
    ]
    courses: list[Course] = []
    for iso_start, iso_end, title, loc in SEED_IPA_2026:
        courses.append(Course(
            course_id=make_course_id("IPA", title, iso_start, loc),
            provider="Institute of Physical Art (IPA)",
            title=title,
            start_date=iso_start,
            end_date=iso_end,
            location=loc,
            url=base_url,
            audience="PT, PTA (FMT system)",
            pt_ceus="yes",
            notes="IPA courses count toward CFMT Certification and IPA Residency/Fellowship.",
        ))
    return courses


# ----------------------------------------------------------------------------
# Great Lakes Seminars (GLS)
# ----------------------------------------------------------------------------

def scrape_great_lakes() -> list[Course]:
    """Great Lakes Seminars — California / western US 2026 in-person dates.

    Source: https://glseminars.com/courses/view-schedule/
    The live page is JS-rendered via Arlo widget; we seed from the May 10,
    2026 scrape. GLS rotates dates throughout the year, so re-check quarterly.
    """
    base_url = "https://glseminars.com/courses/view-schedule/"
    SEED_GLS_2026 = [
        ("2026-06-06", "2026-06-07", "Mobilization of the Cervical and Thoracic Spine and Ribs",
         "Burlingame, CA"),
        ("2026-08-08", "2026-08-09", "An Introduction to Vestibular Rehabilitation",
         "Novato, CA"),
    ]
    courses: list[Course] = []
    for iso_start, iso_end, title, loc in SEED_GLS_2026:
        courses.append(Course(
            course_id=make_course_id("GLS", title, iso_start, loc),
            provider="Great Lakes Seminars (GLS)",
            title=title,
            start_date=iso_start,
            end_date=iso_end,
            location=loc,
            url=base_url,
            audience="PT, PTA",
            pt_ceus="yes",
            notes="Approved for 16.5 CEUs in CA (CERS) and most other states by reciprocity.",
        ))
    return courses


# ----------------------------------------------------------------------------
# Herman & Wallace Pelvic Rehabilitation Institute
# ----------------------------------------------------------------------------

def scrape_herman_wallace() -> list[Course]:
    """Herman & Wallace — 2026 western-state satellite courses.

    Source: https://hermanwallace.com/continuing-education-courses
    H&W satellite courses combine remote lecture with hosted in-person lab
    sites; we only count the hosted in-person sites toward the geographic
    radius. Their site is slow / 5xxs frequently — re-check periodically.
    """
    base_url = "https://hermanwallace.com/continuing-education-courses"
    SEED_HW_2026 = [
        ("2026-08-29", "2026-08-30", "Pelvic Function Level 2B (Satellite)", "Torrance, CA"),
    ]
    courses: list[Course] = []
    for iso_start, iso_end, title, loc in SEED_HW_2026:
        courses.append(Course(
            course_id=make_course_id("HW", title, iso_start, loc),
            provider="Herman & Wallace Pelvic Rehabilitation Institute",
            title=title,
            start_date=iso_start,
            end_date=iso_end,
            location=loc,
            url=base_url,
            audience="PT, PTA, OT, OTA, RN, NP, midwives",
            pt_ceus="yes",
            notes="Satellite format: remote lectures + hosted in-person lab. H&W "
                  "publishes new satellite dates ~3-6 months out.",
        ))
    return courses


# ----------------------------------------------------------------------------
# Kevin Wilk — live courses (Northeast Seminars / kevinwilk.com)
# ----------------------------------------------------------------------------

def scrape_wilk() -> list[Course]:
    """Kevin E. Wilk live courses.

    Sources: https://kevinwilk.com/, https://www.neseminars.com/wilk-shoulder-knee/
    As of May 10, 2026, Wilk's 2026 live schedule has NO western-state dates;
    his live courses are East/Central US only (NYC Aug, Natick MA Oct). His
    online subscription (Wilk Physical Therapy Institute, WPTI) carries PT CEUs
    in AZ, OR, WA and many other states.
    """
    base_url = "https://www.neseminars.com/wilk-shoulder-knee/"
    # The 2026 western-state list is empty by design — included here as a
    # documented zero result so the scraper logs make it clear nothing was
    # missed. When Wilk announces a western-state live date, add it here.
    SEED_WILK_2026: list[tuple] = []
    courses: list[Course] = []
    for iso_start, iso_end, title, loc in SEED_WILK_2026:
        courses.append(Course(
            course_id=make_course_id("Wilk", title, iso_start, loc),
            provider="Kevin Wilk / Northeast Seminars",
            title=title,
            start_date=iso_start,
            end_date=iso_end,
            location=loc,
            url=base_url,
            audience="PT, PTA, ATC, MD, OT",
            pt_ceus="yes",
        ))
    return courses


# ----------------------------------------------------------------------------
# Vestibular providers (separate from Wilk)
# ----------------------------------------------------------------------------

def scrape_vestibular() -> list[Course]:
    """Vestibular-rehab-focused providers — Pitt AVPT, Johns Hopkins, AIB,
    VestibularPT.com.

    These providers rotate locations and most courses are hybrid (virtual
    lectures + one in-person module). Only the in-person Western-state
    components are included here.
    """
    SEED_VESTIBULAR_2026: list[tuple] = [
        # American Institute of Balance — only Western US in-person date is
        # El Segundo, CA in September 2026.
        ("2026-09-26", "2026-09-27",
         "AIB Vestibular Competency-Based Certification (in-person component)",
         "El Segundo, CA",
         "American Institute of Balance",
         "https://www.dizzy.com/",
         "Hybrid program. Verify the exact 2026 in-person date and CE hours."),
    ]
    courses: list[Course] = []
    for iso_start, iso_end, title, loc, provider, url, notes in SEED_VESTIBULAR_2026:
        courses.append(Course(
            course_id=make_course_id(provider, title, iso_start, loc),
            provider=provider,
            title=title,
            start_date=iso_start,
            end_date=iso_end,
            location=loc,
            url=url,
            audience="PT, PTA, AuD, MD",
            pt_ceus="yes",
            notes=notes,
        ))
    return courses


# ----------------------------------------------------------------------------
# Functional Movement Systems (FMS) — Gray Cook's organization
# ----------------------------------------------------------------------------

def _fms_render_with_form() -> str | None:
    """Drive the FMS events search form via JavaScript evaluation.

    Confirmed from May 2026 dump:
      - <select id="Country"> with options like value="US" / text="United States"
      - <select id="Distance"> with options value="5"..."1000"
      - <button id="btnSubmit">Search</button>
      - Both selects are wrapped in Bootstrap-Select, which hides them after
        page load. That's why Playwright's `select_option` couldn't find
        them as "visible" — it refuses to interact with hidden form fields.

    Workaround: use page.evaluate() to set values directly on the underlying
    selects, fire change events for Bootstrap-Select / Kendo to pick up,
    refresh selectpicker, then click btnSubmit via JS. This bypasses all
    of Playwright's actionability checks.

    Returns the rendered HTML, or None on failure.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        return None
    events_url = "https://www.functionalmovement.com/events"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1280, "height": 900},
                )
                page = context.new_page()
                page.goto(events_url, wait_until="domcontentloaded", timeout=15000)

                # Wait for the form to be in the DOM. state="attached" doesn't
                # require visibility, so it works even when Bootstrap-Select
                # has hidden the underlying <select>.
                try:
                    page.wait_for_selector("select#Country", state="attached",
                                            timeout=10000)
                except Exception:
                    log("    FMS form: #Country never attached to DOM")
                    return page.content()  # best-effort, parser may still find events

                # Step 1: set form values via JS (sidesteps Playwright's
                # visibility check on Bootstrap-Select-wrapped <select>s).
                set_result = page.evaluate("""() => {
                    const out = {};
                    const setSelect = (id, value) => {
                        const sel = document.getElementById(id);
                        if (!sel) return 'NOT_FOUND';
                        sel.value = value;
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        if (window.jQuery) {
                            try { jQuery(sel).selectpicker('refresh'); } catch(e) {}
                            try { jQuery(sel).trigger('change'); } catch(e) {}
                        }
                        return sel.value || 'SET_EMPTY';
                    };
                    out.country = setSelect('Country', 'US');
                    out.distance = setSelect('Distance', '1000');
                    return out;
                }""")
                log(f"    FMS form: country={set_result.get('country')!r}, "
                    f"distance={set_result.get('distance')!r}")

                # Step 2: register the response listener BEFORE clicking,
                # to avoid a race where the AJAX fires faster than the listener
                # attaches. We use page.expect_response as a context manager
                # so the listener is set up before we trigger the action.
                ajax_ok = False
                try:
                    with page.expect_response(
                        lambda r: "events_read" in r.url and r.status == 200,
                        timeout=10000,
                    ):
                        # Click submit AND bump Kendo's pageSize to 200 in one
                        # step. The pageSize bump fetches all ~29 events in a
                        # single AJAX response, so we don't have to paginate.
                        click_result = page.evaluate("""() => {
                            const out = {};
                            // Bump pageSize so we get every event, not just
                            // the first 10. Default is 10; total worldwide
                            // is typically <50, so 200 covers all of them.
                            try {
                                const lv = jQuery('#eventListView').data('kendoListView');
                                if (lv && lv.dataSource) {
                                    lv.dataSource.pageSize(200);
                                    out.pageSize = 'set-200';
                                } else {
                                    out.pageSize = 'no-kendo-listview';
                                }
                            } catch(e) {
                                out.pageSize = 'error: ' + e.message;
                            }
                            // Click the Search button — this triggers a fresh
                            // dataSource.read() with the current filter values
                            // and the new pageSize.
                            const btn = document.getElementById('btnSubmit');
                            if (btn) {
                                btn.click();
                                out.submit = 'clicked';
                            } else {
                                out.submit = 'not_found';
                            }
                            return out;
                        }""")
                    ajax_ok = True
                    log(f"    FMS form: pageSize={click_result.get('pageSize')!r}, "
                        f"submit={click_result.get('submit')!r}, "
                        f"events_read AJAX completed")
                except Exception as e:
                    log(f"    FMS form: events_read AJAX didn't arrive in 10s "
                        f"({type(e).__name__})")

                # Step 3: wait for Kendo to repaint the listview from the
                # AJAX data. Without this, page.content() may snapshot
                # before the new <li>s render.
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_selector("li.latest-event-item",
                                            timeout=3000)
                except Exception:
                    pass

                return page.content()
            finally:
                browser.close()
    except Exception as e:
        try:
            log(f"  FMS form render failed: {e}")
        except NameError:
            pass
        return None



def scrape_fms() -> list[Course]:
    """Functional Movement Systems — FMS Level 1/2, SFMA Level 1/2, FCS, YBT.

    Source: https://www.functionalmovement.com/events

    The events page is JavaScript-rendered. Two paths:

    1) If Playwright is installed AND --use-playwright was passed on the CLI:
       render the page in a headless Chromium browser, wait for events to
       hydrate, then parse the rendered HTML. This is the "real" path.

    2) Otherwise: static fetch with requests + BeautifulSoup. The events list
       will likely be empty ('No results returned') and the scraper logs a
       clear instruction telling the user how to install Playwright or add
       seed entries manually.

    To install Playwright (one-time, on the user's machine):
        python3 -m pip install playwright
        python3 -m playwright install chromium

    Then run the scraper with:
        python3 ceu_scraper.py --use-playwright
    """
    events_url = "https://www.functionalmovement.com/events"
    SEED_FMS_2026: list[tuple] = []  # Manual additions go here

    courses: list[Course] = []
    html: str | None = None
    source_label = "static"

    # 1) Try Playwright if requested.
    if USE_PLAYWRIGHT:
        if not _PLAYWRIGHT_AVAILABLE:
            log("  FMS: --use-playwright requested but Playwright not installed. "
                "Run: python3 -m pip install playwright && python3 -m playwright "
                "install chromium")
        else:
            log("  FMS: rendering with Playwright (headless Chromium, "
                "triggering events search)...")
            # The FMS events page is a search form, not a calendar. Empty
            # search returns "No results returned". We need to:
            #   1. Select Country = United States
            #   2. Pick a wide distance
            #   3. Click Search
            # The exact selector names are not publicly documented, so we
            # try multiple variants for each field.
            html = _fms_render_with_form()
            if html:
                source_label = "playwright"
                log(f"  FMS: rendered HTML ({len(html)} chars) — parsing...")
                # Always dump the rendered FMS HTML so selectors can be
                # discovered after-the-fact. The page is JS-driven so the
                # raw response is the only ground truth we have.
                dump_html_for_debug("fms_rendered", events_url, html)

    # 2) Fall back to static fetch.
    if html is None:
        html = http_get(events_url)
        source_label = "static"

    if not html:
        log("  FMS: live fetch returned no HTML (network error or block)")
        # Drop into seed-entry pass.
    else:
        is_empty = "No results returned" in html and "<table" not in html.lower()
        if source_label == "static" and is_empty:
            hint = (
                "Install Playwright for real scraping: "
                "`python3 -m pip install playwright && "
                "python3 -m playwright install chromium`, then re-run with "
                "`python3 ceu_scraper.py --use-playwright`."
                if not _PLAYWRIGHT_AVAILABLE
                else "Run with `python3 ceu_scraper.py --use-playwright` to "
                     "render the JavaScript and see the live events."
            )
            log(f"  FMS: events page is JavaScript-rendered (static HTML shows "
                f"'No results returned'). {hint} Or add confirmed CA dates to "
                f"SEED_FMS_2026 in scrape_fms().")
        elif is_empty and source_label == "playwright":
            log("  FMS: Playwright rendered but no events surfaced — try "
                "raising wait_ms in render_with_playwright(), or the page "
                "may have no upcoming CA dates.")
        else:
            try:
                courses.extend(_parse_fms_events(html, events_url, source_label))
            except Exception as e:
                log(f"  FMS: parse error from {source_label} HTML: {e}")

    # 3) Seed entries (manually confirmed dates).
    for iso_start, iso_end, title, loc in SEED_FMS_2026:
        courses.append(Course(
            course_id=make_course_id("FMS", title, iso_start, loc),
            provider="Functional Movement Systems (FMS)",
            title=title,
            start_date=iso_start,
            end_date=iso_end,
            location=loc,
            url=events_url,
            audience="PT, PTA, ATC, DC, S&C, personal trainers",
            pt_ceus="yes",
            notes="FMS courses are CEU-approved in most US states; verify CA "
                  "PT board status before registering.",
        ))
    return courses


def _parse_fms_events(html: str, source_url: str, label: str) -> list[Course]:
    """Parse FMS rendered events page HTML for CA in-person events.

    Confirmed structure (from May 2026 dump):
        <li class="latest-event-item k-listview-item">
          <div class="event-info">
            <div class="event-time">
              <h5 class="location">City, State</h5>
            </div>
            <div class="table row package-content">
              <div class="col-sm-8">Course Title</div>
              <div class="col-sm-4 text-right">May 14-15, 2026</div>
            </div>
            ... (one package-content per offered course at that event)
          </div>
        </li>

    One <li> represents an event location and may host multiple courses
    (e.g. SFMA Level 1 on day 1 + SFMA Level 2 on day 2 at the same venue).
    Each course is emitted as a separate Course record.
    """
    soup = BeautifulSoup(html, "html.parser")
    courses: list[Course] = []
    seen: set[tuple] = set()

    date_pattern_full = re.compile(
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December|"
        r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
        r"\.?\s+(\d{1,2})"
        r"(?:[\s\-–]+(\d{1,2}))?,?\s+(20\d{2})"
    )

    def to_iso(month: str, day: str, year: str) -> str:
        """Parse a date that might be 'May' or 'Sep' or 'September'."""
        m = month.rstrip(".")
        # %B requires full name; try both abbreviations.
        for fmt in ("%B", "%b"):
            try:
                return datetime.strptime(f"{m} {day} {year}", f"{fmt} %d %Y").date().isoformat()
            except ValueError:
                continue
        # Try expanded abbreviation
        if m.lower() == "sept":
            return datetime.strptime(f"Sep {day} {year}", "%b %d %Y").date().isoformat()
        raise ValueError(f"Cannot parse date: {month} {day} {year}")

    # Strategy 1 (PRIMARY): Kendo ListView item cards.
    card_count = 0
    for li in soup.select("li.latest-event-item, li.k-listview-item"):
        loc_el = li.select_one("h5.location")
        if not loc_el:
            continue
        location = loc_el.get_text(" ", strip=True)
        # Skip virtual / online events.
        loc_low = location.lower()
        if any(k in loc_low for k in ("virtual", "zoom", "online", "webinar")):
            continue
        # CA-only filter.
        if not keep_for_listing(location):
            continue

        # Pull each package-content row inside the event.
        for pkg in li.select(".package-content, div.row.package-content"):
            cells = pkg.select(".col-sm-8, .col-sm-4")
            if len(cells) < 2:
                continue
            title = cells[0].get_text(" ", strip=True)
            date_text = cells[1].get_text(" ", strip=True)
            if not title or not date_text:
                continue
            dm = date_pattern_full.search(date_text)
            if not dm:
                continue
            mo, d1, d2, yy = dm.groups()
            try:
                iso_start = to_iso(mo, d1, yy)
                iso_end = to_iso(mo, d2 or d1, yy)
            except ValueError:
                continue
            key = (title.lower(), iso_start, location.lower())
            if key in seen:
                continue
            seen.add(key)
            courses.append(_make_fms_course(title, iso_start, iso_end, location, source_url))
            card_count += 1

    # Strategy 2 (LEGACY/FALLBACK): tables of events.
    table_count = 0
    if not courses:
        for row in soup.select("table tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            text_blob = " | ".join(cells)
            date_match = date_pattern_full.search(text_blob)
            if not date_match:
                continue
            mo, dd, dd2, yy = date_match.groups()
            try:
                iso_start = to_iso(mo, dd, yy)
                iso_end = to_iso(mo, dd2 or dd, yy)
            except ValueError:
                continue
            location = next((c for c in cells if "," in c and not date_pattern_full.search(c)), "")
            title = next(
                (c for c in cells if any(k in c for k in ("FMS", "SFMA", "YBT", "FCS"))),
                cells[0],
            )
            if not keep_for_listing(location):
                continue
            key = (title.lower(), iso_start, location.lower())
            if key in seen:
                continue
            seen.add(key)
            courses.append(_make_fms_course(title, iso_start, iso_end, location, source_url))
            table_count += 1

    # Diagnostic: how many event-list-items did the page contain at all?
    total_items = len(soup.select("li.latest-event-item, li.k-listview-item"))
    log(f"  FMS: parsed {len(courses)} CA event(s) from {label} HTML "
        f"(cards={card_count}, tables={table_count}, total-list-items={total_items})")
    return courses


def _make_fms_course(title: str, iso_start: str, iso_end: str,
                     location: str, src_url: str) -> Course:
    return Course(
        course_id=make_course_id("FMS", title, iso_start, location),
        provider="Functional Movement Systems (FMS)",
        title=title,
        start_date=iso_start,
        end_date=iso_end,
        location=location,
        url=src_url,
        audience="PT, PTA, ATC, DC, S&C, personal trainers",
        pt_ceus="yes",
        notes="FMS courses are CEU-approved in most US states; CA PTs may "
              "need to self-submit syllabus to PTBC.",
    )


# ----------------------------------------------------------------------------
# AMSI Training (PT continuing-ed group, Cumming GA)
# ----------------------------------------------------------------------------

# AMSI maintains 5 product pages. Each has a "Date" header followed by one or
# more lines like "Month DD-DD YYYY City ST". We fetch each product page and
# regex-parse those date strings.
AMSI_PRODUCT_SLUGS = [
    "lumbar-differential",
    "comprehensive-dry-needling-and-manual-therapy-cdnmt-1",
    "comprehensive-dry-needling-and-manual-therapy-cdnmt-2",
    "vestibular-and-concussion-certification",
    "spinal-manipulation-for-the-manual-therapist",
]

# Pattern matches things like:
#   "September 19-21 2025 Oshkosh WI"
#   "April 17-19 2026 San Diego CA"
#   "May 30-31 2026 Cumming GA"
# Group 1=month, 2=start day, 3=end day (optional), 4=year, 5=city, 6=state.
AMSI_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+"
    r"(\d{1,2})(?:[\-\u2013](\d{1,2}))?\s+"
    r"(20\d{2})\s+"
    r"([A-Za-z][A-Za-z\.\-' ]+?)\s+"
    r"([A-Z]{2})\b"
)


def _amsi_parse_product_page(html: str, slug: str) -> list[tuple]:
    """Return a list of (iso_start, iso_end, title, location) tuples parsed
    from an AMSI product page's HTML."""
    if not html:
        return []
    # Pull a course title near the top of the product page.
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else slug
    # Date strings live near the "Date" header. Easier to regex the whole page.
    out = []
    for m in AMSI_DATE_RE.finditer(html):
        month, d1, d2, year, city, state = m.groups()
        try:
            start = datetime.strptime(f"{month} {d1} {year}", "%B %d %Y").date()
        except ValueError:
            continue
        if d2:
            try:
                end = datetime.strptime(f"{month} {d2} {year}", "%B %d %Y").date()
            except ValueError:
                end = start
        else:
            end = start
        location = f"{city.strip()}, {state}"
        out.append((start.isoformat(), end.isoformat(), title, location))
    return out


def scrape_amsi() -> list[Course]:
    """AMSI Training — fetch each of the 5 product pages and parse dates.

    Source: https://www.amsitraining.com/store
    Each product page lists its current live dates under a "Date" header in
    the format "Month DD-DD YYYY City ST".

    Catalog (May 2026):
      - Lumbar Differential ($550)
      - Comprehensive Dry Needling and Manual Therapy (CDNMT-1) ($895)
      - Comprehensive Dry Needling and Manual Therapy (CDNMT-2) ($895)
      - Vestibular and Concussion Certification ($895)
      - Spinal Manipulation for the Manual Therapist ($250)

    CE accreditations: FSBPT 28 CEUs, BOC 20 hrs Cat 1A, Texas board 26 CCUs.
    """
    store_url = "https://www.amsitraining.com/store"
    SEED_AMSI_2026: list[tuple] = []  # Manual additions go here

    courses: list[Course] = []
    parsed_count = 0
    for slug in AMSI_PRODUCT_SLUGS:
        product_url = f"https://www.amsitraining.com/product/{slug}"
        html = http_get(product_url)
        if not html:
            continue
        for iso_start, iso_end, title, location in _amsi_parse_product_page(html, slug):
            parsed_count += 1
            # in_radius() filter is applied later in the main loop; the
            # date-cutoff filter (past dates) is also applied later. We emit
            # everything here so the diff engine sees the full picture.
            courses.append(Course(
                course_id=make_course_id("AMSI", title, iso_start, location),
                provider="AMSI Training",
                title=title,
                start_date=iso_start,
                end_date=iso_end,
                location=location,
                url=product_url,
                audience="PT, OT, PTA, ATC, MD, DC, Acupuncturists",
                pt_ceus="yes",
                notes="FSBPT-credentialed (28 CEUs), BOC (20 1A credits), "
                      "Texas board (26 CCUs); local-board self-submission "
                      "supported.",
            ))
    if parsed_count:
        log(f"  AMSI: parsed {parsed_count} dated entries across "
            f"{len(AMSI_PRODUCT_SLUGS)} product pages")

    # Seed additions (manual confirmations).
    for iso_start, iso_end, title, loc in SEED_AMSI_2026:
        courses.append(Course(
            course_id=make_course_id("AMSI", title, iso_start, loc),
            provider="AMSI Training",
            title=title,
            start_date=iso_start,
            end_date=iso_end,
            location=loc,
            url=store_url,
            audience="PT, OT, PTA, ATC, MD, DC, Acupuncturists",
            pt_ceus="yes",
            notes="FSBPT-credentialed (28 CEUs).",
        ))
    return courses


# ----------------------------------------------------------------------------
# AAMT — American Academy of Manipulative Therapy / Spinal Manipulation Institute
# ----------------------------------------------------------------------------
# Dr. James Dunning's organization. Offers SMT-1/2/3/4, DN-1/2/3, VCS-1/2,
# MSKU-1 through 6, BFR, EMT-1, IASTM-1, NTM-1/2, DD-1, PFDN, AMAC-1, and the
# AAMT Fellowship in OMPT. Audience: PT, DC, ATC, MD (osteopractic cert track).

# AAMT uses The Events Calendar (theeventscalendar.com) WordPress plugin.
# In the rendered HTML, each "Upcoming Seminars" widget entry is structured as:
#   <a class="...event-link..." href="https://spinalmanipulation.org/seminar/...">
#     <h3 class="tribe-events-calendar-list__event-title">Title here</h3>
#   </a>
#   <time class="tribe-events-calendar-list__event-datetime"
#         datetime="2026-05-15T...">May 15</time>
#
# Falling-back text strategy (works for both Tribe and other event plugins):
# walk the text after "Upcoming Seminars" looking for "Month D[D], YYYY -
# Month D[D], YYYY" then the next non-empty line that doesn't itself look
# like a date — that's the title.

AAMT_DATE_RANGE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})\s*-\s*"
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})"
)

# Location heuristic: titles like "DN-1 Los Angeles, CA".
AAMT_LOC_RE = re.compile(r"([A-Za-z][A-Za-z\.\-' ]+?),\s*([A-Z]{2})\s*$")


def scrape_aamt() -> list[Course]:
    """American Academy of Manipulative Therapy / Spinal Manipulation Institute.

    Source: https://spinalmanipulation.org/osteopractor-seminars/

    The page renders cleanly server-side. The "Upcoming Seminars" widget shows
    the next 5 events. The page uses The Events Calendar WordPress plugin —
    we walk the rendered text after the "Upcoming Seminars" anchor looking
    for date-range strings, then capture the title that follows.
    """
    seminars_url = "https://spinalmanipulation.org/osteopractor-seminars/"
    SEED_AAMT_2026: list[tuple] = []

    courses: list[Course] = []
    html = http_get(seminars_url, allow_playwright_fallback=True)
    if not html:
        log("  AAMT: live fetch returned no HTML")
        for iso_start, iso_end, title, loc in SEED_AAMT_2026:
            courses.append(_make_aamt_course(title, iso_start, iso_end, loc, seminars_url))
        return courses

    try:
        soup = BeautifulSoup(html, "html.parser")
        for s in soup(["script", "style"]):
            s.decompose()

        # Get text in two forms: line-broken (for skip patterns) and flat
        # (for date-range matching). Use whichever gives more matches.
        body_text = soup.get_text("\n", strip=True)
        anchor_idx = body_text.find("Upcoming Seminars")
        text_count = 0
        if anchor_idx >= 0:
            tail = body_text[anchor_idx + len("Upcoming Seminars"):]
            stop_idx = tail.find("View Calendar")
            if stop_idx > 0:
                tail = tail[:stop_idx]

            # Convert newlines to a sentinel char first so we can still
            # detect line boundaries after a flatten pass.
            tail_flat = re.sub(r"\s+", " ", tail.replace("\n", " | "))

            for m in AAMT_DATE_RANGE_RE.finditer(tail):
                # Walk forward in the tail for a non-date title line.
                after = tail[m.end():m.end() + 600]
                title = _aamt_pick_title(after, forward=True)
                if not title:
                    # Walk backward — many event widgets put title before date.
                    before = tail[max(0, m.start() - 600):m.start()]
                    title = _aamt_pick_title(before, forward=False)
                if not title:
                    continue
                if _consume_aamt_entry(m, title, seminars_url, seminars_url, courses):
                    text_count += 1

            # Defensive fallback: if newline-based pass produced nothing,
            # try the flattened text. This handles sites where BS4 collapses
            # everything to a single line (e.g. nested <span>s without
            # block-level wrappers).
            if text_count == 0:
                for m in AAMT_DATE_RANGE_RE.finditer(tail_flat):
                    after = tail_flat[m.end():m.end() + 400]
                    # Each "line" in flat text is separated by " | ".
                    title = _aamt_pick_title(after.replace(" | ", "\n"),
                                             forward=True)
                    if not title:
                        before = tail_flat[max(0, m.start() - 400):m.start()]
                        title = _aamt_pick_title(before.replace(" | ", "\n"),
                                                 forward=False)
                    if not title:
                        continue
                    if _consume_aamt_entry(m, title, seminars_url, seminars_url, courses):
                        text_count += 1

        log(f"  AAMT: parsed {len(courses)} upcoming seminar(s) "
            f"(text-walk={text_count})")
        if text_count == 0:
            # Diagnostic: did we even find "Upcoming Seminars" in the
            # body text?
            anchor_present = "Upcoming Seminars" in body_text
            date_present = bool(AAMT_DATE_RANGE_RE.search(body_text))
            log(f"  AAMT: 'Upcoming Seminars' anchor in text: {anchor_present}, "
                f"any date-range match in body: {date_present}")
            dump_html_for_debug("aamt", seminars_url, html)
            snippet = re.sub(r"\s+", " ", html[:400])
            log(f"  AAMT: response starts with: {snippet!r}")
    except Exception as e:
        log(f"  AAMT: parse error: {e}")

    # Seed pass.
    for iso_start, iso_end, title, loc in SEED_AAMT_2026:
        courses.append(_make_aamt_course(title, iso_start, iso_end, loc, seminars_url))

    return courses


def _aamt_pick_title(chunk: str, forward: bool) -> str:
    """Find the first non-date title-like line in chunk, walking forward or
    backward. Skips short month-day fragments ("Jan 1") and date-range lines.
    """
    lines = chunk.split("\n")
    if not forward:
        lines = list(reversed(lines))
    for line in lines:
        line = line.strip().strip('"').strip()
        if not line or len(line) < 4:
            continue
        if AAMT_DATE_RANGE_RE.search(line):
            continue
        # Month-short fragments like "Jan 1", "May 15".
        if re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*\d+$",
                    line, re.IGNORECASE):
            continue
        # Skip pure-number day fragments
        if re.match(r"^\d{1,2}$", line):
            continue
        # Skip standalone month-name fragments
        if re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?$",
                    line, re.IGNORECASE):
            continue
        return line
    return ""


def _consume_aamt_entry(date_match, title: str, href: str, fallback_url: str,
                        courses: list) -> bool:
    """Helper: parse a single AAMT entry. Returns True if added, False if
    filtered out as non-course (fellowship admin, supervision, etc.)."""
    mo1, d1, y1, mo2, d2, y2 = date_match.groups()
    try:
        start = datetime.strptime(f"{mo1} {d1} {y1}", "%B %d %Y").date()
        end = datetime.strptime(f"{mo2} {d2} {y2}", "%B %d %Y").date()
    except ValueError:
        return False

    title = title.strip().strip('"').strip()
    low = title.lower()
    # Filter out non-seminar admin entries.
    if any(skip in low for skip in (
        "fellowship", "tuition deposit", "supervision",
        "facetime", "wordpress",
    )):
        return False

    location = ""
    loc_m = AAMT_LOC_RE.search(title)
    if loc_m:
        location = f"{loc_m.group(1).strip()}, {loc_m.group(2)}"

    # De-dupe by (title, start_date, location).
    key = (title.lower(), start.isoformat(), location.lower())
    for c in courses:
        if (c.title.lower(), c.start_date, c.location.lower()) == key:
            return False

    courses.append(_make_aamt_course(
        title, start.isoformat(), end.isoformat(), location,
        href if href.startswith("http") else fallback_url,
    ))
    return True


def _make_aamt_course(title: str, iso_start: str, iso_end: str,
                     location: str, url: str) -> Course:
    return Course(
        course_id=make_course_id("AAMT", title, iso_start, location),
        provider="American Academy of Manipulative Therapy (AAMT)",
        title=title,
        start_date=iso_start,
        end_date=iso_end,
        location=location,
        url=url,
        audience="PT, DC, ATC, MD (osteopractic cert track)",
        pt_ceus="yes",
        notes="AAMT courses count toward the Diploma in Osteopractic; "
              "verify CA PT board status by self-submission.",
    )


# ----------------------------------------------------------------------------
# ELDOA — Guy Voyer DO's spinal-decoaptation method
# ----------------------------------------------------------------------------
# Cross-disciplinary (S&C, Pilates, yoga, DC, PT). NSCA CEU-approved per the
# official providers; PTs and ATCs are welcome and often submit syllabi for
# local-board credit. Multi-day in-person seminars: ELDOA 1, 2, 1&2 Combination,
# 3, 4 Practical, 4 Theory, 5.1, 5.2.
#
# Two source URLs, both problematic:
#   1) https://eldoa.com/pages/seminars — provided by user but STALE
#      (last published dates are from 2022). Kept as a fetch target in case
#      it ever gets refreshed.
#   2) https://eldoavoyer.com/courses — Guy Voyer's official site. Has more
#      recent dates BUT no explicit year stamps on the entries, which makes
#      year-inference fragile when listings straddle a year boundary.
#
# Approach: fetch both, regex-parse what we can, log clearly when year
# inference is uncertain, and rely on the past-date filter downstream to drop
# stale entries. Manual SEED_ELDOA additions are the most reliable path until
# eldoa.com posts a current calendar.

# Tabular row format from eldoa.com seminars page:
#   | **ELDOA 3** | Aug 5 - Aug 7 | New Orleans | Justin Brien |
# Captures: level, start month, start day, end month (optional), end day, location.
ELDOA_TABLE_ROW_RE = re.compile(
    r"\*\*(ELDOA[^|*]+?)\*\*\s*\|\s*"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+"
    r"(\d{1,2})\s*(?:[\-\u2013]\s*"
    r"(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+)?"
    r"(\d{1,2}))?\s*\|\s*"
    r"([^|]+?)\s*\|",
    re.IGNORECASE,
)

# List-item format from eldoavoyer.com courses page:
#   - [ELDOA 1&2 Combination June 5-8 Bryce Turner. •. Seal Beach, CA](#)
# More flexible — we look for an ELDOA-prefixed line containing a month/date
# and a city/state at the end.
ELDOA_LIST_RE = re.compile(
    r"(ELDOA[^\n\]]+?)\s+"
    r"(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+"
    r"(\d{1,2})(?:[\-\u2013](\d{1,2}))?"
    r"[^\n\]]*?"
    r"([A-Z][A-Za-z\.\-' ]+?,\s*(?:[A-Z]{2}|"
    r"California|Oregon|Washington|Nevada|Arizona|Colorado))",
    re.IGNORECASE,
)

ELDOA_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}


def _eldoa_infer_year(month_num: int, today: date) -> int:
    """Pick the year that makes the date land in the future, defaulting to
    current year if the date hasn't passed yet."""
    candidate = date(today.year, month_num, 1)
    if candidate >= today.replace(day=1):
        return today.year
    return today.year + 1


def scrape_eldoa() -> list[Course]:
    """ELDOA — Guy Voyer's SomaVOYER paradigm seminars.

    Primary source: https://www.somavoyer.com/course-calendar
    This is the master schedule across all 4 Guy Voyer programs (SomaTraining,
    SomaTherapy, ELDOA, Osteopathy/Etiotherapy) with explicit MM/DD/YYYY dates.
    Each course entry on the rendered page contains a date in the form
    "M/D/YYYY" plus a city/state pair.

    Fallback sources (kept for completeness):
      - https://eldoa.com/pages/seminars (stale, last events from 2022)
      - https://eldoavoyer.com/courses (year-ambiguous list)
      - https://www.somavoyer.com/somatherapy/schedule (mostly "TBD")
    """
    primary_url = "https://www.somavoyer.com/course-calendar"
    voyer_url = "https://eldoavoyer.com/courses"
    eldoa_url = "https://eldoa.com/pages/seminars"
    # Confirmed CA dates from May 2026 master-calendar scrape — fallback when
    # network is unavailable. Live fetch dedupes against these.
    # Format: (start_iso, end_iso, title, location, program)
    SEED_ELDOA: list[tuple] = [
        ("2026-05-08", "2026-05-10", "ELDOA® 3",                                            "Seal Beach, CA",    "ELDOA"),
        ("2026-06-07", "2026-06-07", "2-3: 2TLS - Upper Limbs",                             "Newport Beach, CA", "SomaTherapy"),
        ("2026-07-23", "2026-07-24", "1-1: Strengthening of the Abdominals & Thoracic Diaphragm", "Seal Beach, CA", "SomaTraining"),
        ("2026-07-25", "2026-07-26", "3-1: Strengthening of the Upper Limb & Trunk",        "Seal Beach, CA",    "SomaTraining"),
        ("2026-08-14", "2026-08-16", "ELDOA® 1&2 Combination",                              "Seal Beach, CA",    "ELDOA"),
        ("2026-09-11", "2026-09-13", "ELDOA® 3",                                            "Seal Beach, CA",    "ELDOA"),
        ("2026-09-25", "2026-09-25", "1-3: 2TLS - Lower Limbs",                             "Newport Beach, CA", "SomaTherapy"),
        ("2026-11-06", "2026-11-08", "ELDOA® 4",                                            "Seal Beach, CA",    "ELDOA"),
        ("2026-12-04", "2026-12-04", "3-3: 2TLS - Trunk & Pelvis",                          "Newport Beach, CA", "SomaTherapy"),
        ("2026-12-10", "2026-12-11", "3-2: Proprioception & Awareness",                     "Seal Beach, CA",    "SomaTraining"),
        ("2026-12-12", "2026-12-13", "2-2: Myofascial Stretching",                          "Seal Beach, CA",    "SomaTraining"),
    ]

    courses: list[Course] = []
    today = date.today()
    seen_keys: set[tuple] = set()

    def emit(title: str, iso_start: str, iso_end: str, location: str, src_url: str,
             program: str = "ELDOA"):
        key = (title.lower().strip(), iso_start, location.lower().strip())
        if key in seen_keys:
            return
        seen_keys.add(key)
        provider_name = "SomaVOYER" if program != "ELDOA" else "ELDOA"
        courses.append(Course(
            course_id=make_course_id("SomaVOYER", f"{program} {title}", iso_start, location),
            provider=provider_name,
            title=f"{program}: {title}".strip(),
            start_date=iso_start,
            end_date=iso_end or iso_start,
            location=location.strip(),
            url=src_url,
            audience="PT, DC, ATC, S&C, Pilates, yoga, MD/DO",
            pt_ceus="yes (self-submit; NSCA CEU-approved at minimum)",
            notes="Cross-disciplinary movement education. CA PTs typically "
                  "self-submit; NSCA CEUs are standard. Verify with the host "
                  "instructor before registering.",
        ))

    # --- Primary source: somavoyer.com/course-calendar ---
    # Each course block contains: program name (heading), course title, the
    # date as "M/D/YYYY", and a location like "Seal Beach, CA, USA". We extract
    # by walking text and finding "M/D/YYYY" anchors, then looking at adjacent
    # text for title/program/location. Most robust approach: find all date
    # strings, then read backward/forward in the surrounding text.
    html_primary = http_get(primary_url)
    if html_primary:
        # Strip script/style and reduce to text with newlines, then split into
        # blocks separated by date markers.
        soup = BeautifulSoup(html_primary, "html.parser")
        for s in soup(["script", "style"]):
            s.decompose()
        text = soup.get_text("\n", strip=True)

        # Pattern matches "M/D/YYYY" followed within ~120 chars by a location.
        # We work backward from each date to find the most recent program +
        # title pair.
        date_re = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
        program_set = {"ELDOA™", "ELDOA", "SomaTraining", "SomaTherapy",
                       "Osteopathy / Etiotherapy", "MasterClass"}

        lines = text.split("\n")
        primary_count = 0
        for i, line in enumerate(lines):
            m = date_re.fullmatch(line.strip())
            if not m:
                continue
            mo, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                start = date(yy, mo, dd)
            except ValueError:
                continue

            # Look backward for program (typically 4-6 lines back).
            program = "ELDOA"
            title = ""
            for j in range(max(0, i - 8), i):
                if lines[j].strip() in program_set:
                    program = lines[j].strip().replace("™", "")
                    # Course title is typically the next non-image line after
                    # the program name.
                    for k in range(j + 1, min(j + 4, i)):
                        candidate = lines[k].strip()
                        if candidate and not candidate.startswith("by"):
                            title = candidate
                            break
                    break

            # Look forward for location (typically 1-3 lines ahead).
            location = ""
            for j in range(i + 1, min(i + 5, len(lines))):
                cand = lines[j].strip()
                if re.match(r"^[A-Z][A-Za-z\.\-' ]+,\s*[A-Z]{2}", cand):
                    location = cand.rstrip(",").replace(", USA", "").strip()
                    break

            if not title or not location:
                continue
            if not keep_for_listing(location):
                continue

            emit(title, start.isoformat(), start.isoformat(), location,
                 primary_url, program=program)
            primary_count += 1

        if primary_count:
            log(f"  ELDOA/SomaVOYER: parsed {primary_count} entries from "
                f"somavoyer.com/course-calendar")
        else:
            log("  ELDOA/SomaVOYER: somavoyer.com/course-calendar returned no "
                "CA entries (verify with web browser; format may have changed)")

    # --- Fallback source: eldoavoyer.com/courses (year-ambiguous) ---
    html_voyer = http_get(voyer_url)
    if html_voyer:
        voyer_count = 0
        list_re = re.compile(
            r"(ELDOA[^\n\]]+?)\s+"
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+"
            r"(\d{1,2})(?:[\-\u2013](\d{1,2}))?"
            r"[^\n\]]*?"
            r"([A-Z][A-Za-z\.\-' ]+?,\s*(?:[A-Z]{2}|California))",
            re.IGNORECASE,
        )
        month_map = {
            "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
            "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
            "november": 11, "december": 12,
        }
        for m in list_re.finditer(html_voyer):
            level, mo1, d1, d2, loc = m.groups()
            mo_num = month_map.get(mo1.lower())
            if not mo_num:
                continue
            year = today.year if mo_num >= today.month else today.year + 1
            try:
                start = date(year, mo_num, int(d1))
                end = date(year, mo_num, int(d2)) if d2 else start
            except ValueError:
                continue
            clean_level = re.sub(r"\.\s*$", "", level).strip()
            if not keep_for_listing(loc):
                continue
            emit(clean_level, start.isoformat(), end.isoformat(), loc, voyer_url)
            voyer_count += 1
        if voyer_count:
            log(f"  ELDOA: parsed {voyer_count} entries from eldoavoyer.com "
                "courses (year inference is best-effort)")

    # --- Fallback source: eldoa.com/pages/seminars (typically stale) ---
    html_eldoa = http_get(eldoa_url)
    if html_eldoa and "2026" in html_eldoa:
        # Only parse if 2026 content exists; otherwise skip silently.
        log("  ELDOA: eldoa.com seminars page returned content; verify manually")

    # --- Manual seed entries ---
    for iso_start, iso_end, title, loc, program in SEED_ELDOA:
        emit(title, iso_start, iso_end, loc, primary_url, program=program)

    return courses


# ----------------------------------------------------------------------------
# Northeast Seminars (Wilk Shoulder/Knee, BFRT, Mulligan, Combat Athlete)
# ----------------------------------------------------------------------------

def scrape_northeast_seminars() -> list[Course]:
    """Northeast Seminars — umbrella provider for Wilk, Mulligan, BFRT, and
    other Live & On-Demand PT continuing-ed offerings.

    Source: https://www.neseminars.com/ (homepage What's Coming Up) and
            https://www.neseminars.com/wilk-shoulder-knee/ (Wilk calendar)

    NE Seminars publishes its live calendar across multiple landing pages:
      - Homepage shows the top ~3 upcoming events
      - Wilk Shoulder & Knee page lists Wilk events in-line
      - Mulligan, BFRT, and Combat Athlete pages each have their own listings

    Many NE Seminars 2026 Wilk events are East Coast (NY, MA, FL); CA-only
    filter typically drops everything. We still fetch and parse so any future
    CA event gets surfaced automatically.

    NOTE: Mulligan live courses are tracked separately via scrape_mulligan().
    Wilk masterclasses are also tracked via scrape_wilk() — this scraper
    captures the broader NE Seminars catalog (BFRT cert, Combat Athlete, etc.)
    """
    seminars_urls = [
        "https://www.neseminars.com/",
        "https://www.neseminars.com/wilk-shoulder-knee/",
    ]
    courses: list[Course] = []
    seen: set[tuple] = set()

    # Pattern matches "Month DD, YYYY - Month DD, YYYY" with location nearby
    range_re = re.compile(
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})\s*-\s*"
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+(\d{1,2}),\s+(20\d{2})",
        re.IGNORECASE,
    )
    # Course Location: anchor — the venue/city info that follows
    loc_re = re.compile(
        r"Course Location[:\s]*[^,\n]*?,\s*([A-Z][A-Za-z\.\-' ]+?,\s*[A-Z]{2})",
        re.IGNORECASE,
    )
    # Title pattern from heading anchors. Approximate.
    title_inline_re = re.compile(
        r"in\s+([A-Z][A-Za-z\.\-' ]+?,\s+[A-Z]{2})\b",
    )

    parsed_count = 0
    for url in seminars_urls:
        html = http_get(url, allow_playwright_fallback=True)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for s in soup(["script", "style"]):
            s.decompose()
        text = soup.get_text("\n", strip=True)

        # Walk through date matches; for each, try to find nearby location.
        for m in range_re.finditer(text):
            mo1, d1, y1, mo2, d2, y2 = m.groups()
            try:
                start = datetime.strptime(f"{mo1} {d1} {y1}", "%B %d %Y").date()
                end = datetime.strptime(f"{mo2} {d2} {y2}", "%B %d %Y").date()
            except ValueError:
                continue
            # Pull a chunk of text around the match for context.
            ctx_start = max(0, m.start() - 400)
            ctx_end = min(len(text), m.end() + 400)
            ctx = text[ctx_start:ctx_end]

            # Look for "Course Location:" first — it's the most reliable.
            location = ""
            lm = loc_re.search(ctx)
            if lm:
                location = lm.group(1).strip()
            else:
                # Fallback: look for "in <City, ST>" in the heading.
                tm = title_inline_re.search(ctx)
                if tm:
                    location = tm.group(1).strip()
            if not location or not keep_for_listing(location):
                continue

            # Title heuristic: a heading-like phrase near the date.
            title_match = re.search(
                r"(?:Wilk[^\n]{0,80}|Recent Advances[^\n]{0,80}|"
                r"What.?s New[^\n]{0,80}|Mulligan[^\n]{0,80}|BFRT[^\n]{0,80}|"
                r"Combat Athlete[^\n]{0,80})", ctx)
            title = title_match.group(0).strip() if title_match else "Northeast Seminars Event"

            key = (title.lower(), start.isoformat(), location.lower())
            if key in seen:
                continue
            seen.add(key)
            parsed_count += 1

            courses.append(Course(
                course_id=make_course_id("Northeast Seminars", title, start.isoformat(), location),
                provider="Northeast Seminars",
                title=title,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                location=location,
                url=url,
                audience="PT, PTA, ATC, OT, MD",
                pt_ceus="yes (state-by-state — see each course page)",
                notes="NE Seminars has been an APTA / state-PT-board approved CE "
                      "provider since 1981. CA recognition typically by self-submission.",
            ))

    if parsed_count:
        log(f"  Northeast Seminars: parsed {parsed_count} CA-area event(s)")
    else:
        log("  Northeast Seminars: 0 CA-area events on current public pages "
            "(Wilk 2026 is NY + Natick MA only; verify Mulligan/BFRT pages "
            "if you want a wider trawl)")
    return courses


# ----------------------------------------------------------------------------
# Functional Range Systems (Dr. Andreo Spina's FRS)
# ----------------------------------------------------------------------------

def scrape_frs() -> list[Course]:
    """Functional Range Systems (FRS / FR / FRC / FRA / Kinstretch / FRS-ISM).

    Source: https://functionalanatomyseminars.com/seminars/find-a-seminar/

    FRS lists most of its in-person dates on this single page, organized in
    HTML tables grouped by certification track. Many are ONLINE; we filter
    those out via in_radius(). The CA in-person live seminars rotate — at
    times zero are listed publicly because they happen as private host events.

    Audience: FR (manual therapists only — PT, DC, ATC, MD), FRC/Kinstretch
    (open to all coaches), FRS-ISM (FRC-certified only).
    """
    url = "https://functionalanatomyseminars.com/seminars/find-a-seminar/"
    html = http_get(url)
    if not html:
        return []
    courses: list[Course] = []
    seen: set[tuple] = set()
    soup = BeautifulSoup(html, "html.parser")

    # Table rows: | Event | Venue | Date | Register |
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            event_name, venue, date_text = cells[0], cells[1], cells[2]
            # Skip header rows.
            if event_name.lower() == "event":
                continue
            # Date pattern: "Jun 13, 2026" or "Sept 12, 2026"
            m = re.match(
                r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z\.]*\s+"
                r"(\d{1,2}),\s+(20\d{2})",
                date_text,
                re.IGNORECASE,
            )
            if not m:
                continue
            try:
                start = datetime.strptime(
                    f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}",
                    "%b %d %Y",
                ).date()
            except ValueError:
                continue

            # Determine audience from event name.
            audience = "PT, DC, ATC, S&C, Pilates, yoga, MD/DO"
            if "Non-Therapist" in event_name:
                audience = "Coaches, S&C, trainers (non-manual-therapy track)"
            elif event_name.startswith("FR®"):
                audience = "PT, DC, ATC, MD (manual therapists only)"

            # Location: venue string. ONLINE entries fail the radius filter.
            location = venue
            if not keep_for_listing(location):
                continue

            key = (event_name.lower(), start.isoformat(), location.lower())
            if key in seen:
                continue
            seen.add(key)

            # Try to capture the registration URL from the same row.
            reg_link = row.find("a", href=re.compile(r"event-registration"))
            href = reg_link["href"] if reg_link else url

            courses.append(Course(
                course_id=make_course_id("FRS", event_name, start.isoformat(), location),
                provider="Functional Range Systems (FRS)",
                title=event_name,
                start_date=start.isoformat(),
                end_date=start.isoformat(),
                location=location,
                url=href,
                audience=audience,
                pt_ceus="varies — verify per course page",
                notes="Dr. Andreo Spina's FRS curriculum. Many events are online "
                      "(filtered out here). Most in-person CA dates run as private "
                      "host events; check the public calendar if a CA seminar appears.",
            ))

    if courses:
        log(f"  FRS: parsed {len(courses)} CA in-person event(s)")
    else:
        log("  FRS: 0 CA in-person events on current public calendar (most "
            "FRS 2026 dates are ONLINE or international/private hosts)")
    return courses


# ----------------------------------------------------------------------------
# Pain Free Training (Dr. John Rusin's PPSC, FKT, LPSC, Pain-Free Mobility)
# ----------------------------------------------------------------------------

def scrape_painfree() -> list[Course]:
    """Pain-Free Performance Specialist Certification + companion courses.

    Source: https://painfreetraining.com/events/

    Catalog as of May 2026:
      - PPSC — 2-day Pain-Free Performance Specialist Certification (live + online)
      - FKT — Functional Kettlebell Training Certification (live + online)
      - LPSC — Lifelong Performance Programming Certification (on-demand only)
      - Pain-Free Mobility Certification (on-demand only)

    The /events/ page currently shows only on-demand product cards in static
    HTML; the live event calendar appears to be JavaScript-rendered (the
    in-page anchor #events-list never resolves to course data in the static
    response). We do a best-effort scrape and clearly log when no live events
    surface, so user can either (a) check the page in a browser and add seeds,
    or (b) wait for the provider to ship server-rendered events.

    PT CEU notes: PPSC carries NSCA + NASM/AFAA/ACE CEUs. CA PT board
    typically requires self-submission of the syllabus.
    """
    events_url = "https://painfreetraining.com/events/"
    SEED_PAINFREE: list[tuple] = []  # Manual additions go here

    courses: list[Course] = []
    today = date.today()
    seen: set[tuple] = set()

    def emit(start_iso, end_iso, title, location, href):
        key = (title.lower(), start_iso, location.lower())
        if key in seen:
            return
        seen.add(key)
        courses.append(Course(
            course_id=make_course_id("Pain Free Training", title, start_iso, location),
            provider="Pain Free Training (PPSC)",
            title=title,
            start_date=start_iso,
            end_date=end_iso,
            location=location,
            url=href,
            audience="PT, DC, ATC, S&C, personal trainers, fitness pros",
            pt_ceus="yes (self-submit to CA PT board)",
            notes="PPSC: 2-day live cert. NSCA + NASM/AFAA/ACE CEUs. CA PTs "
                  "typically self-submit syllabus to PTBC.",
        ))

    # 1) Live fetch.
    html = http_get(events_url)
    live_count = 0
    if html:
        # Heuristic: scan for date patterns "Month DD, YYYY" or
        # "Month DD-DD, YYYY" with a nearby CA location string. Each
        # event-card on the rendered page is typically structured as
        # "<title> | <date> | <location> | Register" though static HTML
        # may not include those at all.
        date_re = re.compile(
            r"(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+(\d{1,2})"
            r"(?:[\s\-–]+(\d{1,2}))?(?:,\s+(20\d{2}))?",
            re.IGNORECASE,
        )
        # Find any date that has a CA location within 200 chars.
        for m in date_re.finditer(html):
            ctx_start = max(0, m.start() - 200)
            ctx_end = min(len(html), m.end() + 400)
            ctx = html[ctx_start:ctx_end]

            month_name, d1, d2, year_str = m.groups()
            try:
                year = int(year_str) if year_str else today.year
                start = datetime.strptime(
                    f"{month_name} {d1} {year}", "%B %d %Y"
                ).date()
                end = (datetime.strptime(
                    f"{month_name} {d2 or d1} {year}", "%B %d %Y"
                ).date())
            except (ValueError, TypeError):
                continue
            if start < today:
                continue

            # Look for a CA-city marker nearby.
            loc_m = re.search(
                r"([A-Z][A-Za-z\.\-' ]{1,30}),\s*(?:CA|California)\b",
                ctx,
            )
            if not loc_m:
                continue
            location = f"{loc_m.group(1).strip()}, CA"

            # Title heuristic: look for known certification names in context.
            title = "PPSC: Pain-Free Performance Specialist Certification"
            ctx_low = ctx.lower()
            if "functional kettlebell" in ctx_low or "fkt" in ctx_low:
                title = "FKT: Functional Kettlebell Training Certification"
            elif "mobility" in ctx_low and "on demand" not in ctx_low:
                title = "Pain-Free Mobility Certification (Live)"
            elif "lifelong performance" in ctx_low or "lpsc" in ctx_low:
                title = "LPSC: Lifelong Performance Programming Certification"

            emit(start.isoformat(), end.isoformat(), title, location, events_url)
            live_count += 1

    if live_count:
        log(f"  Pain Free Training: parsed {live_count} CA live event(s)")
    else:
        log("  Pain Free Training: 0 CA live events in static HTML. The "
            "events page appears to be JavaScript-rendered (only on-demand "
            "products visible). Check https://painfreetraining.com/events/ "
            "in a browser; add confirmed CA dates to SEED_PAINFREE in this "
            "function.")

    # 2) Seed entries.
    for iso_start, iso_end, title, loc in SEED_PAINFREE:
        emit(iso_start, iso_end, title, loc, events_url)
    return courses


# ----------------------------------------------------------------------------
# DNS — Dynamic Neuromuscular Stabilization (Prague School of Rehabilitation)
# ----------------------------------------------------------------------------

def scrape_dns() -> list[Course]:
    """DNS courses from the Prague School of Rehabilitation (Kolar method).

    Source: https://www.rehabps.cz/rehab/co.php

    The page is plain static HTML with anchor-tagged course entries in the
    format:
       [City, State?, Country - Month DD - DD, Year - Course Title](url)

    Examples:
       Pasadena, CA, USA - August 29 - 30, 2026 - Basic course "A"
       San Diego, USA - October 30 - November 1, 2026 - Basic course "A"
       Stanford, USA - June 6 - 7, 2026 - DNS Movement Performance Summit
       Bangkok, Thailand - June 30 - July 2, 2026 - Basic course "A"

    Course tracks:
      - Clinical: Basic A/B, Intermediate C, Advanced D
      - Pediatric: Part 1, 2, 3
      - Exercise: Part I, II, III
      - Strength Training: Part I, II, III
      - Specialized: scoliosis, pelvic, manual therapy, sports-specific, etc.

    Audience: PT, DC, MD, DO, ATC, manual therapists, S&C coaches.
    CA PT CEU: typically self-submission to PTBC; DNS is well-respected and
    the course-website lists CE accreditation in many states.
    """
    url = "https://www.rehabps.cz/rehab/co.php"
    # Confirmed CA dates from May 2026 scrape — fallback when network is
    # unavailable. Live fetch dedupes against these.
    SEED_DNS: list[tuple] = [
        ("2026-06-06", "2026-06-07", "DNS Movement Performance Summit",                  "Stanford, CA"),
        ("2026-06-19", "2026-06-21", "DNS Pediatric Course Part 1",                      "Pasadena, CA"),
        ("2026-07-27", "2026-07-28", "DNS Skills Course on Scoliosis",                   "Pasadena, CA"),
        ("2026-07-30", "2026-08-02", "DNS Pediatric Course Part 2",                      "Pasadena, CA"),
        ("2026-08-21", "2026-08-23", "DNS Skills Course in Viscero-Somatic Patterns",    "San Diego, CA"),
        ("2026-08-29", "2026-08-30", "DNS Basic Course \"A\"",                           "Pasadena, CA"),
        ("2026-10-02", "2026-10-04", "DNS Basic Course \"B\"",                           "Pasadena, CA"),
        ("2026-10-30", "2026-11-01", "DNS Basic Course \"A\"",                           "San Diego, CA"),
        ("2027-01-16", "2027-01-18", "DNS Intermediate Course \"C\"",                    "Pasadena, CA"),
        ("2027-02-27", "2027-02-28", "DNS Basic Course \"A\"",                           "Pasadena, CA"),
        ("2027-03-19", "2027-03-21", "DNS Basic Course \"B\"",                           "Pasadena, CA"),
    ]

    courses: list[Course] = []
    today = date.today()
    cutoff_future = today.replace(year=today.year + 2)
    seen: set[tuple] = set()

    def emit(start_iso, end_iso, title, location, href):
        key = (title.lower(), start_iso, location.lower())
        if key in seen:
            return
        seen.add(key)
        courses.append(Course(
            course_id=make_course_id("DNS Prague School", title, start_iso, location),
            provider="DNS / Prague School of Rehabilitation",
            title=title,
            start_date=start_iso,
            end_date=end_iso,
            location=location,
            url=href,
            audience="PT, DC, MD, DO, ATC, manual therapists, S&C coaches",
            pt_ceus="yes (self-submit syllabus to CA PTBC)",
            notes="DNS = Dynamic Neuromuscular Stabilization (Kolar method). "
                  "International curriculum; US courses run by certified DNS "
                  "instructors. CA PT CEU via self-submission.",
        ))

    html = http_get(url)
    if not html:
        log("  DNS: live fetch returned no HTML (network error or block)")
        for iso_start, iso_end, title, loc in SEED_DNS:
            emit(iso_start, iso_end, title, loc, url)
        return courses

    soup = BeautifulSoup(html, "html.parser")

    # Each course is an <a> with text shaped like:
    #   "Pasadena, CA, USA - August 29 - 30, 2026 - Basic course "A""
    # Some have variants like "Pasadena, USA", "Pasadena CA, USA",
    # "Stanford, USA", "San Diego, USA", "Buenos Aires, Argentina".
    #
    # We use re.search (no ^ anchor) for robustness against leading whitespace
    # or wrapping artifacts from BeautifulSoup. The greedy location at start
    # is balanced by requiring a Month/day/year sequence to anchor the date.
    entry_re = re.compile(
        r"(.+?)\s+-\s+"                               # 1: location
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+"   # 2: start month
        r"(\d{1,2})"                                  # 3: start day
        r"(?:\s*-\s*"
        r"(?:(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+)?" # 4: end month (optional)
        r"(\d{1,2}))?,\s+"                            # 5: end day (optional)
        r"(20\d{2})\s+-\s+"                           # 6: year
        r"(.+?)\s*$",                                 # 7: title
        re.IGNORECASE,
    )

    parsed_count = 0
    ca_count = 0
    anchor_count = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "course.php" not in href:
            continue
        anchor_count += 1
        text = a.get_text(" ", strip=True)
        # Normalize unicode quotes, non-breaking spaces, and hyphens.
        text = (text
                .replace("\u00a0", " ")        # &nbsp;
                .replace("\u2013", "-")        # en-dash
                .replace("\u2014", "-")        # em-dash
                .replace("\u201c", '"')        # left curly quote
                .replace("\u201d", '"')        # right curly quote
                .replace("\u2018", "'")
                .replace("\u2019", "'"))
        text = re.sub(r"\s+", " ", text).strip()

        m = entry_re.search(text)
        if not m:
            continue
        parsed_count += 1
        loc_raw, mo1, d1, mo2, d2, year, title = m.groups()
        # Decode CA mentions:
        #   "Pasadena, CA, USA"  -> loc has "CA" component, treat as CA
        #   "Pasadena CA, USA"   -> rare; CA still in there
        #   "San Diego, USA"     -> need city-keyword match
        #   "Stanford, USA"      -> need city-keyword match (we added one)
        #   "Buenos Aires, Argentina" -> drop
        # Use the consolidated keep_for_listing() helper. The local hand-rolled
        # "is_ca" check above (", ca," / endswith ", ca") was historically
        # added because DNS's location strings ("Pasadena, USA",
        # "Stanford, USA") sometimes drop the state. keep_for_listing()
        # delegates to in_radius() which already handles those, AND it accepts
        # virtual/online events for the new virtual section.
        loc_lower = loc_raw.lower()
        keep = (
            ", ca," in loc_lower or ", ca " in loc_lower or
            loc_lower.endswith(", ca") or
            " ca, " in loc_lower or
            keep_for_listing(loc_raw)
        )
        if not keep:
            continue

        try:
            year_n = int(year)
            start = date(year_n, MONTHS[mo1.lower()[:3]], int(d1))
        except (ValueError, KeyError):
            continue
        if d2:
            try:
                end_mo = MONTHS[(mo2 or mo1).lower()[:3]]
                # If end month is earlier than start month, year increments
                end_year = year_n + 1 if end_mo < MONTHS[mo1.lower()[:3]] else year_n
                end = date(end_year, end_mo, int(d2))
            except (ValueError, KeyError):
                end = start
        else:
            end = start

        # Drop past dates and very-far-future dates outside our window.
        if end < today or start > cutoff_future:
            continue

        # Clean up location string for output.
        loc_clean = re.sub(r",?\s*USA\s*$", "", loc_raw).strip()
        # Normalize CA-suffix cases. Source data uses inconsistent formats:
        # "Pasadena, CA, USA", "Pasadena CA, USA", "Pasadena, USA",
        # "Stanford, USA", "San Diego, USA". Force everything to "City, CA".
        ca_cities = ("pasadena", "stanford", "san diego")
        low = loc_clean.lower()
        # Strip a bare "CA" suffix or comma-CA-comma fragment if present.
        loc_clean = re.sub(r"[\s,]+CA\s*$", "", loc_clean, flags=re.IGNORECASE).strip(", ")
        # Then re-append ", CA" canonically for our known CA cities.
        if any(c in low for c in ca_cities):
            loc_clean = f"{loc_clean}, CA"

        # Clean unicode smart quotes around title (e.g. "A" vs "A")
        title = title.replace("\u201c", '"').replace("\u201d", '"').strip()

        emit(start.isoformat(), end.isoformat(), title, loc_clean, href)
        ca_count += 1

    log(f"  DNS: found {anchor_count} course-anchor links, regex matched "
        f"{parsed_count}; {ca_count} match California")
    if anchor_count == 0 and html:
        log("  DNS: ZERO anchors with 'course.php' found in response — the "
             "page received is not the expected courses page. Dumping for "
             "inspection.")
        dump_html_for_debug("dns", url, html)
        # Also log the first 300 chars of the response so debugging is
        # possible without opening the dump file.
        snippet = re.sub(r"\s+", " ", html[:400])
        log(f"  DNS: response starts with: {snippet!r}")

    for iso_start, iso_end, title, loc in SEED_DNS:
        emit(iso_start, iso_end, title, loc, url)
    return courses


# ----------------------------------------------------------------------------
# JSON-LD helpers (shared by state-board + Eventbrite scrapers)
# ----------------------------------------------------------------------------
# Modern association-management platforms (YourMembership, MemberClicks, Wild
# Apricot) and Eventbrite both embed Schema.org Event blocks in their HTML.
# These helpers walk a parsed JSON-LD payload and flatten Event records into
# Course objects.

def _extract_events_from_jsonld(data) -> list[dict]:
    """Walk a JSON-LD object/array and return every @type=Event dict found.

    Handles common wrappers: bare lists, @graph arrays, itemListElement
    arrays, and nested subEvent collections.
    """
    out: list[dict] = []
    if isinstance(data, list):
        for item in data:
            out.extend(_extract_events_from_jsonld(item))
    elif isinstance(data, dict):
        t = data.get("@type", "")
        if isinstance(t, list):
            is_event = any("Event" in str(x) for x in t)
        else:
            is_event = "Event" in str(t)
        if is_event:
            out.append(data)
        for key in ("@graph", "itemListElement", "subEvent", "item"):
            if key in data:
                out.extend(_extract_events_from_jsonld(data[key]))
    return out


def _flatten_jsonld_location(loc) -> str:
    """Turn a Schema.org `location` field (string | Place | list) into text."""
    if not loc:
        return ""
    if isinstance(loc, str):
        return loc
    if isinstance(loc, list):
        return ", ".join(_flatten_jsonld_location(item) for item in loc if item)
    if isinstance(loc, dict):
        parts: list[str] = []
        name = loc.get("name", "")
        if name:
            parts.append(str(name))
        addr = loc.get("address", "")
        if isinstance(addr, str):
            parts.append(addr)
        elif isinstance(addr, dict):
            for key in ("streetAddress", "addressLocality",
                        "addressRegion", "postalCode"):
                v = addr.get(key, "")
                if v:
                    parts.append(str(v))
        return ", ".join(p for p in parts if p)
    return ""


def _jsonld_event_to_course(ev: dict, provider_name: str,
                             fallback_url: str,
                             audience: str = "PT, PTA",
                             pt_ceus: str = "yes",
                             extra_notes: str = "") -> Course | None:
    """Convert a Schema.org Event dict to a Course (or None if unusable).

    Filters by in_radius() so events outside the western-states window are
    dropped before they reach the diff layer.
    """
    name = (ev.get("name") or ev.get("headline") or "").strip()
    if not name:
        return None
    iso_start = parse_date_loose(ev.get("startDate", "") or "")
    iso_end = parse_date_loose(ev.get("endDate", "") or "")
    location = _flatten_jsonld_location(ev.get("location", ""))
    if not keep_for_listing(location):
        return None
    course_url = ev.get("url") or fallback_url
    if isinstance(course_url, list):
        course_url = course_url[0] if course_url else fallback_url
    description = ev.get("description", "") or ""
    if isinstance(description, str):
        description = re.sub(r"\s+", " ", description).strip()[:200]
    notes = f"From {provider_name}. {description}".strip()
    if extra_notes:
        notes = f"{notes} {extra_notes}".strip()
    return Course(
        course_id=make_course_id(provider_name, name, iso_start or "TBD", location),
        provider=provider_name,
        title=name,
        start_date=iso_start,
        end_date=iso_end,
        location=location,
        url=str(course_url),
        audience=audience,
        pt_ceus=pt_ceus,
        notes=notes,
    )


# ----------------------------------------------------------------------------
# State APTA chapter / PT board CE calendars
# ----------------------------------------------------------------------------
# State PT *boards* (regulators) rarely publish course-level CE lists — they
# only publish approved-provider lists. Course-level calendars live on each
# state's APTA *chapter* site. This module scrapes the chapters as a proxy
# for "the state board's CE list."
#
# Chapter sites are typically built on YourMembership / MemberClicks / Wild
# Apricot. Layouts differ, but almost all emit Schema.org Event JSON-LD in
# the HTML response — that's our primary parser. CSS-class heuristics are a
# fallback for sites that don't.
#
# CA is intentionally omitted — CPTA already has its own scraper (key:
# `cpta`) and lives behind Cloudflare with seed-list handling.
#
# To VERIFY/UPDATE a chapter URL: open the site in a browser, navigate to
# the events page, copy the URL into APTA_CHAPTERS below, and run
#     python3 ceu_scraper.py --provider state_<code>
# If 0 courses come back, check ./debug/state_<code>.html.
# ----------------------------------------------------------------------------

APTA_CHAPTERS: dict[str, dict] = {
    # CA-only policy: CA is covered by the dedicated `cpta` scraper (which
    # handles CPTA's Cloudflare challenge). Other-state chapters intentionally
    # omitted — host their events outside California by definition.
    #
    # If the geographic scope is ever broadened, restore entries like:
    #   "OR": {"name": "APTA Oregon (OPTA)", "url": "https://www.opta.org/events"},
    #   "WA": {"name": "APTA Washington (PTWA)", "url": "https://ptwa.org/events"},
    #   "NV": {"name": "APTA Nevada (NPTA)", "url": "https://nvapta.org/events"},
    #   "AZ": {"name": "APTA Arizona (AzPTA)", "url": "https://www.aptaaz.org/events"},
    #   "CO": {"name": "APTA Colorado (APTA-CO)", "url": "https://www.aptaco.org/events"},
}


def scrape_state_board_ce_list(state: str) -> list[Course]:
    """Scrape one APTA state chapter's events calendar.

    Parse strategy:
      1. JSON-LD Event blocks (preferred — most modern AMS platforms emit
         these).
      2. CSS-class heuristics on common event-card containers (fallback).

    Best-effort: returns [] on any failure so the rest of the run continues.
    """
    cfg = APTA_CHAPTERS.get(state.upper())
    if not cfg:
        log(f"  state_board: no config for state '{state}' — "
            f"add it to APTA_CHAPTERS")
        return []

    provider_name = cfg["name"]
    url = cfg["url"]
    needs_pw = cfg.get("needs_playwright", False)

    html = http_get(url, allow_playwright_fallback=needs_pw)
    if not html:
        log(f"  state_board ({state}): no HTML returned from {url}")
        return []

    soup = BeautifulSoup(html, "html.parser")
    courses: list[Course] = []
    seen_ids: set[str] = set()
    jsonld_raw_count = 0

    # --- Strategy 1: JSON-LD ---
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue
        for ev in _extract_events_from_jsonld(data):
            jsonld_raw_count += 1
            course = _jsonld_event_to_course(
                ev,
                provider_name=provider_name,
                fallback_url=url,
                audience="PT, PTA",
                pt_ceus="yes",
                extra_notes="CERS/state-board approval likely; verify on event page.",
            )
            if course and course.course_id not in seen_ids:
                seen_ids.add(course.course_id)
                courses.append(course)

    # --- Strategy 2: CSS-class heuristics (only if JSON-LD found nothing) ---
    if not courses:
        from urllib.parse import urljoin
        candidates = soup.select(
            ".event-item, .event-card, .events-list-item, "
            ".eventListItem, .event-listing, "
            "[class*='event-card'], [class*='EventCard'], "
            "[class*='event-row']"
        )
        for el in candidates:
            title_el = el.find(["h2", "h3", "h4", "a"])
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title or len(title) < 4:
                continue
            date_el = el.find(class_=re.compile(r"date|when|time", re.I))
            iso_start = parse_date_loose(
                date_el.get_text(" ", strip=True) if date_el else ""
            )
            loc_el = el.find(class_=re.compile(r"location|venue|where", re.I))
            location = loc_el.get_text(" ", strip=True) if loc_el else ""
            if not keep_for_listing(location):
                continue
            link_el = el.find("a", href=True)
            course_url = urljoin(url, link_el["href"]) if link_el else url
            cid = make_course_id(provider_name, title, iso_start or "TBD", location)
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            courses.append(Course(
                course_id=cid,
                provider=provider_name,
                title=title,
                start_date=iso_start,
                location=location,
                url=course_url,
                audience="PT, PTA",
                pt_ceus="yes",
                notes=f"From {provider_name} events calendar (CSS-fallback "
                      f"parse). Verify CE hours on the event page.",
            ))

    log(f"  state_board ({state}): JSON-LD events seen={jsonld_raw_count}; "
        f"kept {len(courses)} in-radius courses from {url}")
    if not courses and html:
        dump_html_for_debug(f"state_{state.lower()}", url, html)
    return courses


# ----------------------------------------------------------------------------
# Eventbrite scraper REMOVED (2026-06). The public search URL now returns 404
# and Eventbrite no longer embeds Schema.org Event JSON-LD in these pages, so
# the scraper parsed zero events on every run. Removed to stop dead fetches and
# log noise. (The shared JSON-LD helpers above are still used by the
# state-board scraper.) If Eventbrite is ever revisited, it needs a rewrite
# against the current page structure — and a check of their scraping ToS.
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# Wrapper factory — the state-board scraper takes a `state` arg, but the
# PROVIDERS registry expects no-arg callables. Each configured state gets its
# own wrapper with a unique __name__ for logging.
# ----------------------------------------------------------------------------

def _make_state_board_wrapper(state: str) -> Callable[[], list[Course]]:
    def wrapper() -> list[Course]:
        return scrape_state_board_ce_list(state)
    wrapper.__name__ = f"scrape_state_board_{state.lower()}"
    wrapper.__doc__ = f"State-board / APTA-chapter CE wrapper for {state}."
    return wrapper


# ----------------------------------------------------------------------------
# USC Division of Biokinesiology and Physical Therapy
# ----------------------------------------------------------------------------
# PTBC-recognized approving agency. Public course list at:
#   https://pt.usc.edu/upcoming-courses/
#
# Static HTML, no JS required. Each course block follows this pattern:
#
#   05.30.2026                                  <- date line (single day)
#                                                  or "07.18.2026 - 07.19.2026"
#
#   ###### [Course Title](registration-url)     <- title in h6 link
#
#   Instructor Name, PT, DPT...
#   CEUs: 0.3 CEUs (3 contact hours) Cost:...
#   Location: Center for Health Professions, 1540 Alcazar Street, Los Angeles, CA 90033
#
#   [REGISTER](registration-url)
#
# Most courses are held at the Center for Health Professions, USC Health Sciences
# Campus, 1540 Alcazar Street, Los Angeles, CA 90033. A handful run at other
# California locations; the in_radius() filter keeps both. Online self-paced
# courses appear without a date line and are caught by an "ONLINE SELF-PACED
# COURSES" section header — those flow into the virtual section automatically.
# ----------------------------------------------------------------------------

def scrape_usc() -> list[Course]:
    """Scrape USC Bkn-PT's upcoming-courses page."""
    url = "https://pt.usc.edu/upcoming-courses/"
    html = http_get(url, allow_playwright_fallback=True)
    if not html:
        log("  USC: no HTML returned")
        return []

    courses: list[Course] = []
    soup = BeautifulSoup(html, "html.parser")

    # Find the content body — USC uses a WordPress theme; the courses are
    # in the main #content area. Use the body text for line-by-line scanning.
    body = soup.find("main") or soup.find(id="content") or soup.body
    if not body:
        log("  USC: page structure changed; dumping HTML for inspection")
        dump_html_for_debug("usc", url, html)
        return []

    # Convert to text but preserve link structure: walk children, extracting
    # date headings and the heading-link pairs that follow each one.
    text = body.get_text("\n", strip=False)

    # Match the date line variants:
    #   "05.30.2026"
    #   "07.18.2026 - 07.19.2026"
    #   "07.31.2026 - 01.08.2027"
    date_pattern = re.compile(
        r"\b(\d{2}\.\d{2}\.\d{4})\s*(?:[\u2010-\u2015\-]\s*(\d{2}\.\d{2}\.\d{4}))?\b"
    )

    def parse_usc_date(s: str) -> str:
        try:
            mo, da, yr = s.split(".")
            return f"{yr}-{mo}-{da}"
        except ValueError:
            return ""

    # Find all (h6/h7) title links — these are the course titles. They sit
    # right after the date line. Build a mapping of (anchor_text, href, position_in_text).
    title_links: list[tuple[str, str, int]] = []
    for a in body.find_all("a", href=True):
        # USC title links point at cvent.com (registration platform).
        href = a["href"]
        if "cvent" not in href and "REGISTER" in a.get_text(strip=True).upper():
            continue
        text_a = a.get_text(strip=True)
        # Skip the "REGISTER" buttons — title link text is the actual course name
        if text_a.upper() == "REGISTER":
            continue
        if "cvent" in href and text_a and len(text_a) > 10:
            # Locate this link's text inside the body text to align positions.
            try:
                pos = text.index(text_a)
            except ValueError:
                continue
            title_links.append((text_a, href, pos))

    # For each date match, pair with the next title link by position.
    seen = set()
    online_section = False
    for ln in text.split("\n"):
        if "ONLINE SELF-PACED COURSES" in ln.upper():
            online_section = True

    for m in date_pattern.finditer(text):
        date_pos = m.start()
        iso_start = parse_usc_date(m.group(1))
        iso_end = parse_usc_date(m.group(2)) if m.group(2) else iso_start
        if not iso_start:
            continue
        # Skip dates in the online self-paced section (no specific dates anyway,
        # but be defensive).
        # Find the title link that comes RIGHT AFTER this date position.
        following = [(t, h, p) for (t, h, p) in title_links if p > date_pos]
        if not following:
            continue
        following.sort(key=lambda x: x[2])
        title, course_url, title_pos = following[0]

        # Pull a context window (date_pos .. next_date_pos) to extract location.
        next_date_m = date_pattern.search(text, m.end())
        window_end = next_date_m.start() if next_date_m else min(date_pos + 2000, len(text))
        ctx = text[date_pos:window_end]

        # Location: USC formats as "Location: <venue>, <city>, <state> <zip>".
        loc_match = re.search(
            r"Location:\s*([^\n]{5,200}?)(?:\s{2,}|\n|$)", ctx, re.I
        )
        location = loc_match.group(1).strip() if loc_match else \
                   "Center for Health Professions, 1540 Alcazar Street, Los Angeles, CA 90033"

        if not keep_for_listing(location):
            continue

        # CEUs: capture "X.X CEUs (Y contact hours)" or similar.
        ceus_match = re.search(
            r"CEUs?:\s*([\d\.]+\s*(?:CEUs?|units?)?\s*\([^\)]*?\bcontact hours?\b[^\)]*\))",
            ctx, re.I
        )
        ceus_text = ceus_match.group(1).strip() if ceus_match else ""

        cid = make_course_id("USC Bkn-PT", title, iso_start, location)
        if cid in seen:
            continue
        seen.add(cid)

        notes_parts = ["PTBC-recognized provider (USC Div. of Biokinesiology and PT)."]
        if ceus_text:
            notes_parts.append(ceus_text)
        notes = " ".join(notes_parts)

        courses.append(Course(
            course_id=cid,
            provider="USC Division of Biokinesiology and Physical Therapy",
            title=title,
            start_date=iso_start,
            end_date=iso_end if iso_end != iso_start else "",
            location=location,
            url=course_url,
            audience="PT, PTA",
            pt_ceus="yes",
            notes=notes,
        ))

    log(f"  USC: parsed {len(courses)} in-radius courses from {url}")
    if not courses:
        dump_html_for_debug("usc", url, html)
    return courses


# ----------------------------------------------------------------------------
# CPTA Approved Continuing Education courses — one-time import (persistent)
# ----------------------------------------------------------------------------
# This is the official CPTA list of approved courses
# (public_approvedconedcourses.xlsx, downloaded from ccapta.org). Imported
# once into cpta_approved_courses.json next to this script, and re-emitted
# on every run so the entries persist across runs without being marked as
# "removed" in the diff.
#
# Filter applied at import time: kept only CA in-person OR online/webinar
# entries (per user spec). Out-of-state in-person courses (Raleigh NC,
# Colorado Springs, etc.) and entries with location "TBD" were dropped.
#
# To refresh the list, drop a new public_approvedconedcourses.xlsx in the
# script folder and re-run the importer (see scripts/import_cpta_approved.py
# in the README) — or just edit cpta_approved_courses.json by hand.
# ----------------------------------------------------------------------------

CPTA_APPROVED_JSON = SCRIPT_DIR / "cpta_approved_courses.json"


def scrape_cpta_approved_list() -> list[Course]:
    """Load CPTA-approved CE courses from the JSON sidecar.

    File format: a JSON array of dicts with keys matching the Course
    dataclass fields (course_id, provider, title, start_date, end_date,
    location, url, audience, pt_ceus, notes). Missing optional fields
    fall back to dataclass defaults.

    Best-effort: returns [] if the file is missing or unreadable so the
    rest of the run continues. Logs a clear message either way.
    """
    if not CPTA_APPROVED_JSON.exists():
        log(f"  cpta_approved: {CPTA_APPROVED_JSON.name} not found — "
            f"skip (this is fine if you haven't imported the official "
            f"CPTA list yet).")
        return []

    try:
        raw = json.loads(CPTA_APPROVED_JSON.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log(f"  cpta_approved: failed to read {CPTA_APPROVED_JSON.name}: {e}")
        return []

    if not isinstance(raw, list):
        log(f"  cpta_approved: {CPTA_APPROVED_JSON.name} root is not a "
            f"JSON array — skip")
        return []

    courses: list[Course] = []
    skipped = 0
    for entry in raw:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        title = entry.get("title", "").strip()
        provider = entry.get("provider", "").strip()
        if not title or not provider:
            skipped += 1
            continue
        start_date = entry.get("start_date", "")
        location = entry.get("location", "")
        # Rebuild course_id deterministically so the diff engine sees the
        # same id across runs even if the JSON omits it.
        cid = entry.get("course_id") or make_course_id(
            provider, title, start_date, location
        )
        courses.append(Course(
            course_id=cid,
            provider=provider,
            title=title,
            start_date=start_date,
            end_date=entry.get("end_date", ""),
            location=location,
            url=entry.get("url", ""),
            audience=entry.get("audience", "PT, PTA"),
            pt_ceus=entry.get("pt_ceus", "yes"),
            notes=entry.get("notes", "CPTA-approved continuing education."),
        ))

    log(f"  cpta_approved: loaded {len(courses)} courses from "
        f"{CPTA_APPROVED_JSON.name}"
        + (f" (skipped {skipped} malformed)" if skipped else ""))
    return courses


# Register all scrapers here.
PROVIDERS: dict[str, Callable[[], list[Course]]] = {
    "art": scrape_active_release,
    "barbell": scrape_barbell_rehab,
    "mulligan": scrape_mulligan,
    "cup": scrape_cup_therapy,
    "agile": scrape_agile_pt,
    "cpta": scrape_cpta,
    "ucsf": scrape_ucsf,
    "pri": scrape_pri,
    "ipa": scrape_ipa,
    "gls": scrape_great_lakes,
    "hw": scrape_herman_wallace,
    "wilk": scrape_wilk,
    "vestibular": scrape_vestibular,
    "fms": scrape_fms,
    "amsi": scrape_amsi,
    "aamt": scrape_aamt,
    "eldoa": scrape_eldoa,
    "ne_seminars": scrape_northeast_seminars,
    "frs": scrape_frs,
    "painfree": scrape_painfree,
    "dns": scrape_dns,
    "usc": scrape_usc,
    "cpta_approved": scrape_cpta_approved_list,
}

# Register each configured APTA state chapter as its own provider key:
#   state_or, state_wa, state_nv, state_az, state_co
for _state_code in APTA_CHAPTERS:
    PROVIDERS[f"state_{_state_code.lower()}"] = _make_state_board_wrapper(_state_code)


# ----------------------------------------------------------------------------
# State / diff / output
# ----------------------------------------------------------------------------

def load_state() -> dict[str, dict]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        log("  STATE FILE CORRUPT — starting fresh")
        return {}


def save_state(state: dict[str, dict]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def diff_courses(
    previous: dict[str, dict], current: list[Course]
) -> tuple[list[Course], list[tuple[Course, dict]], list[dict]]:
    """Return (added, changed, removed)."""
    cur_map = {c.course_id: c for c in current}
    prev_ids = set(previous.keys())
    cur_ids = set(cur_map.keys())

    added = [cur_map[i] for i in (cur_ids - prev_ids)]
    removed = [previous[i] for i in (prev_ids - cur_ids)]

    changed: list[tuple[Course, dict]] = []
    for cid in cur_ids & prev_ids:
        prev = previous[cid]
        cur = asdict(cur_map[cid])
        # Ignore discovered_at when comparing
        prev_cmp = {k: v for k, v in prev.items() if k != "discovered_at"}
        cur_cmp = {k: v for k, v in cur.items() if k != "discovered_at"}
        if prev_cmp != cur_cmp:
            changed.append((cur_map[cid], prev))
    return added, changed, removed


def render_markdown(courses: list[Course]) -> str:
    """Render the current course list as markdown.

    Output structure:
      # California PT CEU Courses — Live Listing
      ## Legend
      # In-Person — California
        ## <Month Year>   (grouped)
          ### <date> — <title>
            ...
      # Virtual / Online / Webinar
        ## <Month Year>   (grouped)
          ### <date> — <title>
            ...

    The two top-level buckets are decided by is_virtual() on each course's
    location field. Courses with an empty location are treated as in-person CA
    (rare; surfaced for manual review).
    """
    courses = sorted(courses, key=lambda c: (c.start_date or "9999-99-99", c.provider))
    today_str = date.today().isoformat()

    # Split into the two top-level buckets.
    ca_courses = [c for c in courses if not is_virtual(c.location)]
    virtual_courses = [c for c in courses if is_virtual(c.location)]

    lines: list[str] = []
    lines.append(f"# California PT CEU Courses — Live Listing\n")
    lines.append(f"*Auto-generated by ceu_scraper.py on {today_str}.*\n")
    lines.append(f"*Window: {today_str} to {(date.today() + timedelta(days=365)).isoformat()}.*\n")
    lines.append(
        f"*Total tracked: **{len(courses)}** "
        f"(in-person CA: {len(ca_courses)}, "
        f"virtual / online / webinar: {len(virtual_courses)}).*\n"
    )
    lines.append("")
    lines.append("## Legend\n")
    lines.append("- PT CEUs: ✅ = yes, ❌ = no, ⚠️ = via reciprocity / verify, ❓ = unknown\n")
    lines.append("")

    def render_bucket(bucket: list[Course], heading: str) -> None:
        """Append a top-level section for `bucket`, grouped by month."""
        lines.append(f"\n# {heading}\n")
        if not bucket:
            lines.append("*No courses currently tracked in this category.*\n")
            return
        current_month = None
        for c in bucket:
            if c.start_date:
                try:
                    d = date.fromisoformat(c.start_date)
                    month_key = d.strftime("%B %Y")
                except ValueError:
                    month_key = "Date TBD"
            else:
                month_key = "Date TBD"
            if month_key != current_month:
                lines.append(f"\n## {month_key}\n")
                current_month = month_key

            pt_marker = {
                "yes": "✅",
                "no": "❌",
                "via reciprocity": "⚠️",
                "unknown": "❓",
            }.get(c.pt_ceus.lower(), "❓")

            date_str = c.start_date
            if c.end_date:
                date_str = f"{c.start_date} – {c.end_date}"

            lines.append(f"### {date_str} — {c.title}")
            lines.append(f"- **Provider:** {c.provider}")
            lines.append(f"- **Location:** {c.location}")
            if c.audience:
                lines.append(f"- **Audience:** {c.audience}")
            lines.append(f"- **PT CEUs:** {pt_marker} {c.pt_ceus}")
            if c.notes:
                lines.append(f"- **Notes:** {c.notes}")
            if c.url:
                lines.append(f"- **URL:** {c.url}")
            lines.append("")

    render_bucket(ca_courses, "In-Person — California")
    render_bucket(virtual_courses, "Virtual / Online / Webinar")
    return "\n".join(lines)


def render_changes(
    added: list[Course],
    changed: list[tuple[Course, dict]],
    removed: list[dict],
) -> str:
    today_str = date.today().isoformat()
    lines = [f"# CEU Course Changes — {today_str}\n"]
    lines.append("")

    if not (added or changed or removed):
        lines.append("No changes detected since the last run.\n")
        return "\n".join(lines)

    if added:
        lines.append(f"## 🟢 Newly listed ({len(added)})\n")
        for c in sorted(added, key=lambda x: x.start_date or "9999"):
            lines.append(f"- **{c.start_date or 'TBD'}** — {c.title} *({c.provider})* — {c.location}")
            if c.url:
                lines.append(f"  - {c.url}")
        lines.append("")

    if changed:
        lines.append(f"## 🟡 Changed ({len(changed)})\n")
        for cur, prev in changed:
            lines.append(f"- **{cur.start_date}** — {cur.title} *({cur.provider})*")
            for field_name in ("location", "start_date", "end_date", "url", "notes", "pt_ceus"):
                prev_val = prev.get(field_name, "")
                cur_val = getattr(cur, field_name)
                if prev_val != cur_val:
                    lines.append(f"  - `{field_name}`: {prev_val!r} → {cur_val!r}")
        lines.append("")

    if removed:
        lines.append(f"## 🔴 Removed / no longer listed ({len(removed)})\n")
        for c in removed:
            lines.append(f"- **{c.get('start_date', 'TBD')}** — {c.get('title', '?')} *({c.get('provider', '?')})*")
        lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def run(only_provider: str | None = None, notify: bool = False) -> int:
    log(f"==== Run start (providers: {only_provider or 'ALL'}) ====")
    today = date.today()
    all_courses: list[Course] = []
    failed_providers: list[str] = []

    for name, fn in PROVIDERS.items():
        if only_provider and name != only_provider:
            continue
        log(f"-- {name} ({fn.__name__}) --")
        try:
            courses = fn()
            log(f"   found {len(courses)} courses")
            # Defensive geo filter. Most scrapers already call
            # keep_for_listing() internally, but a few (AMSI, AAMT) explicitly
            # defer filtering. Keep CA in-person AND virtual/online/webinar
            # events; drop only out-of-region in-person events. Empty-location
            # courses are kept and surfaced for manual review.
            in_ca = [
                c for c in courses
                if not c.location or keep_for_listing(c.location)
            ]
            geo_dropped = len(courses) - len(in_ca)
            if geo_dropped:
                log(f"   dropped {geo_dropped} out-of-region courses")
            # Filter to upcoming 365 days
            in_window = [c for c in in_ca if within_window(c.start_date, today)]
            log(f"   {len(in_window)} within 365-day window")
            # Filter to PT-attendable only
            pt_attendable = [c for c in in_window if is_pt_attendable(c)]
            dropped = len(in_window) - len(pt_attendable)
            if dropped:
                log(f"   dropped {dropped} non-PT-attendable courses")
            all_courses.extend(pt_attendable)
        except Exception as e:
            log(f"   ERROR: {e}")
            failed_providers.append(name)

    # Deduplicate by course_id (some courses cross-listed)
    seen: dict[str, Course] = {}
    for c in all_courses:
        if c.course_id not in seen:
            c.discovered_at = datetime.now().isoformat(timespec="seconds")
            seen[c.course_id] = c
    deduped = list(seen.values())
    log(f"== Total after dedup: {len(deduped)} ==")

    # Diff vs previous state
    previous = load_state()
    # Preserve original discovered_at timestamps for unchanged entries
    for c in deduped:
        if c.course_id in previous:
            c.discovered_at = previous[c.course_id].get("discovered_at", c.discovered_at)

    added, changed, removed = diff_courses(previous, deduped)
    log(f"== Diff: +{len(added)} added, ~{len(changed)} changed, -{len(removed)} removed ==")
    if failed_providers:
        log(f"!! FAILED: {failed_providers}")

    # Write outputs
    OUTPUT_MD.write_text(render_markdown(deduped))
    CHANGES_MD.write_text(render_changes(added, changed, removed))
    save_state({c.course_id: asdict(c) for c in deduped})

    log(f"   wrote {OUTPUT_MD.name} ({len(deduped)} courses)")
    log(f"   wrote {CHANGES_MD.name}")

    if notify and (added or changed or removed):
        try_notify(added, changed, removed)
    log("==== Run done ====\n")
    return 0


def try_notify(added, changed, removed) -> None:
    """Best-effort desktop notification on macOS / Linux. Skip silently on failure."""
    summary = f"CEU scraper: +{len(added)} new, ~{len(changed)} changed, -{len(removed)} removed"
    try:
        if sys.platform == "darwin":
            os.system(
                f"""osascript -e 'display notification "{summary}" with title "CEU Scraper"'"""
            )
        elif sys.platform.startswith("linux"):
            os.system(f"""notify-send "CEU Scraper" "{summary}" """)
        # Windows: skip — Toast notifications need winrt; user can read changes.md
    except Exception as e:
        log(f"  notify failed: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="California PT CEU course scraper")
    parser.add_argument(
        "--provider", choices=list(PROVIDERS), help="Run only one provider"
    )
    parser.add_argument(
        "--notify", action="store_true", help="Send a desktop notification on changes"
    )
    parser.add_argument(
        "--list-providers", action="store_true", help="List configured providers and exit"
    )
    parser.add_argument(
        "--use-playwright", action="store_true",
        help="Force the headless-browser fallback ON (now the default whenever "
             "playwright is installed). Kept for backward compatibility."
    )
    parser.add_argument(
        "--no-playwright", action="store_true",
        help="Disable the headless-browser fallback even if playwright is installed."
    )
    args = parser.parse_args()

    global USE_PLAYWRIGHT
    if args.no_playwright:
        USE_PLAYWRIGHT = False
    elif args.use_playwright:
        USE_PLAYWRIGHT = True
    if USE_PLAYWRIGHT and not _PLAYWRIGHT_AVAILABLE:
        print("WARNING: browser fallback requested but the `playwright` package "
              "is not installed. Install with:\n"
              "  python3 -m pip install playwright\n"
              "  python3 -m playwright install chromium\n"
              "Continuing with static fetch only.", file=sys.stderr)
        USE_PLAYWRIGHT = False

    if args.list_providers:
        print("Configured providers:")
        for name, fn in PROVIDERS.items():
            print(f"  {name:<14} -> {fn.__name__}")
        return 0

    return run(only_provider=args.provider, notify=args.notify)


if __name__ == "__main__":
    sys.exit(main())
