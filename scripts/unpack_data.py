"""Unpack the committed data snapshot into raw/.

data/raw/*.csv.gz is the exact output of simulate_source_systems.py (seed
20260910), compressed so it fits comfortably in git. Unpacking it is equivalent
to running the simulator, and either one gives build_marts.py what it reads.

    python scripts/unpack_data.py
    python build_marts.py
"""
from __future__ import annotations

import gzip
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "raw"
DST = ROOT / "raw"


def main() -> int:
    files = sorted(SRC.glob("*.csv.gz"))
    if not files:
        print("No snapshot found in data/raw/. Run python simulate_source_systems.py instead.")
        return 1
    DST.mkdir(exist_ok=True)
    for f in files:
        out = DST / f.name[:-3]
        with gzip.open(f, "rb") as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
        print("  {:38s} {:>12,} bytes".format(out.name, out.stat().st_size))
    manifest = SRC / "_manifest.json"
    if manifest.exists():
        shutil.copy2(manifest, DST / "_manifest.json")
        print("  _manifest.json")
    print("\nraw/ is ready. Next: python build_marts.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
