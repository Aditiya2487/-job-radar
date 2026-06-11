#!/usr/bin/env python3
"""
job_scraper.py — personal backend-job radar.

Sources (no logins, no scraping wars):
  * Greenhouse public boards   https://boards-api.greenhouse.io/v1/boards/<slug>/jobs
  * Lever public postings      https://api.lever.co/v0/postings/<slug>?mode=json
  * RemoteOK public API        https://remoteok.com/api
  * Remotive public API        https://remotive.com/api/remote-jobs

Usage:
  pip install requests
  python job_scraper.py            # fetch, filter, build dashboard.html
  python job_scraper.py --check    # verify which company slugs are valid
  python job_scraper.py --demo     # build dashboard from sample data (no network)

Each run remembers what it has shown you (seen_jobs.json) and flags
anything new since the last run at the top of the dashboard.
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import webbrowser

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

# ----------------------------------------------------------------------------
# CONFIG — edit this section, nothing else needs touching
# ----------------------------------------------------------------------------

# Title must contain at least one of these (case-insensitive).
INCLUDE_KEYWORDS = [
    # backend track
    "backend", "back-end", "back end", "software engineer", "sde",
    "python", "java developer", "platform engineer", "api",
    # full stack
    "full stack", "fullstack", "full-stack",
    # sdet / qa automation track
    "sdet", "qa automation", "automation engineer", "test engineer",
    "qa engineer", "quality engineer", "automation tester",
]

# Title containing any of these is dropped.
EXCLUDE_KEYWORDS = [
    "senior staff", "principal", "director", "manager", "intern",
    "manual test", "ios", "android", "salesforce",
    "staff engineer",  # remove this line if you want staff roles too
]

# Location must contain one of these (or be empty/remote-friendly).
LOCATION_KEYWORDS = [
    "india", "remote", "bangalore", "bengaluru", "gurgaon", "gurugram",
    "noida", "delhi", "hyderabad", "pune", "mumbai", "anywhere",
]

# Company board slugs. Verify with:  python job_scraper.py --check
# Find a slug by visiting a company's careers page and looking at the URL:
#   boards.greenhouse.io/<slug>   or   jobs.lever.co/<slug>
GREENHOUSE_COMPANIES = [
    "postman",
    "hasura",
    "browserstack",
    "cleartax",
    "rippling",
]
LEVER_COMPANIES = [
    "razorpay",
    "groww",
    "zeptonow",
    "dream11",
]

USE_REMOTEOK = True    # remote-global board; filtered by tags/keywords
USE_REMOTIVE = True    # remote-global board; searched by "backend"

# JobSpy (pip install python-jobspy) — covers LinkedIn, Naukri, Indeed, etc.
# Heavier than the APIs above; LinkedIn may rate-limit from datacenter IPs.
USE_JOBSPY = True
JOBSPY_SITES = ["naukri", "indeed", "linkedin", "glassdoor"]
JOBSPY_SEARCHES = [
    "backend engineer",
    "full stack developer",
    "sdet",
    "qa automation engineer",
]
JOBSPY_LOCATION = "India"
JOBSPY_RESULTS_PER_SITE = 20
JOBSPY_HOURS_OLD = 24   # only postings from the last day (script runs 2x/day)

SEEN_FILE = "seen_jobs.json"
DASHBOARD_FILE = os.environ.get("DASHBOARD_OUT", "dashboard.html")
IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"
TIMEOUT = 15
HEADERS = {"User-Agent": "Mozilla/5.0 (personal job tracker script)"}

# ----------------------------------------------------------------------------
# Fetchers — each returns a list of normalized job dicts
# ----------------------------------------------------------------------------


def _job(jid, title, company, location, url, posted, source):
    return {
        "id": f"{source}:{jid}",
        "title": title.strip(),
        "company": company.strip(),
        "location": (location or "").strip(),
        "url": url,
        "posted": posted or "",
        "source": source,
    }


def fetch_greenhouse(slug):
    r = requests.get(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        headers=HEADERS, timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append(_job(
            j["id"], j.get("title", ""), slug,
            (j.get("location") or {}).get("name", ""),
            j.get("absolute_url", ""),
            (j.get("updated_at") or "")[:10],
            "greenhouse",
        ))
    return out


def fetch_lever(slug):
    r = requests.get(
        f"https://api.lever.co/v0/postings/{slug}?mode=json",
        headers=HEADERS, timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for j in r.json():
        ts = j.get("createdAt")
        posted = dt.datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else ""
        out.append(_job(
            j["id"], j.get("text", ""), slug,
            (j.get("categories") or {}).get("location", ""),
            j.get("hostedUrl", ""), posted, "lever",
        ))
    return out


def fetch_remoteok():
    r = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        if not isinstance(j, dict) or "position" not in j:
            continue  # first element is a legal notice
        out.append(_job(
            j.get("id", j.get("slug", "")), j.get("position", ""),
            j.get("company", "?"), j.get("location", "Remote"),
            j.get("url", ""), (j.get("date") or "")[:10], "remoteok",
        ))
    return out


def fetch_remotive():
    r = requests.get(
        "https://remotive.com/api/remote-jobs",
        params={"search": "backend"}, headers=HEADERS, timeout=TIMEOUT,
    )
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append(_job(
            j.get("id", ""), j.get("title", ""), j.get("company_name", "?"),
            j.get("candidate_required_location", "Remote"),
            j.get("url", ""), (j.get("publication_date") or "")[:10], "remotive",
        ))
    return out


def fetch_jobspy():
    """LinkedIn + Naukri + Indeed + Glassdoor via the python-jobspy library."""
    from jobspy import scrape_jobs  # imported here so the script runs without it
    out = []
    for term in JOBSPY_SEARCHES:
        try:
            df = scrape_jobs(
                site_name=JOBSPY_SITES,
                search_term=term,
                location=JOBSPY_LOCATION,
                results_wanted=JOBSPY_RESULTS_PER_SITE,
                hours_old=JOBSPY_HOURS_OLD,
                country_indeed="India",
                verbose=0,
            )
        except Exception as e:
            print(f"    jobspy '{term}' failed ({e})")
            continue
        for _, r in df.iterrows():
            url = str(r.get("job_url") or "")
            if not url:
                continue
            posted = r.get("date_posted")
            out.append(_job(
                url,                       # URL doubles as the stable id
                str(r.get("title") or ""),
                str(r.get("company") or "?"),
                str(r.get("location") or ""),
                url,
                str(posted)[:10] if posted is not None else "",
                str(r.get("site") or "jobspy"),
            ))
    return out


# ----------------------------------------------------------------------------
# Filtering + seen-tracking
# ----------------------------------------------------------------------------


def matches(job):
    title = job["title"].lower()
    loc = job["location"].lower()
    if not any(k in title for k in INCLUDE_KEYWORDS):
        return False
    if any(k in title for k in EXCLUDE_KEYWORDS):
        return False
    if loc and not any(k in loc for k in LOCATION_KEYWORDS):
        return False
    return True


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=1)


# ----------------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------------

CSS = """
:root{
  --bg:#101418; --panel:#171d24; --panel2:#1c242d; --line:#26303b;
  --ink:#dde4ea; --dim:#8696a5; --amber:#f0a830; --amber-dim:#8a6420;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);
  font:15px/1.55 "Segoe UI",system-ui,sans-serif;padding:32px 20px 64px}
