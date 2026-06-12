#!/usr/bin/env python3
"""
rebuild_courses.py  (v2 — patches index.html)
──────────────────────────────────────────────
After ceu_scraper.py runs, this script:
  1. Reads courses_state.json
  2. Filters to California in-person + hybrid, PT-attendable
  3. Replaces the data block inside index.html (between the
     @@BEGIN_COURSES_DATA@@ / @@END_COURSES_DATA@@ markers)

Run:
    python ceu_scraper.py && python rebuild_courses.py
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

# ── Edit these if your files live elsewhere ──────────────────────────────────
SCRAPER_OUTPUT = Path(__file__).parent / "courses_state.json"
HTML_FILE      = Path(__file__).parent / "index.html"
# ─────────────────────────────────────────────────────────────────────────────

HYBRID_KEYWORDS = ["satellite", "hybrid", "online lecture", "remote lecture"]


def is_california(location: str) -> bool:
    return bool(re.search(r",\s*ca\b", location.lower()))


def get_format(notes: str, location: str) -> str:
    n = notes.lower()
    if any(k in n for k in HYBRID_KEYWORDS):
        return "hybrid"
    if "online" in location.lower() or "virtual" in location.lower():
        return "online"
    return "in-person"


def normalize_ceu(ceu_str: str) -> str:
    s = ceu_str.lower()
    if "self-submit" in s or "self submit" in s:
        return "self-submit"
    if "reciprocity" in s or "via reciprocity" in s:
        return "reciprocity"
    return "yes"


def escape_js(s: str) -> str:
    return (s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", " ")
             .replace("\r", ""))


def course_to_js(c: dict) -> str:
    fields = {
        "id":  c.get("course_id", ""),
        "t":   c.get("title", ""),
        "p":   c.get("provider", ""),
        "s":   c.get("start_date", ""),
        "e":   c.get("end_date", ""),
        "loc": c.get("location", ""),
        "aud": c.get("audience", ""),
        "ceu": normalize_ceu(c.get("pt_ceus", "yes")),
        "n":   c.get("notes", ""),
        "url": c.get("url", ""),
    }
    parts = ", ".join(f'{k}:"{escape_js(str(v))}"' for k, v in fields.items())
    return f"      {{{parts}}},"


def main():
    if not SCRAPER_OUTPUT.exists():
        print(f"ERROR: {SCRAPER_OUTPUT} not found. Run ceu_scraper.py first.")
        sys.exit(1)
    if not HTML_FILE.exists():
        print(f"ERROR: {HTML_FILE} not found.")
        sys.exit(1)

    with open(SCRAPER_OUTPUT, encoding="utf-8") as f:
        raw: dict = json.load(f)

    courses, skipped = [], {"non-ca": 0, "online": 0, "not-pt": 0}
    for course in raw.values():
        loc, notes = course.get("location", ""), course.get("notes", "")
        fmt = get_format(notes, loc)
        if not course.get("pt_attendable", False):
            skipped["not-pt"] += 1; continue
        if not is_california(loc):
            skipped["non-ca"] += 1; continue
        if fmt == "online":
            skipped["online"] += 1; continue
        courses.append(course)

    courses.sort(key=lambda c: c.get("start_date", ""))

    print(f"Loaded {len(raw)} total → keeping {len(courses)} CA in-person/hybrid courses")
    print(f"  Skipped: {skipped['not-pt']} non-PT, {skipped['non-ca']} non-CA, {skipped['online']} online-only")

    today = date.today().isoformat()
    course_lines = "\n".join(course_to_js(c) for c in courses).rstrip(",") + "\n"

    new_block = (
        f'    /* @@BEGIN_COURSES_DATA@@ */\n'
        f'    const SCRAPED_DATE = "{today}";\n'
        f'    const COURSES = [\n'
        f'{course_lines}'
        f'    ];\n'
        f'    /* @@END_COURSES_DATA@@ */'
    )

    html = HTML_FILE.read_text(encoding="utf-8")

    pattern = r'/\* @@BEGIN_COURSES_DATA@@ \*/.*?/\* @@END_COURSES_DATA@@ \*/'
    if not re.search(pattern, html, flags=re.DOTALL):
        print("ERROR: markers @@BEGIN_COURSES_DATA@@ / @@END_COURSES_DATA@@ not found in index.html")
        sys.exit(1)

    html = re.sub(pattern, new_block, html, flags=re.DOTALL)
    HTML_FILE.write_text(html, encoding="utf-8")

    print(f"✓ Patched {HTML_FILE.name}  ({len(courses)} courses, scraped {today})")


if __name__ == "__main__":
    main()
