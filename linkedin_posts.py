"""LinkedIn hiring posts, read with your own logged-in LinkedIn session.

Automated access to logged-in LinkedIn is against LinkedIn's User Agreement and can get the
account restricted, so this source is opt-in, slow and small: a few searches every few hours,
scrolling at reading pace. If LinkedIn shows a security check it stops and asks you to log in
again instead of trying to get past it.

The session lives in ./linkedin_session, a separate profile for your installed Google Chrome
(Chrome doesn't allow automating your everyday profile). Your password is never stored by this
code: you log in yourself in a normal Chrome window, once.

Usage:
    py linkedin_posts.py --login          # open Chrome, log in to LinkedIn, close when done
    py linkedin_posts.py --status         # check the saved session still works
    py linkedin_posts.py --search         # run the configured searches and print matches
    py linkedin_posts.py --dump "query"   # save one search page for debugging
"""

import argparse
import datetime as dt
import hashlib
import random
import re
import sys
import time
import unicodedata
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT / "linkedin_session"
SEARCH_URL = "https://www.linkedin.com/search/results/content/"
LOGGED_IN = re.compile(r"linkedin\.com/(feed|in/|mynetwork|jobs|search|notifications|messaging)")
BLOCKED = re.compile(r"/(login|authwall|checkpoint|uas/login)")

DEFAULT_QUERIES = ["hiring SQA Bangladesh", "hiring SQA Dhaka", "hiring QA engineer Bangladesh",
                   "hiring software tester Dhaka", "QA automation hiring Dhaka"]

# A post must mention a place in Bangladesh, be hiring (not job-seeking), and name a QA role.
BD_PLACE = re.compile(
    r"\b(bangladesh|dhaka|chatt?ogram|chittagong|sylhet|khulna|rajshahi|barishal|barisal|rangpur|mymensingh|"
    r"comilla|cumilla|gazipur|narayanganj|savar|mirpur|gulshan|banani|baridhara|uttara|dhanmondi|mohakhali|"
    r"tejgaon|bashundhara|badda|motijheel|panthapath|farmgate|niketan|bdt)\b", re.I)
HIRING = re.compile(
    r"\b(hiring|we(?:'|’)?re looking|we are looking|looking for (?:an?|experienced|talented|skilled|passionate|dedicated)|"
    r"vacanc\w*|job opening|open position|join (?:our|the) team|send (?:your )?(?:cv|resume)|"
    r"(?:drop|share|submit) (?:your )?(?:updated )?(?:cv|resume)|career opportunit\w*|job circular|job post)\b", re.I)
# Course and training ads ("SQA batch starts…") use the same words; drop them unless clearly hiring.
TRAINING = re.compile(r"\b(courses?|training|batch|enrol\w*|admission|bootcamp|workshop|webinar|seminar|"
                      r"classes|class starts|free session|certification program|learn \w+ from)\b", re.I)
CLEARLY_HIRING = re.compile(r"\b(hiring|vacanc\w*|job opening|open position|send (?:your )?(?:cv|resume)|salary)\b", re.I)
# Signs a post is about software, so a factory or group that's hiring for its IT team still counts.
SOFTWARE = re.compile(r"\b(software|test (?:cases?|plans?|scripts?)|automat\w+ test\w*|selenium|playwright|cypress|appium|"
                      r"api testing|defects?|bugs?|cse|computer science|it department|web app\w*|mobile app\w*|sdlc|stlc)\b", re.I)
SEEKER = re.compile(
    r"(#?open ?to ?work|looking for (?:a |an |new )*(?:job|opportunit\w*|role|position|internship)|"
    r"seeking (?:a |an |new )*(?:job|opportunit\w*|role|position)|"
    r"\bi(?: am|'m|’m) (?:an? )?(?:experienced |senior |junior |certified )?(?:sqa|qa|software|test)|"
    r"bd partner|partnership)", re.I)
POSTED = re.compile(r"^(\d+)\s*(m|h|d|w|mo|yr)\b")
UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000, "yr": 31536000}
LEADING_JUNK = re.compile(r"^[\W_]*(?:(?:urgent(?:ly)?|immediate(?:ly)?|now|we(?:'|’)?re|we are|are|is|job)\s+)*"
                          r"(?:hiring|recruiting)\s*(?:for|an?|the)?\s*[:\-–—|]*\s*", re.I)

# Runs in the page: one record per post card in the search results.
COLLECT_JS = """() => [...document.querySelectorAll('[role="listitem"]')].map(el => {
  const menu = el.querySelector('[aria-label^="Open control menu for post by"]');
  if (!menu) return null;
  return {
    author: menu.getAttribute('aria-label').replace('Open control menu for post by', '').trim(),
    text: el.innerText,
    links: [...el.querySelectorAll('a[href]')].map(a => a.href),
  };
}).filter(Boolean)"""


