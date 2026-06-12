# How to Use the CEU Scraper — Step by Step

This is the no-prior-Python-experience walkthrough. Follow it once to get the scraper running on your computer, then use Part B as a quick reference for everyday use.

---

## Part A — One-time setup

### Step 1. Confirm you have Python 3

**On Mac:** open the Terminal app (⌘+Space, type "Terminal") and run:
```
python3 --version
```
You should see something like `Python 3.11.7`. Any version `3.9` or newer is fine. If you get "command not found", install Python from https://www.python.org/downloads/macos/ — pick the latest "macOS 64-bit universal2 installer".

**On Windows:** open PowerShell (Win+X, then "Terminal" or "Windows PowerShell") and run:
```
python --version
```
Same expected output. If you don't have it, install from https://www.python.org/downloads/windows/ and **check the box "Add python.exe to PATH"** during install — this matters.

✅ Done when `python3 --version` (Mac) or `python --version` (Windows) prints a version 3.9 or newer.

---

### Step 2. Put the scraper files in a folder you'll remember

Download the 4 files I shared from this conversation into one folder on your computer:

- `ceu_scraper.py`
- `README.md`
- `requirements.txt`
- `courses_current.md` (the sample output — you can delete it later if you want)

A good folder name and location:
- **Mac:** `~/Documents/ceu-tracker/`
- **Windows:** `C:\Users\<you>\Documents\ceu-tracker\`

After this step, listing the folder should show all 4 files.

✅ Done when all 4 files are in the same folder.

---

### Step 3. Install the two dependencies

In Terminal/PowerShell, navigate into the folder you just made:

**Mac:**
```
cd ~/Documents/ceu-tracker
```

**Windows:**
```
cd C:\Users\<you>\Documents\ceu-tracker
```

Then install the libraries:

**Mac:**
```
python3 -m pip install -r requirements.txt
```

**Windows:**
```
python -m pip install -r requirements.txt
```

You'll see a few lines of "Collecting..." and "Successfully installed requests-... beautifulsoup4-...". That's it.

✅ Done when you see "Successfully installed" with no red error text.

---

### Step 4. Put your email in the user agent

The scraper introduces itself politely to each provider's website. Right now it says `replace_with_your_email@example.com` — you should swap in your real email so providers can reach you if needed.

1. Open `ceu_scraper.py` in any text editor (TextEdit on Mac, Notepad on Windows, or VS Code, or whatever you have).
2. Near the top of the file, find this block:
   ```python
   USER_AGENT = (
       "WesternUS-CEU-Scraper/1.1 (personal CE planning tool; "
       "contact: replace_with_your_email@example.com)"
   )
   ```
3. Replace `replace_with_your_email@example.com` with your actual email.
4. Save the file. Done.

✅ Done when the placeholder email is replaced.

---

### Step 4b. (Optional) Install Playwright for FMS

This step is **optional** but worth doing. Without it, the FMS scraper can't see the events on https://www.functionalmovement.com/events because that page is built with JavaScript. With it, the scraper opens a headless Chrome browser, lets the page finish rendering, and then reads the real events.

You only need this if you want to track FMS events. Everything else works fine without it.

**To install (one-time):**

**Mac:**
```
python3 -m pip install playwright
python3 -m playwright install chromium
```

**Windows:**
```
python -m pip install playwright
python -m playwright install chromium
```

The first command installs the Python library (~5 MB). The second downloads the Chromium browser binary (~150 MB). This is a one-time download.

**To use it,** add `--use-playwright` to your scraper command:
```
python3 ceu_scraper.py --use-playwright
```

The full run will take ~45 seconds instead of ~30 seconds with this flag (the browser takes about 10 seconds to render the FMS page). Other providers are unaffected.

If you skip this step, the FMS scraper still runs — it just logs a message saying the page is JavaScript-rendered and you'll see 0 FMS events. You can also manually add FMS events to `SEED_FMS_2026` inside `scrape_fms()` if you find dates in your browser.

✅ Done when `python3 -m playwright install chromium` completes without errors. (Or skip this step if you don't care about FMS.)

---

## Part B — Running it

### Step 5. Do a first run

From inside your `ceu-tracker` folder:

**Mac:**
```
python3 ceu_scraper.py
```

**Windows:**
```
python ceu_scraper.py
```

It takes about 30 seconds. You'll see one line per provider with how many courses it found and how many fell in the date window. Final line: `==== Run done ====`.

After the run, your folder will contain 4 new files:

| File | What it is |
|---|---|
| **`courses_current.md`** | The full current course list, grouped by month. **This is the file you read.** Open it in any markdown viewer (or just any text editor). |
| **`changes.md`** | What's new since the last run. Empty on the first run; populated on every later run. |
| `courses_state.json` | The scraper's memory of what it saw last time. Don't edit by hand. |
| `scraper.log` | A running log of every run. Useful if something goes wrong. |

✅ Done when `courses_current.md` exists and has ~50+ courses in it.

---

### Step 6. Read the output

Open `courses_current.md` in any text editor or markdown viewer (Mac Preview won't render it nicely; Mac users — try opening with TextEdit, or install a free markdown viewer like Typora or Macdown).

The file is organized:
- One section per month (May 2026, June 2026, …)
- Within each month, one block per course
- Each course shows provider, location, audience, PT CEU status, URL

You can search it (⌘+F or Ctrl+F) for anything: a city, a provider name, a course topic.

---

### Step 7. Re-run it weekly or monthly

Whenever you want fresh data, repeat Step 5. **Read `changes.md` first** — it's the diff since your last run:

- `## New courses` — added this run (you might want to register)
- `## Changed courses` — date or location moved (you might already be registered)
- `## Removed courses` — no longer offered or already past

