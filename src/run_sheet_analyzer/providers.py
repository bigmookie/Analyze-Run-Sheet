"""LLM providers: Anthropic Claude (primary) and OpenAI (fallback).

Every model call in the project goes through one object with a single method::

    text, truncated = provider.complete(
        user_prompt=..., usage=..., log=..., system=..., thinking=...,
    )

``complete`` runs one prompt to completion, auto-continuing when the model hits
its output cap, and accumulates token usage into ``usage`` keyed by model id.

Three provider shapes:

  * ``AnthropicProvider`` — Claude via the Messages API, adaptive thinking at
    high effort, 1-hour prompt caching on the system block.
  * ``OpenAIProvider``    — GPT via the Responses API, ``reasoning.effort``,
    automatic prompt caching.
  * ``FailoverProvider``  — tries Claude, and if Claude is unavailable (529
    overloaded, 5xx, rate limits, connection failures, bad key, missing model)
    re-runs that call on OpenAI. Per-call failover, so one overloaded window no
    longer kills a whole run. After a failover the primary is skipped for a
    cooldown window instead of every remaining tract paying the retry latency.

Build one with ``build_provider()``; it reads the env and available API keys.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import anthropic


# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────

# Claude's visible-output cap per call. Large tracts exceed it; truncation
# triggers the continuation loop rather than a partial report.
MAX_TOKENS = 16_000
MAX_CONTINUATIONS = 4        # if the model still hits the cap, keep asking it to continue
MAX_API_RETRIES = 5
RETRY_BACKOFF_BASE = 3.0

# Floor models: the oldest each provider will run on, and the fallback when the
# API lookup can't be made.
OPUS = "claude-opus-5"
GPT = "gpt-5.6-sol"          # OpenAI's frontier tier; "gpt-5.6" aliases to it

# Reasoning models bill hidden reasoning tokens as output and count them against
# the output cap, so OpenAI gets a larger budget than Claude for the same amount
# of visible text. Unused headroom costs nothing — you pay for what's generated.
OPENAI_MAX_OUTPUT = 48_000

MODEL_ENV_VAR = "RUN_SHEET_MODEL"
OPENAI_MODEL_ENV_VAR = "RUN_SHEET_OPENAI_MODEL"
PROVIDER_ENV_VAR = "RUN_SHEET_PROVIDER"
EFFORT_ENV_VAR = "RUN_SHEET_OPENAI_EFFORT"
MAX_OUTPUT_ENV_VAR = "RUN_SHEET_OPENAI_MAX_OUTPUT"
STORE_ENV_VAR = "RUN_SHEET_OPENAI_STORE"
COOLDOWN_ENV_VAR = "RUN_SHEET_FAILOVER_COOLDOWN"

_AUTO_VALUES = {"", "auto", "latest"}

# Pricing per 1M tokens (USD) — verify at console.anthropic.com and
# platform.openai.com. A model that isn't listed is costed at its family's
# rates, so the run summary is an estimate whenever the id differs from these.
_RATES: dict[str, dict[str, float]] = {
    OPUS: {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    GPT:  {"input": 5.00, "output": 30.00, "cache_write": 6.25, "cache_read": 0.50},
}

# HTTP statuses worth another attempt. 529 (Anthropic "overloaded") is the one
# that matters most here: the SDK models it as a plain APIStatusError, not an
# InternalServerError, so classifying by status rather than by exception class
# keeps this correct across SDK versions.
_RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

# Statuses that mean *our request* is malformed. Retrying won't help and neither
# will another provider — the same request would fail there too.
_FATAL_STATUS = frozenset({400, 422})


class AnalysisInterrupted(Exception):
    pass


stop_event = threading.Event()


def _check_stop() -> None:
    if stop_event.is_set():
        raise AnalysisInterrupted("Interrupted by user")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# ──────────────────────────────────────────────────────────────────────────
# Token accounting
# ──────────────────────────────────────────────────────────────────────────


def _rates_for(model: str) -> dict[str, float]:
    if model in _RATES:
        return _RATES[model]
    if model.startswith("claude-"):
        return _RATES[OPUS]
    if model.startswith("gpt-"):
        return _RATES[GPT]
    return _RATES[OPUS]


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, counts: "TokenUsage") -> None:
        self.input_tokens       += counts.input_tokens
        self.output_tokens      += counts.output_tokens
        self.cache_write_tokens += counts.cache_write_tokens
        self.cache_read_tokens  += counts.cache_read_tokens

    def cost_usd(self, model: str) -> float:
        rates = _rates_for(model)
        return (
            self.input_tokens         * rates["input"]
            + self.output_tokens      * rates["output"]
            + self.cache_write_tokens * rates["cache_write"]
            + self.cache_read_tokens  * rates["cache_read"]
        ) / 1_000_000

    def summary(self) -> str:
        return (
            f"in={self.input_tokens:,} cached={self.cache_read_tokens:,} "
            f"cache_write={self.cache_write_tokens:,} out={self.output_tokens:,}"
        )


# ──────────────────────────────────────────────────────────────────────────
# Retry / failover classification
# ──────────────────────────────────────────────────────────────────────────


def _is_transient(exc: Exception) -> bool:
    """Worth another attempt against the same provider."""
    if type(exc).__name__ in ("APIConnectionError", "APITimeoutError"):
        return True
    return getattr(exc, "status_code", None) in _RETRY_STATUS


def _retry_call(
    fn: Callable[[], Any], log: Callable[[str], None] | None = None
) -> Any:
    """Call ``fn`` with visible exponential backoff on transient failures."""
    say = log or (lambda s: None)
    for attempt in range(1, MAX_API_RETRIES + 1):
        _check_stop()
        t0 = time.time()
        try:
            return fn()
        except AnalysisInterrupted:
            raise
        except Exception as e:
            dt = time.time() - t0
            status = getattr(e, "status_code", None)
            label = f"{type(e).__name__}" + (f" {status}" if status else "")
            if not _is_transient(e):
                say(f"API error: {label}: {e}")
                raise
            if attempt >= MAX_API_RETRIES:
                say(f"API gave up after {attempt} attempts: {label}: {e}")
                raise
            sleep_s = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), 60.0)
            say(
                f"API attempt {attempt}/{MAX_API_RETRIES} failed in {dt:.1f}s "
                f"({label}); retrying in {sleep_s:.0f}s"
            )
            if stop_event.wait(timeout=sleep_s):
                raise AnalysisInterrupted("Interrupted during retry backoff")


# ──────────────────────────────────────────────────────────────────────────
# Provider base — the continuation loop, shared by both providers
# ──────────────────────────────────────────────────────────────────────────


_CONTINUE_PROMPT = (
    "The previous response was cut off at the model output limit. "
    "Continue from exactly where you stopped. Do NOT add a preamble, "
    "do NOT repeat any content, do NOT restate context. Resume from "
    "the exact word or punctuation where the prior turn ended, "
    "even if mid-sentence."
)


class _BaseProvider:
    """Shared continuation loop. Subclasses implement ``_one_call``/``_resolve``."""

    name = ""
    errors: tuple[type[Exception], ...] = ()

    def __init__(self) -> None:
        self._model: str | None = None
        self._model_lock = threading.Lock()   # tracts are analyzed in parallel

    # -- model resolution ------------------------------------------------
    def model(self, log: Callable[[str], None] | None = None) -> str:
        with self._model_lock:
            if self._model is None:
                self._model = self._resolve(log)
            return self._model

    def _resolve(self, log: Callable[[str], None] | None) -> str:
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}: {self.model()}"

    # -- one API call ----------------------------------------------------
    def _one_call(
        self,
        *,
        system: str | None,
        messages: list[dict],
        thinking: bool,
        log: Callable[[str], None],
    ) -> tuple[str, bool, TokenUsage]:
        """Returns (text, hit_output_cap, usage_counts)."""
        raise NotImplementedError

    # -- prompt to completion --------------------------------------------
    def complete(
        self,
        *,
        user_prompt: str,
        usage: dict[str, TokenUsage] | None = None,
        log: Callable[[str], None] | None = None,
        system: str | None = None,
        thinking: bool = False,
    ) -> tuple[str, bool]:
        """Run one prompt to completion, auto-continuing past the output cap.

        Accumulates token usage into ``usage[model]``. Returns
        ``(text, still_truncated)``; ``still_truncated`` is True only if the
        model was cut off even after ``MAX_CONTINUATIONS`` attempts.
        """
        say = log or (lambda s: None)
        usage = usage if usage is not None else {}
        model = self.model(say)
        messages: list[dict] = [{"role": "user", "content": user_prompt}]
        accumulated = ""

        for attempt in range(1 + MAX_CONTINUATIONS):
            _check_stop()
            if attempt == 0:
                say(f"calling {model} …")
            else:
                say(f"output truncated; continuation #{attempt} ({model}) …")
                messages = messages + [
                    {"role": "assistant", "content": accumulated},
                    {"role": "user", "content": _CONTINUE_PROMPT},
                ]

            text, hit_cap, counts = self._one_call(
                system=system, messages=messages, thinking=thinking, log=say
            )
            usage.setdefault(model, TokenUsage()).add(counts)
            accumulated += text

            if not hit_cap:
                return accumulated.strip(), False

        say(f"WARNING: still truncated after {MAX_CONTINUATIONS} continuations ({model})")
        return accumulated.strip(), True


# ──────────────────────────────────────────────────────────────────────────
# Anthropic
# ──────────────────────────────────────────────────────────────────────────

# 1-hour cache TTL for the (small) system prompt that's reused across tracts.
_CACHE_LONG = {"type": "ephemeral", "ttl": "1h"}
_CACHE_BETA_HEADER = {"anthropic-beta": "extended-cache-ttl-2025-04-11"}


class AnthropicProvider(_BaseProvider):
    name = "anthropic"
    errors = (anthropic.APIError,)

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        super().__init__()
        # max_retries=0: _retry_call owns the backoff so it's visible in the log.
        self.client = client or anthropic.Anthropic(max_retries=0, timeout=600.0)

    def _resolve(self, log: Callable[[str], None] | None) -> str:
        """The model id this run uses on Claude.

          1. RUN_SHEET_MODEL, when set to anything other than "auto"/"latest".
          2. The newest ``claude-opus-*`` the Models API reports — never older
             than OPUS, and never outside the Opus family (so it won't drift
             onto a differently-priced tier on its own).
          3. OPUS, when the lookup fails.
        """
        pin = os.environ.get(MODEL_ENV_VAR, "").strip()
        if pin.lower() not in _AUTO_VALUES:
            if log:
                log(f"model pinned by {MODEL_ENV_VAR}: {pin}")
            return pin

        try:
            available = list(self.client.models.list())
        except Exception as e:   # offline, bad key, response shape change
            if log:
                log(f"model lookup failed ({type(e).__name__}); using {OPUS}")
            return OPUS

        opus = [m for m in available if m.id.startswith("claude-opus-")]
        newest = max(opus, key=lambda m: m.created_at, default=None)
        floor = next((m for m in opus if m.id == OPUS), None)
        if newest is None or (floor is not None and newest.created_at < floor.created_at):
            resolved = OPUS
        else:
            resolved = newest.id
        if log:
            log(f"model: {resolved}"
                + ("" if resolved == OPUS else f" (newest Opus; floor {OPUS})"))
        return resolved

    def _one_call(self, *, system, messages, thinking, log):
        kwargs: dict[str, Any] = dict(
            model=self.model(log),
            max_tokens=MAX_TOKENS,
            messages=messages,
            extra_headers=_CACHE_BETA_HEADER,
        )
        if system:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": _CACHE_LONG}
            ]
        if thinking:
            # Adaptive thinking + high effort. effort goes through extra_body to
            # stay compatible with older anthropic SDK builds that don't type
            # output_config.
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["extra_body"] = {"output_config": {"effort": "high"}}

        t0 = time.time()
        resp = _retry_call(lambda: self.client.messages.create(**kwargs), log)
        u = resp.usage
        counts = TokenUsage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        )
        log(f"API returned in {time.time() - t0:.1f}s ({counts.summary()})")
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return text, resp.stop_reason == "max_tokens", counts


# ──────────────────────────────────────────────────────────────────────────
# OpenAI
# ──────────────────────────────────────────────────────────────────────────

# Model families that can't produce a title report, so they're never candidates
# when the preferred id isn't visible to the key.
_OPENAI_AUX = (
    "image", "realtime", "audio", "transcribe", "tts", "whisper", "embedding",
    "moderation", "sora", "search", "computer-use", "codex", "chat-latest",
)
# Cheaper/smaller tiers — usable, but only if no frontier tier is available.
_OPENAI_CHEAP = ("mini", "nano", "luna", "terra")

# Stable cache key so parallel workers sharing the system prompt land on the
# same cached prefix (Responses API caches automatically above 1024 tokens).
_PROMPT_CACHE_KEY = "run-sheet-analyzer-v1"


class OpenAIProvider(_BaseProvider):
    name = "openai"

    def __init__(self, client: Any | None = None) -> None:
        super().__init__()
        import openai   # imported lazily so the package is optional

        self._openai = openai
        self.errors = (openai.APIError,)
        self.client = client or openai.OpenAI(max_retries=0, timeout=600.0)

    def _resolve(self, log: Callable[[str], None] | None) -> str:
        """The model id this run uses on OpenAI.

          1. RUN_SHEET_OPENAI_MODEL, when set to anything but "auto"/"latest".
          2. GPT (the frontier tier), when the key can see it.
          3. The newest plausible ``gpt-5*`` text model the key *can* see —
             frontier tiers preferred over mini/nano tiers.
        """
        pin = os.environ.get(OPENAI_MODEL_ENV_VAR, "").strip()
        if pin.lower() not in _AUTO_VALUES:
            if log:
                log(f"OpenAI model pinned by {OPENAI_MODEL_ENV_VAR}: {pin}")
            return pin

        try:
            available = list(self.client.models.list())
        except Exception as e:
            if log:
                log(f"OpenAI model lookup failed ({type(e).__name__}); using {GPT}")
            return GPT

        ids = {m.id for m in available}
        if GPT in ids:
            return GPT

        gpt5 = [
            m for m in available
            if m.id.startswith("gpt-5") and not any(x in m.id for x in _OPENAI_AUX)
        ]
        frontier = [m for m in gpt5 if not any(x in m.id for x in _OPENAI_CHEAP)]
        newest = max(frontier or gpt5, key=lambda m: m.created, default=None)
        if newest is None:
            if log:
                log(f"WARNING: no gpt-5* model visible to this OpenAI key; "
                    f"trying {GPT} anyway")
            return GPT
        if log:
            log(f"WARNING: {GPT} is not available to this OpenAI key; "
                f"using {newest.id} instead")
        return newest.id

    def _effort(self, thinking: bool) -> str:
        if not thinking:
            return "low"
        return os.environ.get(EFFORT_ENV_VAR, "").strip() or "high"

    def _one_call(self, *, system, messages, thinking, log):
        model = self.model(log)
        kwargs: dict[str, Any] = dict(
            model=model,
            input=messages,
            max_output_tokens=_env_int(MAX_OUTPUT_ENV_VAR, OPENAI_MAX_OUTPUT),
            reasoning={"effort": self._effort(thinking)},
            # Off by default: run sheets are confidential client work, so don't
            # leave a 30-day server-side copy of the prompt behind. Set
            # RUN_SHEET_OPENAI_STORE=1 to opt in.
            store=_truthy(os.environ.get(STORE_ENV_VAR)),
            # prompt_cache_key rides in extra_body so this works on older
            # openai SDK builds that don't type the parameter.
            extra_body={"prompt_cache_key": _PROMPT_CACHE_KEY},
        )
        if system:
            kwargs["instructions"] = system

        t0 = time.time()
        resp = _retry_call(lambda: self.client.responses.create(**kwargs), log)
        counts = _openai_counts(resp)
        log(f"API returned in {time.time() - t0:.1f}s ({counts.summary()})")

        hit_cap = (
            getattr(resp, "status", None) == "incomplete"
            and getattr(getattr(resp, "incomplete_details", None), "reason", None)
            == "max_output_tokens"
        )
        return _openai_text(resp), hit_cap, counts


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _openai_text(resp: Any) -> str:
    """The visible text of a Responses API reply, reasoning items excluded."""
    text = getattr(resp, "output_text", None)
    if text:
        return text
    parts: list[str] = []
    for item in getattr(resp, "output", None) or []:
        for block in getattr(item, "content", None) or []:
            if getattr(block, "type", None) == "output_text":
                parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _openai_counts(resp: Any) -> TokenUsage:
    """Normalize Responses API usage onto the Anthropic-shaped buckets.

    OpenAI reports ``input_tokens`` as the *total* prompt size with the cached
    and cache-written portions broken out as subsets; Anthropic reports the
    three buckets disjointly. Subtracting keeps ``cost_usd`` honest — a
    cache-written token bills once at 1.25x input, not at 1x plus 1.25x.
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return TokenUsage()
    in_details = getattr(u, "input_tokens_details", None)
    out_details = getattr(u, "output_tokens_details", None)
    cached = getattr(in_details, "cached_tokens", 0) or 0
    written = getattr(in_details, "cache_write_tokens", 0) or 0
    total_in = getattr(u, "input_tokens", 0) or 0
    return TokenUsage(
        input_tokens=max(0, total_in - cached - written),
        # Reasoning tokens are billed as output and already included in
        # output_tokens; out_details is read only to keep that assumption
        # explicit if OpenAI ever splits them out of the total.
        output_tokens=(getattr(u, "output_tokens", 0) or 0)
        or (getattr(out_details, "reasoning_tokens", 0) or 0),
        cache_write_tokens=written,
        cache_read_tokens=cached,
    )


