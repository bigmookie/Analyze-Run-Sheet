"""Per-tract title-report generation.

For each tract:
  1. Filter run-sheet rows that belong to the tract, sort by date_recorded.
  2. Send ONE Claude call with a focused prompt.
  3. Return the model's plain-text response (the report section for that tract).

No JSON schemas, no multi-turn agentic conversations, no tool use. Just:
filter, sort, ask, get text back.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from typing import Callable

import anthropic


# Every evaluation (tract reading, mineral chain, report assembly) runs on Opus.
# OPUS is the floor: the oldest model this project will run on, and the fallback
# when the API lookup below can't be made.
OPUS = "claude-opus-5"

# By default the analyzer asks the API which models exist and uses the newest
# Opus, so a new Opus release is picked up with no code change. Set
# RUN_SHEET_MODEL in .env to pin an exact id (e.g. RUN_SHEET_MODEL=claude-opus-5)
# when a run has to be reproducible.
MODEL_ENV_VAR = "RUN_SHEET_MODEL"
_AUTO_VALUES = {"", "auto", "latest"}
_resolved_model: str | None = None
_model_lock = threading.Lock()   # tracts are analyzed in parallel

MAX_TOKENS = 16_000          # large tracts can exceed 8K; truncation triggers continuation
MAX_CONTINUATIONS = 4        # if the model still hits max_tokens, keep asking it to continue
MAX_API_RETRIES = 5
RETRY_BACKOFF_BASE = 3.0

# 1-hour cache TTL for the (small) system prompt that's reused across tracts.
_CACHE_LONG = {"type": "ephemeral", "ttl": "1h"}
_CACHE_BETA_HEADER = {"anthropic-beta": "extended-cache-ttl-2025-04-11"}

# Pricing per 1M tokens (USD) — verify at console.anthropic.com. A model that
# isn't listed (a newer Opus picked up automatically) is costed at these rates,
# so the run summary is an estimate whenever the id differs from OPUS.
_RATES: dict[str, dict[str, float]] = {
    OPUS: {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
}


def active_model(
    client: anthropic.Anthropic | None = None,
    log: Callable[[str], None] | None = None,
) -> str:
    """The model id this run uses, decided once per process.

      1. RUN_SHEET_MODEL, when set to anything other than "auto"/"latest".
      2. The newest ``claude-opus-*`` the Models API reports — never older than
         OPUS, and never outside the Opus family (so it won't drift onto a
         differently-priced tier on its own).
      3. OPUS, when the lookup fails or no client is available yet.
    """
    global _resolved_model
    with _model_lock:
        if _resolved_model:
            return _resolved_model

        pin = os.environ.get(MODEL_ENV_VAR, "").strip()
        if pin.lower() not in _AUTO_VALUES:
            _resolved_model = pin
            if log:
                log(f"model pinned by {MODEL_ENV_VAR}: {pin}")
            return _resolved_model

        if client is None:
            return OPUS      # unresolved — don't cache; a client may arrive later

        try:
            available = list(client.models.list())
        except Exception as e:   # offline, bad key, response shape change — stay on the floor
            if log:
                log(f"model lookup failed ({type(e).__name__}); using {OPUS}")
            _resolved_model = OPUS
            return _resolved_model

        opus = [m for m in available if m.id.startswith("claude-opus-")]
        newest = max(opus, key=lambda m: m.created_at, default=None)
        floor = next((m for m in opus if m.id == OPUS), None)
        if newest is None or (floor is not None and newest.created_at < floor.created_at):
            _resolved_model = OPUS
        else:
            _resolved_model = newest.id
        if log:
            log(f"model: {_resolved_model}"
                + ("" if _resolved_model == OPUS else f" (newest Opus; floor {OPUS})"))
        return _resolved_model


class AnalysisInterrupted(Exception):
    pass


stop_event = threading.Event()


def _check_stop() -> None:
    if stop_event.is_set():
        raise AnalysisInterrupted("Interrupted by user")


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, response) -> None:
        u = response.usage
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0

    def cost_usd(self, model: str) -> float:
        rates = _RATES.get(model, _RATES[OPUS])
        return (
            self.input_tokens         * rates["input"]
            + self.output_tokens      * rates["output"]
            + self.cache_write_tokens * rates["cache_write"]
            + self.cache_read_tokens  * rates["cache_read"]
        ) / 1_000_000


def _acc(usage_by_model: dict, model: str, response) -> None:
    """Accumulate one response's token usage into the per-model dict."""
    usage_by_model.setdefault(model, TokenUsage()).add(response)


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
    # When False, the mineral-chain (Opus) phase is skipped and the report covers
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
    cite = row.cite   # Book/Page when present, else Instrument No.
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
    """Prompt for the Opus mineral-chain analysis (minerals only)."""
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

    Included → hand the Opus mineral block to the assembler to fold in verbatim.
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
            "and do not raise mineral issues under ATTORNEY REVIEW. This applies to "
            "OBSERVED EXCEPTIONS too — that section still appears, but it covers only "
            "unflagged SURFACE matters (easements, defects, heirship clouds, gaps)."
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
        "- Place its **MINERAL OBSERVED EXCEPTIONS** items in the OBSERVED EXCEPTIONS "
        "section, keeping each item's basis parenthetical. Do NOT put them in a "
        "numbered bucket.\n"
        "- Place its **MINERAL LEASES** items under Exceptions bucket 5 (Mineral Leases) — "
        "except any line ending in `[UNFLAGGED]`, which goes in OBSERVED EXCEPTIONS "
        "instead. Strip the `[UNFLAGGED]` marker and replace it with a basis "
        "parenthetical in the usual form.\n"
        "- Fold its **MINERAL ATTORNEY REVIEW** items into your ATTORNEY REVIEW section.\n"
        "- Use the supplied mineral analysis for everything about the mineral estate "
        "(do not recompute fractions).\n\n"
        f"```\n{block}\n```"
    )


