"""Per-borehole Open Graph share cards (1200x630 PNG) — Phase-2 step 3.

For every station in the artifact pack, render a STATUS-NEUTRAL share card:
borehole name, county/aquifer, a 12-month observed-level sparkline, and the
baked honesty caveat strip. Status-neutral by design (PHASE2-SEO-SPEC §6):
social scrapers cache images for weeks, so a card that said "below normal"
would routinely be stale — the live status belongs in og:title/description
and on the page, never baked into the image.

Stability / cache-busting: the sparkline is cut at the CURRENT MONTH START,
so a card's SVG (hence its content-hash filename share.<hash>.png) changes
only at month boundaries or when the template/caveat changes — hashed
filenames bust scraper caches exactly when the content really changed, and
the sitemap's lastmod anti-churn is not defeated by daily pixel noise.

Rendering: resvg_py (a self-contained wheel) with the repo-shipped DejaVu
fonts (data/fonts/ — the VPS has no fontconfig; shipping the fonts makes the
render deterministic on every machine). If resvg_py is missing the stage
prints a loud warning and exits 0 WITHOUT a manifest — build_seo_stubs then
simply omits og:image, which is the pre-cards behaviour, never a broken URL.

Outputs:
  web/b/<slug>/share.<hash12>.png     one per station (old hashes cleaned)
  web/og/default.png (+ hashed twin)  status-neutral site-wide fallback card
  web/apple-touch-icon.png            180x180 raster of the favicon
  outputs/og_cards.json               {slug: filename} manifest for the stubs

Wired into run_chain after build_artifact_pack and BEFORE build_seo_stubs.
Pure read of outputs/pack/stations/*.json; stdlib + resvg_py only.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts.seo_common import esc, slug  # noqa: E402
from scripts.geo_region import region_for  # noqa: E402

PACK_DIR = _ROOT / "outputs" / "pack" / "stations"
OUT_DIR = _ROOT / "web" / "b"
OG_DIR = _ROOT / "web" / "og"
MANIFEST = _ROOT / "outputs" / "og_cards.json"
FONT_DIR = _ROOT / "data" / "fonts"
FAVICON = _ROOT / "web" / "favicon.svg"
TOUCH_ICON = _ROOT / "web" / "apple-touch-icon.png"

# Bump to force a full re-render of every card (template or caveat change).
TEMPLATE_VERSION = 1
CAVEAT = "Indicative, experimental — not a flood or drought warning · England only"

W, H = 1200, 630
INK = "#1a3a5c"
TEXT = "#2b3138"
MUTED = "#6b7280"
LINE = "#33414f"
BG = "#f7f8fa"
PANEL = "#ffffff"
ACCENT = "#1f77b4"


def _spark_points(series, x0, y0, w, h):
    """Downsampled 12-month sparkline polyline points, cut at the current
    month start (stability: the card changes monthly, not daily)."""
    cut = date.today().replace(day=1).isoformat()
    pts = [(d, v) for d, v in series if d < cut and v is not None]
    pts = pts[-366:]
    if len(pts) < 2:
        return None
    step = max(1, len(pts) // 60)
    pts = pts[::step]
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        lo -= 0.5
        hi += 0.5
    out = []
    n = len(pts)
    for i, (_, v) in enumerate(pts):
        x = x0 + (i / (n - 1)) * w
        y = y0 + (1 - (v - lo) / (hi - lo)) * h
        out.append(f"{x:.1f},{y:.1f}")
    return " ".join(out)


def card_svg(name: str, region: str | None, aquifer: str | None, series) -> str:
    sub = " · ".join(x for x in (region, aquifer) if x) or "England"
    # Name auto-shrink: long station names must not overflow the canvas.
    fs = 84 if len(name) <= 22 else 64 if len(name) <= 32 else 48
    spark = _spark_points(series or [], 90, 300, 1020, 170)
    spark_el = (
        f'<polyline points="{spark}" fill="none" stroke="{LINE}" stroke-width="4" '
        f'stroke-linejoin="round" stroke-linecap="round"/>' if spark else
        f'<text x="90" y="395" font-family="DejaVu Sans" font-size="30" '
        f'fill="{MUTED}">Monitored borehole — observation history on the site</text>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
  <!-- template v{TEMPLATE_VERSION} -->
  <rect width="{W}" height="{H}" fill="{BG}"/>
  <rect width="{W}" height="86" fill="{INK}"/>
  <circle cx="66" cy="43" r="20" fill="#2f5e86"/>
  <path d="M66 30c-7 9-11 14-11 19a11 11 0 0 0 22 0c0-5-4-10-11-19z" fill="#bcd9f0"/>
  <text x="100" y="55" font-family="DejaVu Sans" font-weight="bold" font-size="34" fill="#ffffff">GroundwaterCast UK</text>
  <text x="{W - 90}" y="55" text-anchor="end" font-family="DejaVu Sans" font-size="26" fill="#c3d4e6">groundwatercast.com</text>
  <text x="90" y="196" font-family="DejaVu Sans" font-weight="bold" font-size="{fs}" fill="{INK}">{esc(name)}</text>
  <text x="90" y="248" font-family="DejaVu Sans" font-size="32" fill="{MUTED}">{esc(sub)}</text>
  <rect x="70" y="272" width="1060" height="226" rx="14" fill="{PANEL}" stroke="#e6e6ea"/>
  {spark_el}
  <text x="90" y="540" font-family="DejaVu Sans" font-size="28" fill="{ACCENT}">Indicative groundwater outlook — updated daily, on open data</text>
  <rect y="{H - 56}" width="{W}" height="56" fill="#fff8e1"/>
  <text x="{W // 2}" y="{H - 20}" text-anchor="middle" font-family="DejaVu Sans" font-size="24" fill="#6b4e00">{esc(CAVEAT)}</text>
</svg>'''


def default_svg() -> str:
    # Illustrative seasonal recession/recharge curve (fixed dates, so the
    # default card is byte-stable) — never real data, never a status claim.
    import math
    series = [(f"2000-01-{i + 1:02d}", math.cos(i / 58.0 * 2 * math.pi) + 0.12 * math.sin(i * 1.7))
              for i in range(56)]
    return card_svg("England's groundwater, per borehole", None,
                    "Daily probabilistic forecasts for 1,000+ monitored boreholes",
                    series)


def _render(svg: str) -> bytes | None:
    try:
        import resvg_py
    except ImportError:
        return None
    fonts = [str(p) for p in FONT_DIR.glob("*.ttf")]
    return bytes(resvg_py.svg_to_bytes(
        svg_string=svg, font_files=fonts, skip_system_fonts=bool(fonts),
        sans_serif_family="DejaVu Sans"))


def build(pack_dir: Path = PACK_DIR, out_dir: Path = OUT_DIR,
          manifest_path: Path = MANIFEST) -> dict:
    try:
        import resvg_py  # noqa: F401
    except ImportError:
        print("WARNING: resvg_py not installed — no share cards rendered; "
              "stubs will omit og:image (pip install resvg-py to enable).")
        if manifest_path.exists():
            manifest_path.unlink()               # never leave a stale manifest
        return {"cards": 0, "skipped": "no resvg_py"}

    seen: dict[str, str] = {}
    manifest: dict[str, str] = {}
    n = reused = 0
    for fp in sorted(pack_dir.glob("*.json")):
        d = json.loads(fp.read_text(encoding="utf-8"))
        stn = d.get("station") or {}
        sid = stn.get("station_id")
        name = stn.get("name") or sid
        sl = stn.get("slug")
        if not sl:                                # pre-slug pack fallback
            sl = slug(name)
            if sl in seen and seen[sl] != sid:
                sl = f"{sl}-{str(sid)[:6]}"
        seen[sl] = sid
        region = region_for(stn.get("lat"), stn.get("lon"))
        series = (d.get("observed") or {}).get("series") or []
        svg = card_svg(name, region, stn.get("aquifer_designation"), series)
        h = hashlib.sha256(svg.encode("utf-8")).hexdigest()[:12]
        fname = f"share.{h}.png"
        dst_dir = out_dir / sl
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / fname
        if not dst.exists():                      # content-hash: render only on change
            png = _render(svg)
            dst.write_bytes(png)
            n += 1
        else:
            reused += 1
        for old in dst_dir.glob("share.*.png"):   # clean superseded hashes
            if old.name != fname:
                old.unlink()
        manifest[sl] = fname

    # Site-wide status-neutral fallback card at a STABLE path (static heads
    # can't know a hash) + the apple-touch-icon raster.
    OG_DIR.mkdir(parents=True, exist_ok=True)
    (OG_DIR / "default.png").write_bytes(_render(default_svg()))
    if FAVICON.exists():
        try:
            import resvg_py
            png = bytes(resvg_py.svg_to_bytes(
                svg_string=FAVICON.read_text(encoding="utf-8"),
                width=180, height=180))
            TOUCH_ICON.write_bytes(png)
        except Exception as exc:                  # icon is a nicety, never fatal
            print(f"  ! apple-touch-icon render failed: {exc}")

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(
        {"template_version": TEMPLATE_VERSION, "cards": manifest},
        separators=(",", ":")), encoding="utf-8")
    print(f"og cards: {n} rendered, {reused} reused (content-hash), "
          f"{len(manifest)} in manifest + default card + touch icon")
    return {"cards": len(manifest), "rendered": n, "reused": reused}


if __name__ == "__main__":
    build()
