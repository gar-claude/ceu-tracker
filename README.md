# Western US CEU Course Scraper

Weekly scraper that monitors a curated list of physical therapy, strength and
conditioning, massage therapy, chiropractic, and osteopathic CE provider sites
and produces a refreshed course list for the 6 western states (California,
Oregon, Washington, Nevada, Arizona, Colorado) — plus a changelog of what's new
since the last run.

## What it does

Each run does three things:

1. **Fetches** the upcoming events page from each configured provider.
2. **Filters** events to the 6 western states (CA, OR, WA, NV, AZ, CO) using
   keyword match against state names, abbreviations, and major-metro city
   names; to live in-person courses in the next 365 days; **and to courses
   PTs can attend** (PT-primary providers, or cross-disciplinary courses
   with PT in the target audience or carrying PT CE credit).
3. **Compares** the current scrape against the previous run's state and writes:
   - `courses_current.md` — the full current course list, grouped by month
   - `changes.md` — a changelog of new, changed, and dropped courses
   - `courses_state.json` — machine-readable state for the next diff
   - `scraper.log` — append-only log of every run

## The PT-attendable filter

The scraper includes a course only if **either** of these is true:

- The provider is PT-primary (UCSF DPT CE, CPTA, Agile PT, Herman & Wallace,
  EIM, Great Lakes, Summit, Mulligan MCTA, VestibularPT, PRI, IPA, Northeast
  Seminars/Wilk, Pitt AVPT, AIB, Johns Hopkins, etc.), **or**
- The course is cross-disciplinary and PT (or PTA, or "physical therapy") is
  explicitly in the audience field, **or** the course has PT CEUs listed as
  "yes" or "via reciprocity".

If you add a provider whose courses are purely DC-only, LMT-only, DO-only, or
S&C-instructor-cert-only, those courses will be silently dropped by the filter.
To override this for an individual course, set `pt_attendable=True` and put
"PT" in the `audience` field. To disable the filter globally, comment out the
filter line in `run()` (search for "PT-attendable filter").

## Providers covered

| Key          | Provider                                          | Notes                                                  |
|--------------|---------------------------------------------------|--------------------------------------------------------|
| `art`        | Active Release Techniques                         | Full workshop schedule                                 |
| `barbell`    | Barbell Rehab Method                              | Cert schedule + advanced cert variants                 |
| `mulligan`   | Mulligan MWM USA / MCTA                           | Upcoming course list                                   |
| `cup`        | Cup Therapy (Myofascial Decompression)            | Course calendar                                        |
| `agile`      | Agile Physical Therapy (Palo Alto-based)          | Sports Movement Analysis Series + guest faculty        |
| `cpta`       | California Physical Therapy Association            | Annual conference banner; expand for district CEs      |
| `ucsf`       | UCSF DPT continuing education                     | Often returns 403 (manual check recommended)           |
| `pri`        | Postural Restoration Institute                    | Seeded from 2026 brochure; live page is JS-rendered    |
| `ipa`        | Institute of Physical Art (FMT — Greg & Vicky Johnson) | Seeded from 5/10/2026 scrape of scheduled-courses table |
| `gls`        | Great Lakes Seminars                              | Seeded from May 2026 schedule scrape                   |
| `hw`         | Herman & Wallace Pelvic Rehab Institute           | Satellite-format courses (remote lectures + local lab) |
| `wilk`       | Kevin Wilk / Northeast Seminars                   | Currently zero western-state live dates in window      |
| `vestibular` | Pitt AVPT / Johns Hopkins / AIB / VestibularPT    | Composite vestibular-provider tracker                  |
| `fms`        | Functional Movement Systems (Gray Cook)           | JavaScript-rendered events page — use `--use-playwright` flag for live scraping |
| `amsi`       | AMSI Training (Cumming, GA)                       | Vestibular cert, dry needling, spinal manip, lumbar diff; dates on product pages |
| `aamt`       | American Academy of Manipulative Therapy / Spinal Manipulation Institute | SMT, DN, EMT, VCS, MSKU, BFR, IASTM, DD, PFDN, AMAC, Fellowship |
| `eldoa`      | ELDOA / SomaVOYER (Guy Voyer DO method)           | Primary fetch from somavoyer.com/course-calendar; covers ELDOA + SomaTraining + SomaTherapy + Etiotherapy |
| `ne_seminars`| Northeast Seminars                                | Wilk Shoulder/Knee, Mulligan, UT-BFRT, Combat Athlete |
| `frs`        | Functional Range Systems (Spina)                  | FR/FRC/Kinstretch/FRA/FRS-ISM; mostly online or international |
| `painfree`   | Pain Free Training (Dr. John Rusin)               | PPSC + FKT + Pain-Free Mobility certs; events page is JS-rendered |
| `dns`        | DNS / Prague School of Rehabilitation             | Kolar method; rich CA calendar (Pasadena, Stanford, San Diego) |