class LoginRequired(RuntimeError):
    pass


def _browser(p, headless):
    return p.chromium.launch_persistent_context(
        str(PROFILE), channel="chrome", headless=headless,
        viewport=None if not headless else {"width": 1280, "height": 900})


def _has_session(ctx):
    return any(c["name"] == "li_at" and c["value"] for c in ctx.cookies("https://www.linkedin.com"))


def login(timeout_minutes=10):
    """Open a visible Chrome window on LinkedIn's login page and wait until you're signed in.
    Signed in = LinkedIn has set its session cookie and no security check is showing."""
    from playwright.sync_api import Error, sync_playwright
    with sync_playwright() as p:
        ctx = _browser(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.linkedin.com/login")
        deadline = time.time() + timeout_minutes * 60
        try:
            while time.time() < deadline:
                try:
                    urls = [pg.url for pg in ctx.pages]
                    if _has_session(ctx) and not any("checkpoint" in u or "/login" in u for u in urls):
                        time.sleep(3)  # let LinkedIn finish writing cookies
                        return True
                except Error:  # window closed by the user
                    return False
                if not ctx.pages:
                    return False
                time.sleep(2)
            return False
        finally:
            try:
                ctx.close()
            except Error:
                pass


def status():
    """True if the saved session still opens your LinkedIn feed."""
    if not PROFILE.exists():
        return False
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _browser(p, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            return bool(LOGGED_IN.search(page.url)) and not BLOCKED.search(page.url)
        finally:
            ctx.close()


def _search_url(query):
    return f"{SEARCH_URL}?" + urllib.parse.urlencode(
        {"keywords": query, "datePosted": '"past-week"', "sortBy": '"date_posted"'})


def _pause(lo, hi):
    time.sleep(random.uniform(lo, hi))  # reading pace, not a burst of requests


def _real_link(href):
    """LinkedIn wraps outside links in /safety/go/?url=…; unwrap them."""
    if "/safety/go" in href:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("url", [href])[0]
    return href


def _posted(text):
    for line in text.splitlines()[:15]:
        m = POSTED.match(line.strip())
        if m:
            ago = int(m.group(1)) * UNITS[m.group(2)]
            return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ago)).isoformat(timespec="seconds")
    return ""


NOT_A_TITLE = re.compile(
    r"^([•▪●◦\-*–]|\d+[.)]|(experience|location|salary|deadline|requirements?|qualifications?|skills?|"
    r"responsibilit\w*|education|job type|benefits?|apply|email|contact|note)\b\s*[:\-])", re.I)


def _body_lines(text):
    """Post lines after the card header (author, group or headline, "2d", "Follow")."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines[:12]):
        if POSTED.match(line):
            rest = lines[i + 1:]
            return rest[1:] if rest and rest[0].lower() in ("follow", "+ follow", "join", "connect") else rest
    return lines


def _role_line(cfg, text, is_sqa):
    """The first short, title-like line near the top of the post that names a QA role.
    Bullets and "Experience: …"-style lines are job descriptions, not the role being hired."""
    for line in _body_lines(text)[:30]:
        if len(line) > 110 or NOT_A_TITLE.match(line) or re.fullmatch(r"(#\w+\s*)+", line):
            continue
        if is_sqa(cfg, {"title": line, "company": ""}):
            title = re.sub(r"^.{0,60}?\bis\s+hiring\b\s*(?:for|an?)?\s*[:\-–—|]*\s*", "", line, flags=re.I) or line
            title = LEADING_JUNK.sub("", title)
            title = re.sub(r"[!🚀📢💼✨🔥⭐️✅📍📌🎯💻]+", "", title).strip(" -–—|:!.")
            return title[:120] or line[:120]
    return ""


def parse_post(cfg, post, is_sqa):
    """A job record for one post card, or None if it isn't a Bangladesh QA hiring post."""
    text = unicodedata.normalize("NFKC", post["text"])  # 𝗦𝗤𝗔 → SQA
    text = text.replace("… more", "").strip()
    links = [_real_link(h) for h in post["links"]]
    profile = next((h for h in links if re.search(r"linkedin\.com/(in|company|showcase)/", h)), "")
    if SEEKER.search(text) or not HIRING.search(text):
        return None
    if TRAINING.search(text) and not CLEARLY_HIRING.search(text):
        return None
    if re.search(cfg["filters"]["exclude"], post["author"], re.I) and not SOFTWARE.search(text):
        return None  # e.g. a garments factory hiring "QA", unless it's for its software/IT team
    place = BD_PLACE.search(text) or BD_PLACE.search(profile)
    if not place:
        return None
    title = _role_line(cfg, text, is_sqa)
    if not title:
        return None
    post_link = next((h for h in links if "highlightedUpdateUrn" in h or "/feed/update/" in h or "/posts/" in h), "")
    apply = next((h for h in links if not re.search(r"linkedin\.com/", h) and not h.startswith("mailto:")), "")
    email = next((h[7:] for h in links if h.startswith("mailto:")), "")
    # Reposts of one job by several accounts share the role and apply link (or email), or the post text
    # itself (the card header with the poster's name is left out): count them once.
    if apply or email:
        key_src = f"{title.lower()}|{apply or email}"
    else:
        key_src = f"{title.lower()}|{re.sub(r'[^0-9a-z]+', '', ' '.join(_body_lines(text)).lower())[:400]}"
    key = hashlib.sha1(key_src.encode()).hexdigest()[:16]
    return {
        "id": f"linkedin_post:{key}", "source": "LinkedIn post", "title": title,
        "company": post["author"], "location": place.group(0).title() if place.group(0).lower() != "bdt" else "Bangladesh",
        "url": post_link or apply or profile or "https://www.linkedin.com/", "posted": _posted(post["text"]),
        "deadline": "", "salary": "", "experience": f"Apply: {email}" if email and not apply else "",
        "tech": True, "post_text": text[:3000], "work_mode": _guess_mode(text),
    }


