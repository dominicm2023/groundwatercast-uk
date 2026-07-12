"""Purge the Cloudflare edge cache — the deploy step that ends stale-JS windows.

Cloudflare edge-caches ``.js``/``.css`` for ~4 h, so a deploy that changes a
script serves the OLD copy until TTL expiry unless the cache is purged. This
reads ``CF_ZONE_ID`` + ``CF_PURGE_TOKEN`` from the same env file as
``log_cf_usage`` (``~/.gwc-cf.env``; the purge token is separate from the
read-only analytics one) and purges either specific URLs or everything.

Usage:
  python -m scripts.purge_cf --files https://groundwatercast.com/valley/test/stations.js
  python -m scripts.purge_cf                 # purge everything (post-deploy)

Targeted purges are kinder to the cache (the pack and vendor files stay hot) —
prefer ``--files`` in cron; reserve purge-everything for code deploys.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_ENV = Path.home() / ".gwc-cf.env"
API = "https://api.cloudflare.com/client/v4"
MAX_FILES = 30                       # Cloudflare's per-request URL limit


def read_env(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"{path} missing — create it with CF_ZONE_ID=... and "
                         "CF_PURGE_TOKEN=... (KEY=value lines)")
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def purge(zone: str, token: str, files: list[str] | None) -> dict:
    body = {"files": files} if files else {"purge_everything": True}
    req = urllib.request.Request(
        f"{API}/zones/{zone}/purge_cache",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--files", nargs="*",
                    help="full URLs to purge (default: purge everything)")
    ap.add_argument("--env", type=Path, default=DEFAULT_ENV,
                    help=f"env file with CF_ZONE_ID/CF_PURGE_TOKEN (default {DEFAULT_ENV})")
    args = ap.parse_args()
    env = read_env(args.env)
    zone, token = env.get("CF_ZONE_ID"), env.get("CF_PURGE_TOKEN")
    if not zone or not token:
        raise SystemExit(f"{args.env} must define CF_ZONE_ID and CF_PURGE_TOKEN")

    batches = ([args.files[i:i + MAX_FILES]
                for i in range(0, len(args.files), MAX_FILES)]
               if args.files else [None])
    for batch in batches:
        res = purge(zone, token, batch)
        what = f"{len(batch)} URLs" if batch else "everything"
        if res.get("success"):
            print(f"purged {what}")
        else:
            print(f"purge FAILED ({what}): {res.get('errors')}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
