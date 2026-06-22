# GroundwaterCast UK — dashboard + pipeline image (Linux deployment).
#
# The image holds CODE + DEPENDENCIES only. All pipeline artefacts
# (data/, models/, outputs/ — all rebuildable) live on
# volumes mounted at runtime: see docker-compose.yml and docs/deploy.md.
# Nothing under those paths is needed at build time (.dockerignore keeps
# the heavyweight paths out of the build context).
#
# Build:  docker build -t groundwatercast .
# Run:    docker compose up -d            -> http://localhost:8501

# Matches .python-version (the project is developed and tested on 3.14).
FROM python:3.14-slim

WORKDIR /app

# Dependencies first, so this layer caches across code-only rebuilds.
#
# GRIB stack: requirements.txt comments out ecmwf-opendata/cfgrib/eccodes
# because the binaries are awkward on Windows — on Linux they are plain
# manylinux wheels, and config/config.json's production ensemble provider
# is `ecmwf_opendata`, so the image installs them. The dev provider
# (`open_meteo`) needs only `requests`; drop this line if you want a
# slimmer open_meteo-only image.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir ecmwf-opendata cfgrib eccodes xarray

COPY . .

# Hosted deployments schedule the refresh scripts with cron (docs/deploy.md
# has the crontab) — the in-app staleness-gated refresh nets are therefore
# OFF by default in the container. Set GWC_APP_START_REFRESH=1 at runtime
# to re-enable them on a host with no scheduler.
ENV GWC_APP_START_REFRESH=0 \
    PYTHONUNBUFFERED=1

EXPOSE 8501

CMD ["streamlit", "run", "app.py", \
     "--server.address", "0.0.0.0", \
     "--server.headless", "true"]
