"""Command-line entry point for the Run Sheet Analyzer.

Usage:
    python analyze_run_sheet.py
    python analyze_run_sheet.py "C:\\path\\to\\Run Sheet.xlsx"

The script prompts in the terminal for everything it needs — no GUI dialogs.
"""
from __future__ import annotations

import argparse
import os
import platform
import signal
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from tkinter import filedialog

import yaml

# Ensure src/ is on path when run as a script.
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import anthropic

from run_sheet_analyzer import analyzer as analyzer_mod
from run_sheet_analyzer import cache as cache_mod
from run_sheet_analyzer.analyzer import (
    OPUS, SONNET, AnalysisInterrupted, TokenUsage, JobConfig,
    analyze_tract, load_refs_or_die, stop_event,
)
from run_sheet_analyzer.parser import MissingColumnsError, parse
from run_sheet_analyzer.renderer import render_report
from run_sheet_analyzer.template_builder import ensure_template


TEMPLATE_SOURCE = ROOT / "Abstract - Report - Minerals.docx"
TEMPLATE_DEST = ROOT / "templates" / "title-report.docx"
REFS_DIR = ROOT / "refs"


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _check_env() -> str | None:
    missing = []
    if not os.environ.get("VOYAGE_API_KEY"):
        missing.append("VOYAGE_API_KEY")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        return (
            "Missing environment variable(s): " + ", ".join(missing) +
            ".\n\nCopy .env.example to .env in the project root and fill in your keys."
        )
    return None


def _clean_path(raw: str) -> str:
    """Strip quoting that PowerShell / drag-and-drop may include."""
    return raw.strip().strip('"').strip("'")


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"  {label}{suffix}: ").strip()
    return val or default


def _confirm(label: str, default_yes: bool = True) -> bool:
    suffix = "[Y/n]" if default_yes else "[y/N]"
    while True:
        val = input(f"  {label} {suffix}: ").strip().lower()
        if not val:
            return default_yes
        if val in ("y", "yes"):
            return True
        if val in ("n", "no"):
            return False


def _load_job(run_sheet_dir: Path) -> JobConfig:
    job_path = run_sheet_dir / "job.yaml"
    today = date.today().strftime("%B %d, %Y")
    if not job_path.exists():
        return JobConfig(signing_date=today)
    try:
        data = yaml.safe_load(job_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"WARNING: could not read job.yaml: {e}; continuing without it.", flush=True)
        return JobConfig(signing_date=today)
    return JobConfig(
        effective_date=str(data.get("effective_date", "") or ""),
        addressee=str(data.get("addressee", "") or ""),
        county=str(data.get("county", "") or ""),
        state=str(data.get("state", "Mississippi") or "Mississippi"),
        signing_date=str(data.get("signing_date", today) or today),
        parcels=data.get("parcels") or {},
    )


def _prompt_job_fields(current: JobConfig) -> JobConfig:
    print()
    print("Job details (press Enter to accept the bracketed default):", flush=True)
    addressee = _prompt("Addressee", current.addressee or "")
    effective = _prompt("Effective Date (e.g. March 13, 2026)", current.effective_date or "")
    county = _prompt("County", current.county or "Simpson")
    signing = _prompt(
        "Signing Date",
        current.signing_date or date.today().strftime("%B %d, %Y"),
    )
    return JobConfig(
        effective_date=effective,
        addressee=addressee,
        county=county,
        state=current.state,
        signing_date=signing,
        parcels=current.parcels,
    )


def _prompt_tracts(tract_ids: list[str]) -> list[str]:
    print()
    print(f"Discovered {len(tract_ids)} tracts:", flush=True)
    print("  " + ", ".join(tract_ids), flush=True)
    raw = input(
        "  Tracts to SKIP (comma-separated, blank to run all): "
    ).strip()
    if not raw:
        return list(tract_ids)
    skip = {t.strip() for t in raw.split(",") if t.strip()}
    unknown = skip - set(tract_ids)
    if unknown:
        print(f"  WARNING: unknown tract(s) ignored: {', '.join(sorted(unknown))}", flush=True)
    return [t for t in tract_ids if t not in skip]


def _pick_run_sheet_dialog() -> str | None:
    """Open a Tk file picker for the run sheet, then destroy the Tk root.

    Tk is used only here; the rest of the program is pure CLI.
    """
    root = tk.Tk()
    root.withdraw()
    # Windows: shove the dialog to the foreground so it doesn't get stranded
    # behind PowerShell.
    try:
        root.attributes("-topmost", True)
        root.update()
    except Exception:
        pass
    try:
        path = filedialog.askopenfilename(
            parent=root,
            title="Select a run sheet (.xlsx)",
            filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
        )
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return path or None