To add more providers — chiropractic (DCCE), massage (SFSM), OPSC, etc. —
see "Adding a provider" below.

## Setup

### Install

```bash
cd ceu_scraper
python3 -m venv venv
source venv/bin/activate           # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Optional: Install Playwright (for FMS)

The FMS events page is JavaScript-rendered. To scrape it, also install Playwright:

```bash
pip install playwright
python3 -m playwright install chromium
```

The first command adds the Python library; the second downloads the Chromium binary (~150 MB, one-time). Once installed, use the `--use-playwright` flag below.

### First run

```bash
python ceu_scraper.py
```

This populates `courses_state.json` for the first time. The first run's
`changes.md` will show everything as "newly listed" — that's expected.

### Subsequent runs

```bash
python ceu_scraper.py                    # all providers, no JS rendering
python ceu_scraper.py --use-playwright   # adds FMS scraping; ~+10s
python ceu_scraper.py --notify           # desktop notification on macOS/Linux if anything changed
python ceu_scraper.py --provider art     # debug one provider
python ceu_scraper.py --list-providers   # show what's configured
```

## Schedule it weekly

### macOS / Linux — cron

Edit your crontab (`crontab -e`) and add a line that runs every Monday at 7am:

```cron
0 7 * * 1 cd /Users/you/ceu_scraper && /Users/you/ceu_scraper/venv/bin/python ceu_scraper.py --notify
```

Use absolute paths — cron's PATH is minimal.

### macOS — launchd (more reliable than cron when laptop sleeps)

Create `~/Library/LaunchAgents/com.you.ceuscraper.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.you.ceuscraper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/ceu_scraper/venv/bin/python</string>
    <string>/Users/you/ceu_scraper/ceu_scraper.py</string>
    <string>--notify</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Weekday</key><integer>1</integer>
    <key>Hour</key><integer>7</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key><string>/Users/you/ceu_scraper/launchd.log</string>
  <key>StandardErrorPath</key><string>/Users/you/ceu_scraper/launchd.err</string>
