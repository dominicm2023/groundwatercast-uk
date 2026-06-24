# GroundwaterCast explorer (static front-end)

A dependency-light MapLibre map that reads the published **artifact pack**
([`docs/artifact_contract.md`](../docs/artifact_contract.md)) and shows, per
borehole, the product's three-horizon story in one vocabulary — **below / near
/ above normal**:

- **map** — every borehole coloured by *current status vs normal*; an outer
  ring marks the boreholes that also carry a 14-day forecast;
- **detail panel** (click a borehole) — status-vs-normal ladder → 14-day
  P10/P50/P90 fan with breach probability → months 1–6 seasonal terciles.

No build step. Vanilla JS + a vendored MapLibre GL JS (BSD-3, in `vendor/`) +
hand-rolled SVG charts. The only runtime external dependency is the basemap
tile source (OpenFreeMap — free, no API key, swappable in `config.js`).

## Run it locally

The explorer needs the pack served from the same origin under `/pack`. The
preview server does both:

```bash
python -m scripts.build_artifact_pack          # builds outputs/pack (once)
python scripts/serve_explorer.py               # -> http://127.0.0.1:8080
```

## Configure

Everything deploy-specific is in [`config.js`](config.js):

- `packBase` — where the pack is served (`/pack` by default).
- `basemapStyle` — any MapLibre style URL. Default is OpenFreeMap `positron`
  (free, key-free, commercial-OK). Self-host the tiles later if you prefer.
- `center` / `zoom` — initial view.
- `palette` — status colours (kept in sync with `src/dashboard/status.py`).

## Deploy (VPS)

Serve `web/` at `/` and the pack at `/pack` from one origin. The pack is the
`outputs/pack/` directory the daily `run_chain --publish` step rebuilds.

**Caddy** (auto-TLS):

```
groundwatercast.example.org {
    root * /srv/groundwatercast/web
    @pack path /pack/*
    handle @pack {
        root * /srv/groundwatercast/outputs
        uri strip_prefix /pack
        file_server
    }
    file_server
    encode gzip
}
```

**nginx**:

```nginx
server {
    listen 80;
    server_name groundwatercast.example.org;
    root /srv/groundwatercast/web;

    location /pack/ {
        alias /srv/groundwatercast/outputs/pack/;
        gzip_static on;
    }
    location / { try_files $uri $uri/ =404; }
}
```

The pack is plain JSON/GeoJSON — let the web server gzip it (it compresses
~5×). Point a CDN at the origin if you expect traffic; `manifest.json` carries
per-file hashes for cache-busting.

## Contract

The explorer reads only fields declared in [`contract_fields.js`](contract_fields.js);
`tests/test_explorer_contract.py` asserts every one is part of the published
pack schema, so a pack change that would break the explorer fails a Python
test (no JS test runner needed).
