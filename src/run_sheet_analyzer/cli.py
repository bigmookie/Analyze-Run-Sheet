"""Command-line entry point for the Run Sheet Analyzer.

Usage:
    python analyze_run_sheet.py
    python analyze_run_sheet.py "C:\\path\\to\\Run Sheet.xlsx"
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
from pathlib import Path
from tkinter import filedialog

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

import anthropic

from run_sheet_analyzer import assignment
from run_sheet_analyzer import cache as cache_mod
from run_sheet_analyzer.analyzer import (
    AnalysisInterrupted,
    JobConfig,
    OPUS,
    SONNET,
    TokenUsage,
    analyze_tract,
    stop_event,
)
from run_sheet_analyzer.parser import MissingColumnsError, parse
from run_sheet_analyzer.renderer import render_report
from run_sheet_analyzer.template_builder import ensure_template


TEMPLATE_SOURCE = ROOT / "Abstract - Report - Minerals.docx"
TEMPLATE_DEST = ROOT / "templates" / "title-report.docx"


def _check_env() -> str | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return (
            "Missing ANTHROPIC_API_KEY.\n"
            "Copy .env.example to .env in the project root and set the key."
        )
    return None


def _clean_path(raw: str) -> str:
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
    if not job_path.exists():
        return JobConfig()
    try:
        data = yaml.safe_load(job_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"WARNING: could not read job.yaml: {e}", flush=True)
        return JobConfig()
    raw_tracts = data.get("tracts") or []
    tracts = [str(t).strip() for t in raw_tracts if str(t).strip()] if isinstance(raw_tracts, list) else []
    return JobConfig(
        effective_date=str(data.get("effective_date", "") or ""),
        addressee=str(data.get("addressee", "") or ""),
        county=str(data.get("county", "") or ""),
        state=str(data.get("state", "Mississippi") or "Mississippi"),
        signing_date=str(data.get("signing_date", "") or ""),
        parcels=data.get("parcels") or {},
        tracts=tracts,
        tract_granularity=str(data.get("tract_granularity", "") or ""),
        description_file=str(data.get("description_file", "") or ""),
        include_minerals=bool(data.get("include_minerals", True)),
    )


def _prompt_job_fields(current: JobConfig) -> JobConfig:
    print()
    print("Job details (press Enter to leave a field blank — a placeholder goes in the report):",
          flush=True)
    addressee = _prompt("Addressee", current.addressee or "")
    effective = _prompt("Effective Date (e.g. March 13, 2026)", current.effective_date or "")
    county = _prompt("County", current.county or "")
    signing = _prompt("Signing Date", current.signing_date or "")
    include_minerals = _confirm("Include mineral analysis?", default_yes=current.include_minerals)
    return JobConfig(
        effective_date=effective,
        addressee=addressee,
        county=county,
        state=current.state,
        signing_date=signing,
        parcels=current.parcels,
        tracts=current.tracts,
        tract_granularity=current.tract_granularity,
        description_file=current.description_file,
        include_minerals=include_minerals,
    )


def _prompt_tracts(tract_ids: list[str]) -> list[str]:
    print()
    print(f"Discovered {len(tract_ids)} tracts:", flush=True)
    print("  " + ", ".join(tract_ids), flush=True)
    print("  Enter tract IDs to RUN (comma-separated), or prefix with '-' to EXCLUDE.", flush=True)
    print("  Blank = run all.", flush=True)
    raw = input("  > ").strip()
    if not raw:
        return list(tract_ids)
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    # If any token starts with '-', treat the whole line as an exclude list.
    if any(t.startswith("-") for t in tokens):
        skip = {t.lstrip("-").strip() for t in tokens}
        unknown = skip - set(tract_ids)
        if unknown:
            print(f"  WARNING: unknown tract(s) ignored: {', '.join(sorted(unknown))}", flush=True)
        return [t for t in tract_ids if t not in skip]
    # Otherwise it's an include list — preserve the run-sheet order.
    requested = set(tokens)
    unknown = requested - set(tract_ids)
    if unknown:
        print(f"  WARNING: unknown tract(s) ignored: {', '.join(sorted(unknown))}", flush=True)
    return [t for t in tract_ids if t in requested]


_GRANULARITIES = [
    ("Section (640 ac)", "section"),
    ("Quarter section (160 ac)", "quarter section"),
    ("Quarter-quarter section (40 ac)", "quarter-quarter section"),
    ("Full legal description", "full legal description"),
]


def _prompt_granularity() -> str:
    """Ask at what granularity the examiner wants to group the blank rows."""
    print()
    print("At what granularity do you want to define tracts?", flush=True)
    for i, (label, _) in enumerate(_GRANULARITIES, 1):
        print(f"  {i}. {label}", flush=True)
    while True:
        raw = input("  Choose 1-4: ").strip()
        if raw in ("1", "2", "3", "4"):
            return _GRANULARITIES[int(raw) - 1][1]


def _prompt_units(prefill: list[str]) -> list[str]:
    """Collect the tract units, one per line. Blank line ends entry.

    If `prefill` (from job.yaml `tracts:`) is non-empty, an empty first line
    accepts it as-is.
    """
    print()
    if prefill:
        print("Tract units from job.yaml:", flush=True)
        for u in prefill:
            print(f"  - {u}", flush=True)
        if _confirm("Use these units?", default_yes=True):
            return list(prefill)
    print("Enter one tract unit per line (e.g. 'NENE Sec 14-T6N-R2E'). Blank line to finish:", flush=True)
    units: list[str] = []
    while True:
        line = input("  > ").strip()
        if not line:
            break
        units.append(line)
    return units


def _load_units_from_file(path: Path, client, confirm: bool) -> list:
    """Read a .txt/.docx legal-description file and extract its tracts via Claude.

    Returns a list of assignment.TractUnit (possibly empty if declined).
    """
    text = assignment.read_document(path)
    units = assignment.extract_units(
        client=client, document_text=text,
        on_progress=lambda m: print(f"  {m}", flush=True),
    )
    if not units:
        print("  No tracts could be read from that document.", flush=True)
        return []
    print(f"  Read {len(units)} tract(s) from {path.name}:", flush=True)
    for u in units:
        preview = u.description if len(u.description) <= 100 else u.description[:97] + "…"
        print(f"    - {u.id}: {preview}", flush=True)
    if confirm and not _confirm("Use these tracts?", default_yes=True):
        return []
    return units


def _define_tracts(job: JobConfig, args, client) -> tuple[list, str]:
    """Build the tract units for the blank-Tract-column flow.

    Returns (units, granularity_label). Units are assignment.TractUnit objects;
    an empty list means the caller should abort.
    """
    interactive = not (args.yes or args.tracts)
    doc_label = "named tracts from the examiner's description document"

    if not interactive:
        # Non-interactive: everything must come from job.yaml.
        if job.description_file:
            return _load_units_from_file(Path(job.description_file), client, confirm=False), doc_label
        if job.tracts:
            units = [assignment.TractUnit(assignment._safe_id(u), u) for u in job.tracts]
            return units, job.tract_granularity or "as provided"
        return [], ""

    # Interactive: choose where the tract definitions come from.
    print()
    print("How do you want to define the tracts?", flush=True)
    print("  1. Browse for a legal-description file (.txt / .docx)", flush=True)
    print("  2. Enter the path to a legal-description file", flush=True)
    print("  3. Type the tract units myself", flush=True)
    while True:
        choice = input("  Choose 1-3: ").strip()
        if choice in ("1", "2", "3"):
            break

    if choice in ("1", "2"):
        if choice == "1":
            print("  Opening file picker — select the legal-description file.", flush=True)
            picked = _pick_description_dialog(job.description_file)
            if not picked:
                print("  No file selected.", flush=True)
                return [], ""
            path = Path(picked)
        else:
            raw = _prompt("Path to description file (.txt/.docx)", job.description_file or "")
            path = Path(_clean_path(raw))
        if not path.is_file():
            print(f"  ERROR: file not found: {path}", flush=True)
            return [], ""
        return _load_units_from_file(path, client, confirm=True), doc_label

    granularity = _prompt_granularity()
    typed = _prompt_units(prefill=job.tracts)
    units = [assignment.TractUnit(assignment._safe_id(u), u) for u in typed]
    return units, granularity


def _parse_tracts_arg(arg: str, all_tracts: list[str]) -> tuple[list[str], list[str]]:
    """Apply the same include/exclude rules from --tracts as from the prompt.
    Returns (selected, unknown)."""
    tokens = [t.strip() for t in arg.split(",") if t.strip()]
    if not tokens:
        return list(all_tracts), []
    if any(t.startswith("-") for t in tokens):
        skip = {t.lstrip("-").strip() for t in tokens}
        unknown = sorted(skip - set(all_tracts))
        return [t for t in all_tracts if t not in skip], unknown
    requested = set(tokens)
    unknown = sorted(requested - set(all_tracts))
    return [t for t in all_tracts if t in requested], unknown


def _gather_commentary(args) -> str:
    """Resolve commentary from flags or an interactive prompt.

      --no-commentary  → skip entirely (returns "").
      --commentary T   → use T verbatim (no prompt).
      --yes            → no prompt; returns "" unless --commentary given.
      otherwise        → ask whether to provide commentary; if yes, collect
                         multi-line input terminated by a blank line.
    """
    if args.no_commentary:
        return ""
    if args.commentary:
        return args.commentary.strip()
    if args.yes:
        return ""
    print()
    if not _confirm("Provide commentary/instructions for the AI before analysis?",
                    default_yes=False):
        return ""
    print("  Type your commentary. Press Enter on a blank line to finish.", flush=True)
    lines: list[str] = []
    while True:
        try:
            line = input("  | ")
        except EOFError:
            break
        if line.strip() == "":
            break
        lines.append(line)
    commentary = "\n".join(lines).strip()
    if commentary:
        print(f"  Commentary captured ({len(commentary)} chars).", flush=True)
    return commentary


def _pick_run_sheet_dialog() -> str | None:
    root = tk.Tk()
    root.withdraw()
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


def _pick_description_dialog(initial: str = "") -> str | None:
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        root.update()
    except Exception:
        pass
    kwargs = dict(
        parent=root,
        title="Select a legal-description file (.txt / .docx)",
        filetypes=[
            ("Legal description", "*.txt *.docx"),
            ("Text files", "*.txt"),
            ("Word documents", "*.docx"),
            ("All files", "*.*"),
        ],
    )
    if initial:
        p = Path(initial)
        if p.parent.is_dir():
            kwargs["initialdir"] = str(p.parent)
        if p.name:
            kwargs["initialfile"] = p.name
    try:
        path = filedialog.askopenfilename(**kwargs)
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return path or None


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


_ctrl_c_count = 0


def _install_interrupt_handler() -> None:
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


def main() -> int:
    parser_ = argparse.ArgumentParser(
        description="Analyze a real-estate run sheet and produce a draft Mississippi title report.",
    )
    parser_.add_argument("run_sheet", nargs="?", help="Path to the run sheet .xlsx.")
    parser_.add_argument("--yes", "-y", action="store_true",
                         help="Run all tracts and accept all defaults without prompting.")
    parser_.add_argument(
        "--tracts",
        help=(
            "Tract IDs to analyze, comma-separated. Examples: "
            "'23.1,23.2,26.4' (only these), or "
            "'-22.1,-22.2' (all except these). "
            "Skips the interactive tract prompt."
        ),
    )
    parser_.add_argument(
        "--commentary",
        help="Commentary/instructions for the AI, applied to every tract. "
             "Providing this skips the interactive commentary prompt.",
    )
    parser_.add_argument(
        "--no-commentary",
        action="store_true",
        help="Skip the commentary step entirely (no prompt, no commentary).",
    )
    parser_.add_argument(
        "--no-minerals",
        action="store_true",
        help="Exclude minerals: skip the mineral-chain (Opus) phase and produce a "
             "surface-only report. Overrides job.yaml and the interactive prompt.",
    )
    args = parser_.parse_args()

    _install_interrupt_handler()

    print(flush=True)
    print("=" * 60, flush=True)
    print("  Run Sheet Analyzer", flush=True)
    print(f"  Mineral chain : {OPUS}", flush=True)
    print(f"  Report assembly: {SONNET}", flush=True)
    print("  Kill: press Ctrl+C (twice to force-quit).", flush=True)
    print("=" * 60, flush=True)
    print(flush=True)

    env_err = _check_env()
    if env_err:
        print(f"ERROR: {env_err}", flush=True)
        return 1

    # Run sheet path
    if args.run_sheet:
        rs_path = Path(_clean_path(args.run_sheet))
    else:
        print(">> Opening file picker — select your run sheet .xlsx.", flush=True)
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
        print(f"  {len(parsed.unparseable_le_rows)} bare-LE rows will be omitted.", flush=True)
        if not args.yes and not _confirm("Continue?", default_yes=True):
            print("Aborted.", flush=True)
            return 1

    # Template
    print("Preparing report template …", flush=True)
    ensure_template(TEMPLATE_SOURCE, TEMPLATE_DEST)

    # Job config
    print("Loading job config …", flush=True)
    job = _load_job(rs_path.parent)
    if not args.yes:
        job = _prompt_job_fields(job)
    # --no-minerals wins over job.yaml and the interactive prompt.
    if args.no_minerals:
        job.include_minerals = False
    if not job.effective_date:
        print("WARNING: no Effective Date provided; '[EFFECTIVE DATE]' will appear in the report.",
              flush=True)
    if not job.include_minerals:
        print("Minerals EXCLUDED — surface-only report (mineral chain phase skipped).", flush=True)

    client = anthropic.Anthropic(max_retries=0, timeout=600.0)

    # Tract selection
    tract_ids = parsed.tract_ids()
    if not tract_ids:
        # The Tract column is blank on every row. Let the examiner define the
        # tracts and have Claude assign each row to them.
        print(flush=True)
        print(f"All {len(parsed.rows)} rows have a blank Tract column — no tracts to discover.", flush=True)
        print("  Note: 'Less and Except' (LE) and 'Not Subject' (NS) cannot be derived "
              "in this mode; manage those by hand if needed.", flush=True)

        try:
            units, granularity = _define_tracts(job, args, client)
        except (OSError, ValueError, anthropic.APIError) as e:
            print(f"ERROR: could not read tracts: {e}", flush=True)
            return 1
        if not units:
            if args.yes or args.tracts:
                print("ERROR: blank Tract column with --yes/--tracts requires "
                      "'description_file:' or 'tracts:' in job.yaml.", flush=True)
            else:
                print("No tracts defined; exiting.", flush=True)
            return 1

        try:
            mapping = assignment.propose_assignment(
                client=client, rows=parsed.rows, units=units,
                granularity=granularity, on_progress=lambda m: print(f"  {m}", flush=True),
            )
        except (ValueError, anthropic.APIError) as e:
            print(f"ERROR: tract assignment failed: {e}", flush=True)
            return 1

        unassigned_rows = assignment.apply_assignment(parsed, mapping)
        if unassigned_rows:
            print(f"  {len(unassigned_rows)} row(s) matched no unit and are left out (review):", flush=True)
            for r in unassigned_rows:
                print(f"    - {r.cite}: {r.brief_description or '(no description)'}", flush=True)

        tract_ids = parsed.tract_ids()
        if not tract_ids:
            print("ERROR: no rows could be assigned to any tract unit.", flush=True)
            return 1
        print(f"  Assigned rows into {len(tract_ids)} tract(s): {', '.join(tract_ids)}", flush=True)

        # Write a copy of the run sheet with the Tract column filled in.
        assigned_path = rs_path.parent / f"{rs_path.stem} - Tracts Assigned.xlsx"
        try:
            assignment.write_assigned_run_sheet(rs_path, parsed, assigned_path)
            print(f"  Tract-assigned run sheet written: {assigned_path}", flush=True)
        except Exception as e:
            print(f"  WARNING: could not write tract-assigned run sheet: "
                  f"{type(e).__name__}: {e}", flush=True)

    if args.tracts:
        selected, unknown = _parse_tracts_arg(args.tracts, tract_ids)
        if unknown:
            print(f"ERROR: unknown tract(s) in --tracts: {', '.join(unknown)}", flush=True)
            print(f"  Known tracts: {', '.join(tract_ids)}", flush=True)
            return 1
    elif args.yes:
        selected = tract_ids
    else:
        selected = _prompt_tracts(tract_ids)
    if not selected:
        print("No tracts selected; exiting.", flush=True)
        return 1
    print()
    print(f"Confirmed {len(selected)} tracts: {', '.join(selected)}", flush=True)

    # Optional examiner commentary / instructions for the AI.
    commentary = _gather_commentary(args)

    out_dir = rs_path.parent / "out"
    out_dir.mkdir(exist_ok=True)
    rebuild = bool(os.environ.get("ANALYZER_REBUILD"))

    sections: dict[str, str] = {}
    total_usage: dict[str, TokenUsage] = {}
    state_lock = threading.Lock()
    log_lock = threading.Lock()

    def log(msg: str) -> None:
        with log_lock:
            print(msg, flush=True)

    max_workers = max(1, int(os.environ.get("ANALYZER_PARALLEL", "2")))
    log(f"Parallel workers: {max_workers}")
    log("")

    job_dict_for_hash = {
        "effective_date": job.effective_date,
        "addressee": job.addressee,
        "county": job.county,
        "signing_date": job.signing_date,
        "commentary": commentary,
        "include_minerals": job.include_minerals,
    }

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
        input_hash = cache_mod.tract_hash(tract, job_dict_for_hash)
        cached = None if rebuild else cache_mod.load(out_dir, tid, input_hash)
        if cached is not None:
            log(f"[{tid}] cached — skipping API call")
            with state_lock:
                sections[tid] = cached
            return
        log(f"[{tid}] starting ({len(tract.rows)} events) …")
        try:
            text, usage_by_model = analyze_tract(
                client=client,
                tract=tract,
                p=parsed,
                job=job,
                commentary=commentary,
                on_progress=lambda s, tid=tid: log(f"[{tid}] {s}"),
            )
            _merge_usage(usage_by_model)
            cache_mod.save(out_dir, tid, text, input_hash)
            with state_lock:
                sections[tid] = text
                cum_cost = sum(u.cost_usd(m) for m, u in total_usage.items())
                done = len(sections)
                total = len(selected)
            log(f"[{tid}] done  ({len(text):,} chars)  |  {done}/{total} done, ~${cum_cost:.4f}")
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
                    for pending in futures:
                        pending.cancel()
                try:
                    f.result()
                except Exception:
                    pass
        except KeyboardInterrupt:
            stop_event.set()
            for pending in futures:
                pending.cancel()

    if stop_event.is_set():
        log("\n!! Run interrupted — rendering whatever completed.")
    if not sections:
        log("\nNo successful tract analyses. Nothing to render.")
        return 1

    # Render
    log("\nRendering consolidated report …")
    ordered = [(tid, sections[tid]) for tid in selected if tid in sections]
    report_path = rs_path.parent / f"{rs_path.stem} - Draft Report.docx"
    try:
        render_report(
            template_path=TEMPLATE_DEST,
            output_path=report_path,
            job=job,
            tract_sections=ordered,
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
        log("─" * 56)

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