</dict>
</plist>
```

Load it: `launchctl load ~/Library/LaunchAgents/com.you.ceuscraper.plist`

### Windows — Task Scheduler

`Task Scheduler` → Create Basic Task → Weekly → Action: Start a Program →
Program: `C:\path\to\venv\Scripts\python.exe`, Arguments: `ceu_scraper.py`,
Start in: `C:\path\to\ceu_scraper`.

### Claude Code routines

From inside Claude Code, run `/schedule` and create a routine that runs:

```
cd ~/ceu_scraper && ./venv/bin/python ceu_scraper.py
```

Weekly. Routines run on Anthropic-managed infrastructure, so it keeps firing
even when your laptop is off.

## Customization

### Change the radius / city list

Edit `BAY_AREA_KEYWORDS` near the top of `ceu_scraper.py`. It's just a list of
lowercased city names that get matched against venue text. To go wider
(e.g. include Reno, Tahoe, Monterey), add those keywords. To go tighter
(SF + Peninsula only), drop the Sacramento and Santa Cruz entries.

### Change the window

`within_window(start_date, today, days=365)` — change `days` to widen / narrow.

### Adding a provider

Each provider is a function returning `list[Course]`. Pattern:

```python
def scrape_my_provider() -> list[Course]:
    html = http_get("https://example.com/courses")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    courses = []
    for item in soup.select(".course-card"):
        title = item.find("h3").get_text(strip=True)
        location = item.find(".venue").get_text(strip=True)
        if not in_radius(location):
            continue
        iso = parse_date_loose(item.find(".date").get_text(strip=True))
        courses.append(Course(
            course_id=make_course_id("My Provider", title, iso, location),
            provider="My Provider",
            title=title,
            start_date=iso,
            location=location,
            url=item.find("a")["href"],
            audience="PT, ATC",
            pt_ceus="yes",
        ))
    return courses
```

Then register it in `PROVIDERS` at the bottom of the file:

```python
PROVIDERS = {
    ...,
    "myprovider": scrape_my_provider,
}
```

### Suggested next providers to add

- **DCCE** (chiropractic, SF + San Jose) — https://www.dcce.com/dates-locations.php
- **SF School of Massage** (NCBTMB) — https://www.sfsm.edu/workshops/
- **OPSC** (osteopathic CME by the Bay) — https://www.opsc.org/events/event_list.asp
- **Herman & Wallace** (pelvic) — https://hermanwallace.com/continuing-education-courses
- **Great Lakes Seminars CA dates** — https://glseminars.com/california/
- **Summit Professional Education** (CA in-person filter) — https://summit-education.com/courses/format/live

## Things to know / honest limitations

1. **Sites change.** Each scraper is fragile to HTML layout changes. When a
   provider redesigns their site, that scraper will silently return 0 results
   and the next run's `changes.md` will show its courses as "removed". Re-check
   the provider site by hand and update the parser. Treat the scraper as a
   helper, not the source of truth.

2. **UCSF blocks scrapers.** UCSF's CE page returned a 403 on direct fetch.
   You'll need to either run the script from a different network/IP, use a
   different `User-Agent`, or accept that UCSF requires a manual weekly check.
   The script logs this and continues.

3. **Bot etiquette.** This scraper adds a polite 2-second delay between requests
   and a clear `User-Agent` string. Before deploying:
   - Replace the email in `USER_AGENT` with your real email so providers can
     contact you if something goes wrong.
   - Don't run it more than once a week — providers don't post new live courses
     daily. Hammering their sites is rude and risks getting blocked.

4. **CE approval status changes.** A course listed as "PT CEUs ✅" at scrape
   time may have its CERS approval expire. Always reconfirm CEU hours and
   state-board approval on the provider's registration page before paying.

5. **No geocoding.** The radius check uses keyword matching against city names,
   not actual mileage. Sacramento (~88 mi) is in the list; Monterey (~115 mi)
   is not. Edit `BAY_AREA_KEYWORDS` to suit. For accurate distance filtering,
   integrate a geocoding API (Google Maps, Mapbox, or OpenStreetMap Nominatim)
   in `in_radius()`.

6. **Live in-person only.** The script skips events whose location contains
   "webinar / online / virtual / zoom / livestream / remote". If you also want
   to track live webinars, remove that filter in `in_radius()`.

## Outputs

After running:

```
ceu_scraper/
├── ceu_scraper.py
├── requirements.txt
├── README.md
├── courses_current.md      # ← the human-readable course list, refreshed every run
├── changes.md              # ← what changed since last run
├── courses_state.json      # ← internal state (don't edit)
└── scraper.log             # ← append-only run log
```

Read `changes.md` after each weekly run; that's where the value is.