# ──────────────────────────────────────────────────────────────────────────
# Failover
# ──────────────────────────────────────────────────────────────────────────


def _is_unavailable(provider: _BaseProvider, exc: Exception) -> bool:
    """True when ``exc`` means "this provider can't serve the request right now".

    Overload, rate limits, 5xx, connection failures, a bad key, a model the
    account can't reach — all worth trying elsewhere. A malformed request is
    not: the other provider would reject it too.
    """
    if not isinstance(exc, provider.errors or (Exception,)):
        return False
    return getattr(exc, "status_code", None) not in _FATAL_STATUS


class FailoverProvider:
    """Tries the primary, falls back to the secondary for the whole call.

    Failover is per ``complete()`` call, so a tract that Claude can't serve is
    re-run from scratch on OpenAI rather than spliced together mid-section.
    After a failover the primary is skipped for ``cooldown`` seconds — without
    that, every remaining tract pays the full retry ladder before giving up on
    an outage that's already known to be in progress.
    """

    name = "auto"

    def __init__(self, primary: _BaseProvider, fallback: _BaseProvider,
                 cooldown: float | None = None) -> None:
        self.primary = primary
        self.fallback = fallback
        self.cooldown = float(
            cooldown if cooldown is not None else _env_int(COOLDOWN_ENV_VAR, 300)
        )
        self._lock = threading.Lock()
        self._skip_until = 0.0

    def model(self, log: Callable[[str], None] | None = None) -> str:
        return self.primary.model(log)

    def describe(self) -> str:
        return (f"{self.primary.model()}  (fallback: {self.fallback.model()}"
                f", cooldown {self.cooldown:.0f}s)")

    def _primary_available(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._skip_until

    def _trip(self) -> bool:
        """Start (or extend) the cooldown. True if this call started it."""
        with self._lock:
            now = time.monotonic()
            first = now >= self._skip_until
            self._skip_until = now + self.cooldown
            return first

    def complete(self, **kwargs) -> tuple[str, bool]:
        say = kwargs.get("log") or (lambda s: None)
        if self._primary_available():
            try:
                return self.primary.complete(**kwargs)
            except AnalysisInterrupted:
                raise
            except Exception as e:
                if not _is_unavailable(self.primary, e):
                    raise
                status = getattr(e, "status_code", None)
                label = type(e).__name__ + (f" {status}" if status else "")
                if self._trip():
                    say(f"{self.primary.name} unavailable ({label}) — failing over "
                        f"to {self.fallback.model(say)} and skipping "
                        f"{self.primary.name} for {self.cooldown:.0f}s")
                else:
                    say(f"{self.primary.name} unavailable ({label}) — "
                        f"failing over to {self.fallback.model(say)}")
        else:
            say(f"{self.primary.name} in failover cooldown — "
                f"using {self.fallback.model(say)}")
        return self.fallback.complete(**kwargs)


# ──────────────────────────────────────────────────────────────────────────
# Construction
# ──────────────────────────────────────────────────────────────────────────

Provider = _BaseProvider   # for type hints at call sites

# Every provider's error base, for call sites that catch API failures broadly.
API_ERRORS: tuple[type[Exception], ...] = (anthropic.APIError, OSError, ValueError)


class ProviderConfigError(RuntimeError):
    pass


def _has(key: str) -> bool:
    return bool(os.environ.get(key, "").strip())


def env_summary() -> str:
    """One line describing which keys are present — for the startup banner."""
    have = []
    if _has("ANTHROPIC_API_KEY"):
        have.append("ANTHROPIC_API_KEY")
    if _has("OPENAI_API_KEY"):
        have.append("OPENAI_API_KEY")
    return ", ".join(have) or "none"


def check_env() -> str | None:
    """None when the env can support a run; otherwise the error to print."""
    choice = os.environ.get(PROVIDER_ENV_VAR, "").strip().lower() or "auto"
    if choice not in ("auto", "anthropic", "claude", "openai"):
        return (f"{PROVIDER_ENV_VAR}={choice!r} is not valid. "
                "Use 'auto', 'anthropic', or 'openai'.")
    if choice in ("anthropic", "claude") and not _has("ANTHROPIC_API_KEY"):
        return (f"{PROVIDER_ENV_VAR}={choice} but ANTHROPIC_API_KEY is not set.\n"
                "Set it in .env, or switch to 'auto'/'openai'.")
    if choice == "openai" and not _has("OPENAI_API_KEY"):
        return (f"{PROVIDER_ENV_VAR}=openai but OPENAI_API_KEY is not set.\n"
                "Set it in .env, or switch to 'auto'/'anthropic'.")
    if not _has("ANTHROPIC_API_KEY") and not _has("OPENAI_API_KEY"):
        return ("No API key found. Copy .env.example to .env in the project root "
                "and set ANTHROPIC_API_KEY and/or OPENAI_API_KEY.")
    return None


def build_provider(log: Callable[[str], None] | None = None):
    """Build the provider for this run from the env.

    ``RUN_SHEET_PROVIDER`` controls the choice:

      auto (default) — Claude, with OpenAI as automatic per-call failover when
                       both keys are set.
      anthropic      — Claude only; a Claude outage fails the run.
      openai         — OpenAI only.
    """
    err = check_env()
    if err:
        raise ProviderConfigError(err)

    say = log or (lambda s: None)
    choice = os.environ.get(PROVIDER_ENV_VAR, "").strip().lower() or "auto"

    if choice == "openai":
        return OpenAIProvider()
    if choice in ("anthropic", "claude"):
        return AnthropicProvider()

    # auto
    if not _has("ANTHROPIC_API_KEY"):
        say("ANTHROPIC_API_KEY not set — running on OpenAI.")
        return OpenAIProvider()
    primary = AnthropicProvider()
    if not _has("OPENAI_API_KEY"):
        say("OPENAI_API_KEY not set — no fallback provider "
            "(a Claude outage will fail the run).")
        return primary
    try:
        fallback = OpenAIProvider()
    except ImportError:
        say("openai package not installed — no fallback provider. "
            "Install it with: pip install openai")
        return primary
    return FailoverProvider(primary, fallback)