That's the day-to-day workflow: run, skim `changes.md`, read `courses_current.md` if anything catches your eye.

---

## Part C — Make it run automatically (optional)

If you don't want to remember to run it manually, schedule it.

### On Mac (using cron, every Monday at 7am)

1. Open Terminal.
2. Type `crontab -e` and press Enter.
3. Press `i` to enter insert mode.
4. Add this line (replace `<you>` with your home folder name):
   ```
   0 7 * * 1 cd /Users/<you>/Documents/ceu-tracker && /usr/bin/python3 ceu_scraper.py
   ```
5. Press Esc, type `:wq`, press Enter to save and exit.
6. Verify with `crontab -l` — you should see your line.

The scraper will now run every Monday at 7am as long as your Mac is awake. If your Mac sleeps often, ask me for the `launchd` version instead — it handles sleep better.

### On Windows (using Task Scheduler)

1. Open Task Scheduler (Start menu, type "Task Scheduler").
2. Right pane → "Create Basic Task...".
3. Name: "CEU Scraper". Click Next.
4. Trigger: "Weekly", click Next. Pick a day and time. Click Next.
5. Action: "Start a program". Click Next.
6. Program/script: `python`
7. Add arguments: `ceu_scraper.py`
8. Start in: `C:\Users\<you>\Documents\ceu-tracker` (no quotes)
9. Click Next, then Finish.

The scraper will now run on the schedule you chose.

---

## Part D — Common situations

### "I want to run just one provider"

Useful when you're debugging or only care about one source:

```
python3 ceu_scraper.py --provider pri
```

Provider keys: `art`, `barbell`, `mulligan`, `cup`, `agile`, `cpta`, `ucsf`, `pri`, `ipa`, `gls`, `hw`, `wilk`, `vestibular`, `fms`, `amsi`, `aamt`, `eldoa`, `ne_seminars`, `frs`, `painfree`, `dns`.

List them with:
```
python3 ceu_scraper.py --list-providers
```

### "I want FMS events to actually appear"

