"""Local preview server for the MapLibre explorer (web/) + the artifact pack.

Serves ``web/`` at ``/`` and ``outputs/pack/`` at ``/pack`` from one origin
(so the explorer's ``packBase: "/pack"`` resolves) — the same layout a VPS
nginx/Caddy uses in production (see web/README.md). Stdlib only.

    python scripts/serve_explorer.py [--port 8080] [--pack outputs/pack]

Build the pack first if it's missing:  python -m scripts.build_artifact_pack
"""
from __future__ import annotations

import argparse
import functools
import http.server
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serve web/ at /, and /pack/* from the pack dir."""

    pack_dir: Path = ROOT / "outputs" / "pack"

    def translate_path(self, path: str) -> str:
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean == "/pack" or clean.startswith("/pack/"):
            rel = clean[len("/pack"):].lstrip("/")
            return str(self.pack_dir / rel)
        rel = clean.lstrip("/") or "index.html"
        return str(WEB / rel)

    def end_headers(self):
        # No caching in dev so edits show immediately.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--pack", default=str(ROOT / "outputs" / "pack"))
    args = ap.parse_args(argv)

    pack = Path(args.pack).resolve()
    if not (pack / "stations.geojson").exists():
        print(f"WARNING: no pack at {pack} — run `python -m scripts.build_artifact_pack` first.")

    handler = functools.partial(_Handler)
    _Handler.pack_dir = pack
    # Threaded: a page load fans out to many station JSONs; a single-threaded
    # server stalls (and the browser times out) under that concurrency.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler) as httpd:
        print(f"GroundwaterCast explorer -> http://127.0.0.1:{args.port}")
        print(f"  web : {WEB}")
        print(f"  pack: {pack}  (served at /pack)")
        print("Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
