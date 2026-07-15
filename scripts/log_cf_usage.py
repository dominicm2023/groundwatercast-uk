"""Append one UTC day's Cloudflare traffic rollup to a CSV — a usage time series.

Queries the Cloudflare GraphQL Analytics API for a single day's zone aggregate
(unique visitors, requests, bytes served, threats, and the top traffic
countries) and appends a row to ``data/model/cf_usage.csv``. Meant for a daily
cron so we build a usage history over the launch (feeds a "usage over time"
stat and the community-adoption story). Re-running for a date already logged
replaces that row rather than duplicating it, so backfills/retries are safe.

Auth (NEVER committed): reads ``CF_API_TOKEN`` + ``CF_ZONE_ID`` from an env file
(``~/.gwc-cf.env`` by default; ``KEY=value`` lines). The token needs only
**Zone → Analytics → Read** on the groundwatercast.com zone — create it at
dash.cloudflare.com → My Profile → API Tokens → Create Token → "Read analytics
and logs" template, scoped to the single zone. Read-only; it cannot change
anything. Find the Zone ID on the zone's Overview page (right rail).

Run (main env — needs ``requests``):
  .venv/bin/python -m scripts.log_cf_usage                 # yesterday (UTC)
  .venv/bin/python -m scripts.log_cf_usage --date 2026-07-06
  .venv/bin/python -m scripts.log_cf_usage --backfill 3    # last 3 full days
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = Path.home() / ".gwc-cf.env"
DEFAULT_OUT = ROOT / "data" / "model" / "cf_usage.csv"
GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
FIELDNAMES = ["date", "unique_visitors", "requests", "bytes", "threats",
              "top_countries", "fetched_at"]

# One UTC day's zone rollup. httpRequests1dGroups is the free-plan daily
# aggregate that powers the dashboard's traffic cards (uniq.uniques = "unique
# visitors"). countryMap gives per-country request counts.
_QUERY = """
query ($zone: String!, $date: String!) {
  viewer {
    zones(filter: {zoneTag: $zone}) {
      httpRequests1dGroups(limit: 1, filter: {date: $date}) {
        dimensions { date }
        uniq { uniques }
        sum {
          requests
          bytes
          threats
          countryMap { clientCountryName requests }
        }
      }
    }
  }
}
"""


def load_env(path: Path) -> dict[str, str]:
    """Parse a KEY=value env file (blank lines / #comments ignored)."""
    if not path.exists():
        raise SystemExit(f"env file not found: {path}\n"
                         "Create it with CF_API_TOKEN=... and CF_ZONE_ID=... "
                         "(see this script's docstring).")
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def fetch_day(token: str, zone: str, day: str) -> dict | None:
    """Return the parsed rollup dict for ``day`` (YYYY-MM-DD), or None if the
    zone reported no data for it. Raises on auth/transport/GraphQL errors."""
    resp = requests.post(
        GRAPHQL_URL,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"query": _QUERY, "variables": {"zone": zone, "date": day}},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    zones = payload["data"]["viewer"]["zones"]
    if not zones or not zones[0]["httpRequests1dGroups"]:
        return None
    g = zones[0]["httpRequests1dGroups"][0]
    s = g["sum"]
    countries = sorted(s.get("countryMap") or [],
                       key=lambda c: c["requests"], reverse=True)[:5]
    top = ";".join(f"{c['clientCountryName']}:{c['requests']}" for c in countries)
    return {
        "date": g["dimensions"]["date"],
        "unique_visitors": (g.get("uniq") or {}).get("uniques"),
        "requests": s.get("requests"),
        "bytes": s.get("bytes"),
        "threats": s.get("threats"),
        "top_countries": top,
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def upsert(out_path: Path, row: dict) -> None:
    """Append ``row``, replacing any existing row for the same date. Keeps the
    file sorted by date so it reads as a clean time series."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict] = {}
    if out_path.exists():
        with out_path.open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                rows[r["date"]] = r
    rows[row["date"]] = row
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        w.writeheader()
        for d in sorted(rows):
            w.writerow(rows[d])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="UTC day YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--backfill", type=int, default=0, metavar="N",
                    help="also log the N full days before --date/yesterday")
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV,
                    help=f"env file with CF_API_TOKEN/CF_ZONE_ID (default {DEFAULT_ENV})")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"output CSV (default {DEFAULT_OUT})")
    args = ap.parse_args()

    env = load_env(args.env)
    token, zone = env.get("CF_API_TOKEN"), env.get("CF_ZONE_ID")
    if not token or not zone:
        raise SystemExit(f"{args.env} must define CF_API_TOKEN and CF_ZONE_ID")

    end = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
           else date.today() - timedelta(days=1))          # default: yesterday UTC
    days = [end - timedelta(days=i) for i in range(args.backfill + 1)]

    logged = 0
    for d in sorted(days):
        iso = d.isoformat()
        try:
            row = fetch_day(token, zone, iso)
        except Exception as exc:
            print(f"  {iso}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        if row is None:
            print(f"  {iso}: no data reported (retention/too recent) — skipped")
            continue
        upsert(args.out, row)
        logged += 1
        gb = (row["bytes"] or 0) / 1e9
        print(f"  {iso}: {row['unique_visitors']} uniques, {row['requests']} requests, "
              f"{gb:.2f} GB, {row['threats']} threats | {row['top_countries']}")
    print(f"Logged {logged} day(s) → {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
