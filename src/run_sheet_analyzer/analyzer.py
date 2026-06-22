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


SONNET = "claude-sonnet-4-6"          # surface chain, description, exceptions, assembly
OPUS = "claude-opus-4-8"              # mineral chain analysis (more complex)

MAX_TOKENS = 16_000          # large tracts can exceed 8K; truncation triggers continuation
MAX_CONTINUATIONS = 4        # if the model still hits max_tokens, keep asking it to continue
MAX_API_RETRIES = 5
RETRY_BACKOFF_BASE = 3.0

# 1-hour cache TTL for the (small) system prompt that's reused across tracts.
_CACHE_LONG = {"type": "ephemeral", "ttl": "1h"}
_CACHE_BETA_HEADER = {"anthropic-beta": "extended-cache-ttl-2025-04-11"}

# Pricing per 1M tokens (USD) — verify at console.anthropic.com.
_RATES: dict[str, dict[str, float]] = {
    SONNET: {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    OPUS:   {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
}


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
        rates = _RATES.get(model, _RATES[SONNET])
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


def _build_tract_prompt(
    tract_id: str, rows: list, le_rows: list, job: JobConfig,
    commentary: str = "", mineral_analysis: str = "",
) -> str:
    """Prompt for the Sonnet full-report assembly. The mineral analysis from the
    Opus phase is supplied verbatim for the assembler to fold in."""
    events_text, le_text = _events_blocks(rows, le_rows)
    parcel = job.for_tract(tract_id)
    job_context_parts = [
        f"Addressee: {job.addressee or '—'}",
        f"County:    {job.county or '—'}",
    ]
    if parcel:
        job_context_parts.append(f"Parcel ID for this tract: {parcel.get('parcel_id', '—')}")
    job_context = "\n".join(job_context_parts)

    mineral_block = (mineral_analysis or "").strip() or "_No mineral analysis provided._"

    template = _load_prompt("tract.md")
    return template.format(
        tract_id=tract_id,
        effective_date=job.effective_date or "<not provided>",
        commentary=_commentary_block(commentary),
        events=events_text,
        le_rows=le_text,
        job_context=job_context,
        mineral_analysis=mineral_block,
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
            # Opus 4.8 uses adaptive thinking; effort via extra_body keeps this
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
    """Two-phase per-tract analysis:

      1. Mineral chain — Opus 4.8 (more complex; adaptive thinking). Produces a
         concise mineral-vesting / mineral-exception block.
      2. Full report — Sonnet 4.6. Assembles CHAIN OF TITLE, surface vesting,
         description, and exceptions, folding in the Opus mineral block verbatim.

    Returns (report_text, usage_by_model) where usage_by_model maps each model
    id to its TokenUsage.
    """
    log = on_progress or (lambda s: None)
    usage: dict[str, TokenUsage] = {}
    system_blocks = [{"type": "text", "text": _system_prompt(), "cache_control": _CACHE_LONG}]

    # Phase 1 — mineral chain on Opus.
    log("mineral chain analysis (Opus) …")
    mineral_prompt = _build_mineral_prompt(tract.id, tract.rows, tract.le_rows, job, commentary)
    mineral_text = _generate(client, OPUS, system_blocks, mineral_prompt, usage, log, thinking=True)

    # Phase 2 — full report on Sonnet, given the Opus mineral analysis.
    log("assembling report (Sonnet) …")
    report_prompt = _build_tract_prompt(
        tract.id, tract.rows, tract.le_rows, job, commentary, mineral_text
    )
    report_text = _generate(client, SONNET, system_blocks, report_prompt, usage, log)

    return report_text, usage
