#!/usr/bin/env python3
"""
Algorithm Watch — Lane B: the announcement log.

Weekly. Pulls ONLY Google's own confirmed announcements and appends them to
_search-updates.json. Reads; never edits the routine, the skill or the prompts.

Runs inside the WEEKLY SITE HEALTH routine, not the monthly SEO routine — it
already runs on a schedule and has its own repo and token. Findings hand off to
the monthly routine through Drive, the same read-only path the site-health file
already uses.

    python3 scripts/algorithm_watch.py --out _search-updates.json
    python3 scripts/algorithm_watch.py --out _search-updates.json --dry-run

NETWORK: both hosts must be on the environment's Custom network allowlist —
status.search.google.com and developers.google.com. Neither is in the default
Trusted list, and without them the proxy answers 403 to CONNECT before the
request is made. That is the exact failure this script is built to make loud
rather than silent, so it is reported as an allowlist problem by name.

WHY THIS EXITS NON-ZERO ON A FETCH FAILURE, unlike backlinks_fetch.py:
that script must never abort a client's monthly report, so it always exits 0.
This one is the opposite. A check that quietly returns nothing looks exactly
like a quiet month, and a quiet month is the single conclusion this log exists
to disprove. Silence has to be earned, so an unreachable or unparseable source
is a hard failure that leaves the log untouched.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

TIMEOUT = 45
UA = "Mozilla/5.0 (compatible; AlgorithmWatch/1.0; +weekly-site-health)"

# Source 1 (priority): the Search Status Dashboard's machine-readable history.
# The dashboard's own footer publishes this alongside a schema, so it is the
# documented interface rather than the HTML table, which is a rendering of it.
INCIDENTS_URL = "https://status.search.google.com/incidents.json"
INCIDENTS_SCHEMA_URL = "https://status.search.google.com/incidents.schema.json"

# Source 2: Search Central. Official, but prose — no start/end dates, and most
# posts are events or documentation, not ranking changes. Kept in a separate
# array for that reason; see ANNOUNCEMENTS below.
BLOG_FEED_URL = "https://developers.google.com/search/blog/feed.xml"

# Incidents carry a service key. Ranking is what the spec asks for. Serving
# outages ride in the same feed under pKUD9XkLn3TBLquSpQMD and are EXCLUDED
# deliberately, not by accident — a serving disruption is a plausible answer to
# "why did traffic dip", so if that is wanted later, add its key here rather
# than widening the filter to everything.
RANKING_SERVICE_KEY = "rGHU1u87FJnkP6W2GwMi"

# The dashboard carries years of history and the blog feed is a rolling window.
# Parsing far fewer than this means the endpoint moved or the shape changed —
# NOT that Google has been quiet. Deliberately well under the live counts (10
# incidents, 10 blog items when this was written) so ordinary variation does
# not trip it, but zero or near-zero always does.
MIN_PLAUSIBLE_INCIDENTS = 4
MIN_PLAUSIBLE_BLOG_ITEMS = 3

# Google embeds bare URLs in angle brackets inside incident text.
ANGLE_URL_RE = re.compile(r"\s*<https?://[^>]+>")


class SourceFailure(RuntimeError):
    """A source did not answer, or answered with something unusable."""


def fetch(url: str) -> tuple[bytes, int]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read(), resp.status
    except urllib.error.HTTPError as err:
        raise SourceFailure(f"{url} answered HTTP {err.code}") from err
    except urllib.error.URLError as err:
        reason = str(getattr(err, "reason", err))
        if "403" in reason or "CONNECT" in reason.upper():
            raise SourceFailure(
                f"cannot reach {url} — the proxy refused CONNECT. The host is "
                f"almost certainly missing from the environment's Custom network "
                f"allowlist; this is an environment setting, not a code fault."
            ) from err
        raise SourceFailure(f"cannot reach {url} — {reason}") from err
    except TimeoutError as err:
        raise SourceFailure(f"{url} timed out after {TIMEOUT}s") from err


def classify(name: str) -> str:
    """Google's own naming is consistent enough to type from, and nothing else
    in the payload distinguishes a core update from a spam update."""
    low = name.lower()
    for needle, kind in (
        ("core update", "core"),
        ("spam update", "spam"),
        ("reviews update", "reviews"),
        ("helpful content", "helpful-content"),
        ("discover", "discover"),
        ("page experience", "page-experience"),
        ("link spam", "spam"),
    ):
        if needle in low:
            return kind
    return "other"


def google_wording(incident: dict[str, Any]) -> str:
    """Google's own words, quoted sparingly. `updates` is newest-first, so the
    LAST entry is the original release note — the one that says what shipped,
    rather than 'the rollout was complete'."""
    updates = incident.get("updates") or []
    text = (updates[-1].get("text") if updates else "") or ""
    return ANGLE_URL_RE.sub("", text).strip()


def parse_incidents(raw: bytes) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise SourceFailure(f"{INCIDENTS_URL} did not return valid JSON — {err}") from err
    if not isinstance(data, list):
        raise SourceFailure(
            f"{INCIDENTS_URL} returned {type(data).__name__}, expected a list — "
            "the published schema has changed."
        )
    if len(data) < MIN_PLAUSIBLE_INCIDENTS:
        raise SourceFailure(
            f"{INCIDENTS_URL} returned only {len(data)} incident(s). The dashboard "
            f"carries years of history, so fewer than {MIN_PLAUSIBLE_INCIDENTS} means "
            "the endpoint or its shape changed. Treating this as a fault, NOT as a "
            "quiet period."
        )
    return data


def to_entry(incident: dict[str, Any]) -> dict[str, Any]:
    name = (incident.get("external_desc") or "").strip()
    began = (incident.get("begin") or "")[:10]
    ended = (incident.get("end") or "")[:10] or None
    return {
        "id": incident.get("id"),
        "name": name,
        "type": classify(name),
        "started": began,
        "ended": ended,
        "ongoing": ended is None,
        "source_url": f"https://status.search.google.com/{incident.get('uri', '')}".rstrip("/"),
        "summary": google_wording(incident),
        "first_logged": dt.date.today().isoformat(),
    }


def parse_blog(raw: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as err:
        raise SourceFailure(f"{BLOG_FEED_URL} did not return parseable XML — {err}") from err
    items = root.findall("./channel/item")
    if len(items) < MIN_PLAUSIBLE_BLOG_ITEMS:
        raise SourceFailure(
            f"{BLOG_FEED_URL} returned only {len(items)} item(s) — expected a rolling "
            f"window of at least {MIN_PLAUSIBLE_BLOG_ITEMS}. Treating this as a fault, "
            "NOT as a quiet period."
        )
    out = []
    for item in items:
        def text(tag: str) -> str:
            node = item.find(tag)
            return (node.text or "").strip() if node is not None else ""
        published = ""
        raw_date = text("pubDate")
        if raw_date:
            try:
                from email.utils import parsedate_to_datetime
                published = parsedate_to_datetime(raw_date).date().isoformat()
            except (TypeError, ValueError):
                published = ""
        out.append({
            "id": text("guid") or text("link"),
            "title": text("title"),
            "published": published,
            "source_url": text("link"),
            "first_logged": dt.date.today().isoformat(),
        })
    return out


def load_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"updates": [], "announcements": [], "checks": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise SourceFailure(
            f"{path} exists but is not valid JSON — {err}. Refusing to overwrite it; "
            "the log is append-only and its history is the whole point."
        ) from err
    for key in ("updates", "announcements", "checks"):
        data.setdefault(key, [])
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Algorithm Watch — Lane B announcement log.")
    ap.add_argument("--out", default="_search-updates.json", help="the append-only log")
    ap.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    args = ap.parse_args()

    out_path = Path(args.out)
    checked_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"Algorithm Watch — Lane B  {checked_at}")

    try:
        log = load_log(out_path)

        raw_inc, status_inc = fetch(INCIDENTS_URL)
        incidents = parse_incidents(raw_inc)
        ranking = [i for i in incidents if i.get("service_key") == RANKING_SERVICE_KEY]
        print(f"  dashboard  HTTP {status_inc}  {len(incidents)} incident(s), "
              f"{len(ranking)} ranking")

        raw_blog, status_blog = fetch(BLOG_FEED_URL)
        posts = parse_blog(raw_blog)
        print(f"  blog       HTTP {status_blog}  {len(posts)} item(s)")

    except SourceFailure as err:
        # Loud, and the log is left exactly as it was. A partial write here would
        # be indistinguishable from a genuinely quiet week next time someone looks.
        print(f"  FAILED   {err}", file=sys.stderr)
        print("  Lane B did NOT run. The log is unchanged — do not read its silence "
              "as 'no updates'.", file=sys.stderr)
        return 1

    known_updates = {u.get("id") for u in log["updates"]}
    known_posts = {a.get("id") for a in log["announcements"]}

    new_updates = [to_entry(i) for i in ranking if i.get("id") not in known_updates]
    new_posts = [p for p in posts if p.get("id") not in known_posts]

    # Append only. Sorted newest-first for reading; never rewritten in place.
    log["updates"] = sorted(log["updates"] + new_updates,
                            key=lambda u: u.get("started") or "", reverse=True)
    log["announcements"] = sorted(log["announcements"] + new_posts,
                                  key=lambda a: a.get("published") or "", reverse=True)

    # The heartbeat. This is what makes "no updates this week" a provable claim
    # rather than an absence of evidence: every run leaves a dated record that
    # both sources answered and how much they carried. A gap in `checks` means
    # the watch stopped, which is a different problem from Google being quiet,
    # and without this row the two are indistinguishable.
    log["checks"].append({
        "checked_at": checked_at,
        "dashboard": {"url": INCIDENTS_URL, "http": status_inc,
                      "total_seen": len(incidents), "ranking_seen": len(ranking),
                      "new_appended": len(new_updates)},
        "blog": {"url": BLOG_FEED_URL, "http": status_blog,
                 "total_seen": len(posts), "new_appended": len(new_posts)},
    })

    for entry in new_updates:
        window = entry["started"] + (f" to {entry['ended']}" if entry["ended"] else " — ongoing")
        print(f"  + UPDATE   {entry['name']}  [{entry['type']}]  {window}")
    for post in new_posts:
        print(f"  + post     {post['published']}  {post['title'][:64]}")
    if not new_updates and not new_posts:
        print("  no new entries — both sources answered and carried nothing new")

    if args.dry_run:
        print("  [dry-run] nothing written")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  wrote {out_path} — {len(log['updates'])} update(s), "
          f"{len(log['announcements'])} announcement(s), {len(log['checks'])} check(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
