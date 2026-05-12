"""Per-tract on-disk cache.

Each tract's report section is stored as two files:
  out/<tract>.txt   — the generated report text
  out/<tract>.hash  — sha256 of the input rows + job config

On re-run, if the hash matches we reuse the .txt file and skip the API call.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _hash_inputs(tract, job_dict: dict) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(job_dict, sort_keys=True).encode())
    for row in tract.rows + tract.le_rows:
        h.update(repr(row).encode())
    return h.hexdigest()


def tract_hash(tract, job_dict: dict) -> str:
    return _hash_inputs(tract, job_dict)


def load(out_dir: Path, tract_id: str, expected_hash: str) -> str | None:
    txt = out_dir / f"{tract_id}.txt"
    hashfile = out_dir / f"{tract_id}.hash"
    if not txt.exists() or not hashfile.exists():
        return None
    try:
        if hashfile.read_text().strip() != expected_hash:
            return None
        return txt.read_text(encoding="utf-8")
    except Exception:
        return None


def save(out_dir: Path, tract_id: str, text: str, input_hash: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    txt = out_dir / f"{tract_id}.txt"
    hashfile = out_dir / f"{tract_id}.hash"
    txt.write_text(text, encoding="utf-8")
    hashfile.write_text(input_hash)
    return txt