def _build_tract_prompt(
    tract_id: str, rows: list, le_rows: list, job: JobConfig,
    commentary: str = "", mineral_analysis: str = "",
) -> str:
    """Prompt for the Opus full-report assembly. The mineral analysis from the
    Opus phase is supplied verbatim for the assembler to fold in (or, when
    minerals are excluded, a surface-only instruction replaces it)."""
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
# API call with visible retries
# ──────────────────────────────────────────────────────────────────────────


def _create_with_retry(
    client: anthropic.Anthropic,
    kwargs: dict,
    on_progress: Callable[[str], None] | None,
) -> anthropic.types.Message:
    transient = (
        anthropic.APIConnectionError,
        anthropic.APITimeoutError,
        anthropic.RateLimitError,
        anthropic.InternalServerError,
    )
    for attempt in range(1, MAX_API_RETRIES + 1):
        _check_stop()
        t0 = time.time()
        try:
            resp = client.messages.create(**kwargs)
            dt = time.time() - t0
            if on_progress:
                u = resp.usage
                inp = u.input_tokens
                cached = getattr(u, "cache_read_input_tokens", 0) or 0
                cwrite = getattr(u, "cache_creation_input_tokens", 0) or 0
                out = u.output_tokens
                on_progress(
                    f"API returned in {dt:.1f}s "
                    f"(in={inp:,} cached={cached:,} cache_write={cwrite:,} out={out:,})"
                )
            return resp
        except transient as e:
            dt = time.time() - t0
            if attempt >= MAX_API_RETRIES:
                if on_progress:
                    on_progress(f"API gave up after {attempt} attempts: {type(e).__name__}: {e}")
                raise
            sleep_s = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), 60.0)
            if on_progress:
                on_progress(
                    f"API attempt {attempt}/{MAX_API_RETRIES} failed in {dt:.1f}s "
                    f"({type(e).__name__}); retrying in {sleep_s:.0f}s"
                )
            if stop_event.wait(timeout=sleep_s):
                raise AnalysisInterrupted("Interrupted during retry backoff")
        except anthropic.APIStatusError as e:
            if on_progress:
                on_progress(f"API error: {type(e).__name__}: {e}")
            raise


