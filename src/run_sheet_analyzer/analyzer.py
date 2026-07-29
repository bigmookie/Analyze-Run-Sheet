"""Per-tract title-report generation.

For each tract:
  1. Filter run-sheet rows that belong to the tract, sort by date_recorded.
  2. Send ONE model call per phase with a focused prompt.
  3. Return the model's plain-text response (the report section for that tract).

No JSON schemas, no multi-turn agentic conversations, no tool use. Just:
filter, sort, ask, get text back.

*Which* model answers is providers.py's business — Claude by default, OpenAI
when Claude is unavailable. Everything here is provider-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from typing import Callable

from run_sheet_analyzer.providers import (
    API_ERRORS,
    GPT,
    MAX_CONTINUATIONS,
    MAX_TOKENS,
    OPUS,
    AnalysisInterrupted,
    Provider,
    ProviderConfigError,
    TokenUsage,
    build_provider,
    check_env,
    env_summary,
    stop_event,
)

# Re-exported so callers keep importing these from analyzer.
__all__ = [
    "API_ERRORS",
    "GPT",
    "JobConfig",
    "MAX_CONTINUATIONS",
    "MAX_TOKENS",
    "OPUS",
    "AnalysisInterrupted",
    "Provider",
    "ProviderConfigError",
    "TokenUsage",
    "analyze_tract",
    "build_provider",
    "check_env",
    "env_summary",
    "stop_event",
]


@dataclass
class JobConfig:
    effective_date: str = ""
    addressee: str = ""
    county: str = ""
    state: str = "Mississippi"
    signing_date: str = ""
    parcels: dict[str, dict] = field(default_factory=dict)
    # Used only when the run sheet's Tract column is blank: the units the examiner
    # wants to group rows into (or a legal-description file to read them from),
    # and the granularity of typed units.
    tracts: list[str] = field(default_factory=list)
    tract_granularity: str = ""
    description_file: str = ""
    # When False, the mineral-chain phase is skipped and the report covers
    # surface title only — no mineral vesting line, no mineral leases section.
    include_minerals: bool = True

    def for_tract(self, tract_id: str) -> dict:
        return self.parcels.get(tract_id, {}) if self.parcels else {}


# ──────────────────────────────────────────────────────────────────────────
# Prompt assembly
# ──────────────────────────────────────────────────────────────────────────


def _load_prompt(name: str) -> str:
    return resources.files("run_sheet_analyzer.prompts").joinpath(name).read_text(encoding="utf-8")


def _format_event(row, idx: int) -> str:
    """One bullet per row in the events table."""
    cite = f"Book {row.book} at Page {row.page}"
    if row.instrument_no:
        cite = f"Instrument No. {row.instrument_no} ({cite})"
    recorded = row.date_recorded.isoformat() if row.date_recorded else "n/a"
    grantors = " | ".join(row.grantors) or "—"
    grantees = " | ".join(row.grantees) or "—"
    parts = [
        f"{idx}. [{recorded}] {row.doc_title}",
        f"   Citation: {cite}",
        f"   Grantor: {grantors}",
        f"   Grantee: {grantees}",
    ]
    if row.brief_description:
        parts.append(f"   Brief description: {row.brief_description.replace(chr(10), '; ')}")
    if row.mineral_reservations:
        parts.append(f"   Mineral reservations: {row.mineral_reservations}")
    if row.notes:
        parts.append(f"   Abstractor notes: {row.notes}")
    flags = []
    if row.exception_flag:
        flags.append("EXCEPTION")
    if row.mineral_transfer_flag:
        flags.append("MINERAL TRANSFER")
    if flags:
        parts.append(f"   Abstractor flags: {', '.join(flags)}")
    return "\n".join(parts)


def _commentary_block(commentary: str) -> str:
    if not commentary.strip():
        return ""
    return (
        "# Examiner commentary and instructions (HIGH PRIORITY)\n\n"
        "The examining attorney has provided the following commentary and/or "
        "instructions for this analysis. Follow them carefully; where they "
        "conflict with a general rule, the examiner's instructions control:\n\n"
        f"{commentary.strip()}"
    )


def _events_blocks(rows: list, le_rows: list):
    sorted_rows = sorted(rows, key=lambda r: (r.date_recorded or date(1800, 1, 1)))
    events_text = "\n\n".join(_format_event(r, i + 1) for i, r in enumerate(sorted_rows)) or "_None._"
    le_text = "\n\n".join(_format_event(r, i + 1) for i, r in enumerate(le_rows)) or "_None._"
    return events_text, le_text


def _build_mineral_prompt(
    tract_id: str, rows: list, le_rows: list, job: JobConfig, commentary: str = ""
) -> str:
    """Prompt for the mineral-chain analysis (minerals only)."""
    events_text, le_text = _events_blocks(rows, le_rows)
    template = _load_prompt("mineral.md")
    return template.format(
        tract_id=tract_id,
        effective_date=job.effective_date or "<not provided>",
        commentary=_commentary_block(commentary),
        events=events_text,
        le_rows=le_text,
    )


def _mineral_section(mineral_analysis: str, include_minerals: bool) -> str:
    """The mineral portion injected into the assembler prompt.

    Included → hand the phase-1 mineral block to the assembler to fold in verbatim.
    Excluded → instruct a surface-only report that omits all mineral content.
    """
    if not include_minerals:
        return (
            "## Minerals — EXCLUDED (surface title only)\n\n"
            "This report covers **surface title only**. Do NOT analyze or mention "
            "the mineral estate. Specifically:\n"
            "- In VESTING, omit the `Minerals` sub-label and its bullets entirely "
            "(output only the `Surface` block).\n"
            "- Omit the `Mineral Leases` exceptions section entirely. The EXCEPTIONS "
            "sections are therefore: 1. Voluntary Liens, 2. Involuntary Liens, "
            "3. Servitudes, 4. Other Matters of Record.\n"
            "- Do not add mineral reservations, severances, or leases to any section, "
            "and do not raise mineral issues under ATTORNEY REVIEW."
        )
    block = (mineral_analysis or "").strip() or "_No mineral analysis provided._"
    return (
        "## Mineral chain analysis (already completed — use verbatim)\n\n"
        "A separate, more thorough mineral-estate analysis has already been "
        "performed for this tract. **Do not redo the mineral fractional analysis.** "
        "Incorporate the block below as follows:\n\n"
        "- Use its **MINERAL VESTING** bullet lines (and the `Total …` line) as the "
        "holder bullets under the `Minerals` sub-label in VESTING, verbatim — one "
        "holder per line.\n"
        "- Place its **MINERAL EXCEPTIONS** items under Exceptions bucket 4 (Other Matters of Record).\n"
        "- Place its **MINERAL LEASES** items under Exceptions bucket 5 (Mineral Leases).\n"
        "- Fold its **MINERAL ATTORNEY REVIEW** items into your ATTORNEY REVIEW section.\n"
        "- Use the supplied mineral analysis for everything about the mineral estate "
        "(do not recompute fractions).\n\n"
        f"```\n{block}\n```"
    )


def _build_tract_prompt(
    tract_id: str, rows: list, le_rows: list, job: JobConfig,
    commentary: str = "", mineral_analysis: str = "",
) -> str:
    """Prompt for the full-report assembly. The mineral analysis from phase 1 is
    supplied verbatim for the assembler to fold in (or, when minerals are
    excluded, a surface-only instruction replaces it)."""
    events_text, le_text = _events_blocks(rows, le_rows)
    parcel = job.for_tract(tract_id)
    job_context_parts = [
        f"Addressee: {job.addressee or '—'}",
        f"County:    {job.county or '—'}",
    ]
    if parcel:
        job_context_parts.append(f"Parcel ID for this tract: {parcel.get('parcel_id', '—')}")
    job_context = "\n".join(job_context_parts)

    template = _load_prompt("tract.md")
    return template.format(
        tract_id=tract_id,
        effective_date=job.effective_date or "<not provided>",
        commentary=_commentary_block(commentary),
        events=events_text,
        le_rows=le_text,
        job_context=job_context,
        mineral_section=_mineral_section(mineral_analysis, job.include_minerals),
    )


def _system_prompt() -> str:
    return _load_prompt("system.md")


# ──────────────────────────────────────────────────────────────────────────
# Public surface
# ──────────────────────────────────────────────────────────────────────────


def _generate(
    provider: Provider,
    system: str,
    user_prompt: str,
    usage_by_model: dict,
    log: Callable[[str], None],
    *,
    thinking: bool = False,
) -> str:
    """Run one prompt to completion and return the text.

    Continuation past the output cap and token accounting live in the provider;
    all this adds is the in-report marker for when even the continuations ran out.
    """
    text, truncated = provider.complete(
        user_prompt=user_prompt,
        usage=usage_by_model,
        log=log,
        system=system,
        thinking=thinking,
    )
    if truncated:
        text += (
            "\n\n*** ATTORNEY REVIEW — SECTION TRUNCATED: model hit output cap "
            f"after {MAX_CONTINUATIONS} continuation attempts. ***"
        )
    return text


def analyze_tract(
    *,
    provider: Provider,
    tract,
    p,                       # ParsedRunSheet (kept for forward-compat / context)
    job: JobConfig,
    commentary: str = "",
    on_progress: Callable[[str], None] | None = None,
) -> tuple[str, dict]:
    """Two-phase per-tract analysis, both phases reasoning at high effort:

      1. Mineral chain (more complex). Produces a concise mineral-vesting /
         mineral-exception block.
      2. Full report. Assembles CHAIN OF TITLE, surface vesting, description,
         and exceptions, folding in the phase-1 mineral block verbatim.

    Returns (report_text, usage_by_model) where usage_by_model maps each model
    id to its TokenUsage. With failover enabled the two phases can land on
    different providers, so that dict is also the record of what produced what.
    """
    log = on_progress or (lambda s: None)
    usage: dict[str, TokenUsage] = {}
    system = _system_prompt()

    # Phase 1 — mineral chain (skipped entirely when minerals excluded).
    mineral_text = ""
    if job.include_minerals:
        log("mineral chain analysis …")
        mineral_prompt = _build_mineral_prompt(tract.id, tract.rows, tract.le_rows, job, commentary)
        mineral_text = _generate(provider, system, mineral_prompt, usage, log, thinking=True)
    else:
        log("minerals excluded — surface-only report")

    # Phase 2 — full report, given the phase-1 mineral analysis.
    log("assembling report …")
    report_prompt = _build_tract_prompt(
        tract.id, tract.rows, tract.le_rows, job, commentary, mineral_text
    )
    report_text = _generate(provider, system, report_prompt, usage, log, thinking=True)

    return report_text, usage
