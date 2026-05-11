"""Per-tract on-disk JSON cache keyed by input hash."""
from __future__ import annotations

import json
from pathlib import Path

from .models import TractAnalysis


def cache_path(out_dir: Path, tract_id: str) -> Path:
    return out_dir / f"{tract_id}.json"


def load(out_dir: Path, tract_id: str, expected_hash: str) -> TractAnalysis | None:
    p = cache_path(out_dir, tract_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("input_hash") != expected_hash:
        return None
    try:
        return TractAnalysis.model_validate(data)
    except Exception:
        return None


def save(out_dir: Path, ta: TractAnalysis) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = cache_path(out_dir, ta.tract)
    p.write_text(ta.model_dump_json(indent=2), encoding="utf-8")
    return p


def save_consolidated(out_dir: Path, name: str, job_dict: dict, tracts: dict[str, TractAnalysis]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{name}.analysis.json"
    payload = {
        "job": job_dict,
        "tracts": {tid: json.loads(t.model_dump_json()) for tid, t in tracts.items()},
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p