_ctrl_c_count = 0


def _install_interrupt_handler() -> None:
    """Two-stage Ctrl+C:
       1st press → drain in-flight work, write any completed reports, exit cleanly.
       2nd press → immediate force-quit (os._exit).
    """
    def handler(signum, frame):
        global _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            stop_event.set()
            print(
                "\n\n!! Ctrl+C — finishing in-flight API calls, then stopping.\n"
                "   Press Ctrl+C AGAIN to force-quit immediately.\n",
                flush=True,
            )
        else:
            print("\nForce-quit.\n", flush=True)
            os._exit(130)
    try:
        signal.signal(signal.SIGINT, handler)
    except Exception:
        pass


def _open_file_native(path: Path) -> None:
    try:
        if platform.system() == "Windows":
            os.startfile(str(path))   # noqa
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze a real-estate run sheet and produce a draft "
                    "Mississippi title report.",
    )
    parser.add_argument(
        "run_sheet",
        nargs="?",
        help="Path to the run sheet .xlsx (if omitted, you'll be prompted).",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Run all tracts and accept all defaults without prompting.",
    )
    args = parser.parse_args()

    _install_interrupt_handler()

    print(flush=True)
    print("=" * 60, flush=True)
    print("  Run Sheet Analyzer", flush=True)
    print(f"  Analysis model  : {SONNET}", flush=True)
    print(f"  Escalation model: {OPUS}  (on low confidence)", flush=True)
    print("  Kill: press Ctrl+C (twice to force-quit).", flush=True)
    print("=" * 60, flush=True)
    print(flush=True)

    env_err = _check_env()
    if env_err:
        print(f"ERROR: {env_err}", flush=True)
        return 1

    # Run sheet path — file dialog by default, CLI arg overrides it.
    if args.run_sheet:
        rs_path = Path(_clean_path(args.run_sheet))
    else:
        print(">> Opening file picker — select your run sheet .xlsx.", flush=True)
        print("   (If the dialog doesn't appear, check the taskbar for a Tk window.)",
              flush=True)
        picked = _pick_run_sheet_dialog()
        if not picked:
            print("No file selected; exiting.", flush=True)
            return 1
        rs_path = Path(picked)

    if not rs_path.is_file():
        print(f"ERROR: file not found: {rs_path}", flush=True)
        return 1
    print(f"Selected: {rs_path}", flush=True)

    # Parse
    print("Parsing run sheet …", flush=True)
    try:
        parsed = parse(rs_path)
    except MissingColumnsError as e:
        print(f"ERROR: {e}", flush=True)
        return 1
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return 1
    print(f"  {len(parsed.rows)} rows, {len(parsed.tract_ids())} tracts, "
          f"{len(parsed.not_subject_rows)} NS, {len(parsed.unparseable_le_rows)} bare-LE",
          flush=True)

    if parsed.unparseable_le_rows:
        msg = (
            f"  {len(parsed.unparseable_le_rows)} row(s) tagged as Less-and-Except "
            "have no base tract; they will be omitted."
        )
        print(msg, flush=True)
        if not args.yes and not _confirm("Continue?", default_yes=True):
            print("Aborted.", flush=True)
            return 1

    # Reference library
    print("Loading reference libraries …", flush=True)
    try:
        refs_lib = load_refs_or_die(REFS_DIR)
    except RuntimeError as e:
        print(f"ERROR: {e}", flush=True)
        return 1
    print(f"  loaded: {', '.join(refs_lib.stats().keys())}", flush=True)

    # Template
    print("Preparing report template …", flush=True)
    ensure_template(TEMPLATE_SOURCE, TEMPLATE_DEST)

    # Job config
    print("Loading job config …", flush=True)
    job = _load_job(rs_path.parent)
    if not args.yes:
        job = _prompt_job_fields(job)
    if not job.effective_date:
        print("ERROR: an Effective Date is required.", flush=True)
        return 1

    # Tract selection
    tract_ids = parsed.tract_ids()
    if not tract_ids:
        print("ERROR: no tract IDs found in the run sheet.", flush=True)
        return 1
    selected = tract_ids if args.yes else _prompt_tracts(tract_ids)
    if not selected:
        print("No tracts selected; exiting.", flush=True)
        return 1
    print()
    print(f"Confirmed {len(selected)} tracts: {', '.join(selected)}", flush=True)

    # Run analysis
    out_dir = rs_path.parent / "out"
    out_dir.mkdir(exist_ok=True)
    rebuild = bool(os.environ.get("ANALYZER_REBUILD"))

    # max_retries=0 so analyzer._create_with_retry owns the retry loop.
    client = anthropic.Anthropic(max_retries=0, timeout=600.0)
    analyses: dict[str, analyzer_mod.TractAnalysis] = {}
    total_usage: dict[str, TokenUsage] = {}
    state_lock = threading.Lock()
    log_lock = threading.Lock()

    def log(msg: str) -> None:
        with log_lock:
            print(msg, flush=True)

    max_workers = max(1, int(os.environ.get("ANALYZER_PARALLEL", "2")))
    log(f"Parallel workers: {max_workers}")
    log("")

    def _merge_usage(src: dict[str, TokenUsage]) -> None:
        with state_lock:
            for model, u in src.items():
                if model not in total_usage:
                    total_usage[model] = TokenUsage()
                total_usage[model].input_tokens       += u.input_tokens
                total_usage[model].output_tokens      += u.output_tokens
                total_usage[model].cache_write_tokens += u.cache_write_tokens
                total_usage[model].cache_read_tokens  += u.cache_read_tokens

    def process_tract(tid: str) -> None:
        if stop_event.is_set():
            log(f"[{tid}] skipped (interrupted)")
            return
        tract = parsed.tracts[tid]
        input_hash = analyzer_mod._tract_hash(tract, parsed, job)
        cached = None if rebuild else cache_mod.load(out_dir, tid, input_hash)
        if cached is not None:
            log(f"[{tid}] cached — skipping API calls")
            with state_lock:
                analyses[tid] = cached
            return
        log(f"[{tid}] starting …")
        try:
            ta, tract_usage = analyze_tract(
                client=client,
                p=parsed,
                tract=tract,
                job=job,
                refs_lib=refs_lib,
                on_progress=lambda s, tid=tid: log(f"[{tid}] {s}"),
            )
            _merge_usage(tract_usage)
            cache_mod.save(out_dir, ta)
            with state_lock:
                analyses[tid] = ta
            used = ta.model_used
            log(
                f"[{tid}] done  "
                f"surface={ta.surface.confidence}/{used.get('surface','?')}  "
                f"mineral={ta.mineral.confidence}/{used.get('mineral','?')}  "
                f"exceptions={ta.exceptions.confidence}/{used.get('exceptions','?')}"
            )
        except AnalysisInterrupted:
            log(f"[{tid}] interrupted before completion")
        except Exception as e:
            log(f"[{tid}] FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(process_tract, tid) for tid in selected]
        try:
            for f in as_completed(futures):
                if stop_event.is_set():
                    # Cancel anything still queued; in-flight calls finish naturally.
                    for pending in futures:
                        pending.cancel()
                f.result()
        except KeyboardInterrupt:
            # Belt-and-suspenders: signal handler should have already set stop_event.
            stop_event.set()
            for pending in futures:
                pending.cancel()

    if stop_event.is_set():
        log("\n!! Run interrupted by user — rendering whatever completed.")
    if not analyses:
        log("\nNo successful tract analyses. Nothing to render.")
        return 1

    # Render
    log("\nRendering consolidated report …")
    ordered = [analyses[tid] for tid in selected if tid in analyses]
    report_path = rs_path.parent / f"{rs_path.stem} - Draft Report.docx"
    try:
        render_report(
            template_path=TEMPLATE_DEST,
            output_path=report_path,
            job=job,
            analyses=ordered,
        )
        cache_mod.save_consolidated(
            out_dir, rs_path.stem,
            {
                "effective_date": job.effective_date,
                "addressee": job.addressee,
                "county": job.county,
                "signing_date": job.signing_date,
            },
            analyses,
        )
        log(f"Report written: {report_path}")
    except Exception as e:
        traceback.print_exc()
        log(f"Render FAILED: {type(e).__name__}: {e}")
        return 1

    # Cost summary
    if total_usage:
        log("")
        log("─" * 56)
        log("  Token usage")
        log("─" * 56)
        grand_total = 0.0
        for model in sorted(total_usage):
            u = total_usage[model]
            cost = u.cost_usd(model)
            grand_total += cost
            log(f"  {model}")
            log(f"    Input (new)  : {u.input_tokens:>12,}")
            log(f"    Input (cache): {u.cache_read_tokens:>12,}")
            log(f"    Cache writes : {u.cache_write_tokens:>12,}")
            log(f"    Output       : {u.output_tokens:>12,}")
            log(f"    Subtotal     : ~${cost:.4f}")
        log("─" * 56)
        log(f"  TOTAL ESTIMATED COST : ~${grand_total:.4f}")
        log("  (Voyage AI charges not included)")
        log("  (Rates may change — verify at console.anthropic.com)")
        log("─" * 56)

    # Offer to open the report
    if not args.yes and _confirm("\nOpen the report now?", default_yes=True):
        _open_file_native(report_path)

    log(f"\nDone.  Report: {report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
        raise SystemExit(130)