def _guess_mode(text):
    from sqa_jobs import guess_mode  # imported here: sqa_jobs imports this module lazily too
    return guess_mode(text)


def search_posts(cfg, is_sqa, log=print, raw=None):
    """Run each configured query once, scrolling a few screens, and return matching job records.
    Raises LoginRequired if LinkedIn asks for a login or shows a security check.
    Pass a list as raw to also collect every post card seen (for tuning the parser offline)."""
    from playwright.sync_api import sync_playwright
    opts = cfg.get("linkedin_posts") or {}
    queries = opts.get("queries") or DEFAULT_QUERIES
    if not PROFILE.exists():
        raise LoginRequired("Not logged in to LinkedIn yet")
    found, seen = [], set()
    with sync_playwright() as p:
        ctx = _browser(p, headless=not opts.get("show_browser"))
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            for i, query in enumerate(queries):
                if i:
                    _pause(20, 40)
                page.goto(_search_url(query), wait_until="domcontentloaded")
                _pause(4, 7)
                if BLOCKED.search(page.url):
                    raise LoginRequired(f"LinkedIn asked to log in again ({page.url.split('?')[0]})")
                for _ in range(opts.get("scrolls", 4)):
                    page.mouse.wheel(0, random.randint(1800, 2600))
                    _pause(3, 6)
                posts = page.evaluate(COLLECT_JS)
                if raw is not None:
                    raw.extend({**p, "query": query} for p in posts)
                matched = 0
                for post in posts:
                    job = parse_post(cfg, post, is_sqa)
                    if job and job["id"] not in seen:
                        seen.add(job["id"])
                        found.append(job)
                        matched += 1
                log(f"LinkedIn posts {query!r}: {len(posts)} posts, {matched} Bangladesh QA hiring posts")
        finally:
            ctx.close()
    return found


def dump(query, out):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = _browser(p, headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(_search_url(query))
        page.wait_for_timeout(6000)
        for _ in range(3):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(2500)
        Path(out).write_text(page.content(), encoding="utf-8")
        Path(out).with_suffix(".txt").write_text(page.inner_text("body"), encoding="utf-8")
        print("url:", page.url)
        ctx.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--status", action="store_true", help="check the saved session still works")
    ap.add_argument("--search", action="store_true", help="run the configured searches and print matches")
    ap.add_argument("--save-raw", metavar="FILE", help="with --search: also save every post card seen, as JSON")
    ap.add_argument("--dump", metavar="QUERY")
    ap.add_argument("--out", default="linkedin_dump.html")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.login:
        ok = login()
        print("Logged in." if ok else "Login not completed (window closed or timed out).")
        sys.exit(0 if ok else 1)
    if args.status:
        ok = status()
        print("Session OK." if ok else "Not logged in.")
        sys.exit(0 if ok else 1)
    if args.search:
        import json
        import sqa_jobs
        raw = [] if args.save_raw else None
        for j in search_posts(sqa_jobs.load_config(), sqa_jobs.is_sqa, raw=raw):
            print(f"- {j['title']} | {j['company']} | {j['location']} | {j['posted'][:10]}\n  {j['url']}")
        if args.save_raw:
            Path(args.save_raw).write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.dump:
        dump(args.dump, args.out)


if __name__ == "__main__":
    main()
