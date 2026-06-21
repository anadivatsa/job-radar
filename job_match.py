#!/usr/bin/env python3
"""
job_match.py — Daily job-board scanner for Neo.

Fetches postings from Greenhouse/Lever company boards, scores them against
a keyword profile, deduplicates via SQLite, and sends Telegram alerts for
new matches above threshold.

CLI:
  python3 job_match.py --check-sources          # verify connectivity for all boards
  python3 job_match.py --run-once               # fetch/score/dedup/mark-seen, no alert
  python3 job_match.py --run-once --notify      # same + send Telegram alerts

Scheduled entry point (imported by tasks/job_scan.py):
  from job_match import run_daily_scan
  run_daily_scan()   # equivalent to --run-once --notify
"""

import argparse
import html as html_mod
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

log = logging.getLogger("job_match")

_BASE        = Path(__file__).parent
_SOURCES     = _BASE / "sources.json"
_PROFILE     = _BASE / "profile.json"
_DB          = _BASE / "data" / "jobs.db"
_NOTIFIER    = _BASE / "notifier.env"

load_dotenv(_NOTIFIER)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Neo-JobBot/1.0; personal automation)",
    "Accept":     "application/json",
}
_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html_mod.unescape(text)
    return " ".join(text.split()).lower()


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch_greenhouse(token: str, company: str, region: str = "") -> list[dict]:
    host = "boards-api.eu.greenhouse.io" if region == "eu" else "boards-api.greenhouse.io"
    url  = f"https://{host}/v1/boards/{token}/jobs?content=true"
    r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    out  = []
    for j in jobs:
        out.append({
            "uid":         f"gh:{token}:{j['id']}",
            "title":       j.get("title", ""),
            "company":     company,
            "location":    j.get("location", {}).get("name", ""),
            "description": _strip_html(j.get("content", "")),
            "url":         j.get("absolute_url", ""),
        })
    return out


