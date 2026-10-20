# SQA Job Alert (Bangladesh)

I built this tool for myself while looking for SQA opportunities. It checks Bdjobs, LinkedIn, company careers pages and remote job boards for SQA / QA / testing jobs and notifies me about postings it hasn't seen before.

## Why I built it
Searching for SQA jobs in Bangladesh by hand was slow and noisy:
- **Openings are scattered.** Some are only on Bdjobs or LinkedIn, some only on a company's own careers page, and some only appear as "We're hiring" posts on LinkedIn.
- **"QA" usually isn't software.** Most QA results are garment, pharma or food quality-control roles.
- **Most remote jobs aren't open to Bangladesh.** They're limited to the US, EU or another region.
- **I kept seeing the same job again.** Postings are reposted and listed on several sites.

## What it does
- **Searches 10+ sources twice a day.** Bdjobs, LinkedIn jobs, LinkedIn hiring posts, about 60 company careers pages (Greenhouse, Lever, Ashby, easy.jobs and plain HTML), and remote boards (Himalayas, Jobicy, RemoteOK, Working Nomads, We Work Remotely).
- **Filters out non-software QA.** Regex filters separate software-testing titles from quality-control roles. They're backed by a unit-test suite of about 115 real job titles and look-alikes.
- **Keeps only remote jobs I can apply to.** These are jobs open worldwide, to Asia/APAC or to Bangladesh.
- **Removes duplicates.** It matches company + title across sources and reposts.
- **Scores each job against my CV.** Playwright, TypeScript, API testing and automation rank higher; Java-only or platform-specific roles rank lower.
- **Tracks my applications.** A local web UI holds the boards: Inbox, Saved for later, Applied (with a follow-up nudge after a week), and Archive. I can add notes to each job.
- **Alerts me about new jobs.** Alerts come by desktop notification, Telegram or email.

Built with Python (standard library only, plus optional Playwright), SQLite, a single-page HTML/JS UI, and a Windows scheduled task.

## Setup (Windows)
1. Install Python 3.10+ and clone this repo.
2. Run `py app.py`. On first run `config.example.json` is copied to `config.json`, which holds your own settings and secrets and is not committed (see `.gitignore`).
3. Optional: `py -m pip install playwright` and Google Chrome for LinkedIn hiring posts.
4. Install the background check from **Schedule & log** in the UI, or with `.\install_task.ps1`.

Everything else uses the Python standard library.

## Web UI
Double-click `ui.cmd` (or run `py app.py`) to open http://127.0.0.1:8765 in your browser. From there you can:
- **Jobs**: two boards, **Bangladesh** and **Global remote**. Search and filter tracked jobs, mark them Saved / Applied / Dismissed, and keep notes on each. **Inbox** shows new jobs. Click **Save for later** on a job you want to apply to later: it leaves every other list and moves to **Saved for later**, one list from both boards, closest deadline first, each showing when you saved it. Click **Mark applied** on a job once you've applied (or **in Easy Apply** on a LinkedIn job you applied to with Easy Apply): it leaves every other list and moves to **Applied**, a single list of your applications from both boards, oldest first, marked "via LinkedIn Easy Apply" where that's how you applied, each showing when you applied (amber after a week, as a follow-up nudge). Click **Applied** again to undo.
  - **Bangladesh** filters by work arrangement: Remote, Hybrid or On-site. Bdjobs, Lever, easy.jobs and Ashby say so directly. LinkedIn is labelled with two small extra searches for remote and hybrid jobs. Careers pages and posts are read from the text, defaulting to On-site.
  - **Global remote** shows who can apply to each job ("Worldwide", "Only United States"…) and filters to **Open to Bangladesh** (worldwide, Asia, APAC or Bangladesh) or **All remote**.
  - **Archive:** tick jobs (or "select all shown") and click **Archive** to remove them from your lists. Archived jobs keep their status and notes, stay in the **Archive** tab, don't alert again, and can be restored.
  - **Delete forever:** from the Archive. Deleted jobs' IDs are remembered in `jobs.db`, so a posting that's still online won't come back as new.