# ──────────────────────────────────────────────────────────────────────────
# Public surface
# ──────────────────────────────────────────────────────────────────────────


_CONTINUE_PROMPT = (
    "The previous response was cut off at the model output limit. "
    "Continue from exactly where you stopped. Do NOT add a preamble, "
    "do NOT repeat any content, do NOT restate context. Resume from "
    "the exact word or punctuation where the prior turn ended, "
    "even if mid-sentence."
)


def _generate(
    client: anthropic.Anthropic,
    model: str,
    system_blocks: list[dict],
    user_prompt: str,
    usage_by_model: dict,
    log: Callable[[str], None],
    *,
    thinking: bool = False,
) -> str:
    """Run one model to completion, auto-continuing if it hits max_tokens.
    Accumulates token usage into usage_by_model[model]. Returns the text."""
    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    accumulated = ""

    for attempt in range(1 + MAX_CONTINUATIONS):
        _check_stop()
        if attempt == 0:
            log(f"calling {model} …")
        else:
            log(f"output truncated; continuation #{attempt} ({model}) …")
            messages = messages + [
                {"role": "assistant", "content": accumulated},
                {"role": "user", "content": _CONTINUE_PROMPT},
            ]

        kwargs = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            messages=messages,
            extra_headers=_CACHE_BETA_HEADER,
        )
        if thinking:
            # Adaptive thinking + high effort. effort goes through extra_body to stay
            # compatible with older anthropic SDK builds that don't type output_config.
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["extra_body"] = {"output_config": {"effort": "high"}}

        resp = _create_with_retry(client, kwargs, log)
        _acc(usage_by_model, model, resp)
        chunk = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        accumulated += chunk

        if resp.stop_reason != "max_tokens":
            break
    else:
        log(f"WARNING: still truncated after {MAX_CONTINUATIONS} continuations ({model})")
        accumulated += (
            "\n\n*** ATTORNEY REVIEW — SECTION TRUNCATED: model hit output cap "
            f"after {MAX_CONTINUATIONS} continuation attempts. ***"
        )

    return accumulated.strip()


def analyze_tract(
    *,
    client: anthropic.Anthropic,
    tract,
    p,                       # ParsedRunSheet (kept for forward-compat / context)
    job: JobConfig,
    commentary: str = "",
    on_progress: Callable[[str], None] | None = None,
) -> tuple[str, dict]:
    """Two-phase per-tract analysis, both phases on Opus with adaptive thinking:

      1. Mineral chain (more complex). Produces a concise mineral-vesting /
         mineral-exception block.
      2. Full report. Assembles CHAIN OF TITLE, surface vesting, description,
         and exceptions, folding in the phase-1 mineral block verbatim.

    Returns (report_text, usage_by_model) where usage_by_model maps each model
    id to its TokenUsage.
    """
    log = on_progress or (lambda s: None)
    usage: dict[str, TokenUsage] = {}
    model = active_model(client, log)
    system_blocks = [{"type": "text", "text": _system_prompt(), "cache_control": _CACHE_LONG}]

    # Phase 1 — mineral chain on Opus (skipped entirely when minerals excluded).
    mineral_text = ""
    if job.include_minerals:
        log("mineral chain analysis (Opus) …")
        mineral_prompt = _build_mineral_prompt(tract.id, tract.rows, tract.le_rows, job, commentary)
        mineral_text = _generate(client, model, system_blocks, mineral_prompt, usage, log, thinking=True)
    else:
        log("minerals excluded — surface-only report")

    # Phase 2 — full report on Opus, given the Opus mineral analysis.
    log("assembling report (Opus) …")
    report_prompt = _build_tract_prompt(
        tract.id, tract.rows, tract.le_rows, job, commentary, mineral_text
    )
    report_text = _generate(
        client, model, system_blocks, report_prompt, usage, log, thinking=True
    )

    return report_text, usage
