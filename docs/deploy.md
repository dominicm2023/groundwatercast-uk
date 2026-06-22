# Linux / container deployment

How to run the dashboard and its refresh pipeline in Docker on a Linux
host. For what each pipeline stage does and the full dependency DAG, run
`python -m scripts.run_chain --list` — this page only covers the container
packaging of those same commands.

## The model: code in the image, data on volumes

The image (`Dockerfile`, single stage, `python:3.14-slim`) contains the
repo code and Python dependencies only. All pipeline artefacts — `data/`,
`models/`, `outputs/`, ~14 GB locally and entirely rebuildable — stay on
host bind mounts declared in `docker-compose.yml`:

```yaml
volumes:
  - ./data:/app/data
  - ./models:/app/models
  - ./outputs:/app/outputs
```

So artefacts persist across image rebuilds, and the small committed
assets under `data/` and `outputs/` (region boundaries, threshold
definitions, catalogue inputs, …) are present because the mounts point at
the repo checkout itself. Run the container from a clone of the repo, not a
bare directory.

## Quick start

```bash
git clone <repo-url> && cd <repo>
docker compose build
docker compose up -d
# -> http://localhost:8501
```

On a host that already has the data artefacts (e.g. copied from the dev
machine), that is all. On a fresh host, the dashboard will start but show
empty/missing-artefact states until you populate the volume:

## First run: populate the data volume

Run the bootstrap chain once, inside the container against the same
mounts (or on the host with a local Python env — the artefacts are
interchangeable):

```bash
docker compose run --rm app python -m src.catalogue.build
docker compose run --rm app python -m src.pipeline.run
docker compose run --rm app python -m scripts.run_chain --all
```

`src.pipeline.run` builds `data/features/joined_timeseries.csv`, which
every `run_chain` stage sits on top of; `run_chain --all` then covers the
core rebuild, xref, live and ensemble stages in dependency order
(and skips the Pastas stages with a warning — see below). Step 1 of the
core chain also repairs the documented gotcha that the features stage
wipes the dipped-station ingestion. The initial raw-data download
dominates the wall time; the chain itself is fast once data is local.

## Scheduling the refreshes (host cron)

The image deliberately ships **no cron daemon**, and the in-app
auto-refresh nets are **disabled in the image** (`GWC_APP_START_REFRESH=0`
is baked in as an ENV default): in a hosted deployment, scheduling belongs
to the host. Cadences:

| Job | Cadence | Command |
|---|---|---|
| Live chain (GW telemetry → shards; rainfall tail) | hourly | `run_chain --live` |
| Daily probabilistic forecast (roll ensemble, 8d/8e) | daily | `run_chain --ensemble` |
| Published artifact pack (`docs/artifact_contract.md`) | daily, after the forecast | `run_chain --publish` (or append `--publish` to the forecast line) |

Host crontab, using one-shot containers against the same image + volumes:

```cron
0 * * * *    cd /path/to/repo && docker compose run --rm app python -m scripts.run_chain --live      >> data/model/cron_live.log 2>&1
30 6 * * *   cd /path/to/repo && docker compose run --rm app python -m scripts.run_chain --ensemble  >> data/model/cron_forecast.log 2>&1
```

The daily job is `--ensemble` rather than `--forecast`
because `--forecast` includes the Pastas stages, which this image cannot
run (next section). `run_chain` skips missing-env Pastas stages rather
than failing mid-chain, but exits 1 to surface the skip — so a cron'd
`--forecast` would flag every run as failed. Switch the crontab line to
`--forecast` only after setting up the Pastas env below.

## Static explorer (the public front-end)

The Streamlit app (port 8501) is the internal/analyst surface. The public
face is the **MapLibre static explorer** in [`web/`](../web/) — a no-build map
that reads the published artifact pack (`outputs/pack/`, rebuilt by the daily
`run_chain --publish`). It has no server-side component: serve `web/` at `/`
and the pack at `/pack` from one origin (any static server / CDN). Full setup,
config and Caddy/nginx snippets: [`web/README.md`](../web/README.md).

Because the explorer is just files, it can sit in front of the same `outputs`
volume the pipeline writes — the daily publish step updates the pack and the
next page load picks it up (the `meta.json` "data as of" banner reflects the
latest run). Local preview: `python scripts/serve_explorer.py`.

### `GWC_APP_START_REFRESH` semantics

`src/dashboard/auto_refresh.py` is the no-cron safety net: when the env
var is **unset or not `0`**, the running app kicks staleness-gated
background refreshes itself (live hourly, forecast daily).
`0` disables both — required when real cron runs the commands, or
the two would race. The image defaults to `0`; if you genuinely cannot
schedule anything on the host, override it (`GWC_APP_START_REFRESH: "1"`
in the compose `environment:` block) and drop the crontab. Don't run both.

## Pastas forecast — out of scope for this image

The Pastas TFN stages (8f–8h, the primary "Forecast outlook" input) run in
a **dedicated environment** (`requirements-pastas.txt`: numba/llvmlite
stack, never imported by the main pipeline). The image does not include
it; everything degrades cleanly without it:

- `run_chain` skips Pastas stages with a warning when `.venv-pastas` is
  absent (exit 1 so cron notices),
- the in-app auto-refresh degrades its daily forecast job to `--ensemble`
  (so the roll artefacts stay fresh for when Pastas is added).

Note: the **Forecast outlook page currently requires the Pastas summary**
— without the Pastas env it shows an instructive empty state rather than
a roll-only triage (roll-only mode is on the backlog). Treat the Pastas
env as required for a user-facing forecast deployment.

To add it: `run_chain` looks for the interpreter at `.venv-pastas/` under
the repo root (`/app` in the container), which is not covered by the data
mounts — so it has to live in the image. Simplest option, a derived image:

```dockerfile
FROM groundwatercast
RUN python -m venv .venv-pastas \
    && .venv-pastas/bin/pip install --no-cache-dir -r requirements-pastas.txt
```

`run_chain` resolves `.venv-pastas/bin/python` automatically (POSIX path
is already supported), after which `--forecast` and the one-off `--pastas`
recalibration work as documented.

## Ensemble provider / GRIB stack

The default ensemble provider is `open_meteo` (plain HTTP; commercial use
needs `GWC_OPEN_METEO_API_KEY` — see `.env.example`). The `ecmwf_opendata`
GRIB provider needs `ecmwf-opendata`/`cfgrib`/`eccodes`, which are
commented out in `requirements.txt` (awkward binaries on Windows) but are
plain wheels on Linux, so **the Dockerfile installs them** to keep the
zero-cost commercial path available. Delete that `pip install` line for a
slimmer open_meteo-only image.

## Known limitations

- **Build untested on this branch** — authored on a machine without a
  Docker daemon; validated by review. Report build breaks as issues.
- **PDF export**: `kaleido` ≥ v1 needs a Chrome binary for Plotly
  static-image export. The slim image does not
  ship Chrome; if an export errors, either run
  `docker compose run --rm app plotly_get_chrome` (persists only until
  the container is recreated) or bake it into a derived image.
- **Timezone**: containers default to UTC; the staleness gates and fetch
  windows are mtime/relative-time based, so this is cosmetic, but set
  `TZ=Europe/London` in the compose `environment:` if log timestamps
  matter to you.
