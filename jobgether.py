"""Jobgether matches (https://jobgether.com/talent/matches), read with your own logged-in session.

The matches page is only shown to a logged-in user. Two ways to give this code that session:
- Reuse the browser you're already logged in with: copy the Cookie header of a jobgether.com
  request (DevTools → Network) into config jobgether.cookie. Copy a fresh one when it expires.
- Or log in once in a separate profile for your installed Google Chrome (--login); Chrome doesn't
  allow automating your everyday profile.
Your password is never stored by this code.

Only the list of matched offers comes from the logged-in page. Each offer's details (title,
company, who may apply, description) come from its public page (/offer/<id>-<slug>), and only
for offers whose title passes your filters.

Usage:
    py jobgether.py --login          # open Chrome, log in to Jobgether, close when done
    py jobgether.py --status         # check the saved session still works
    py jobgether.py --search         # read your matches and print the QA ones
    py jobgether.py --dump           # save the matches page for debugging
"""

import argparse
import datetime as dt
import json
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "jobgether_session"
BASE = "https://jobgether.com"
MATCHES_URL = f"{BASE}/talent/matches"
OFFER = re.compile(r"/offer/([0-9a-f]{24})(?:-([^\"'\s<>?#/\\]*))?")
JSON_LD = re.compile(r'(?s)<script type="application/ld\+json">(.*?)</script>')
# First line of every description: "This a Full Remote job, the offer is available from: United States, Texas (USA)"
AVAILABLE = re.compile(r"(?i)this (?:is )?an? (.{0,40}?) job, the offer is available from:?\s*([^<]+)")


# Jobgether names each country's parent regions too: "Europe, EMEA, United Kingdom" is a UK-only role.
REGIONS = re.compile(
    r"(?i)(africa|asia|europe|oceania|north america|south america|central america|latin america|latam|americas?|amer|"
    r"middle east|emea|apac|european union|eu|sub-saharan africa|northern africa|north africa|"
    r"(?:south|east|southeast|south-east|central) asia|(?:western|eastern|northern|southern|central) europe|"
    r"nordics|scandinavia|caribbean|dach|benelux|cee|balkans|baltics|worldwide|anywhere|global)")
CONTINENTS = {"africa", "asia", "europe", "oceania", "north america", "south america"}


class LoginRequired(RuntimeError):
    pass


def _browser(p, headless):
    return p.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=headless,
        viewport=None if not headless else {"width": 1280, "height": 900})


def _cookies(header):
    """'name=value; name2=value2' (the Cookie header copied from your browser) → Playwright cookies."""
    out = []
    for part in header.strip().removeprefix("cookie:").removeprefix("Cookie:").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            out.append({"name": name, "value": value, "domain": ".jobgether.com", "path": "/", "secure": True})
    return out


def _session(p, cfg, headless):
    """A browser context logged in to Jobgether: your own browser's session when jobgether.cookie is
    set, otherwise the profile from --login."""
    cookie = ((cfg or {}).get("jobgether") or {}).get("cookie", "")
    if not cookie:
        if not PROFILE.exists():
            raise LoginRequired("Not logged in to Jobgether yet")
        return _browser(p, headless)
    browser = p.chromium.launch(channel="chrome", headless=headless)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.add_cookies(_cookies(cookie))
    ctx.on("close", lambda _: browser.close())
    return ctx


def _expired(cfg):
    if ((cfg or {}).get("jobgether") or {}).get("cookie"):
        return LoginRequired("Your Jobgether cookie has expired: copy a fresh one from your browser")
    return LoginRequired("Jobgether asked to log in again")


def _logged_in(ctx):
    """Jobgether redirects /talent/matches to its home page when you're signed out.
    Uses the context's cookies without touching the page you may be logging in on."""
    resp = ctx.request.get(MATCHES_URL, max_redirects=0, fail_on_status_code=False)
    return resp.status == 200