.wrap{max-width:880px;margin:0 auto}
header{border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:6px}
h1{font-size:21px;font-weight:600;letter-spacing:.3px}
h1 span{color:var(--amber)}
.meta{color:var(--dim);font:12.5px/1.6 ui-monospace,Consolas,monospace;margin-top:4px}
.strip{display:flex;gap:26px;padding:14px 0;border-bottom:1px solid var(--line);
  margin-bottom:26px;font:12.5px ui-monospace,Consolas,monospace;color:var(--dim)}
.strip b{display:block;font-size:19px;color:var(--ink);font-weight:600}
.strip .new b{color:var(--amber)}
h2{font-size:12px;font-weight:600;color:var(--dim);text-transform:uppercase;
  letter-spacing:.12em;margin:30px 0 12px}
.card{display:block;text-decoration:none;color:inherit;background:var(--panel);
  border:1px solid var(--line);border-left:3px solid var(--line);
  border-radius:6px;padding:13px 16px;margin-bottom:9px;
  transition:background .12s,border-color .12s}
.card:hover{background:var(--panel2);border-color:#34414f}
.card:focus-visible{outline:2px solid var(--amber);outline-offset:2px}
.card.new{border-left-color:var(--amber)}
.card .t{font-weight:600;font-size:15.5px}
.card .t .tag{font:10.5px ui-monospace,monospace;color:var(--bg);
  background:var(--amber);border-radius:3px;padding:1px 6px;
  vertical-align:2px;margin-left:8px;letter-spacing:.05em}
.card .s{color:var(--dim);font:12.5px ui-monospace,Consolas,monospace;margin-top:3px}
.card .s em{font-style:normal;color:var(--amber-dim)}
.empty{color:var(--dim);padding:18px 2px;font-size:14px}
@media(prefers-reduced-motion:reduce){.card{transition:none}}
"""


def build_dashboard(jobs, new_ids, errors):
    now = dt.datetime.now().strftime("%a %d %b %Y, %H:%M")
    new = [j for j in jobs if j["id"] in new_ids]
    old = [j for j in jobs if j["id"] not in new_ids]
    key = lambda j: j["posted"] or "0000"
    new.sort(key=key, reverse=True)
    old.sort(key=key, reverse=True)
    sources = len({j["source"] for j in jobs})

    def card(j, is_new):
        tag = '<span class="tag">NEW</span>' if is_new else ""
        return (
            f'<a class="card{" new" if is_new else ""}" href="{html.escape(j["url"])}" '
            f'target="_blank" rel="noopener">'
            f'<div class="t">{html.escape(j["title"])}{tag}</div>'
            f'<div class="s"><em>{html.escape(j["company"])}</em> · '
            f'{html.escape(j["location"] or "location n/a")} · '
            f'{html.escape(j["posted"] or "date n/a")} · {j["source"]}</div></a>'
        )

    err_note = ""
    if errors:
        items = "; ".join(html.escape(e) for e in errors)
        err_note = f'<div class="meta">skipped: {items}</div>'

    new_html = "".join(card(j, True) for j in new) or \
        '<div class="empty">Nothing new since the last run.</div>'
    old_html = "".join(card(j, False) for j in old) or \
        '<div class="empty">No earlier matches on record.</div>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job Radar</title><style>{CSS}</style></head><body><div class="wrap">
<header><h1>Job Radar <span>// night shift</span></h1>
<div class="meta">last sweep: {now}</div>{err_note}</header>
<div class="strip">
  <div class="new"><b>{len(new)}</b>new this run</div>
  <div><b>{len(jobs)}</b>total matches</div>
  <div><b>{sources}</b>sources</div>
</div>
<h2>New since last run</h2>{new_html}
<h2>Previously seen</h2>{old_html}
</div></body></html>"""


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

SAMPLE = [
    _job(1, "Backend Engineer (Python)", "samplecorp", "Bengaluru, India",
         "https://example.com/1", "2026-06-10", "greenhouse"),
    _job(2, "Software Engineer - Platform", "demoworks", "Remote, India",
         "https://example.com/2", "2026-06-08", "lever"),
    _job(3, "SDE II, Payments", "payco", "Gurugram, India",
         "https://example.com/3", "2026-06-01", "lever"),
]


def check_slugs():
    print("Checking board slugs...\n")
    for slug in GREENHOUSE_COMPANIES:
        try:
            n = len(fetch_greenhouse(slug))
            print(f"  greenhouse/{slug:<16} OK   ({n} postings)")
        except Exception as e:
            print(f"  greenhouse/{slug:<16} FAIL ({e})")
    for slug in LEVER_COMPANIES:
        try:
            n = len(fetch_lever(slug))
            print(f"  lever/{slug:<21} OK   ({n} postings)")
        except Exception as e:
            print(f"  lever/{slug:<21} FAIL ({e})")


def run(demo=False):
    jobs, errors = [], []
    if demo:
        jobs = SAMPLE
    else:
        tasks = [(f"greenhouse/{s}", lambda s=s: fetch_greenhouse(s)) for s in GREENHOUSE_COMPANIES]
        tasks += [(f"lever/{s}", lambda s=s: fetch_lever(s)) for s in LEVER_COMPANIES]
        if USE_REMOTEOK:
            tasks.append(("remoteok", fetch_remoteok))
        if USE_REMOTIVE:
            tasks.append(("remotive", fetch_remotive))
        if USE_JOBSPY:
            tasks.append(("jobspy(" + "+".join(JOBSPY_SITES) + ")", fetch_jobspy))
        for name, fn in tasks:
            try:
                got = fn()
                jobs += got
                print(f"  {name:<24} {len(got)} postings")
            except Exception as e:
                errors.append(name)
                print(f"  {name:<24} skipped ({e})")

    matched, seen_ids = [], set()
    for j in jobs:
        if j["id"] in seen_ids:
            continue
        seen_ids.add(j["id"])
        if matches(j):
            matched.append(j)

    seen = load_seen()
    today = dt.date.today().isoformat()
    new_ids = {j["id"] for j in matched if j["id"] not in seen}
    for j in matched:
        seen.setdefault(j["id"], today)
    save_seen(seen)

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(build_dashboard(matched, new_ids, errors))

    print(f"\n{len(matched)} matches ({len(new_ids)} new) -> {DASHBOARD_FILE}")
    if not IN_CI:
        try:
            webbrowser.open("file://" + os.path.abspath(DASHBOARD_FILE))
        except Exception:
            pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Personal job radar")
    p.add_argument("--check", action="store_true", help="verify board slugs")
    p.add_argument("--demo", action="store_true", help="build dashboard from sample data")
    a = p.parse_args()
    if a.check:
        check_slugs()
    else:
        run(demo=a.demo)
