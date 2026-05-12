"""Per-tract agentic Claude conversation orchestration.

Three sequential area turns (surface → mineral → exceptions) per tract, with
prompt caching at the system / full-run-sheet / per-tract layers. Sonnet 4.6
with extended thinking by default; escalates to Opus 4.7 on low confidence
or mineral-fraction reconciliation failure.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable

import anthropic
from pydantic import ValidationError

from .models import Exceptions, MineralChain, SurfaceChain, TractAnalysis
from .parser import ParsedRunSheet, RunSheetRow, Tract
from .reconciler import reconcile


SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-7"

MAX_TOKENS = 16_000
THINKING_BUDGET = 8_000

RETRIEVE_K = 8


@dataclass
class JobConfig:
    effective_date: str = ""
    addressee: str = ""
    county: str = ""
    state: str = "Mississippi"
    signing_date: str = ""
    parcels: dict[str, dict] = None    # tract_id → {parcel_id, last_year, amount, priors_paid}

    def for_tract(self, tract_id: str) -> dict:
        if not self.parcels:
            return {}
        return self.parcels.get(tract_id, {})


def _load_prompt(name: str) -> str:
    return resources.files("run_sheet_analyzer.prompts").joinpath(name).read_text(encoding="utf-8")


def _row_md(row: RunSheetRow) -> str:
    parts = [
        f"- **{row.cite}** ({row.date_recorded.isoformat() if row.date_recorded else '—'})",
        f"  - *{row.doc_title}* — Grantor: {' | '.join(row.grantors) or '—'}; Grantee: {' | '.join(row.grantees) or '—'}",
    ]
    tract_str = ", ".join(row.tracts) if row.tracts else "—"
    if row.is_less_except:
        tract_str = f"LE → {tract_str}"
    elif row.is_not_subject:
        tract_str = "NS"
    parts.append(f"  - Tract: {tract_str}")
    if row.brief_description:
        bd = row.brief_description.replace("\n", "; ")
        parts.append(f"  - Brief description: {bd}")
    if row.mineral_reservations:
        parts.append(f"  - Mineral reservations: {row.mineral_reservations}")
    if row.notes:
        parts.append(f"  - Notes: {row.notes}")
    flags = []
    if row.exception_flag:
        flags.append("EXCEPTION")
    if row.mineral_transfer_flag:
        flags.append("MINERAL TRANSFER")
    if flags:
        parts.append(f"  - Abstractor flags: {', '.join(flags)}")
    return "\n".join(parts)


def _serialize_full_run_sheet(p: ParsedRunSheet) -> str:
    lines = [
        f"# Run sheet — {p.path.name}",
        f"_{len(p.rows)} rows total; {len(p.tract_ids())} tracts; {len(p.not_subject_rows)} NS rows._",
        "",
        "## All rows (chronological)",
        "",
    ]
    sorted_rows = sorted(p.rows, key=lambda r: (r.date_recorded or datetime(1800, 1, 1).date()))
    for row in sorted_rows:
        lines.append(_row_md(row))
        lines.append("")
    return "\n".join(lines)


def _serialize_tract(p: ParsedRunSheet, tract: Tract) -> str:
    lines = [
        f"# Focused view — TRACT {tract.id}",
        "",
        f"## Surface / general events ({len(tract.rows)} rows)",
        "",
    ]
    for row in sorted(tract.rows, key=lambda r: (r.date_recorded or datetime(1800, 1, 1).date())):
        lines.append(_row_md(row))
        lines.append("")

    lines.append(f"## Less-and-Except carve-outs ({len(tract.le_rows)} rows)")
    lines.append("")
    if not tract.le_rows:
        lines.append("_None._")
        lines.append("")
    else:
        for row in tract.le_rows:
            lines.append(_row_md(row))
            lines.append("")

    if p.not_subject_rows:
        lines.append(f"## Not-Subject context ({len(p.not_subject_rows)} rows — informational only)")
        lines.append("")
        for row in p.not_subject_rows[:20]:
            lines.append(_row_md(row))
            lines.append("")
        if len(p.not_subject_rows) > 20:
            lines.append(f"_…and {len(p.not_subject_rows) - 20} more NS rows omitted._")
            lines.append("")
    return "\n".join(lines)


def _tract_retrieval_query(tract: Tract) -> str:
    """Build a focused query from the tract's facts to seed reference retrieval."""
    titles = sorted({r.doc_title for r in tract.rows if r.doc_title})
    parts = ["Mississippi title examination: chain of title, surface and mineral ownership."]
    if any(r.mineral_reservations for r in tract.rows):
        parts.append("Mineral severance and fractional reservation.")
    if any("Oil, Gas" in t or "Mineral Lease" in t for t in titles):
        parts.append("Oil and gas mineral lease.")
    if any("Patent" in t for t in titles):
        parts.append("Sovereignty patent root of title.")
    if any("Tax" in t for t in titles):
        parts.append("Tax sale, redemption, forfeited tax patent.")
    if any("Chancery" in t or "Decree" in t or "Guardian" in t for t in titles):
        parts.append("Chancery court partition, sale to pay debts, guardian's deed.")
    if any("Deed of Trust" in t or "Mortgage" in t for t in titles):
        parts.append("Deed of trust statute of limitations release.")
    if any("Right of Way" in t or "Easement" in t for t in titles):
        parts.append("Right of way easement servitude.")
    if any("Affidavit" in t for t in titles):
        parts.append("Heirship affidavit intestate succession.")
    return " ".join(parts)