- **Companies**: add, edit, pause or remove company careers pages, and **Test** one to see every posting it finds and which ones match your filters.
- **Settings**: edit keywords, sources, filters (with a live "would this title be kept?" tester) and notification channels, then send a test notification.
- **Schedule & log**: install, pause, resume or remove the background task, change how often it runs, and read the log.

"Check now" runs a check immediately. The UI only listens on localhost and stops when you close its window.

## Command line
| Command | What it does |
|---|---|
| `py app.py [--port 8765] [--no-browser]` | Start the web UI |
| `py sqa_jobs.py` | Check now and notify about new jobs |
| `py sqa_jobs.py --all` | Same, and check every company page even if it isn't due yet |
| `py sqa_jobs.py --list` | List all tracked jobs, newest first |
| `py sqa_jobs.py --test` | Send a test notification through every enabled channel |
| `.\install_task.ps1 [-IntervalMinutes 30] [-FirstRun "09:00"]` | (Re)register the scheduled task; currently `-IntervalMinutes 720 -FirstRun "09:00"` = 9 AM and 9 PM daily |
| `Unregister-ScheduledTask -TaskName "SQA Job Alert" -Confirm:$false` | Stop the scheduled checks |

**Duplicates:** a job is tracked once even when several sources list it or it's reposted: jobs are matched by company + title, ignoring tags like "(Remote)", "| Contract", "(m/f/d)", company suffixes (Ltd, Inc…) and punctuation (`dup_key` in sqa_jobs.py; tests in `test_filters.py`). A job you delete forever stays gone from every source.

Files: `jobs.db` (seen jobs), `jobs.html` (report of every tracked job; new ones highlighted), `sqa_jobs.log`. These are personal and are not committed.

## LinkedIn
LinkedIn's public job search is rate-limited (HTTP 429), so `linkedin.keywords` are sent as one combined search (`SQA OR "QA Automation" OR Playwright …`) ranked by relevance, over the last 30 days. Paging stops after `stop_after_empty_pages` (default 4) pages in a row without a QA match, or when results run out (about 7 pages for Bangladesh), which is usually 5–8 requests per run. On a 429 the script waits at least a minute and retries. The same role posted for several cities is kept once.

