"""Local web UI for the SQA job alert.

Usage:
    py app.py                 # serve http://127.0.0.1:8765 and open it in the browser
    py app.py --port 9000 --no-browser
"""

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sqa_jobs

ROOT = Path(__file__).resolve().parent
UI_PATH = ROOT / "ui.html"
TASK_NAME = "SQA Job Alert"
STATUSES = ("new", "saved", "applied", "dismissed")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- #
# Background runs of sqa_jobs.py (check / test notification)
# --------------------------------------------------------------------------- #

class Runner:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"running": False, "kind": None, "output": "", "returncode": None,
                      "started": None, "finished": None}

    def start(self, kind):
        with self.lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "kind": kind, "output": "", "returncode": None,
                          "started": _now(), "finished": None}
        threading.Thread(target=self._run, args=(kind,), daemon=True).start()
        return True

    def _run(self, kind):
        args = {"check": [sys.executable, str(ROOT / "sqa_jobs.py"), "--all"],
                "test": [sys.executable, str(ROOT / "sqa_jobs.py"), "--test"],
                "login": [sys.executable, str(ROOT / "linkedin_posts.py"), "--login"],
                "jobgether_login": [sys.executable, str(ROOT / "jobgether.py"), "--login"]}[kind]
        try:
            proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", creationflags=NO_WINDOW)
            # The script mirrors its log to stderr; stdout holds the summary lines.
            out, code = proc.stdout.strip() or proc.stderr.strip(), proc.returncode
            if code:
                out = (proc.stdout + "\n" + proc.stderr).strip()
        except Exception as exc:
            out, code = f"Failed to start: {exc}", -1
        key = {"login": "linkedin_posts", "jobgether_login": "jobgether"}.get(kind)
        if key and code == 0:  # fresh session: clear any "log in again" flag
            con = sqa_jobs.db_connect()
            state = sqa_jobs.meta_get(con, key, {})
            sqa_jobs.meta_set(con, key, {**state, "needs_login": False, "error": ""})
            con.close()
        with self.lock:
            self.state.update(running=False, output=out, returncode=code, finished=_now())

    def snapshot(self):
        with self.lock:
            return dict(self.state)


runner = Runner()


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