def _retrieve_refs(query: str, lib) -> str:
    hits = lib.retrieve(query, k=RETRIEVE_K, rerank=True)
    if not hits:
        return "_No reference chunks retrieved._"
    out = ["# Reference standards (top-{} hits)".format(len(hits)), ""]
    for h in hits:
        out.append(f"## {h.citation}")
        if h.parent_hierarchy:
            out.append(f"_{' › '.join(h.parent_hierarchy)}_")
        out.append("")
        out.append(h.text.strip())
        out.append("")
    return "\n".join(out)


def _tract_hash(tract: Tract, p: ParsedRunSheet, job: JobConfig) -> str:
    h = hashlib.sha256()
    h.update(job.effective_date.encode())
    for row in tract.rows + tract.le_rows:
        h.update(repr(row).encode())
    for row in p.not_subject_rows:
        h.update(b"ns")
        h.update(repr(row).encode())
    return h.hexdigest()


def _extract_text(response: anthropic.types.Message) -> str:
    """Extract concatenated text from Claude's response, ignoring thinking blocks."""
    parts = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()


def _strip_code_fence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        # Drop the opening fence (with or without language tag) and the closing fence.
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _parse_json(s: str) -> dict:
    return json.loads(_strip_code_fence(s))


def _build_system_blocks() -> list[dict]:
    system_text = _load_prompt("system.md")
    return [
        {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}},
    ]


def _build_full_runsheet_block(p: ParsedRunSheet) -> dict:
    return {
        "type": "text",
        "text": _serialize_full_run_sheet(p),
        "cache_control": {"type": "ephemeral"},
    }


def _build_tract_context_block(p: ParsedRunSheet, tract: Tract, refs_text: str) -> dict:
    body = _serialize_tract(p, tract) + "\n\n" + refs_text
    return {
        "type": "text",
        "text": body,
        "cache_control": {"type": "ephemeral"},
    }


def _build_task_block(prompt_name: str, tract_id: str, job: JobConfig) -> dict:
    raw = _load_prompt(prompt_name)
    rendered = raw.format(tract_id=tract_id, effective_date=job.effective_date or "<not provided>")
    return {"type": "text", "text": rendered}


@dataclass
class _AreaResult:
    text: str
    model: str
    raw: dict


def _call(
    client: anthropic.Anthropic,
    system_blocks: list[dict],
    messages: list[dict],
    model: str,
) -> anthropic.types.Message:
    return client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET},
        system=system_blocks,
        messages=messages,
    )