The FMS events page uses JavaScript to load events, so the basic static scraper sees nothing. Add the `--use-playwright` flag (after installing Playwright per Step 4b above):
```
python3 ceu_scraper.py --use-playwright
```
Or to test just FMS:
```
python3 ceu_scraper.py --provider fms --use-playwright
```

### "I want to see the live log as it runs"

```
python3 ceu_scraper.py --log-to-stderr
```

### "I confirmed a new FMS or AMSI date — how do I add it?"

Open `ceu_scraper.py`, find the function for that provider (e.g. `scrape_fms`), find the `SEED_FMS_2026: list[tuple] = []` line, and add an entry in the format:

```python
SEED_FMS_2026 = [
    ("2026-09-12", "2026-09-13", "FMS Level 1", "Denver, CO"),
]
```

Save the file and re-run. The new course will show up in both `courses_current.md` (under September 2026) and `changes.md` (as a new add).

### "I want to track a new provider that's not on the list"

Slightly more involved but a known recipe. Open `ceu_scraper.py` and:

1. Find an existing scraper that's similar (e.g. `scrape_pri` if you want a seed-based one, or `scrape_active_release` if you want a live-HTML one). Copy the function.
2. Rename it `scrape_yournewprovider`, update the URL, the seed list, the provider name.
3. Add it to the `PROVIDERS` dictionary at the bottom of the file: `"newkey": scrape_yournewprovider,`.
4. If it's a PT-primary provider, add a lowercase substring of the name to the `PT_PRIMARY_PROVIDERS` set (search the file for that name).
5. Test it alone: `python3 ceu_scraper.py --provider newkey`.

### "I want to broaden or narrow the geographic radius"

Near the top of `ceu_scraper.py`, find `WESTERN_US_KEYWORDS`. It's a Python list of strings. Each course's location is checked for a substring match against this list. To add a new state (say Idaho), insert the relevant patterns:

```python
"idaho", ", id ", ", id,", " id, ",
"boise", "meridian", "nampa", "idaho falls",
```

To narrow the radius (e.g. drop Colorado), comment out or delete the Colorado lines.

---

## Part E — When something goes wrong

### A provider shows "found 0 courses" unexpectedly

Open `scraper.log` in a text editor and look at that provider's section. Two common causes:

- **`FETCH FAILED`** — the provider's site blocked the request (this happens with UCSF). Open the URL in your browser to confirm the data is still there, then add any new dates manually to that provider's `SEED_*` list.
- **`no HTML returned`** — same as above, often a 403.

### A course you know about isn't appearing

Two filters drop courses: the radius filter and the PT-attendable filter. To check which one:

```
python3 -c "from ceu_scraper import in_radius, is_pt_attendable, Course; c = Course(course_id='x', provider='Some Provider', title='Some Course', start_date='2026-09-01', location='San Diego, CA', audience='PT, ATC', pt_ceus='yes'); print('in_radius:', in_radius(c.location)); print('pt_attendable:', is_pt_attendable(c))"
```

Adjust the `location`, `provider`, `audience`, `pt_ceus` to match the course in question. If both return `True` but it's still missing, the issue is in the provider's scraper function — check the seed list.

### The state file got corrupted

Delete `courses_state.json` and re-run. The scraper will treat everything as new on the next run.

### "I moved the folder and the schedule broke"

Cron and Task Scheduler both store an absolute path. Update the cron line (Mac: `crontab -e`) or the Task Scheduler entry (Windows: Task Scheduler → find your task → Properties → Actions tab → edit) to point at the new folder location.

---

That's everything. Four commands to remember:

```
python3 ceu_scraper.py                      # full run
python3 ceu_scraper.py --use-playwright     # full run + FMS (slower, needs Playwright)
python3 ceu_scraper.py --provider pri       # just one
python3 ceu_scraper.py --list-providers     # show all keys
```

Read `changes.md` after each run; read `courses_current.md` when you want the full list.
