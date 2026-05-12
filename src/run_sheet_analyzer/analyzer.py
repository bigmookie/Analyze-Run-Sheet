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
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Callable


class AnalysisInterrupted(Exception):
    """Raised when the user requests a graceful stop via Ctrl+C."""


# Set by cli.py from the SIGINT handler. analyzer code checks this between
# API calls and at the boundary of each area turn so a Ctrl+C lands in
# bounded time without leaving zombies.
stop_event = threading.Event()


def _check_stop() -> None:
    if stop_event.is_set():
        raise AnalysisInterrupted("Interrupted by user")

import anthropic
from pydantic import ValidationError

from .models import Exceptions, MineralChain, SurfaceChain, TractAnalysis
from .parser import ParsedRunSheet, RunSheetRow, Tract
from .reconciler import reconcile


SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-7"

MAX_TOKENS = 16_000
THINKING_BUDGET = 8_000     # Sonnet 4.6-style fixed budget

# Opus 4.7 uses a different thinking-control surface: adaptive thinking +
# output_config.effort. Map the effort level to use when escalating.
OPUS_THINKING_EFFORT = "high"


def _model_thinking_params(model: str) -> dict:
    """Return the right thinking/output_config params for the target model.

    Sonnet 4.6 wants thinking={"type":"enabled","budget_tokens":N}.
    Opus 4.7 wants thinking={"type":"adaptive"} + output_config.effort.

    `output_config` is passed via extra_body so this works on every Anthropic
    SDK version, not just those that have added it as a typed kwarg.
    """
    if "opus-4-7" in model:
        return {
            "thinking": {"type": "adaptive"},
            "extra_body": {"output_config": {"effort": OPUS_THINKING_EFFORT}},
        }
    return {
        "thinking": {"type": "enabled", "budget_tokens": THINKING_BUDGET},
    }

# Defensive limits.
MAX_TOOL_ITERATIONS = 6        # cap on tool-use loop iterations per area turn
MAX_API_RETRIES = 5            # our own visible retry count (SDK retries are disabled)
RETRY_BACKOFF_BASE = 3.0       # seconds; doubled each retry, capped at 60s

# Beta header to unlock 1-hour cache TTL.
_CACHE_BETA_HEADER = {"anthropic-beta": "extended-cache-ttl-2025-04-11"}

# Pricing per 1M tokens (USD). Verify current rates at console.anthropic.com.
_RATES: dict[str, dict[str, float]] = {
    SONNET: {"input": 3.00,  "output": 15.00, "cache_write": 3.75,  "cache_read": 0.30},
    OPUS:   {"input": 15.00, "output": 75.00, "cache_write": 18.75, "cache_read": 1.50},
}