def _run_area(
    client: anthropic.Anthropic,
    system_blocks: list[dict],
    messages: list[dict],
    task_block: dict,
    schema_cls,
    *,
    initial_model: str = SONNET,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[Any, str, list[dict]]:
    """Send one area turn, parse the response, escalate to Opus on low confidence.

    Returns (parsed_obj, model_used, updated_messages).
    """
    messages = messages + [{"role": "user", "content": [task_block]}]
    if on_progress:
        on_progress(f"  calling {initial_model}...")
    resp = _call(client, system_blocks, messages, initial_model)
    text = _extract_text(resp)
    messages = messages + [{"role": "assistant", "content": text}]

    try:
        obj = schema_cls.model_validate(_parse_json(text))
    except (json.JSONDecodeError, ValidationError) as e:
        if on_progress:
            on_progress(f"  parse failed; asking for correction: {e}")
        retry = {
            "type": "text",
            "text": (
                f"Your previous response could not be parsed as JSON matching the "
                f"{schema_cls.__name__} schema. Error: {e}. Return ONLY valid JSON "
                f"for the {schema_cls.__name__} schema with no commentary."
            ),
        }
        messages = messages + [{"role": "user", "content": [retry]}]
        resp = _call(client, system_blocks, messages, initial_model)
        text = _extract_text(resp)
        messages = messages + [{"role": "assistant", "content": text}]
        obj = schema_cls.model_validate(_parse_json(text))

    model_used = initial_model
    if obj.confidence == "low" and initial_model == SONNET and not os.environ.get("ANALYZER_NO_ESCALATE"):
        if on_progress:
            on_progress("  low confidence — escalating to Opus 4.7")
        escalate = {
            "type": "text",
            "text": (
                "Your previous response on this area returned `confidence: low`. "
                "Please re-attempt with greater rigor. If you still cannot reach "
                "`high` on specific points, list them precisely under `attorney_review` "
                "and proceed. Return JSON only."
            ),
        }
        messages = messages + [{"role": "user", "content": [escalate]}]
        resp = _call(client, system_blocks, messages, OPUS)
        text = _extract_text(resp)
        messages = messages + [{"role": "assistant", "content": text}]
        obj = schema_cls.model_validate(_parse_json(text))
        model_used = OPUS

    return obj, model_used, messages


def analyze_tract(
    *,
    client: anthropic.Anthropic,
    p: ParsedRunSheet,
    tract: Tract,
    job: JobConfig,
    refs_lib,
    on_progress: Callable[[str], None] | None = None,
) -> TractAnalysis:
    log = on_progress or (lambda s: None)
    log(f"Tract {tract.id}: retrieving authorities …")
    refs_text = _retrieve_refs(_tract_retrieval_query(tract), refs_lib)

    system_blocks = _build_system_blocks()
    base_messages = [
        {
            "role": "user",
            "content": [
                _build_full_runsheet_block(p),
                _build_tract_context_block(p, tract, refs_text),
            ],
        }
    ]

    log(f"Tract {tract.id}: surface chain …")
    surface, surface_model, messages = _run_area(
        client, system_blocks, base_messages,
        _build_task_block("surface.md", tract.id, job),
        SurfaceChain, on_progress=log,
    )

    log(f"Tract {tract.id}: mineral chain …")
    mineral, mineral_model, messages = _run_area(
        client, system_blocks, messages,
        _build_task_block("mineral.md", tract.id, job),
        MineralChain, on_progress=log,
    )

    # Reconcile mineral fractions; on imbalance, ask Claude to fix.
    reconciliation = reconcile(mineral)
    mineral.reconciliation = reconciliation
    if not reconciliation["ok"]:
        log(f"Tract {tract.id}: mineral fractions imbalance ({reconciliation.get('imbalance', '?')}); asking for correction")
        fix = {
            "type": "text",
            "text": (
                "Your current_mineral_owners shares do not sum to 1. "
                f"Imbalance: {reconciliation.get('imbalance')}. Total: {reconciliation['total']}. "
                "Please correct your fractions and return the full MineralChain JSON again. "
                "If you cannot make the fractions sum to 1 because of a gap or ambiguity "
                "in the record, leave the fractions you can verify, set reconciliation.ok=false, "
                "and emit a specific ATTORNEY REVIEW callout naming the missing fraction. "
                "Return JSON only."
            ),
        }
        messages = messages + [{"role": "user", "content": [fix]}]
        resp = _call(client, system_blocks, messages, OPUS)
        text = _extract_text(resp)
        messages = messages + [{"role": "assistant", "content": text}]
        mineral = MineralChain.model_validate(_parse_json(text))
        mineral.reconciliation = reconcile(mineral)
        mineral_model = OPUS

    log(f"Tract {tract.id}: exceptions …")
    exceptions, exceptions_model, messages = _run_area(
        client, system_blocks, messages,
        _build_task_block("exceptions.md", tract.id, job),
        Exceptions, on_progress=log,
    )

    return TractAnalysis(
        tract=tract.id,
        surface=surface,
        mineral=mineral,
        exceptions=exceptions,
        input_hash=_tract_hash(tract, p, job),
        model_used={
            "surface": surface_model,
            "mineral": mineral_model,
            "exceptions": exceptions_model,
        },
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def load_refs_or_die(refs_path: Path):
    """Hard-fail if refs/ is missing or empty."""
    from .retrieval import RefLibrary

    if not refs_path.exists():
        raise RuntimeError(
            f"refs/ directory does not exist at {refs_path}. "
            "Build at least one reference DB with the Embeddings DB Creator first."
        )
    lib = RefLibrary.load(refs_path)
    if not lib.stats():
        raise RuntimeError(
            f"No reference DBs found under {refs_path}. "
            "Build at least one reference DB with the Embeddings DB Creator first."
        )
    return lib
