"""Rasterise the standalone data-flow diagram to a README-ready PNG.

  tectonic docs/dataflow_diagram.tex --outdir docs   # -> docs/dataflow_diagram.pdf
  python scripts/render_dataflow.py                   # -> docs/img/dataflow.png

PyMuPDF (fitz) renders the (tightly-cropped) standalone PDF at 3x for a crisp
raster — no system image tooling needed.
"""
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "docs" / "dataflow_diagram.pdf"
OUT = ROOT / "docs" / "img" / "dataflow.png"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(PDF)
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)  # 3x ≈ 216 dpi
    pix.save(str(OUT))
    print(f"wrote {OUT}  ({pix.width}x{pix.height}px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
