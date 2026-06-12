# Setup Guide — CA PT CEU Course Listings on GitHub Pages

Your shareable URL will look like:  
**`https://YOUR-USERNAME.github.io/ceu-courses/`**

---

## What you need before starting
- A free GitHub account → sign up at https://github.com  
- The project folder on your Mac with these files:
  - `index.html`
  - `ceu_scraper.py`
  - `rebuild_courses.py`
  - `requirements.txt`
  - `courses_state.json`
  - `.github/workflows/scrape.yml`

---

## Step 1 — Create a GitHub repository

1. Go to https://github.com and sign in
2. Click the **+** in the top-right corner → **New repository**
3. Name it `ceu-courses` (or anything you like)
4. Set visibility to **Public** ← required for free GitHub Pages
5. Leave everything else as default
6. Click **Create repository**

✅ You should see an empty repo page with a URL like `github.com/YOUR-USERNAME/ceu-courses`

---

## Step 2 — Upload your files

1. On the empty repo page, click **uploading an existing file** (link in the middle of the page)
2. Drag ALL of these files into the upload box:
   - `index.html`
   - `ceu_scraper.py`
   - `rebuild_courses.py`
   - `requirements.txt`
   - `courses_state.json`
   - `courses_current.md`
   - `changes.md`
3. Scroll down, leave the commit message as-is, click **Commit changes**

✅ You should now see all your files listed in the repo

---

## Step 3 — Upload the GitHub Actions workflow

The workflow file lives inside a hidden folder (`.github/workflows/`) which the drag-and-drop uploader can't create directly. Do it this way:

1. On your repo page, click **Add file** → **Create new file**
2. In the filename box at the top, type exactly:
   ```
   .github/workflows/scrape.yml
   ```
   (GitHub will auto-create the folders as you type the slashes)
3. Open `scrape.yml` on your Mac in TextEdit (right-click → Open With → TextEdit)
4. Select all (⌘A), copy (⌘C)
5. Paste into the GitHub editor
6. Click **Commit changes** → **Commit changes** again

✅ You should see `.github/workflows/scrape.yml` in the file list

---

## Step 4 — Enable GitHub Pages

1. In your repo, click **Settings** (top tab, the gear icon)
2. Scroll down the left sidebar to **Pages**
3. Under **Source**, select:
   - Branch: **main**
   - Folder: **/ (root)**
4. Click **Save**
5. Wait ~60 seconds, then refresh the page
6. A green banner will appear:  
   **"Your site is live at https://YOUR-USERNAME.github.io/ceu-courses/"**

✅ Click that link — your site should be live!

---

## Step 5 — Test the monthly auto-scrape

You don't want to wait until the 1st of the month to verify it works. Trigger it manually now:

1. In your repo, click the **Actions** tab
2. In the left sidebar, click **Monthly CEU Scrape**
3. Click **Run workflow** → **Run workflow** (green button)
4. Watch the yellow dot → it will turn green (success) or red (error) in ~2 minutes

If it turns **green**: the scraper ran, `index.html` was updated, and the site will reflect fresh data within 1–2 minutes.

If it turns **red**: click into the failed run to see the error log, take a screenshot, and share it for help.

---

## Sharing the link

Your permanent URL is:  
`https://YOUR-USERNAME.github.io/ceu-courses/`

- Share this with colleagues directly
- Bookmark it — it never changes
- The "Last scraped" date in the header tells visitors when data was last refreshed

---

## After each monthly scrape

Nothing to do. GitHub Actions handles everything:
1. Runs `ceu_scraper.py` on the 1st of every month at 7 AM UTC
2. Runs `rebuild_courses.py` to patch `index.html`
3. Commits and pushes the updated file
4. GitHub Pages serves the new version within ~1 minute

---

## Troubleshooting quick reference

| Symptom | What to check |
|---------|--------------|
| Site shows old data | Check Actions tab → did the latest run succeed? |
| Actions run fails | Click the red X → read the error log → screenshot and share |
| Page shows blank white | Open browser console (⌘⌥I) → look for red errors |
| Site URL gives 404 | Wait 2 min after enabling Pages; confirm branch is `main` and folder is `/ (root)` |
