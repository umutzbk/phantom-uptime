"""Build the tarball that goes to the server.

Deployment copies a bundle rather than cloning, so the host needs no git access.
Only what the running stack needs goes in: the raw extracts and the DuckDB
warehouse stay on the laptop, because Cube reads the parquet marts and nothing
else does.

    python scripts/make_deploy_bundle.py
    -> dist/copilot-deploy.tar.gz
"""
from __future__ import annotations

import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "copilot-deploy.tar.gz"

FILES = [
    "Dockerfile",
    "docker-compose.yml",
    "Caddyfile",
    "requirements.txt",
    ".dockerignore",
    ".env.production.example",
]
TREES = [
    ("api", lambda p: p.suffix == ".py"),
    ("web", lambda p: True),
    ("cube/model", lambda p: p.suffix in (".yml", ".yaml", ".js")),
    ("demo_traces", lambda p: p.suffix == ".json"),
    ("marts", lambda p: p.suffix == ".parquet"),
]


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    added = 0
    total = 0
    with tarfile.open(OUT, "w:gz") as tar:
        for name in FILES:
            src = ROOT / name
            if not src.exists():
                raise SystemExit("missing: {}".format(name))
            tar.add(src, arcname=name)
            added += 1
            total += src.stat().st_size
        for tree, keep in TREES:
            base = ROOT / tree
            if not base.exists():
                raise SystemExit("missing: {}".format(tree))
            for f in sorted(base.rglob("*")):
                if f.is_file() and keep(f) and "__pycache__" not in f.parts:
                    tar.add(f, arcname=str(f.relative_to(ROOT)).replace("\\", "/"))
                    added += 1
                    total += f.stat().st_size

    print("{} files, {:.1f} MB raw -> {:.1f} MB compressed".format(
        added, total / 1e6, OUT.stat().st_size / 1e6))
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
