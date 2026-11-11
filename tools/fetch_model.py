"""Download a pinned model from configs/models.json (``python3 tools/fetch_model.py <key>``).

Refuses if free disk after the download would fall below ``disk_floor_gb`` in
budget.toml. Downloads the exact revision, verifies the SHA-256, and stores the
file under ``$BASERATE_MODELS`` (default ``~/models``), outside the repository.
Stdlib only. Prints the local path on success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def models_dir() -> Path:
    """Where model files live (outside the repository)."""
    return Path(os.environ.get("BASERATE_MODELS", Path.home() / "models"))


def sha256(path: Path) -> str:
    """Hex SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(key: str) -> Path:
    """Download and verify one pinned model; return its path."""
    spec = json.loads((ROOT / "configs" / "models.json").read_text())["models"][key]
    dest_dir = models_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / spec["file"]
    if dest.exists() and dest.stat().st_size == spec["bytes"] and sha256(dest) == spec["sha256"]:
        return dest
    floor_gb = tomllib.loads((ROOT / "budget.toml").read_text())["disk_floor_gb"]
    free = shutil.disk_usage(dest_dir).free
    if free - spec["bytes"] < floor_gb * 1024**3:
        raise SystemExit(f"refusing: free disk would fall below {floor_gb} GB")
    url = f"https://huggingface.co/{spec['hf_repo']}/resolve/{spec['revision']}/{spec['file']}"
    part = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, part.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1 << 22)
    if sha256(part) != spec["sha256"]:
        part.unlink()
        raise SystemExit(f"hash mismatch for {key}")
    part.rename(dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    """Fetch the named models."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keys", nargs="+")
    args = parser.parse_args(argv)
    for key in args.keys:
        print(fetch(key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