def login(timeout_minutes=10):
    """Open a visible Chrome window on Jobgether and wait until you're signed in."""
    from playwright.sync_api import Error, sync_playwright
    with sync_playwright() as p:
        ctx = _browser(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(MATCHES_URL)  # signed out: lands on the home page with its "Log in" button
        deadline = time.time() + timeout_minutes * 60
        try:
            while time.time() < deadline:
                try:
                    if _logged_in(ctx):
                        time.sleep(3)  # let the site finish writing cookies
                        return True
                except Error:  # window closed by the user
                    return False
                if not ctx.pages:
                    return False
                time.sleep(3)
            return False
        finally:
            try:
                ctx.close()
            except Error:
                pass


def status(cfg=None):
    """True if the saved session (or your browser's cookie) still opens your matches."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            ctx = _session(p, cfg, headless=True)
        except LoginRequired:
            return False
        try:
            return _logged_in(ctx)
        finally:
            ctx.close()


def _pause(lo, hi):
    time.sleep(random.uniform(lo, hi))


def _title_from_slug(slug):
    return re.sub(r"[-_]+", " ", urllib.parse.unquote(slug or "")).strip()


def _open_matches(ctx, cfg, scrolls):
    """The matches page after scrolling (and pressing "Load more", if it has one) a few times."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(MATCHES_URL, wait_until="domcontentloaded")
    _pause(4, 6)
    if "/talent/" not in page.url:
        raise _expired(cfg)
    more = page.get_by_role("button", name=re.compile(r"load more|show more|see more", re.I))
    for _ in range(scrolls):
        page.mouse.wheel(0, random.randint(1800, 2600))
        _pause(2, 4)
        if more.count() and more.first.is_visible():
            more.first.click()
            _pause(3, 5)
    return page


def match_links(cfg, log=print):
    """[(offer id, URL slug)] of every offer on your matches page, in page order.
    Raises LoginRequired if the session has expired."""
    from playwright.sync_api import sync_playwright
    opts = cfg.get("jobgether") or {}
    with sync_playwright() as p:
        ctx = _session(p, cfg, headless=not opts.get("show_browser"))
        try:
            page = _open_matches(ctx, cfg, opts.get("scrolls", 5))
            body = page.content()
        finally:
            ctx.close()
    offers = {}
    for oid, slug in OFFER.findall(body):  # links and any embedded data that names an offer
        if slug or oid not in offers:
            offers[oid] = slug
    log(f"Jobgether: {len(offers)} offers on your matches page")
    return list(offers.items())


def _posted(value):
    """"Wed Oct 21 2026 01:30:28 GMT+0000 (…)" or ISO → ISO."""
    m = re.match(r"\w{3} (\w{3} \d{1,2} \d{4} \d{2}:\d{2}:\d{2})", value or "")
    if m:
        return dt.datetime.strptime(m.group(1), "%b %d %Y %H:%M:%S").replace(
            tzinfo=dt.timezone.utc).isoformat(timespec="seconds")
    return value or ""


def who_can_apply(restriction):
    """The countries a role is really open to: the listed countries when there are any (the regions
    before them are just their parents), the regions otherwise, or "Worldwide" when it spans four or
    more continents (then any countries listed are usually the sanctioned ones)."""
    parts = [p.strip() for p in restriction.split(",") if p.strip()]
    regions = [p for p in parts if REGIONS.fullmatch(p)]
    countries = [p for p in parts if not REGIONS.fullmatch(p)]
    if len(CONTINENTS & {r.lower() for r in regions}) >= 4:
        return "Worldwide"
    return ", ".join(countries or regions)


def parse_offer(oid, page_html):
    """The JobPosting fields of one public offer page, or None if it has none."""
    import sqa_jobs
    posting = None
    for block in JSON_LD.findall(page_html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            posting = data
            break
    if not posting:
        return None
    desc = posting.get("description") or ""
    m = AVAILABLE.search(desc)
    arrangement, restriction = (m.group(1), m.group(2).strip()) if m else ("", "")
    if not restriction:
        restriction = ", ".join(r.get("name", "") for r in posting.get("applicantLocationRequirements") or [])
    remote = posting.get("jobLocationType") == "TELECOMMUTE"
    mode = sqa_jobs.guess_mode(arrangement, default="remote" if remote else "onsite")
    pay = posting.get("baseSalary") or {}
    value = pay.get("value") or {}
    org = posting.get("hiringOrganization") or {}
    return {"id": oid, "title": posting.get("title") or "", "company": org.get("name") or "",
            "restriction": who_can_apply(restriction), "work_mode": mode, "posted": _posted(posting.get("datePosted")),
            "deadline": (posting.get("validThrough") or "")[:10], "text": desc,
            "salary": sqa_jobs._salary(value.get("minValue"), value.get("maxValue"), pay.get("currency"),
                                       (value.get("unitText") or "").lower())}


def to_job(o, url):
    """A job record: remote offers go on the Global board (labelled with who may apply); hybrid and
    on-site offers only on the Bangladesh board, when they're in Bangladesh. None otherwise."""
    import sqa_jobs
    if o["work_mode"] == "remote":
        return sqa_jobs._global_job("Jobgether", o["id"], o["title"], o["company"], url, o["restriction"],
                                    o["posted"], o["deadline"], o["salary"], text=o["text"])
    if not re.search(r"(?i)bangladesh|dhaka", o["restriction"]):
        return None
    return {"id": f"jobgether:{o['id']}", "source": "Jobgether", "title": sqa_jobs._clean(o["title"]),
            "company": sqa_jobs._clean(o["company"]), "location": o["restriction"][:80], "url": url,
            "posted": o["posted"], "deadline": o["deadline"], "salary": o["salary"], "experience": "",
            "tech": True, "text": sqa_jobs._clean(o["text"])[:8000], "work_mode": o["work_mode"]}


def search_matches(cfg, is_sqa, log=print):
    """Job records for the QA offers among your matches. Global jobs not open to Bangladesh are
    dropped when global.only_open_to_bd is on. Raises LoginRequired if the session has expired."""
    import sqa_jobs
    opts = cfg.get("jobgether") or {}
    only_bd = (cfg.get("global") or {}).get("only_open_to_bd", True)
    links = match_links(cfg, log)
    # The URL's title is lowercase and loses punctuation, but the filters are case-insensitive:
    # good enough to skip reading offers that are clearly not QA.
    wanted = [(oid, slug) for oid, slug in links
              if not slug or is_sqa(cfg, {"title": _title_from_slug(slug), "company": "", "tech": True})]
    found = []
    for i, (oid, slug) in enumerate(wanted[:opts.get("max_offers", 40)]):
        if i:
            _pause(1, 2)
        url = f"{BASE}/offer/{oid}-{slug}" if slug else f"{BASE}/offer/{oid}"  # the page needs the slug
        try:
            offer = parse_offer(oid, sqa_jobs.http_get(url, retries=2))
        except Exception as exc:
            log(f"Jobgether offer {oid} skipped: {exc}")
            continue
        if not offer:
            continue
        job = to_job(offer, url)
        if job and is_sqa(cfg, job) and (job.get("open_to_bd", 1) or not only_bd):
            found.append(job)
    log(f"Jobgether: {len(wanted)} QA-looking offers read, {len(found)} kept")
    return found


def dump(cfg, out):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _session(p, cfg, headless=True)
        try:
            page = _open_matches(ctx, cfg, 3)
            Path(out).write_text(page.content(), encoding="utf-8")
            Path(out).with_suffix(".txt").write_text(page.inner_text("body"), encoding="utf-8")
            print("url:", page.url)
        finally:
            ctx.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--status", action="store_true", help="check the saved session still works")
    ap.add_argument("--search", action="store_true", help="read your matches and print the QA ones")
    ap.add_argument("--dump", action="store_true", help="save the matches page for debugging")
    ap.add_argument("--out", default="jobgether_dump.html")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.login:
        ok = login()
        print("Logged in." if ok else "Login not completed (window closed or timed out).")
        sys.exit(0 if ok else 1)
    import sqa_jobs
    cfg = sqa_jobs.load_config()
    if args.status:
        ok = status(cfg)
        print("Session OK." if ok else "Not logged in.")
        sys.exit(0 if ok else 1)
    if args.search:
        for j in search_matches(cfg, sqa_jobs.is_sqa):
            print(f"- {j['title']} | {j['company']} | {j['location']} | {j['posted'][:10]}\n  {j['url']}")
    if args.dump:
        dump(cfg, args.out)


if __name__ == "__main__":
    main()
