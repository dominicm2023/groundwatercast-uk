"""Build the published artifact pack (step 10 — `run_chain --publish`).

Assembles ``outputs/pack/`` from existing pipeline artefacts — pure read, no
network. Schema: ``docs/artifact_contract.md`` / ``src/publish/contract.py``.

Usage:
    python -m scripts.build_artifact_pack [--out DIR] [--now ISO] [--pretty]

``--now`` pins the pack's ``generated_at`` (byte-identical reruns — mostly
for testing/diffing); ``--pretty`` indents the JSON (debugging; the published
pack should stay minified).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.publish.pack import build_pack, load_inputs
from src.utils.io_encoding import force_utf8_stdio

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="output directory "
                        "(default: config.publish.out_dir)")
    parser.add_argument("--now", default=None, help="pin generated_at "
                        "(ISO timestamp, UTC assumed)")
    parser.add_argument("--pretty", action="store_true",
                        help="indent JSON output")
    args = parser.parse_args(argv)

    cfg = json.loads((ROOT / "config" / "config.json").read_text())
    pub = cfg.get("publish", {})
    out_dir = Path(args.out) if args.out else ROOT / pub.get("out_dir", "outputs/pack")

    inputs = load_inputs(cfg, ROOT)
    ts_cfg = cfg.get("diagnostics", {}).get("trend_screen", {})
    meta = build_pack(
        inputs, out_dir, now=args.now,
        history_days=int(pub.get("history_days", 1100)),
        include_history_for=pub.get("include_history_for", "all"),
        public_trend_provenance=frozenset(
            ts_cfg.get("public_provenance", ["artifact_like"])),
        pretty=bool(args.pretty or pub.get("pretty", False)),
    )

    missing = [name for name, src in meta["inputs"].items()
               if src.get("status") == "missing"]
    for name in missing:
        print(f"WARNING: input '{name}' missing - its fields are null in "
              f"this pack (see docs/artifact_contract.md s6).")
    c = meta["counts"]
    cov = meta.get("coverage", {})
    lc = cov.get("live_capable")
    print(f"pack OK (schema {meta['schema_version']}): {c['stations']} stations "
          f"({c['with_forecast']} with forecast, {c['with_seasonal']} with "
          f"seasonal; {c['excluded']} excluded, {c['no_data']} no-data) "
          f"-> {out_dir}")
    print(f"coverage: {cov.get('observed', '?')} observed / "
          f"{cov.get('with_forecast', '?')} forecast / "
          f"{lc if lc is not None else '?'} live-capable / "
          f"{cov.get('catalogued', '?')} catalogued")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