### LinkedIn hiring posts (uses your account)
Optional source for "We're hiring an SQA engineer…" posts, which aren't job listings and need a logged-in session. LinkedIn's User Agreement doesn't allow automated access to logged-in pages and it can restrict accounts that use it, so this runs slowly: the searches in `linkedin_posts.queries` (past week, newest first), at most every `check_every_hours` (default 6), scrolling at reading pace.
- **Log in once:** Settings → LinkedIn hiring posts → *Log in to LinkedIn* (or `py linkedin_posts.py --login`). A Chrome window opens on a separate profile stored in `linkedin_session/`, and you sign in yourself. Your password isn't saved by this code. Your everyday Chrome profile isn't used, because Chrome doesn't allow automating it.
- **What counts:** posts are kept only if they mention a place in Bangladesh, are hiring (not #OpenToWork), and name a QA role your filters accept. The link is the post's apply link, or the poster's profile.
- **Security checks:** if LinkedIn shows a security check or logs you out, the search stops, you get one desktop notification, and Settings asks you to log in again.
- `linkedin_session/` holds your LinkedIn login cookies; don't share or commit it. Requires `py -m pip install playwright` and Google Chrome.

## Global remote jobs
`config.json → global` searches remote SQA jobs, at most every `check_every_hours` (default 6):
- **Himalayas:** `himalayas.queries`, restricted to jobs open to `himalayas.country` (Bangladesh), up to `pages` × 20 per search.
- **Jobicy:** `jobicy.tags`, plus the latest jobs in each region of `jobicy.geos` (`apac`).
- **RemoteOK**, **Working Nomads**, **We Work Remotely:** their latest postings.
- **LinkedIn** (off by default): a worldwide remote search. Its cards show where the employer is, not who may apply, so almost nothing passes the Bangladesh filter, and it's often rate-limited.

Most remote jobs are limited to one country or region. With `only_open_to_bd: true` (the default) only jobs open worldwide, to Asia/APAC or to Bangladesh are kept. Set it to `false` to keep every remote job on the board; `notify_restricted` then decides whether the restricted ones alert you. The first global search fills the board with a single summary alert.

## Company careers pages
`config.json → companies.list` holds careers pages that are checked directly, each at most once every `check_every_hours` (default 6), however often the scheduled task runs. Each entry has a `type`:
- `easyjobs`, `lever`, `greenhouse`, `ashby`: read from the hiring system's public job feed (reliable).
- `bjit`: BJIT's own job API.
- `html`: any other careers page. It's scanned for links to individual postings (`/job/…`, `/careers/…`, `/openings/…`). Pages that only load jobs with JavaScript show nothing; the Test button in the UI tells you.

Add `"location": "Dhaka|Bangladesh"` to keep only matching roles of a global company. Add `"board": "global"` (Edit → Board in the UI) for a remote-first company: its roles go to the **Global remote** board, labelled with who can apply from the posting's location, and only those open worldwide, to Asia/APAC or to Bangladesh are kept. The list includes remote-first companies set up this way (Canonical, GitLab, Wikimedia, OKX, Bybit, Binance, Supabase, PostHog, Zapier, Toptal and others); add more from their Greenhouse / Lever / Ashby boards. Robots.txt is respected for `html` pages. Postings from company pages count as software companies, so a vague title like "QA Executive" is kept there.

## Phone alerts via Telegram
1. In Telegram, message **@BotFather** → `/newbot` → copy the bot token.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. In `config.json` set `notify.telegram` → `enabled: true`, `bot_token`, `chat_id`. Run `py sqa_jobs.py --test`.

## Email alerts (Gmail)
Create an App Password (Google Account → Security → 2-Step Verification → App passwords), then fill in `notify.email` in `config.json`.

## Tuning the filter
In Bangladesh, "QA" often means garment, pharma or food quality control, so `config.json → filters` works like this:
- `strong_title`: unambiguous software-testing titles are always kept. This covers SQA, QA, QE, SDET and UAT roles (engineer, analyst, lead, tester, automation…), "Software Engineer in Test", "<area> Tester" (mobile, API, performance, functional, game, ETL…), "Test <role>", and tool names (Selenium, Playwright, Cypress, Appium, Katalon, JMeter, WebdriverIO, Robot Framework, ISTQB…).
- `weak_title`: vague titles ("QA Executive", "Quality Engineer", "QC Engineer", "Tester") are kept only on a company careers page, in Bdjobs' IT category, or for tech-looking company names.
- `exclude`: industry words (garments, pharma, lab, PLC/SCADA, call center…) that drop a job.
- `skip_level`: seniority you don't want (currently junior / entry-level / associate / fresher and manager / head / director), matched in the title.

## Fit with your CV
`config.json → fit.terms` scores each job against your profile: every term found in the title, experience or description adds its weight (Playwright +4, TypeScript +3, API testing +2, automation +2…; Java −2, platform-specific tools −3, "5+ years" −2…). The board sorts best fit first (switch to "Newest first" in the toolbar) and badges **Strong fit** (6+), **Good fit** (3–5) and **Stack differs** (−2 or less); hover a badge for the terms behind it. Global boards include descriptions, so they're scored well; Bdjobs and LinkedIn only give a title, so most of those stay neutral. Scores refresh on each check.

After editing the filters, run `py test_filters.py`. It checks about 115 real-world title variants and look-alikes; add a title to it whenever you find one the filter gets wrong.