def fetch_lever(token: str, company: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r   = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    jobs = r.json()
    out  = []
    for j in jobs:
        cats = j.get("categories", {})
        desc = (j.get("descriptionPlain") or "") + " " + (j.get("additionalPlain") or "")
        out.append({
            "uid":         f"lv:{token}:{j['id']}",
            "title":       j.get("text", ""),
            "company":     company,
            "location":    cats.get("location", ""),
            "description": desc.lower(),
            "url":         j.get("applyUrl") or j.get("hostedUrl", ""),
        })
    return out


def _fetch_source(src: dict) -> list[dict]:
    platform = src["platform"]
    token    = src["token"]
    company  = src.get("company", token.title())
    region   = src.get("region", "")
    if platform == "greenhouse":
        return fetch_greenhouse(token, company, region)
    if platform == "lever":
        return fetch_lever(token, company)
    raise ValueError(f"Unknown platform: {platform}")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_job(job: dict, profile: dict) -> tuple[int, list[str]]:
    """Return (score 0-100, top reasons list)."""
    title_lower = job["title"].lower()
    desc_lower  = job["description"]   # already lowercased by fetchers
    loc_lower   = (job["location"] or "").lower()

    # Exclusion — immediate zero
    for excl in profile.get("title_exclude", []):
        if excl.lower() in title_lower:
            return 0, []

    reasons = []
    score   = 0

    # Title: take the single highest-scoring match
    best_t_score, best_t_phrase = 0, None
    for kw in profile.get("title_keywords", []):
        if kw["phrase"].lower() in title_lower and kw["score"] > best_t_score:
            best_t_score  = kw["score"]
            best_t_phrase = kw["phrase"]
    if best_t_phrase:
        score += best_t_score
        reasons.append(best_t_phrase)

    # Description: sum all matches
    for kw in profile.get("description_keywords", []):
        if kw["phrase"].lower() in desc_lower:
            score += kw["score"]
            reasons.append(kw["phrase"])

    # Location: take the single highest-scoring match
    best_l_score, best_l_place = 0, None
    for loc in profile.get("location_bonus", []):
        if loc["place"].lower() in loc_lower and loc["score"] > best_l_score:
            best_l_score = loc["score"]
            best_l_place = loc["place"]
    if best_l_place:
        score += best_l_score
        reasons.append(best_l_place)

    return min(100, score), reasons[:5]


# ---------------------------------------------------------------------------
# SQLite dedup
# ---------------------------------------------------------------------------

def _open_db() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            uid        TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL,
            title      TEXT,
            company    TEXT,
            score      INTEGER
        )
    """)
    conn.commit()
    return conn


def _is_seen(conn: sqlite3.Connection, uid: str) -> bool:
    return conn.execute("SELECT 1 FROM seen_jobs WHERE uid=?", (uid,)).fetchone() is not None


def _mark_seen(conn: sqlite3.Connection, job: dict, score: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs (uid, first_seen, title, company, score) VALUES (?,?,?,?,?)",
        (job["uid"], datetime.now(timezone.utc).isoformat(), job["title"], job["company"], score),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("job_match: Telegram not configured — TELEGRAM_BOT_TOKEN/CHAT_ID missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        log.error("job_match: Telegram send failed: %s", exc)
        return False


def _format_message(job: dict, score: int, reasons: list[str]) -> str:
    title    = job["title"]
    company  = job["company"]
    location = job["location"] or "—"
    url      = job["url"]

    # Split reasons: location reasons (last one if it matches location_bonus places) vs keyword reasons
    why_parts = [r for r in reasons if r not in
                 ("bangalore","bengaluru","mumbai","india","gurgaon","gurugram",
                  "hyderabad","pune","remote","uk","london","us","new york")]
    why = " · ".join(why_parts[:4]) if why_parts else "—"

    lines = [
        f"🎯 <b>Job Match — {score}/100</b>",
        "",
        f"<b>{title}</b>",
        f"{company}  ·  {location}",
        f"Why: {why}",
    ]
    if url:
        lines.append(f'\n👉 <a href="{url}">Apply now</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_daily_scan(notify: bool = True) -> dict:
    """
    Full scan: fetch → score → dedup → (optionally) alert.
    Returns summary dict. Called by tasks/job_scan.py and CLI.
    """
    sources   = json.loads(_SOURCES.read_text())
    profile   = json.loads(_PROFILE.read_text())
    threshold = profile.get("threshold", 40)
    conn      = _open_db()

    total_fetched = 0
    new_matches   = []
    errors        = []

    for src in sources:
        company = src.get("company", src["token"].title())
        try:
            jobs = _fetch_source(src)
            log.info("job_match: %s → %d jobs", company, len(jobs))
            total_fetched += len(jobs)

            for job in jobs:
                score, reasons = score_job(job, profile)
                if score < threshold:
                    continue
                if _is_seen(conn, job["uid"]):
                    log.debug("job_match: already seen %s", job["uid"])
                    continue
                _mark_seen(conn, job, score)
                new_matches.append((job, score, reasons))
                log.info("job_match: MATCH %d/100 — %s @ %s [%s]",
                         score, job["title"], company, job["location"])

        except requests.HTTPError as exc:
            msg = f"{company}: HTTP {exc.response.status_code}"
            log.error("job_match: %s", msg)
            errors.append(msg)
        except Exception as exc:
            msg = f"{company}: {exc}"
            log.error("job_match: %s", msg)
            errors.append(msg)

    conn.close()

    alerts_sent = 0
    if notify and new_matches:
        for job, score, reasons in new_matches:
            msg = _format_message(job, score, reasons)
            if _send_telegram(msg):
                alerts_sent += 1
                log.info("job_match: Telegram alert sent for %s", job["title"])
    elif notify and not new_matches:
        log.info("job_match: no new matches above threshold — no alerts sent")

    summary = {
        "fetched":      total_fetched,
        "new_matches":  len(new_matches),
        "alerts_sent":  alerts_sent,
        "errors":       errors,
        "matches":      [(j["title"], j["company"], j["location"], sc) for j, sc, _ in new_matches],
    }
    log.info("job_match: scan done — %d fetched, %d new, %d alerts, %d errors",
             total_fetched, len(new_matches), alerts_sent, len(errors))
    return summary


# ---------------------------------------------------------------------------
# CLI modes
# ---------------------------------------------------------------------------

def cmd_check_sources() -> None:
    sources = json.loads(_SOURCES.read_text())
    print(f"\n{'Company':<20} {'Platform':<12} {'Token':<20} {'Status':<8} {'Jobs':>5}")
    print("─" * 70)
    ok = 0
    for src in sources:
        platform = src["platform"]
        token    = src["token"]
        company  = src.get("company", token.title())
        region   = src.get("region", "")
        try:
            if platform == "greenhouse":
                host = "boards-api.eu.greenhouse.io" if region == "eu" else "boards-api.greenhouse.io"
                url  = f"https://{host}/v1/boards/{token}/jobs"
                r    = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
                r.raise_for_status()
                count = len(r.json().get("jobs", []))
                print(f"{company:<20} {platform:<12} {token:<20} {'OK':<8} {count:>5}")
                ok += 1
            elif platform == "lever":
                url   = f"https://api.lever.co/v0/postings/{token}?mode=json"
                r     = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
                r.raise_for_status()
                count = len(r.json())
                print(f"{company:<20} {platform:<12} {token:<20} {'OK':<8} {count:>5}")
                ok += 1
            else:
                print(f"{company:<20} {platform:<12} {token:<20} {'SKIP':<8} {'?':>5}")
        except requests.HTTPError as exc:
            print(f"{company:<20} {platform:<12} {token:<20} {exc.response.status_code:<8} {'ERR':>5}")
        except Exception as exc:
            short = str(exc)[:30]
            print(f"{company:<20} {platform:<12} {token:<20} {'FAIL':<8} {short}")
    print(f"\n{ok}/{len(sources)} sources reachable.\n")


def cmd_run_once(notify: bool) -> None:
    profile   = json.loads(_PROFILE.read_text())
    threshold = profile.get("threshold", 40)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"\nThreshold: {threshold}/100  |  notify={notify}")
    print("=" * 60)

    summary = run_daily_scan(notify=notify)

    print(f"\n{'─'*60}")
    print(f"Fetched:     {summary['fetched']} jobs total")
    print(f"New matches: {summary['new_matches']}")
    print(f"Alerts sent: {summary['alerts_sent']}")
    if summary["errors"]:
        print(f"Errors ({len(summary['errors'])}):")
        for e in summary["errors"]:
            print(f"  • {e}")
    if summary["matches"]:
        print("\nMatches above threshold:")
        for title, company, loc, score in summary["matches"]:
            print(f"  [{score:>3}/100]  {title}  —  {company}  ({loc})")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Neo job-board scanner")
    parser.add_argument("--check-sources", action="store_true",
                        help="Test connectivity for all configured job boards")
    parser.add_argument("--run-once", action="store_true",
                        help="Run a full scan (fetch/score/dedup). Marks new matches as seen.")
    parser.add_argument("--notify", action="store_true",
                        help="Send Telegram alerts for new matches (use with --run-once)")
    args = parser.parse_args()

    if args.check_sources:
        cmd_check_sources()
    elif args.run_once:
        cmd_run_once(notify=args.notify)
    else:
        parser.print_help()
        sys.exit(1)