@dataclass
class TokenUsage:
    """Cumulative token counts for one model across all API calls."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0   # cache_creation_input_tokens
    cache_read_tokens: int = 0    # cache_read_input_tokens

    def add(self, response) -> None:
        u = response.usage
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0

    def cost_usd(self, model: str) -> float:
        rates = _RATES.get(model, _RATES[SONNET])
        return (
            self.input_tokens      * rates["input"]
            + self.output_tokens   * rates["output"]
            + self.cache_write_tokens * rates["cache_write"]
            + self.cache_read_tokens  * rates["cache_read"]
        ) / 1_000_000


def _acc(usage: dict[str, TokenUsage] | None, model: str, response) -> None:
    """Accumulate response usage into the per-model dict."""
    if usage is None:
        return
    if model not in usage:
        usage[model] = TokenUsage()
    usage[model].add(response)


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


_CACHE_LONG = {"type": "ephemeral", "ttl": "1h"}


def _build_system_blocks() -> list[dict]:
    system_text = _load_prompt("system.md")
    return [
        {"type": "text", "text": system_text, "cache_control": _CACHE_LONG},
    ]


def _build_full_runsheet_block(p: ParsedRunSheet) -> dict:
    return {
        "type": "text",
        "text": _serialize_full_run_sheet(p),
        "cache_control": _CACHE_LONG,
    }


def _build_tract_context_block(p: ParsedRunSheet, tract: Tract) -> dict:
    """Per-tract focused view. Authorities are now fetched on-demand via the
    search_authority tool, not embedded here."""
    return {
        "type": "text",
        "text": _serialize_tract(p, tract),
        "cache_control": _CACHE_LONG,
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


SEARCH_AUTHORITY_TOOL = {
    "name": "search_authority",
    "description": (
        "Search the firm's embedded reference library (Mississippi Title "
        "Examination Standards, abstractor training manual, First American "
        "Agents Manual) for guidance on a specific issue. Use this ONLY for "
        "fringe issues or novel questions where you need authoritative "
        "support beyond your training and the methodology already in the "
        "system prompt. Do NOT call this for routine determinations — your "
        "default knowledge of Mississippi title practice and the system "
        "prompt's methodology cover the standard cases."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "A focused search query describing the issue (e.g., "
                    "'forfeited tax patent curative effect', "
                    "'after-acquired title quitclaim Mississippi')."
                ),
            },
            "k": {
                "type": "integer",
                "description": "Number of chunks to retrieve (default 5, max 10).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}


def _execute_tool(name: str, input_: dict, refs_lib) -> str:
    if name == "search_authority":
        query = (input_ or {}).get("query", "")
        k = max(1, min(int((input_ or {}).get("k", 5)), 10))
        try:
            hits = refs_lib.retrieve(query, k=k, rerank=False)
        except Exception as e:
            return f"Retrieval error: {type(e).__name__}: {e}"
        if not hits:
            return "No relevant authorities found."
        lines = [f"# Retrieved authorities for: {query!r}", ""]
        for h in hits:
            lines.append(f"## {h.citation}")
            if h.parent_hierarchy:
                lines.append("_" + " › ".join(h.parent_hierarchy) + "_")
            lines.append("")
            lines.append(h.text.strip())
            lines.append("")
        return "\n".join(lines)
    return f"Unknown tool: {name}"


def _stream_thinking(stream, on_thinking: Callable[[str], None] | None) -> None:
    """Consume a messages.stream() and forward thinking deltas line-by-line.

    Anthropic emits thinking text as many small chunks; we accumulate until
    a newline and then push one line at a time to the callback.
    """
    buf = ""
    for event in stream:
        if getattr(event, "type", None) != "content_block_delta":
            continue
        delta = getattr(event, "delta", None)
        if delta is None or getattr(delta, "type", None) != "thinking_delta":
            continue
        chunk = getattr(delta, "thinking", "") or ""
        if not chunk:
            continue
        if on_thinking is None:
            continue
        buf += chunk
        while "\n" in buf:
            line, _, buf = buf.partition("\n")
            line = line.rstrip()
            if line:
                on_thinking(line)
    if on_thinking and buf.strip():
        on_thinking(buf.strip())


def _create_with_retry(
    client: anthropic.Anthropic,
    kwargs: dict,
    on_progress: Callable[[str], None] | None,
    on_thinking: Callable[[str], None] | None = None,
) -> anthropic.types.Message:
    """Visible retry loop for the Anthropic API.

    Uses messages.stream() so we can forward extended-thinking text to the
    user in real time. The SDK client is configured with max_retries=0 so
    we see every attempt; backoff is interruptible via stop_event.
    """
    import time
    import anthropic as _anthropic

    transient = (
        _anthropic.APIConnectionError,
        _anthropic.APITimeoutError,
        _anthropic.RateLimitError,
        _anthropic.InternalServerError,
    )

    for attempt in range(1, MAX_API_RETRIES + 1):
        _check_stop()
        t0 = time.time()
        try:
            with client.messages.stream(**kwargs) as stream:
                _stream_thinking(stream, on_thinking)
                resp = stream.get_final_message()
            dt = time.time() - t0
            if on_progress:
                u = resp.usage
                inp = u.input_tokens
                cached = getattr(u, "cache_read_input_tokens", 0) or 0
                cwrite = getattr(u, "cache_creation_input_tokens", 0) or 0
                out = u.output_tokens
                on_progress(
                    f"  API returned in {dt:.1f}s "
                    f"(in={inp:,} cached={cached:,} cache_write={cwrite:,} out={out:,})"
                )
            return resp
        except transient as e:
            dt = time.time() - t0
            if attempt >= MAX_API_RETRIES:
                if on_progress:
                    on_progress(f"  API gave up after {attempt} attempts: {type(e).__name__}: {e}")
                raise
            sleep_s = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), 60.0)
            if on_progress:
                on_progress(
                    f"  API attempt {attempt}/{MAX_API_RETRIES} failed in {dt:.1f}s "
                    f"({type(e).__name__}); retrying in {sleep_s:.0f}s"
                )
            if stop_event.wait(timeout=sleep_s):
                raise AnalysisInterrupted("Interrupted during retry backoff")
        except _anthropic.APIStatusError as e:
            if on_progress:
                on_progress(f"  API error: {type(e).__name__}: {e}")
            raise


def _call(
    client: anthropic.Anthropic,
    system_blocks: list[dict],
    messages: list[dict],
    model: str,
    refs_lib=None,
    usage: dict[str, TokenUsage] | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
) -> tuple[anthropic.types.Message, list[dict]]:
    """Send a request and follow any tool-use loop.

    Returns the final non-tool-use response and the updated message history
    (so the caller can append the model's last reply for the next turn).
    """
    kwargs = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system_blocks,
        messages=messages,
        extra_headers=_CACHE_BETA_HEADER,
        **_model_thinking_params(model),
    )
    if refs_lib is not None:
        kwargs["tools"] = [SEARCH_AUTHORITY_TOOL]

    for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
        resp = _create_with_retry(client, kwargs, on_progress, on_thinking)
        _acc(usage, model, resp)

        if resp.stop_reason != "tool_use":
            return resp, messages

        if iteration == MAX_TOOL_ITERATIONS:
            # Force the model to stop using tools and give a final answer.
            if on_progress:
                on_progress(
                    f"  WARNING: tool-use loop hit cap ({MAX_TOOL_ITERATIONS}); "
                    "requesting final response without tools"
                )
            kwargs.pop("tools", None)

        # Execute every tool_use block in the response.
        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                if on_progress:
                    q = (block.input or {}).get("query", "")
                    on_progress(f"  search_authority({q!r})")
                result_text = _execute_tool(block.name, block.input, refs_lib)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

        messages = messages + [
            {"role": "assistant", "content": resp.content},
            {"role": "user", "content": tool_results},
        ]
        kwargs["messages"] = messages

    # Shouldn't reach here, but if we do, do one final no-tools call.
    kwargs.pop("tools", None)
    resp = _create_with_retry(client, kwargs, on_progress)
    _acc(usage, model, resp)
    return resp, messages


def _run_area(
    client: anthropic.Anthropic,
    system_blocks: list[dict],
    messages: list[dict],
    task_block: dict,
    schema_cls,
    *,
    refs_lib,
    initial_model: str = SONNET,
    on_progress: Callable[[str], None] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    usage: dict[str, TokenUsage] | None = None,
) -> tuple[Any, str, list[dict]]:
    """Send one area turn, parse the response, escalate to Opus on low confidence.

    Returns (parsed_obj, model_used, updated_messages).
    """
    messages = messages + [{"role": "user", "content": [task_block]}]
    if on_progress:
        on_progress(f"  calling {initial_model}...")
    resp, messages = _call(
        client, system_blocks, messages, initial_model,
        refs_lib=refs_lib, usage=usage, on_progress=on_progress, on_thinking=on_thinking,
    )
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
        resp, messages = _call(
            client, system_blocks, messages, initial_model,
            refs_lib=refs_lib, usage=usage, on_progress=on_progress, on_thinking=on_thinking,
        )
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
        resp, messages = _call(
            client, system_blocks, messages, OPUS,
            refs_lib=refs_lib, usage=usage, on_progress=on_progress, on_thinking=on_thinking,
        )
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
    on_thinking: Callable[[str], None] | None = None,
) -> tuple[TractAnalysis, dict[str, TokenUsage]]:
    log = on_progress or (lambda s: None)
    usage: dict[str, TokenUsage] = {}

    _check_stop()
    system_blocks = _build_system_blocks()
    base_messages = [
        {
            "role": "user",
            "content": [
                _build_full_runsheet_block(p),
                _build_tract_context_block(p, tract),
            ],
        }
    ]

    log(f"Tract {tract.id}: surface chain …")
    surface, surface_model, messages = _run_area(
        client, system_blocks, base_messages,
        _build_task_block("surface.md", tract.id, job),
        SurfaceChain, refs_lib=refs_lib, on_progress=log, on_thinking=on_thinking, usage=usage,
    )

    _check_stop()
    log(f"Tract {tract.id}: mineral chain …")
    mineral, mineral_model, messages = _run_area(
        client, system_blocks, messages,
        _build_task_block("mineral.md", tract.id, job),
        MineralChain, refs_lib=refs_lib, on_progress=log, on_thinking=on_thinking, usage=usage,
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
        resp, messages = _call(
            client, system_blocks, messages, OPUS,
            refs_lib=refs_lib, usage=usage, on_progress=log, on_thinking=on_thinking,
        )
        text = _extract_text(resp)
        messages = messages + [{"role": "assistant", "content": text}]
        mineral = MineralChain.model_validate(_parse_json(text))
        mineral.reconciliation = reconcile(mineral)
        mineral_model = OPUS

    _check_stop()
    log(f"Tract {tract.id}: exceptions …")
    exceptions, exceptions_model, messages = _run_area(
        client, system_blocks, messages,
        _build_task_block("exceptions.md", tract.id, job),
        Exceptions, refs_lib=refs_lib, on_progress=log, on_thinking=on_thinking, usage=usage,
    )

    ta = TractAnalysis(
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
    return ta, usage


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
