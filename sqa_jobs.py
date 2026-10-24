"""SQA job alert for Bangladesh.

Polls Bdjobs, LinkedIn, Jobgether, company careers pages and remote job boards for SQA / QA / testing jobs,
stores every posting it has seen in SQLite, and notifies (desktop toast /
Telegram / email) only about postings it has not seen before.

Usage:
    py sqa_jobs.py            # check once, notify about new jobs
    py sqa_jobs.py --all      # same, but check company pages, LinkedIn posts and Jobgether even if not due yet
    py sqa_jobs.py --list     # print the jobs currently tracked (newest first)
    py sqa_jobs.py --test     # send a test notification through every enabled channel
"""

import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import shutil
import smtplib
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import email.utils
from email.mime.text import MIMEText
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "jobs.db"
CONFIG_PATH = ROOT / "config.json"  # your settings and secrets; not committed
EXAMPLE_CONFIG_PATH = ROOT / "config.example.json"
REPORT_PATH = ROOT / "jobs.html"
LOG_PATH = ROOT / "sqa_jobs.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

log = logging.getLogger("sqa_jobs")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def http_get(url, params=None, headers=None, retries=3):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # network hiccups, 429s, etc.
            if attempt == retries:
                raise
            wait = 10 * attempt
            if getattr(exc, "code", None) == 429:  # rate-limited: back off properly
                retry_after = (exc.headers or {}).get("Retry-After", "")
                wait = max(int(retry_after) if retry_after.isdigit() else 0, 60 * attempt)
            log.warning("GET %s failed (%s), retrying in %ds", url, exc, wait)
            time.sleep(wait)


# --------------------------------------------------------------------------- #
# Sources. Each yields dicts: id, source, title, company, location, url,
# posted, deadline, salary, experience, plus optionally region ("bd" or
# "global"), work_mode ("remote", "hybrid", "onsite") and, for global remote
# jobs, eligibility (who may apply) and open_to_bd.
# --------------------------------------------------------------------------- #

