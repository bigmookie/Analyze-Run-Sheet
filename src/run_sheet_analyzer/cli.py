"""Tkinter entry point for the Run Sheet Analyzer.

Usage:  python analyze_run_sheet.py   (or `analyze-run-sheet` after pip install).
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
from run_sheet_analyzer.analyzer import OPUS, SONNET, JobConfig, analyze_tract, load_refs_or_die
from run_sheet_analyzer.parser import MissingColumnsError, parse
from run_sheet_analyzer.renderer import render_report
from run_sheet_analyzer.template_builder import ensure_template


TEMPLATE_SOURCE = ROOT / "Abstract - Report - Minerals.docx"
TEMPLATE_DEST = ROOT / "templates" / "title-report.docx"
REFS_DIR = ROOT / "refs"


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


def _load_job(run_sheet_dir: Path) -> JobConfig:
    job_path = run_sheet_dir / "job.yaml"
    today = date.today().strftime("%B %d, %Y")
    if not job_path.exists():
        return JobConfig(signing_date=today)
    try:
        data = yaml.safe_load(job_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        messagebox.showwarning("job.yaml unreadable", f"Continuing without job config:\n{e}")
        return JobConfig(signing_date=today)
    return JobConfig(
        effective_date=str(data.get("effective_date", "") or ""),
        addressee=str(data.get("addressee", "") or ""),
        county=str(data.get("county", "") or ""),
        state=str(data.get("state", "Mississippi") or "Mississippi"),
        signing_date=str(data.get("signing_date", today) or today),
        parcels=data.get("parcels") or {},
    )


def _prompt_job_fields(parent, current: JobConfig) -> JobConfig:
    """If job.yaml was absent or partial, prompt for the certificate fields."""
    if current.effective_date and current.addressee and current.county:
        return current

    dlg = tk.Toplevel(parent)
    dlg.title("Job details")
    dlg.transient(parent)
    dlg.grab_set()
    dlg.resizable(False, False)

    frm = ttk.Frame(dlg, padding=12)
    frm.pack(fill="both", expand=True)

    def row(label, default):
        ttk.Label(frm, text=label).pack(anchor="w")
        var = tk.StringVar(value=default)
        ent = ttk.Entry(frm, textvariable=var, width=48)
        ent.pack(fill="x", pady=(0, 8))
        return var

    addressee = row("Addressee:", current.addressee or "")
    effective = row("Effective Date (e.g. March 13, 2026):", current.effective_date or "")
    county = row("County:", current.county or "Simpson")
    signing = row("Signing Date:", current.signing_date or date.today().strftime("%B %d, %Y"))

    result = {"ok": False}

    def on_ok():
        result["ok"] = True
        dlg.destroy()

    def on_cancel():
        dlg.destroy()

    btns = ttk.Frame(frm)
    btns.pack(fill="x", pady=(4, 0))
    ttk.Button(btns, text="Cancel", command=on_cancel).pack(side="right", padx=4)
    ttk.Button(btns, text="OK", command=on_ok).pack(side="right")

    dlg.wait_window()

    if not result["ok"]:
        return current
    return JobConfig(
        effective_date=effective.get(),
        addressee=addressee.get(),
        county=county.get(),
        state=current.state,
        signing_date=signing.get(),
        parcels=current.parcels,
    )


def _confirm_tracts(parent, tract_ids: list[str]) -> list[str] | None:
    dlg = tk.Toplevel(parent)
    dlg.title("Confirm tracts to analyze")
    dlg.transient(parent)
    dlg.grab_set()

    ttk.Label(dlg, text="Discovered tracts (uncheck any to skip):", padding=8).pack(anchor="w")

    frm = ttk.Frame(dlg, padding=(8, 0, 8, 0))
    frm.pack(fill="both", expand=True)

    vars_ = {}
    for i, tid in enumerate(tract_ids):
        v = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text=tid, variable=v).grid(row=i // 4, column=i % 4, sticky="w", padx=6, pady=2)
        vars_[tid] = v

    selected: list[str] | None = [None]   # mutable holder; None means cancelled

    def on_ok():
        selected[0] = [tid for tid, v in vars_.items() if v.get()]
        dlg.destroy()

    def on_cancel():
        selected[0] = None
        dlg.destroy()

    btns = ttk.Frame(dlg, padding=8)
    btns.pack(fill="x")
    ttk.Button(btns, text="Cancel", command=on_cancel).pack(side="right", padx=4)
    ttk.Button(btns, text="Run", command=on_ok).pack(side="right")

    dlg.wait_window()
    return selected[0]


class ProgressWindow(tk.Toplevel):
    def __init__(self, parent, title: str = "Analyzing"):
        super().__init__(parent)
        self.title(title)
        self.geometry("800x540")
        self.transient(parent)
        frm = ttk.Frame(self, padding=8)
        frm.pack(fill="both", expand=True)
        self.text = tk.Text(frm, wrap="word")
        self.text.pack(fill="both", expand=True)
        self.text.configure(state="disabled")

    def log(self, line: str):
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")
        self.update_idletasks()


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


def main() -> int:
    print(flush=True)
    print("=" * 60, flush=True)
    print("  Run Sheet Analyzer", flush=True)
    print(f"  Analysis model  : {SONNET}", flush=True)
    print(f"  Escalation model: {OPUS}  (on low confidence)", flush=True)
    print("=" * 60, flush=True)
    print(flush=True)

    root = tk.Tk()
    root.withdraw()

    env_err = _check_env()
    if env_err:
        messagebox.showerror("Missing API keys", env_err)
        return 1

    rs_path = filedialog.askopenfilename(
        title="Select a run sheet (.xlsx)",
        filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
    )
    if not rs_path:
        return 1
    rs_path = Path(rs_path)

    try:
        parsed = parse(rs_path)
    except MissingColumnsError as e:
        messagebox.showerror("Run sheet rejected", str(e))
        return 1
    except Exception as e:
        messagebox.showerror("Could not read run sheet", f"{type(e).__name__}: {e}")
        return 1

    if parsed.unparseable_le_rows:
        msg = (
            f"{len(parsed.unparseable_le_rows)} row(s) tagged as Less-and-Except (LE) "
            "have no base tract. These rows will be omitted. Continue?"
        )
        if not messagebox.askyesno("Unparseable LE rows", msg):
            return 1

    try:
        refs_lib = load_refs_or_die(REFS_DIR)
    except RuntimeError as e:
        messagebox.showerror("Reference library missing", str(e))
        return 1

    ensure_template(TEMPLATE_SOURCE, TEMPLATE_DEST)

    job = _load_job(rs_path.parent)
    job = _prompt_job_fields(root, job)
    if not job.effective_date:
        messagebox.showerror("Cancelled", "An Effective Date is required.")
        return 1

    tract_ids = parsed.tract_ids()
    if not tract_ids:
        messagebox.showerror("No tracts", "No tract IDs found in the run sheet.")
        return 1

    selected = _confirm_tracts(root, tract_ids)
    if selected is None or not selected:
        return 1

    progress = ProgressWindow(root, title=f"Analyzing {rs_path.name}")

    def log(msg: str) -> None:
        """Write to both the terminal and the Tkinter progress window."""
        print(msg, flush=True)
        try:
            progress.log(msg)
        except Exception:
            pass

    log(f"Run sheet : {rs_path}")
    log(f"Tracts    : {', '.join(selected)}")
    log(f"Ref DBs   : {', '.join(refs_lib.stats().keys())}")
    log("")

    out_dir = rs_path.parent / "out"
    out_dir.mkdir(exist_ok=True)
    rebuild = bool(os.environ.get("ANALYZER_REBUILD"))

    client = anthropic.Anthropic()
    analyses: dict[str, "analyzer_mod.TractAnalysis"] = {}

    def worker():
        for tid in selected:
            tract = parsed.tracts[tid]
            input_hash = analyzer_mod._tract_hash(tract, parsed, job)
            cached = None if rebuild else cache_mod.load(out_dir, tid, input_hash)
            if cached is not None:
                log(f"[{tid}] cached — skipping API calls")
                analyses[tid] = cached
                continue
            log(f"[{tid}] starting …")
            try:
                ta = analyze_tract(
                    client=client,
                    p=parsed,
                    tract=tract,
                    job=job,
                    refs_lib=refs_lib,
                    on_progress=lambda s, tid=tid: log(f"[{tid}] {s}"),
                )
                cache_mod.save(out_dir, ta)
                analyses[tid] = ta
                used = ta.model_used
                log(
                    f"[{tid}] done  "
                    f"surface={ta.surface.confidence}/{used.get('surface','?')}  "
                    f"mineral={ta.mineral.confidence}/{used.get('mineral','?')}  "
                    f"exceptions={ta.exceptions.confidence}/{used.get('exceptions','?')}"
                )
            except Exception as e:
                log(f"[{tid}] FAILED: {type(e).__name__}: {e}")
                traceback.print_exc()

        if not analyses:
            log("\nNo successful tract analyses. Nothing to render.")
            return

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
            _open_file_native(report_path)
            messagebox.showinfo(
                "Done",
                f"Draft report written to:\n{report_path}\n\n"
                f"Tracts analyzed: {len(ordered)}",
            )
        except Exception as e:
            traceback.print_exc()
            log(f"Render FAILED: {type(e).__name__}: {e}")
            messagebox.showerror("Render failed", f"{type(e).__name__}: {e}")

    threading.Thread(target=worker, daemon=True).start()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