def list_jobs():
    con = sqa_jobs.db_connect()
    try:
        rows = con.execute("SELECT * FROM jobs ORDER BY first_seen DESC, posted DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def update_job(data):
    sets, params = [], []
    if "status" in data:
        if data["status"] not in STATUSES:
            raise ValueError(f"Unknown status {data['status']!r}")
        sets.append("status=?"), params.append(data["status"])
        # Keep the first date it was marked applied; clear it when it's moved back out of Applied.
        now = dt.datetime.now().isoformat(timespec="seconds")
        sets.append("applied_at=CASE WHEN ?='applied' THEN COALESCE(NULLIF(applied_at,''), ?) ELSE '' END")
        params += [data["status"], now]
        sets.append("saved_at=CASE WHEN ?='saved' THEN COALESCE(NULLIF(saved_at,''), ?) ELSE '' END")
        params += [data["status"], now]
        via = data.get("applied_via", "")
        if via not in ("", "easy_apply"):
            raise ValueError(f"Unknown applied_via {via!r}")
        sets.append("applied_via=?"), params.append(via if data["status"] == "applied" else "")
    if "notes" in data:
        sets.append("notes=?"), params.append(str(data["notes"]))
    if not sets or not data.get("id"):
        raise ValueError("Nothing to update")
    con = sqa_jobs.db_connect()
    try:
        con.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", params + [data["id"]])
        con.commit()
    finally:
        con.close()


def _ids(data):
    ids = [str(i) for i in data.get("ids") or [] if i]
    if not ids:
        raise ValueError("No jobs selected")
    return ids


def archive_jobs(data):
    """Move jobs to the Archive (archived: true) or back (false). Status and notes are kept."""
    ids, stamp = _ids(data), (_now() if data.get("archived", True) else "")
    con = sqa_jobs.db_connect()
    try:
        con.executemany("UPDATE jobs SET archived_at=? WHERE id=?", [(stamp, i) for i in ids])
        con.commit()
    finally:
        con.close()
    return len(ids)


def delete_jobs(data):
    """Delete archived jobs for good, remembering their ids so they aren't re-added as new."""
    ids = _ids(data)
    con = sqa_jobs.db_connect()
    try:
        rows = con.execute(f"SELECT id, title, company FROM jobs WHERE archived_at!='' AND id IN "
                           f"({','.join('?' * len(ids))})", ids).fetchall()
        archived = [r["id"] for r in rows]
        # remembered by dup_key too, so the same job from another source doesn't come back either
        con.executemany("INSERT OR REPLACE INTO removed_jobs (id, removed_at, dup_key) VALUES (?,?,?)",
                        [(r["id"], _now(), sqa_jobs.dup_key(r["title"], r["company"])) for r in rows])
        con.executemany("DELETE FROM jobs WHERE id=?", [(i,) for i in archived])
        con.commit()
    finally:
        con.close()
    return len(archived)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def save_config(cfg):
    for key in ("strong_title", "weak_title", "tech_company", "exclude"):
        try:
            re.compile(cfg["filters"][key])
        except re.error as exc:
            raise ValueError(f"filters.{key} is not a valid regex: {exc}")
    if not cfg.get("search_keywords"):
        raise ValueError("Add at least one search keyword")
    names = set()
    for c in (cfg.get("companies") or {}).get("list", []):
        validate_company(c)
        if c["name"].lower() in names:
            raise ValueError(f"Two companies are named {c['name']!r}")
        names.add(c["name"].lower())
    sqa_jobs.CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def validate_company(c):
    if not str(c.get("name") or "").strip():
        raise ValueError("Every company needs a name")
    if not re.match(r"https?://[^/\s]+\.[^/\s]+", c.get("url") or ""):
        raise ValueError(f"{c['name']}: enter a full careers page URL (https://…)")
    if c.get("type") not in sqa_jobs.COMPANY_TYPES:
        raise ValueError(f"{c['name']}: unknown type {c.get('type')!r}")
    try:
        re.compile(c.get("location") or "")
    except re.error as exc:
        raise ValueError(f"{c['name']}: location filter is not a valid regex: {exc}")


# --------------------------------------------------------------------------- #
# Companies
# --------------------------------------------------------------------------- #

def company_status():
    con = sqa_jobs.db_connect()
    try:
        return {r["name"]: dict(r) for r in con.execute("SELECT * FROM company_status")}
    finally:
        con.close()


def session_source_status(key, profile):
    """Last search and login state of a source that uses your own browser session."""
    con = sqa_jobs.db_connect()
    try:
        state = sqa_jobs.meta_get(con, key, {})
    finally:
        con.close()
    try:
        import playwright  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    return {**state, "installed": installed, "has_session": (ROOT / profile).exists()}


def test_company(data):
    """Fetch one company's page now (unsaved settings allowed) without storing anything."""
    c, cfg = data["company"], data["config"]
    validate_company(c)
    try:
        jobs = sqa_jobs.fetch_company(c)
    except Exception as exc:
        return {"error": str(exc), "jobs": []}
    open_ids = {j["id"] for j in sqa_jobs.global_company_jobs(cfg, c, jobs)}  # all of them unless board is global
    jobs = [{"title": j["title"], "url": j["url"], "location": j["location"],
             "kept": sqa_jobs.is_sqa(cfg, j) and j["id"] in open_ids} for j in jobs]
    return {"error": "", "jobs": sorted(jobs, key=lambda j: not j["kept"])}


def test_filter(data):
    cfg = data["config"]
    job = {"title": data.get("title", ""), "company": data.get("company", ""),
           "category": data.get("category")}
    f = cfg["filters"]
    title, company = job["title"], job["company"]
    reasons = []
    if re.search(f["exclude"], title, re.I):
        reasons.append("title matches exclude")
    elif re.search(f["strong_title"], title, re.I):
        reasons.append("title matches strong_title")
    elif not re.search(f["weak_title"], title, re.I):
        reasons.append("title matches neither strong_title nor weak_title")
    elif re.search(f["exclude"], company, re.I):
        reasons.append("weak title, and company matches exclude")
    elif job["category"] in cfg["bdjobs"]["it_category_ids"]:
        reasons.append("weak title in a Bdjobs IT category")
    elif re.search(f["tech_company"], company, re.I):
        reasons.append("weak title, and company matches tech_company")
    else:
        reasons.append("weak title, but company doesn't look like tech")
    return {"kept": sqa_jobs.is_sqa(cfg, job), "reason": reasons[0]}


# --------------------------------------------------------------------------- #
# Scheduled task
# --------------------------------------------------------------------------- #

def _ps(script):
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-Command", script], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "PowerShell failed")
    return proc.stdout.strip()


def schedule_status():
    out = _ps(f"""
$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if (-not $t) {{ 'null'; exit }}
$i = $t | Get-ScheduledTaskInfo
$fmt = {{ param($d) if ($d -and $d.Year -gt 2000) {{ $d.ToString('s') }} else {{ $null }} }}
[pscustomobject]@{{
  state = "$($t.State)"
  interval = $t.Triggers[0].Repetition.Interval
  last_run = & $fmt $i.LastRunTime
  next_run = & $fmt $i.NextRunTime
  last_result = $i.LastTaskResult
}} | ConvertTo-Json -Compress""")
    info = json.loads(out or "null")
    if info:
        m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", info.get("interval") or "")
        info["interval_minutes"] = (int(m[1] or 0) * 1440 + int(m[2] or 0) * 60 + int(m[3] or 0)) if m else None
    return info


def schedule_action(data):
    action = data.get("action")
    if action == "install":
        minutes = int(data.get("interval_minutes") or 30)
        if not 5 <= minutes <= 1440:
            raise ValueError("Interval must be between 5 and 1440 minutes")
        _ps(f"& '{ROOT / 'install_task.ps1'}' -IntervalMinutes {minutes}")
    elif action == "enable":
        _ps(f"Enable-ScheduledTask -TaskName '{TASK_NAME}' | Out-Null")
    elif action == "disable":
        _ps(f"Disable-ScheduledTask -TaskName '{TASK_NAME}' | Out-Null")
    elif action == "remove":
        _ps(f"Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false")
    else:
        raise ValueError(f"Unknown action {action!r}")
    return schedule_status()


def read_log(lines=300):
    if not sqa_jobs.LOG_PATH.exists():
        return ""
    return "\n".join(sqa_jobs.LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _local_only(self):
        # Reject requests from other sites (DNS rebinding / cross-site form posts).
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._local_only():
            return self._send(403, {"error": "forbidden"})
        path = self.path.split("?")[0]
        try:
            if path == "/":
                return self._send(200, UI_PATH.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/jobs":
                return self._send(200, list_jobs())
            if path == "/api/config":
                return self._send(200, sqa_jobs.load_config())
            if path == "/api/run":
                return self._send(200, runner.snapshot())
            if path == "/api/schedule":
                return self._send(200, schedule_status())
            if path == "/api/log":
                return self._send(200, {"log": read_log()})
            if path == "/api/company-status":
                return self._send(200, company_status())
            if path == "/api/linkedin-posts":
                return self._send(200, session_source_status("linkedin_posts", "linkedin_session"))
            if path == "/api/jobgether":
                s = session_source_status("jobgether", "jobgether_session")
                s["uses_cookie"] = bool((sqa_jobs.load_config().get("jobgether") or {}).get("cookie"))
                s["has_session"] = s["has_session"] or s["uses_cookie"]
                return self._send(200, s)
            if path == "/api/global-status":
                con = sqa_jobs.db_connect()
                try:
                    return self._send(200, sqa_jobs.meta_get(con, "global", {}))
                finally:
                    con.close()
            self._send(404, {"error": "not found"})
        except Exception as exc:
            self._send(500, {"error": str(exc)})

    def do_POST(self):
        # The custom header forces a CORS preflight, which this server never approves.
        if not self._local_only() or self.headers.get("X-Requested-With") != "sqa-ui":
            return self._send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(length) or b"{}")
            path = self.path
            if path == "/api/job":
                update_job(data)
                return self._send(200, {"ok": True})
            if path == "/api/jobs/archive":
                return self._send(200, {"count": archive_jobs(data)})
            if path == "/api/jobs/delete":
                return self._send(200, {"count": delete_jobs(data)})
            if path == "/api/config":
                save_config(data)
                return self._send(200, {"ok": True})
            if path == "/api/filter-test":
                return self._send(200, test_filter(data))
            if path == "/api/company-test":
                return self._send(200, test_company(data))
            if path in ("/api/run", "/api/test", "/api/linkedin-login", "/api/jobgether-login"):
                started = runner.start({"/api/run": "check", "/api/test": "test", "/api/linkedin-login": "login",
                                        "/api/jobgether-login": "jobgether_login"}[path])
                return self._send(200 if started else 409, runner.snapshot())
            if path == "/api/schedule":
                return self._send(200, schedule_action(data))
            self._send(404, {"error": "not found"})
        except (ValueError, KeyError, re.error) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": str(exc)})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    sqa_jobs.db_connect().close()  # make sure the status/notes columns exist
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"SQA Job Alert UI running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