_REMOTE = re.compile(r"\b(remote|work from home|wfh|home[- ]based|fully distributed)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)


def guess_mode(*texts, default="onsite"):
    """Work arrangement from free text; most Bangladesh jobs that don't say otherwise are on-site."""
    text = " ".join(t or "" for t in texts)
    if _HYBRID.search(text):
        return "hybrid"
    if _REMOTE.search(text):
        return "remote"
    return default


def eligibility(restriction):
    """(label, open_to_bd) for a remote job's location restriction, e.g. "Worldwide" or "United States"."""
    text = re.sub(r"\s+", " ", restriction or "").strip(" ,;")
    if not text or re.fullmatch(r"(?i)(100% )?(remote|worldwide|anywhere( in the world)?|global|remote,? worldwide)", text):
        return "Worldwide", 1
    return text[:80], int(bool(re.search(r"(?i)\b(worldwide|anywhere|global|bangladesh|asia|apac)\b", text)))


BDJOBS_API = "https://api.bdjobs.com/Jobs/api/JobSearch/GetJobSearch"


def fetch_bdjobs(cfg):
    seen = set()
    for keyword in cfg["search_keywords"]:
        page = 1
        while True:
            try:
                data = json.loads(http_get(BDJOBS_API, {
                    "keyword": keyword, "pg": page, "rpp": 100,
                    "isPro": 0, "ToggleJobs": "true",
                }, headers={"Origin": "https://bdjobs.com", "Referer": "https://bdjobs.com/h/jobs"}))
            except Exception as exc:  # keep the other keywords' results
                log.warning("Bdjobs keyword %r page %d skipped: %s", keyword, page, exc)
                break
            rows = (data.get("data") or []) + (data.get("premiumData") or [])
            for r in rows:
                jid = str(r.get("Jobid"))
                if jid in seen:
                    continue
                seen.add(jid)
                place = (r.get("WorkPlace") or "").lower()  # "Office", "Home" or both
                mode = ("hybrid" if "home" in place and "office" in place else "remote" if "home" in place
                        else guess_mode(r.get("jobTitle")))
                yield {
                    "work_mode": mode,
                    "id": f"bdjobs:{jid}",
                    "source": "Bdjobs",
                    "title": (r.get("jobTitle") or "").strip(),
                    "company": (r.get("companyName") or "").strip(),
                    "location": r.get("location") or "",
                    "url": f"https://bdjobs.com/h/details/{jid}",
                    "posted": r.get("publishDate") or "",
                    "deadline": r.get("deadline") or "",
                    "salary": r.get("Salary") or "",
                    "experience": r.get("experience") or "",
                    "category": r.get("Cat_id"),
                }
            total_pages = int((data.get("common") or {}).get("totalpages") or 1)
            if page >= total_pages or not rows:
                break
            page += 1


LINKEDIN_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
_LI_CARD = re.compile(r"<li>(.*?)</li>", re.S)


def _li_field(pattern, text):
    m = re.search(pattern, text, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""


def linkedin_query(keywords):
    """All keywords as one boolean search, which LinkedIn ranks by relevance: one paged search
    instead of one per keyword, so far fewer requests and rate limits."""
    return " OR ".join(f'"{k}"' if " " in k else k for k in keywords)


LI_WORK_TYPE = {"remote": "2", "hybrid": "3"}  # LinkedIn's f_WT filter values
LI_WORLDWIDE = "92000000"  # LinkedIn geoId for "Worldwide"


def fetch_linkedin(cfg):
    """Bangladesh jobs. Two small extra searches (remote, hybrid) label the work arrangement;
    everything else counts as on-site."""
    li = cfg["linkedin"]
    where = {"location": li["location"]}
    modes, seen = {}, set()
    if li.get("label_work_mode", True):
        for mode, wt in LI_WORK_TYPE.items():
            for job in _linkedin_search(cfg, {**where, "f_WT": wt}, max_pages=3):
                modes.setdefault(job["id"], (mode, job))
            time.sleep(li.get("delay_seconds", 5))
    for job in _linkedin_search(cfg, where):
        seen.add(job["id"])
        job["work_mode"] = modes[job["id"]][0] if job["id"] in modes else guess_mode(job["title"], job["location"])
        yield job
    for jid, (mode, job) in modes.items():  # labelled results the main search didn't reach
        if jid not in seen:
            yield {**job, "work_mode": mode}


def fetch_linkedin_global(cfg):
    """Remote jobs anywhere in the world; the card's location is where the employer hires from."""
    for job in _linkedin_search(cfg, {"geoId": LI_WORLDWIDE, "f_WT": LI_WORK_TYPE["remote"]},
                                max_pages=cfg.get("global", {}).get("linkedin_pages", 6)):
        label, open_to_bd = eligibility("" if re.search(r"(?i)worldwide", job["location"]) else job["location"])
        yield {**job, "region": "global", "work_mode": "remote", "eligibility": label, "open_to_bd": open_to_bd}


def _linkedin_search(cfg, where, max_pages=None):
    li = cfg["linkedin"]
    query = linkedin_query(li.get("keywords") or cfg["search_keywords"])
    seen, dupes, misses = set(), set(), 0
    for page in range(max_pages or li.get("max_pages", 10)):
        try:
            body = http_get(LINKEDIN_API, {
                "keywords": query, **where,
                "f_TPR": f"r{li['posted_within_seconds']}", "start": page * 10,
            })
        except Exception as exc:  # still rate-limited after backing off: keep what we have
            log.warning("LinkedIn stopped at page %d: %s", page, exc)
            break
        cards = _LI_CARD.findall(body)
        if not cards:
            break
        relevant = 0
        for card in cards:
            m = re.search(r"urn:li:jobPosting:(\d+)", card)
            if not m or m.group(1) in seen:
                continue
            jid = m.group(1)
            seen.add(jid)
            posted = re.search(r'datetime="([^"]+)"', card)
            job = {
                "id": f"linkedin:{jid}",
                "source": "LinkedIn",
                "title": _li_field(r'base-search-card__title">(.*?)</h3>', card),
                "company": _li_field(r'base-search-card__subtitle">(.*?)</h4>', card),
                "location": _li_field(r'job-search-card__location">(.*?)</span>', card),
                "url": f"https://www.linkedin.com/jobs/view/{jid}",
                "posted": posted.group(1) if posted else "",
                "deadline": "", "salary": "", "experience": "",
            }
            key = (job["title"].lower(), job["company"].lower())
            if key in dupes:  # same role posted once per city
                continue
            dupes.add(key)
            relevant += is_sqa(cfg, job)
            yield job
        # Results are ranked by relevance, so a run of pages without a QA job usually means we're past them
        # (though QA jobs can still follow 3 empty pages, so the default allows 4).
        misses = 0 if relevant else misses + 1
        if misses >= li.get("stop_after_empty_pages", 4):
            break
        time.sleep(li.get("delay_seconds", 5))  # LinkedIn rate-limits the guest API


SOURCES = {"bdjobs": fetch_bdjobs, "linkedin": fetch_linkedin}


# --------------------------------------------------------------------------- #
# Global remote jobs (the "Global" board). Each source records who may apply,
# because most "remote" jobs are remote within one country or region.
# --------------------------------------------------------------------------- #

def _global_job(source, key, title, company, url, restriction, posted="", deadline="", salary="", experience="",
                text=""):
    # text: the job description, used only to score fit (not stored)
    label, open_to_bd = eligibility(restriction)
    return {"id": f"{source.lower()}:{key}", "source": source, "title": _clean(title), "company": _clean(company),
            "location": label, "url": url, "posted": posted or "", "deadline": deadline or "",
            "salary": salary or "", "experience": experience or "", "tech": True, "text": _clean(text)[:8000],
            "region": "global", "work_mode": "remote", "eligibility": label, "open_to_bd": open_to_bd}


def _epoch_iso(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat(timespec="seconds") if seconds else ""


def _salary(lo, hi, currency="", period=""):
    if not lo and not hi:
        return ""
    rng = f"{int(lo):,}–{int(hi):,}" if lo and hi and lo != hi else f"{int(lo or hi):,}"
    return f"{currency or ''} {rng}{f' / {period}' if period else ''}".strip()


def fetch_himalayas(cfg):
    h = cfg["global"].get("himalayas") or {}
    # country= makes Himalayas return only jobs a resident of that country may apply to.
    where = {"country": h["country"]} if h.get("country") else {}
    for q in h.get("queries") or ["qa engineer", "sdet", "test automation", "software tester"]:
        for page in range(1, h.get("pages", 2) + 1):
            try:
                data = json.loads(http_get("https://himalayas.app/jobs/api/search", {"q": q, "page": page, **where}, retries=2))
            except Exception as exc:
                log.warning("Himalayas %r page %d skipped: %s", q, page, exc)
                break
            jobs = data.get("jobs") or []
            for j in jobs:
                yield _global_job(
                    "Himalayas", hashlib.sha1(j["guid"].encode()).hexdigest()[:16], j["title"], j.get("companyName"),
                    j.get("applicationLink") or j["guid"], ", ".join(j.get("locationRestrictions") or []),
                    _epoch_iso(j.get("pubDate")), _epoch_iso(j.get("expiryDate"))[:10],
                    _salary(j.get("minSalary"), j.get("maxSalary"), j.get("currency"), j.get("salaryPeriod")),
                    ", ".join(j.get("seniority") or []), j.get("description") or j.get("excerpt"))
            if not jobs or page * data.get("limit", 20) >= data.get("totalCount", 0):
                break
            time.sleep(1)


def fetch_jobicy(cfg):
    jc = cfg["global"].get("jobicy") or {}
    # Each tag is searched everywhere, plus the latest jobs in each region of `geos` (e.g. "apac").
    searches = [{"tag": t} for t in jc.get("tags") or ["qa engineer", "test automation"]]
    searches += [{"geo": g} for g in jc.get("geos") or []]
    for params in searches:
        try:
            data = json.loads(http_get("https://jobicy.com/api/v2/remote-jobs", {"count": 100, **params}, retries=2))
        except Exception as exc:
            log.warning("Jobicy %r skipped: %s", params, exc)
            continue
        for j in data.get("jobs") or []:
            yield _global_job("Jobicy", j["id"], html.unescape(j["jobTitle"]), j.get("companyName"), j["url"],
                              j.get("jobGeo"), (j.get("pubDate") or "").replace(" ", "T"),
                              salary=_salary(j.get("annualSalaryMin"), j.get("annualSalaryMax"), j.get("salaryCurrency"), "year"),
                              experience=j.get("jobLevel"), text=j.get("jobDescription") or j.get("jobExcerpt"))
        time.sleep(1)


def fetch_workingnomads(cfg):
    for j in json.loads(http_get("https://www.workingnomads.com/api/exposed_jobs/", retries=2)):
        yield _global_job("WorkingNomads", hashlib.sha1(j["url"].encode()).hexdigest()[:16], j["title"],
                          j.get("company_name"), j["url"], j.get("location"), j.get("pub_date"),
                          text=f"{j.get('tags') or ''} {j.get('description') or ''}")


def fetch_weworkremotely(cfg):
    body = http_get("https://weworkremotely.com/remote-jobs.rss", retries=2)
    for item in re.findall(r"<item>(.*?)</item>", body, re.S):
        field = lambda tag: html.unescape(_li_field(rf"<{tag}>(.*?)</{tag}>", item))
        company, _, title = field("title").partition(": ")
        url = field("link") or field("guid")
        if not title or not url:
            continue
        posted = field("pubDate")
        try:
            posted = email.utils.parsedate_to_datetime(posted).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            pass
        yield _global_job("WeWorkRemotely", hashlib.sha1(url.encode()).hexdigest()[:16], title, company, url,
                          field("region") or field("country"), posted, text=field("description"))


def fetch_remoteok(cfg):
    # One request for the latest postings; RemoteOK asks API users to link back to it, which the job URL does.
    rows = json.loads(http_get("https://remoteok.com/api", retries=2))
    for j in rows:
        if isinstance(j, dict) and j.get("position"):
            yield _global_job("RemoteOK", j["id"], j["position"], j.get("company"), j.get("url") or f"https://remoteok.com/l/{j['id']}",
                              j.get("location"), j.get("date"), salary=_salary(j.get("salary_min"), j.get("salary_max"), "USD", "year"),
                              text=f"{' '.join(j.get('tags') or [])} {j.get('description') or ''}")


# LinkedIn last: its rate-limit back-offs can take minutes, so the fast feeds finish first.
GLOBAL_SOURCES = {"himalayas": fetch_himalayas, "jobicy": fetch_jobicy, "remoteok": fetch_remoteok,
                  "workingnomads": fetch_workingnomads,
                  "weworkremotely": fetch_weworkremotely, "linkedin": fetch_linkedin_global}


def fetch_global(cfg, con, force=False):
    """Global remote jobs, at most every global.check_every_hours. Returns (jobs, first_time)."""
    g = cfg.get("global") or {}
    prev = meta_get(con, "global", {})
    due_before = dt.datetime.now() - dt.timedelta(hours=g.get("check_every_hours", 6))
    if not force and prev.get("checked") and dt.datetime.fromisoformat(prev["checked"]) > due_before:
        return [], False
    state = {"checked": dt.datetime.now().isoformat(timespec="seconds"), "sources": {}}
    found = []
    only_bd = g.get("only_open_to_bd", True)
    for name, fetch in GLOBAL_SOURCES.items():
        if not (g.get(name) or {}).get("enabled", True):
            continue
        try:
            jobs = [j for j in fetch(cfg) if is_sqa(cfg, j)]
            open_jobs = [j for j in jobs if j["open_to_bd"]]
            found.extend(open_jobs if only_bd else jobs)
            state["sources"][name] = {"found": len(jobs), "open_to_bd": len(open_jobs)}
            log.info("global %s: %d matching jobs (%d open to Bangladesh)", name, len(jobs), len(open_jobs))
        except Exception as exc:
            state["sources"][name] = {"error": str(exc)[:200]}
            log.exception("Global source %s failed", name)
    meta_set(con, "global", state)
    return found, not prev


# --------------------------------------------------------------------------- #
# Company careers pages. Each entry in config companies.list has a name, url
# and type: a hosted hiring system with a public JSON feed (easyjobs, lever,
# greenhouse, ashby, bjit) or "html" for a plain careers page.
# --------------------------------------------------------------------------- #

def _company_job(c, key, title, url, location="", posted="", deadline="", mode=None, text=""):
    mode = {"on-site": "onsite", "on_site": "onsite", "office": "onsite"}.get((mode or "").lower(), (mode or "").lower())
    return {"id": f"company:{key}", "source": "Company site", "title": _clean(title),
            "company": c["name"], "location": location or "", "url": url, "posted": posted or "",
            "deadline": deadline or "", "salary": "", "experience": "", "tech": True, "text": _clean(text)[:8000],
            "work_mode": mode if mode in ("remote", "hybrid", "onsite") else guess_mode(title, location)}


def global_company_jobs(cfg, c, jobs):
    """For a company on the Global board (board: "global"): label who may apply from each posting's
    location, and keep only roles open to Bangladesh unless global.only_open_to_bd is off."""
    if c.get("board") != "global":
        return jobs
    only_bd = (cfg.get("global") or {}).get("only_open_to_bd", True)
    out = []
    for j in jobs:
        label, open_to_bd = eligibility(j["location"])
        if open_to_bd or not only_bd:
            out.append({**j, "region": "global", "work_mode": "remote", "eligibility": label, "open_to_bd": open_to_bd})
    return out


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _slug(url, host_suffix=None):
    """First path segment (jobs.lever.co/<slug>) or subdomain (<slug>.easy.jobs)."""
    parts = urllib.parse.urlsplit(url)
    if host_suffix:
        return parts.hostname.removesuffix(host_suffix)
    return parts.path.strip("/").split("/")[0]


def _ms_to_iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).isoformat(timespec="seconds") if ms else ""


def fetch_easyjobs(c):
    host = urllib.parse.urlsplit(c["url"]).hostname
    data = json.loads(http_get(f"https://{host}/api/career/home", retries=2, headers={
        "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}))["data"]
    for j in data["jobs"]:
        addr = j.get("address") or {}
        loc = ", ".join(x["name"] for x in (addr.get("state"), addr.get("country")) if x)
        yield _company_job(c, f"easyjobs:{j['id']}", j["title"], f"https://{host}/{j['slug']}",
                           loc, deadline=j.get("expire_at"),
                           mode="remote" if (j.get("meta") or {}).get("is_remote") else None)


def fetch_lever(c):
    for j in json.loads(http_get(f"https://api.lever.co/v0/postings/{_slug(c['url'])}", {"mode": "json"}, retries=2)):
        yield _company_job(c, f"lever:{j['id']}", j["text"], j["hostedUrl"],
                           (j.get("categories") or {}).get("location"), _ms_to_iso(j.get("createdAt")),
                           mode=j.get("workplaceType"),
                           text=f"{j.get('descriptionPlain') or ''} {j.get('additionalPlain') or ''}")


def fetch_greenhouse(c):
    board = json.loads(http_get(f"https://boards-api.greenhouse.io/v1/boards/{_slug(c['url'])}/jobs",
                                {"content": "true"}, retries=2))
    for j in board["jobs"]:
        yield _company_job(c, f"greenhouse:{j['id']}", j["title"], j["absolute_url"],
                           (j.get("location") or {}).get("name"), j.get("updated_at"),
                           text=html.unescape(j.get("content") or ""))  # content is escaped HTML


def fetch_ashby(c):
    board = json.loads(http_get(f"https://api.ashbyhq.com/posting-api/job-board/{_slug(c['url'])}", retries=2))
    for j in board["jobs"]:
        if j.get("isListed", True):
            yield _company_job(c, f"ashby:{j['id']}", j["title"], j["jobUrl"], j.get("location"), j.get("publishedAt"),
                               mode=j.get("workplaceType") or ("remote" if j.get("isRemote") else None),
                               text=j.get("descriptionPlain") or "")


def fetch_bjit(c):
    # bjitgroup.com/career loads its list from this API; its "country" filter returns nothing, so read all.
    data = json.loads(http_get("https://api.bjitgroup.com/job/get-all-by-country",
                               {"itemPerPage": 200, "page": 1}, retries=2))
    for j in data["results"]["jobs"]:
        if str(j.get("active", "True")) != "True" or str(j.get("isDeleted", "False")) == "True":
            continue
        yield _company_job(c, f"bjit:{j['_id']}", j["position"], j.get("detailsLink") or c["url"],
                           j.get("country"), j.get("createdAt"))


# Plain careers pages: collect links that look like individual job postings.
_A_TAG = re.compile(r"(?is)<a\b([^>]*)>(.*?)</a>")
_HEADING = re.compile(r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>")
_JOB_PATH = re.compile(r"/(jobs?|careers?|positions?|openings?|vacanc\w*|circulars?)/"
                       r"(?!(page|category|tag|feed|type|location|department)s?/)[^?#]*[a-z0-9]", re.I)
_GENERIC_TEXT = re.compile(r"^(apply|apply now|apply here|view|view details|view job|details|job details|"
                           r"read more|learn more|see more|more|share|open)\W*$", re.I)
_robots = {}


def _robots_ok(url):
    import urllib.robotparser
    root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    if root not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        try:
            rp.parse(http_get(f"{root}/robots.txt", retries=1).splitlines())
        except Exception:
            rp = None  # no robots.txt (or unreachable): allowed
        _robots[root] = rp
    return _robots[root] is None or _robots[root].can_fetch(UA, url)


def _title_from_url(url):
    seg = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").split("/")[-1])
    seg = re.sub(r"[-_](?=[0-9a-f-]*\d)[0-9a-f-]{5,}$|[-_]\d+$", "", seg)  # trailing ids
    words = re.sub(r"[-_]+", " ", seg).split()
    acronyms = {"sqa", "qa", "sdet", "ai", "ml", "api", "ui", "ux", "hr", "it", "dba", "ios", "erp", "devops"}
    return " ".join(w.upper() if w.lower() in acronyms else w.capitalize() for w in words)


def _slug_match(url, title):
    """How many words a title shares with the posting's URL."""
    slug = set(re.findall(r"[a-z]{3,}", _title_from_url(url).lower()))
    return len(slug & set(re.findall(r"[a-z]{3,}", title.lower())))


def fetch_html(c):
    page = c["url"]
    if not _robots_ok(page):
        raise RuntimeError("robots.txt asks crawlers not to read this page")
    body = re.sub(r"(?is)<(script|style|svg|noscript)\b.*?</\1\s*>", " ", http_get(page, retries=2))
    headings = [(m.start(), _clean(m.group(1))) for m in _HEADING.finditer(body)]

    best = {}  # url -> (quality, title)
    for m in _A_TAG.finditer(body):
        href = re.search(r"""href\s*=\s*["']([^"']+)""", m.group(1))
        if not href:
            continue
        url = urllib.parse.urljoin(page, html.unescape(href.group(1))).split("#")[0]
        path = urllib.parse.urlsplit(url).path
        if (url.rstrip("/") == page.rstrip("/") or not _JOB_PATH.search(path)
                or re.search(r"\.(png|jpe?g|gif|webp|svg|css|js|pdf|xml)$", path, re.I)
                or re.search(r"/(log-?in|sign-?in|sign-?up|register|account)/?$", path, re.I)):
            continue
        inner = _HEADING.search(m.group(2))
        first = next((t for t in map(_clean, re.split(r"<[^>]+>", m.group(2))) if re.search(r"\w", t)), "")
        text = _clean(inner.group(1)) if inner else first
        if text and not _GENERIC_TEXT.match(text) and len(text) <= 150:
            cand = (3, text)
        else:  # "Apply now" button: use the nearest heading above it
            above = [t for pos, t in headings if pos < m.start() and m.start() - pos < 4000 and t]
            cand = (2, above[-1]) if above else (1, _title_from_url(url))
            # The heading above may belong to the previous card: trust the URL when they share no words.
            slug_words = {w for w in re.findall(r"[a-z]{4,}", _title_from_url(url).lower())}
            if cand[0] == 2 and len(slug_words) >= 2 and not slug_words & set(re.findall(r"[a-z]{4,}", cand[1].lower())):
                cand = (1, _title_from_url(url))
        prev = best.get(url, (0, ""))
        if cand[0] > prev[0] or (cand[0] == prev[0] and _slug_match(url, cand[1]) > _slug_match(url, prev[1])):
            best[url] = cand  # several links to one posting: keep the best title (sites sometimes mislink cards)

    # A heading shared by several links is a section title ("Current openings"), not a job title.
    shared = [t for q, t in best.values() if q == 2]
    for url, (q, t) in best.items():
        if q == 2 and shared.count(t) > 1:
            best[url] = (1, _title_from_url(url))
    # Drop "/apply" links when the same page also links the posting itself.
    if any("/apply" not in u for u in best):
        best = {u: v for u, v in best.items() if "/apply" not in u}
    for url, (_, title) in best.items():
        yield _company_job(c, url, title, url)


COMPANY_TYPES = {"html": fetch_html, "easyjobs": fetch_easyjobs, "lever": fetch_lever,
                 "greenhouse": fetch_greenhouse, "ashby": fetch_ashby, "bjit": fetch_bjit}


def detect_company_type(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host.endswith(".easy.jobs"):
        return "easyjobs"
    if host == "jobs.lever.co":
        return "lever"
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        return "greenhouse"
    if host == "jobs.ashbyhq.com":
        return "ashby"
    if host.endswith("bjitgroup.com"):
        return "bjit"
    return "html"


def fetch_company(c):
    """All postings on one company's page, before the SQA filter."""
    jobs = list(COMPANY_TYPES[c.get("type") or detect_company_type(c["url"])](c))
    if c.get("location"):  # e.g. only the Dhaka roles of a global company
        jobs = [j for j in jobs if not j["location"] or re.search(c["location"], j["location"], re.I)]
    return jobs


def fetch_companies(cfg, con, force=False):
    """Check each enabled company whose last check is older than companies.check_every_hours."""
    co = cfg.get("companies") or {}
    due_before = dt.datetime.now() - dt.timedelta(hours=co.get("check_every_hours", 6))
    last = {r["name"]: r["checked"] for r in con.execute("SELECT name, checked FROM company_status")}
    found = []
    for c in co.get("list", []):
        if not c.get("enabled", True):
            continue
        if not force and last.get(c["name"]) and dt.datetime.fromisoformat(last[c["name"]]) > due_before:
            continue
        jobs, error = [], ""
        try:
            jobs = fetch_company(c)
        except Exception as exc:
            error = str(exc)[:300]
            log.warning("Company %s failed: %s", c["name"], error)
        matched = global_company_jobs(cfg, c, [j for j in jobs if is_sqa(cfg, j)])
        found.extend(matched)
        con.execute("INSERT OR REPLACE INTO company_status VALUES (?,?,?,?,?)",
                    (c["name"], dt.datetime.now().isoformat(timespec="seconds"), len(jobs), len(matched), error))
        con.commit()
        log.info("company %s: %d postings, %d matching", c["name"], len(jobs), len(matched))
        time.sleep(co.get("delay_seconds", 2))
    return found


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT,
            location TEXT, url TEXT, posted TEXT, deadline TEXT, salary TEXT,
            experience TEXT, first_seen TEXT, last_seen TEXT
        )""")
    # Columns managed from the web UI (app.py); added to older databases on demand.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
    if "status" not in cols:
        con.execute("ALTER TABLE jobs ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")
    if "notes" not in cols:
        con.execute("ALTER TABLE jobs ADD COLUMN notes TEXT NOT NULL DEFAULT ''")
    if "archived_at" not in cols:  # set = moved to the Archive; status and notes are kept
        con.execute("ALTER TABLE jobs ADD COLUMN archived_at TEXT NOT NULL DEFAULT ''")
    # Board ("bd" = Bangladesh, "global" = remote anywhere), work arrangement, and who may apply.
    for col, decl in (("region", "TEXT NOT NULL DEFAULT 'bd'"), ("work_mode", "TEXT NOT NULL DEFAULT ''"),
                      ("eligibility", "TEXT NOT NULL DEFAULT ''"), ("open_to_bd", "INTEGER NOT NULL DEFAULT 1"),
                      ("fit", "INTEGER NOT NULL DEFAULT 0"), ("fit_terms", "TEXT NOT NULL DEFAULT ''"),
                      ("applied_at", "TEXT NOT NULL DEFAULT ''"),  # when you marked it applied
                      ("applied_via", "TEXT NOT NULL DEFAULT ''"),  # how: "" or "easy_apply" (LinkedIn Easy Apply)
                      ("saved_at", "TEXT NOT NULL DEFAULT ''")):  # when you saved it for later
        if col not in cols:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    # Jobs deleted for good: remembered (by id and by dup_key) so a posting that's still online
    # doesn't come back as new, from the same source or another one.
    con.execute("CREATE TABLE IF NOT EXISTS removed_jobs (id TEXT PRIMARY KEY, removed_at TEXT)")
    if "dup_key" not in {r["name"] for r in con.execute("PRAGMA table_info(removed_jobs)")}:
        con.execute("ALTER TABLE removed_jobs ADD COLUMN dup_key TEXT NOT NULL DEFAULT ''")
    con.execute("""
        CREATE TABLE IF NOT EXISTS company_status (
            name TEXT PRIMARY KEY, checked TEXT, found INTEGER, matched INTEGER, error TEXT
        )""")
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return con


def meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def meta_set(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value)))
    con.commit()


_CO_SUFFIX = re.compile(r"\b(ltd|limited|inc|llc|pvt|private|plc|co|company|corp|corporation|gmbh|ab|s\.?a|"
                        r"bd|bangladesh|group)\b\.?")
_TITLE_TAGS = re.compile(r"\((?:[^)]*\b(?:remote|hybrid|on-?site|contract|location)s?\b[^)]*|m/[fwd]/[fwd]|f/m/d)\)|\[[^\]]*\]")
_TITLE_WORDS = re.compile(r"\b(remote|hybrid|on-?site|full[- ]time|part[- ]time|contract|urgent(?:ly)?|hiring)\b")


def _co_key(company):
    co = re.sub(r"\(.*?\)|\[.*?\]|\s\|.*$", " ", (company or "").lower())  # "Binance (Lever)", "CoverGo | Insurtech"
    return re.sub(r"[^a-z0-9]+", "", _CO_SUFFIX.sub(" ", co))


def _title_key(title):
    t = (title or "").lower().replace("sr.", "senior ").replace("jr.", "junior ")
    t = _TITLE_WORDS.sub(" ", re.sub(r"\s\|.*$", " ", _TITLE_TAGS.sub(" ", t)))
    return re.sub(r"[^a-z0-9]+", "", t)


def dup_key(title, company):
    """Company + title, normalized, so one job found on two sources (or reposted) is tracked once:
    "Canonical Ltd" / "QA Engineer (Remote) | Contract" == "canonical" / "QA Engineer".
    Different titles at one company ("QA Engineer" vs "Senior QA Engineer") stay separate.
    Empty when there's no title to go on."""
    t = _title_key(title)
    return f"{_co_key(company)}|{t}" if t else ""


def _is_reposter(reposters):
    keys = {_co_key(r) for r in reposters}
    return lambda company: _co_key(company) in keys


_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_FREE_MAIL = {"gmail", "googlemail", "yahoo", "ymail", "hotmail", "outlook", "live", "msn", "icloud", "aol",
              "proton", "protonmail", "gmx", "mail", "zoho", "yandex"}
_TLDS = {"com", "net", "org", "io", "co", "bd", "ai", "app", "tech", "info", "biz", "dev", "us", "uk", "in", "edu", "gov"}
_LEVEL = re.compile(r"\b(senior|sr|junior|jr|mid|lead|principal|staff|head|manager|intern|internship|trainee)\b")
_LEVEL_ALIAS = {"sr": "senior", "jr": "junior", "internship": "intern"}


def _email(job):
    """The apply email (LinkedIn posts keep it in experience as "Apply: …"), lowercased, or ""."""
    m = _EMAIL.search(job.get("experience") or "")
    return m.group(0).lower() if m else ""


def _email_company(email):
    """pridesys from career@pridesys.com / hr@mail.pridesys.com.bd; "" for free mail like gmail."""
    parts = [p for p in email.split("@")[-1].split(".") if p not in _TLDS]
    name = re.sub(r"[^a-z0-9]+", "", parts[-1]) if parts else ""
    return "" if name in _FREE_MAIL or len(name) < 4 else name


def _levels(title):
    return {_LEVEL_ALIAS.get(w, w) for w in _LEVEL.findall((title or "").lower())}


def _years(experience):
    m = re.search(r"(\d+)(?:\s*(?:-|–|to)\s*\d+)?\s*\+?\s*(?:years?|yrs?)", experience or "", re.I)
    return int(m.group(1)) if m else None


def _salary_range(salary):
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", (salary or "").replace(",", ""))]
    return (min(nums), max(nums)) if nums else None


def _same_job_by_email(a, b):
    """Two jobs tied together by an apply email: the same address, or one's email domain naming the
    other's company (career@pridesys.com ↔ "Pridesys IT Limited"). The title wording may differ, but
    the seniority words must agree ("Senior" vs none stays apart), and experience/salary, when both
    give one, must not clearly differ."""
    if a["region"] != b["region"] or a["levels"] != b["levels"]:
        return False
    if not (a["email"] and a["email"] == b["email"]
            or any(x["domain"] and y["co"] and (y["co"].startswith(x["domain"]) or x["domain"].startswith(y["co"]))
                   for x, y in ((a, b), (b, a)))):
        return False
    if a["years"] is not None and b["years"] is not None and abs(a["years"] - b["years"]) >= 2:
        return False
    sa, sb = a["salary"], b["salary"]
    return not (sa and sb and (sa[1] < sb[0] or sb[1] < sa[0]))


def _keep_first(reposters=()):
    """Sort key for feeding _dedup_index: listings first, then emailed posts, then reposters' copies,
    so the copy that gets dropped is the least informative one."""
    is_rep = _is_reposter(reposters)
    return lambda j: (is_rep(j["company"]), bool(_email(j)))


def _dedup_index(reposters=()):
    """match(job) -> id of the tracked job it duplicates, or None after remembering it as tracked.
    A duplicate has the same dup_key; or is a reposter's copy (a recruiter account that re-posts
    other companies' jobs under its own name) of a job with the same title on the same board; or
    is tied to a tracked job by its apply email (see _same_job_by_email).
    Feed it jobs in _keep_first order so copies find their originals."""
    is_rep = _is_reposter(reposters)
    exact, titles = {}, {}  # dup_key -> id; (region, title key) -> id of a non-reposter job
    known = []  # facts of every tracked job, for the email rule
    def match(j):
        key = dup_key(j["title"], j["company"]) or j["id"]
        t = (j.get("region") or "bd", _title_key(j["title"]))
        rep = is_rep(j["company"])
        if key in exact:
            return exact[key]
        if rep and t[1] and t in titles:
            return titles[t]
        email = _email(j)
        facts = {"id": j["id"], "region": t[0], "levels": _levels(j["title"]), "email": email,
                 "domain": _email_company(email), "co": "" if rep else _co_key(j["company"]),
                 "years": _years(j.get("experience")), "salary": _salary_range(j.get("salary"))}
        same = next((f["id"] for f in known if (email or f["email"]) and _same_job_by_email(facts, f)), None)
        if same:
            return same
        exact[key] = j["id"]
        if not rep and t[1]:
            titles.setdefault(t, j["id"])
        known.append(facts)
        return None
    return match


def merge_duplicates(con, reposters=()):
    """Delete stored jobs that duplicate another one (e.g. saved before a duplicate rule caught them),
    keeping any you've touched (status, notes, saved, applied). Copies go in _keep_first order.
    Returns how many were deleted."""
    match = _dedup_index(reposters)
    rows = sorted(map(dict, con.execute("SELECT * FROM jobs ORDER BY first_seen")), key=_keep_first(reposters))
    now = dt.datetime.now().isoformat(timespec="seconds")
    gone = [r["id"] for r in rows if match(r) and r["status"] == "new"
            and not (r["notes"] or r["saved_at"] or r["applied_at"])]
    # remembered by id only: the original keeps the dup_key and absorbs this copy if it shows up again
    con.executemany("INSERT OR REPLACE INTO removed_jobs (id, removed_at, dup_key) VALUES (?,?,'')",
                    [(i, now) for i in gone])
    con.executemany("DELETE FROM jobs WHERE id=?", [(i,) for i in gone])
    con.commit()
    return len(gone)


def save_jobs(con, jobs, reposters=()):
    """Insert/refresh jobs and return the ones never seen before. A job already tracked under another
    id (same company and title from another source, reposted, or a reposter's copy) is a duplicate:
    it only refreshes the tracked one's last_seen."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    new = []
    order = _keep_first(reposters)
    match = _dedup_index(reposters)
    for r in sorted(map(dict, con.execute("SELECT * FROM jobs ORDER BY first_seen")), key=order):
        match(r)
    removed_ids, removed_keys = set(), set()
    for r in con.execute("SELECT id, dup_key FROM removed_jobs"):
        removed_ids.add(r["id"])
        if r["dup_key"]:
            removed_keys.add(r["dup_key"])
    for j in sorted(jobs, key=order):
        key = dup_key(j["title"], j["company"]) or j["id"]
        if j["id"] in removed_ids or key in removed_keys:
            continue
        j = {"region": "bd", "work_mode": "", "eligibility": "", "open_to_bd": 1, "fit": None, "fit_terms": None, **j}
        exists = con.execute("SELECT 1 FROM jobs WHERE id=?", (j["id"],)).fetchone()
        tracked = None if exists else match(j)
        if tracked:  # same job under another id
            con.execute("UPDATE jobs SET last_seen=? WHERE id=?", (now, tracked))
            continue
        if exists:  # refresh the labels and fit too, so jobs saved before they existed get them
            con.execute("UPDATE jobs SET last_seen=:now, work_mode=COALESCE(NULLIF(:work_mode,''), work_mode),"
                        " eligibility=COALESCE(NULLIF(:eligibility,''), eligibility),"
                        " fit=COALESCE(:fit, fit), fit_terms=COALESCE(:fit_terms, fit_terms) WHERE id=:id",
                        {**j, "now": now})
        else:
            con.execute(
                "INSERT INTO jobs (id, source, title, company, location, url, posted, deadline, salary, experience,"
                " first_seen, last_seen, region, work_mode, eligibility, open_to_bd, fit, fit_terms) VALUES (:id,"
                ":source,:title,:company,:location,:url,:posted,:deadline,:salary,:experience,:first_seen,"
                ":first_seen,:region,:work_mode,:eligibility,:open_to_bd,COALESCE(:fit,0),COALESCE(:fit_terms,''))",
                {**j, "first_seen": now})
            new.append(j)
    con.commit()
    return new


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def _job_line(j):
    return f"{j['title']} — {j['company']} ({j['source']})"


def notify_desktop(title, message, url):
    x = lambda s: html.escape(s, quote=True)
    xml = (f'<toast activationType="protocol" launch="{x(url)}"><visual><binding template="ToastGeneric">'
           f'<text>{x(title)}</text><text>{x(message)}</text></binding></visual></toast>')
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml(@'
{xml}
'@)
$app = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($doc))
"""
    encoded = base64.b64encode(ps.encode("utf-16-le")).decode()
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                   check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def notify_telegram(tg, jobs, header):
    lines = [f"<b>{html.escape(header)}</b>"]
    for j in jobs:
        lines.append(f'• <a href="{html.escape(j["url"])}">{html.escape(j["title"])}</a>'
                     f' — {html.escape(j["company"])} <i>({j["source"]})</i>')
    # Telegram caps messages at 4096 chars; send in chunks.
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > 3800:
            _tg_send(tg, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        _tg_send(tg, chunk)


def _tg_send(tg, text):
    data = urllib.parse.urlencode({"chat_id": tg["chat_id"], "text": text,
                                   "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage", data, timeout=30)


def notify_email(em, jobs, header):
    body = "\n\n".join(f"{_job_line(j)}\n{j['location']}  {j['salary']}\n{j['url']}" for j in jobs)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = header, em["username"], em["to"]
    with smtplib.SMTP(em["smtp_host"], em["smtp_port"], timeout=30) as s:
        s.starttls()
        s.login(em["username"], em["app_password"])
        s.send_message(msg)


def notify(cfg, jobs, header):
    n = cfg["notify"]
    if n.get("desktop"):
        if len(jobs) == 1:
            msg, url = _job_line(jobs[0]), jobs[0]["url"]
        else:
            msg = "\n".join(_job_line(j) for j in jobs[:3])
            if len(jobs) > 3:
                msg += f"\n+{len(jobs) - 3} more"
            url = REPORT_PATH.as_uri()
        try:
            notify_desktop(header, msg, url)
        except Exception:
            log.exception("Desktop notification failed")
    if n["telegram"].get("enabled"):
        try:
            notify_telegram(n["telegram"], jobs, header)
        except Exception:
            log.exception("Telegram notification failed")
    if n["email"].get("enabled"):
        try:
            notify_email(n["email"], jobs, header)
        except Exception:
            log.exception("Email notification failed")


# --------------------------------------------------------------------------- #
# HTML report (opened when a toast covers several jobs)
# --------------------------------------------------------------------------- #

def write_report(con, new_ids):
    rows = con.execute("SELECT * FROM jobs WHERE archived_at='' ORDER BY first_seen DESC, posted DESC").fetchall()
    e = html.escape
    trs = "".join(
        f'<tr class="{"new" if r["id"] in new_ids else ""}"><td>{e(r["first_seen"][:16].replace("T", " "))}</td>'
        f'<td><a href="{e(r["url"])}" target="_blank">{e(r["title"])}</a></td><td>{e(r["company"])}</td>'
        f'<td>{e(r["location"])}</td><td>{e(r["salary"])}</td><td>{e(r["deadline"])}</td><td>{e(r["source"])}</td></tr>'
        for r in rows)
    REPORT_PATH.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>SQA Jobs</title>
<style>body{{font-family:Segoe UI,sans-serif;margin:24px;background:#fff;color:#222}}
table{{border-collapse:collapse;width:100%}}td,th{{padding:6px 10px;border-bottom:1px solid #ddd;text-align:left;font-size:14px}}
tr.new{{background:#fff6d6}}a{{color:#0a58ca}}</style></head><body>
<h2>SQA jobs in Bangladesh — {len(rows)} tracked</h2>
<p>Updated {dt.datetime.now():%Y-%m-%d %H:%M}. Highlighted rows are new since the last check.</p>
<table><tr><th>First seen</th><th>Title</th><th>Company</th><th>Location</th><th>Salary</th><th>Deadline</th><th>Source</th></tr>
{trs}</table></body></html>""", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def load_config():
    if not CONFIG_PATH.exists():
        shutil.copyfile(EXAMPLE_CONFIG_PATH, CONFIG_PATH)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def is_sqa(cfg, job):
    f = cfg["filters"]
    title, company = job["title"], job["company"]
    if re.search(f["exclude"], title, re.I):
        return False
    if f.get("skip_level") and re.search(f["skip_level"], title, re.I):  # levels you don't want, e.g. junior, manager
        return False
    if re.search(f["strong_title"], title, re.I):
        return True
    if not re.search(f["weak_title"], title, re.I) or re.search(f["exclude"], company, re.I):
        return False
    return (job.get("tech")  # posted on a software company's own careers page
            or job.get("category") in cfg["bdjobs"]["it_category_ids"]
            or bool(re.search(f["tech_company"], company, re.I)))


def fit_score(cfg, job):
    """How well a job matches your profile: the summed weights of the config fit.terms found in its
    title, experience and (where the source gives one) description. Returns (score, "label +3, …")."""
    text = " ".join(str(job.get(k) or "") for k in ("title", "experience", "text", "post_text"))
    score, hits = 0, []
    for t in (cfg.get("fit") or {}).get("terms", []):
        if re.search(t["pattern"], text, re.I):
            score += t["weight"]
            hits.append(f"{t['label']} {t['weight']:+d}")
    return score, ", ".join(hits)


def run_check(cfg, force_companies=False):
    con = db_connect()
    first_run = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

    found = []
    for name, fetch in SOURCES.items():
        if not cfg["sources"].get(name):
            continue
        try:
            jobs = [j for j in fetch(cfg) if is_sqa(cfg, j)]
            log.info("%s: %d matching jobs", name, len(jobs))
            found.extend(jobs)
        except Exception:
            log.exception("Source %s failed", name)
    if cfg["sources"].get("companies"):
        found.extend(fetch_companies(cfg, con, force_companies))
    if cfg["sources"].get("linkedin_posts"):
        found.extend(fetch_linkedin_posts(cfg, con, force_companies))
    if cfg["sources"].get("jobgether"):
        found.extend(fetch_jobgether(cfg, con, force_companies))
    first_global = False
    if cfg["sources"].get("global"):
        global_jobs, first_global = fetch_global(cfg, con, force_companies)
        found.extend(global_jobs)

    for j in found:
        j["fit"], j["fit_terms"] = fit_score(cfg, j)
    reposters = (cfg.get("dedupe") or {}).get("reposters", [])
    new = save_jobs(con, found, reposters)
    merged = merge_duplicates(con, reposters)
    if merged:
        log.info("removed %d duplicate jobs", merged)
    write_report(con, {j["id"] for j in new})
    log.info("%d new jobs", len(new))

    # Global remote jobs only alert when you can apply from Bangladesh (unless configured otherwise),
    # and the first global search fills the board quietly with one summary alert.
    notify_all = (cfg.get("global") or {}).get("notify_restricted", False)
    bd_new = [j for j in new if j.get("region", "bd") != "global"]
    gl_new = [j for j in new if j.get("region") == "global" and (j.get("open_to_bd") or notify_all)]
    if first_global:
        gl_all = [j for j in new if j.get("region") == "global"]
        if gl_all:
            notify(cfg, gl_new or gl_all[:3], f"Global remote board ready: {len(gl_all)} SQA jobs, "
                                              f"{sum(j['open_to_bd'] for j in gl_all)} open to Bangladesh")
        gl_new = []
    alert = bd_new + gl_new
    if alert:
        if first_run:
            notify(cfg, alert, f"SQA job alert is running — tracking {len(alert)} current jobs")
        else:
            parts = [f"{len(bd_new)} Bangladesh" if bd_new else "", f"{len(gl_new)} global remote" if gl_new else ""]
            notify(cfg, alert, f"{len(alert)} new SQA job{'s' if len(alert) > 1 else ''} "
                               f"({', '.join(p for p in parts if p)})")
    for j in new:
        print(f"[NEW] {_job_line(j)}\n      {j['url']}")
    print(f"{len(found)} matching jobs found, {len(new)} new. Report: {REPORT_PATH}")


def _session_source(cfg, con, force, key, name, search, settings, home):
    """Run a source that uses your own logged-in browser session (config/meta key `key`), at most
    every <key>.check_every_hours. search() returns job records or raises LoginRequired."""
    opts = cfg.get(key) or {}
    prev = meta_get(con, key, {})
    due_before = dt.datetime.now() - dt.timedelta(hours=opts.get("check_every_hours", 6))
    if not force and prev.get("checked") and dt.datetime.fromisoformat(prev["checked"]) > due_before:
        return []
    state = {"checked": dt.datetime.now().isoformat(timespec="seconds"), "found": 0, "error": "", "needs_login": False}
    jobs = []
    try:
        jobs = search()
        state["found"] = len(jobs)
    except ImportError:
        state["error"] = "Playwright isn't installed (py -m pip install playwright)"
    except Exception as exc:
        state["error"] = str(exc)[:300]
        state["needs_login"] = type(exc).__name__ == "LoginRequired"
    if state["error"]:
        log.warning("%s: %s", name, state["error"])
    meta_set(con, key, state)
    if state["needs_login"] and not prev.get("needs_login") and cfg["notify"].get("desktop"):
        try:  # tell you once, not every run
            notify_desktop(f"SQA job alert: {name.split()[0]} needs you to log in again",
                           f"Open the app → Settings → {settings} → Log in.", home)
        except Exception:
            log.exception("Desktop notification failed")
    return jobs


def fetch_linkedin_posts(cfg, con, force=False):
    """Hiring posts via your logged-in LinkedIn session."""
    def search():
        import linkedin_posts
        return linkedin_posts.search_posts(cfg, is_sqa, log.info)
    return _session_source(cfg, con, force, "linkedin_posts", "LinkedIn posts", search,
                           "LinkedIn hiring posts", "https://www.linkedin.com/")


def fetch_jobgether(cfg, con, force=False):
    """Your Jobgether matches, via your logged-in Jobgether session."""
    def search():
        import jobgether
        return jobgether.search_matches(cfg, is_sqa, log.info)
    return _session_source(cfg, con, force, "jobgether", "Jobgether matches", search,
                           "Global remote jobs → Jobgether", "https://jobgether.com/talent/matches")


def list_jobs():
    con = db_connect()
    for r in con.execute("SELECT * FROM jobs WHERE archived_at='' ORDER BY first_seen DESC, posted DESC"):
        print(f"{r['first_seen'][:16]}  {r['title']} — {r['company']} [{r['source']}]\n"
              f"                  {r['url']}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")]
                        + ([logging.StreamHandler()] if sys.stderr else []))
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    else:  # running windowless under pythonw (Task Scheduler)
        sys.stdout = open(os.devnull, "w")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list tracked jobs")
    ap.add_argument("--test", action="store_true", help="send a test notification")
    ap.add_argument("--all", action="store_true",
                    help="also check company pages that aren't due yet (see companies.check_every_hours)")
    args = ap.parse_args()

    cfg = load_config()
    if args.list:
        list_jobs()
    elif args.test:
        notify(cfg, [{"title": "Test SQA Engineer", "company": "Example Ltd", "source": "Test",
                      "location": "Dhaka", "salary": "", "url": "https://bdjobs.com"}],
               "SQA job alert — test notification")
        print("Test notification sent.")
    else:
        run_check(cfg, force_companies=args.all)


if __name__ == "__main__":
    main()
