"""Render the consolidated draft report.

Strategy:
  1. docxtpl renders the certificate page from `templates/title-report.docx`
     (which only carries the certificate, per template_builder.py).
  2. python-docx is then used to append the per-tract TITLE REPORT sections,
     the chain-of-title appendix tables, and the ATTORNEY REVIEW callouts.

This split keeps the firm's certificate formatting pixel-perfect while giving
us full programmatic control of the analysis sections.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docxtpl import DocxTemplate

from .analyzer import JobConfig
from .models import (
    ChainEntry,
    ExceptionItem,
    Exceptions,
    MineralChain,
    SurfaceChain,
    TractAnalysis,
)


CALLOUT_RED = RGBColor(0xC0, 0x00, 0x00)


def _add_heading(doc, text: str, level: int = 1):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(14 if level == 1 else 12 if level == 2 else 11)
    return p


def _add_body(doc, text: str, *, bold: bool = False, italic: bool = False):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = bold
    r.italic = italic
    return p


def _add_callout(doc, text: str):
    p = doc.add_paragraph()
    r = p.add_run("ATTORNEY REVIEW — " + text)
    r.bold = True
    r.font.color.rgb = CALLOUT_RED
    return p


def _render_owner_line(name: str, share: str, note: str | None) -> str:
    base = f"{name}, an undivided {share} interest"
    if note:
        base += f" ({note})"
    return base + "."


def _render_exception_line(x: ExceptionItem) -> str:
    # The AI's `description` is the full house-style sentence with citation inline.
    # The book/page/instrument_no/recorded fields stay in the JSON sidecar for
    # cross-referencing but are not separately rendered into the report.
    line = x.description.strip()
    # Append a terminating period only if the sentence doesn't already end with
    # one (allowing for a closing paren containing a NOTE, e.g. "... heirs.)").
    if line and line[-1] not in ".!?)\"'":
        line += "."
    if x.disagreement and x.disagreement_note:
        line += f" [Abstractor flag disagreement: {x.disagreement_note}]"
    return line


def _bucket_section(doc, title: str, items: list[ExceptionItem]):
    _add_body(doc, title, bold=True)
    if not items:
        _add_body(doc, "None.")
        return
    for x in items:
        _add_body(doc, _render_exception_line(x))


def _add_chain_table(doc, header: str, chain: list[ChainEntry]):
    _add_heading(doc, header, level=3)
    if not chain:
        _add_body(doc, "None.", italic=True)
        return
    table = doc.add_table(rows=1, cols=6)
    # Use a style that ships with the default template; fall back silently.
    for candidate in ("Table Grid", "Light Grid", "Light Grid Accent 1"):
        try:
            table.style = candidate
            break
        except KeyError:
            continue
    hdr = table.rows[0].cells
    hdr[0].text = "Seq"
    hdr[1].text = "Book/Page"
    hdr[2].text = "Recorded"
    hdr[3].text = "Doc"
    hdr[4].text = "Grantor → Grantee"
    hdr[5].text = "Summary"
    for entry in chain:
        row = table.add_row().cells
        row[0].text = str(entry.seq)
        bp = f"{entry.book}/{entry.page}"
        if entry.instrument_no:
            bp += f"\nInst. {entry.instrument_no}"
        row[1].text = bp
        row[2].text = entry.recorded or ""
        row[3].text = entry.doc_title
        row[4].text = (
            " | ".join(entry.grantors) + "\n→\n" + " | ".join(entry.grantees)
        )
        row[5].text = entry.summary or ""


def _render_tract_section(doc, ta: TractAnalysis, job: JobConfig):
    doc.add_page_break()
    _add_heading(doc, f"TITLE REPORT — Tract {ta.tract}", level=1)
    _add_body(doc, "")

    # A. Title vested in
    _add_heading(doc, "A.  Title Vested in:", level=2)
    _add_body(doc, "As to the Surface Estate:", bold=True)
    if ta.surface.current_vesting:
        for owner in ta.surface.current_vesting:
            _add_body(doc, _render_owner_line(owner.name, owner.share, owner.note))
    else:
        _add_body(doc, "ATTORNEY REVIEW — surface vesting not determined.", italic=True)
    _add_body(doc, "")
    _add_body(doc, "As to the Mineral Estate:", bold=True)
    if ta.mineral.current_mineral_owners:
        for owner in ta.mineral.current_mineral_owners:
            note_bits = []
            if owner.note:
                note_bits.append(owner.note)
            if owner.source_book_page:
                note_bits.append(f"Book/Page {owner.source_book_page}")
            note = "; ".join(note_bits) if note_bits else None
            _add_body(doc, _render_owner_line(owner.name, owner.share, note))
    else:
        _add_body(doc, "ATTORNEY REVIEW — mineral vesting not determined.", italic=True)
    if not ta.mineral.reconciliation.get("ok", False):
        _add_callout(
            doc,
            f"mineral fractions do not reconcile to 1 (total {ta.mineral.reconciliation.get('total')}, "
            f"imbalance {ta.mineral.reconciliation.get('imbalance', '?')}).",
        )

    # B. Description
    _add_heading(doc, "B.  Description:", level=2)
    _add_body(doc, ta.surface.legal_description or "ATTORNEY REVIEW — legal description not synthesized.")

    # C. Title exceptions
    _add_heading(doc, "C.  Title Exceptions:", level=2)
    _bucket_section(doc, "1.  Voluntary Liens (Deeds of Trust, Assignments of Rent, UCCs, etc.):",
                    ta.exceptions.buckets.voluntary_liens)
    _bucket_section(doc, "2.  Involuntary Liens (Judgments, Tax Liens, etc.):",
                    ta.exceptions.buckets.involuntary_liens)
    _bucket_section(doc, "3.  Servitudes (Covenants, Restrictions, Plats, Easements, etc.):",
                    ta.exceptions.buckets.servitudes)
    _bucket_section(doc, "4.  Other Matters of Record (Chancery Causes, Tax Sales, Lis Pendens, etc.):",
                    ta.exceptions.buckets.other_matters)
    _bucket_section(doc, "5.  Mineral Leases:", ta.exceptions.buckets.mineral_leases)

    # 6. County taxes — built from job config rather than from Claude.
    _add_body(doc, "6.  County Taxes:", bold=True)
    parcel_cfg = job.for_tract(ta.tract) or {}
    parcel_id = parcel_cfg.get("parcel_id", "")
    last_year = parcel_cfg.get("last_year", "")
    amount = parcel_cfg.get("amount", "")
    priors_paid = parcel_cfg.get("priors_paid", False)
    _add_body(doc, f"Parcel ID: {parcel_id}")
    if last_year and amount:
        _add_body(doc, f"{last_year} Taxes Paid in the Amount of {amount}")
    if priors_paid:
        _add_body(doc, "Priors paid")
    elif not parcel_cfg:
        _add_callout(doc, f"tax data not supplied for tract {ta.tract} in job.yaml.")

    # Attorney callouts collected from each area
    all_callouts = (
        ta.surface.attorney_review
        + ta.mineral.attorney_review
        + ta.exceptions.attorney_review
    )
    if all_callouts:
        _add_body(doc, "")
        _add_heading(doc, "Attorney review items", level=3)
        for c in all_callouts:
            _add_callout(doc, c)


def _render_appendix(doc, analyses: list[TractAnalysis]):
    doc.add_page_break()
    _add_heading(doc, "Appendix — Chain of Title Tables", level=1)
    for ta in analyses:
        _add_heading(doc, f"Tract {ta.tract}", level=2)
        _add_chain_table(doc, "Surface chain", ta.surface.chain)
        _add_chain_table(doc, "Mineral chain", ta.mineral.chain)


def render_report(
    *,
    template_path: Path,
    output_path: Path,
    job: JobConfig,
    analyses: list[TractAnalysis],
) -> Path:
    # Step 1: docxtpl renders the certificate from the prepared template.
    tpl = DocxTemplate(str(template_path))
    tpl.render({
        "addressee": job.addressee or "",
        "effective_date": job.effective_date or "",
        "county": job.county or "",
        "signing_date": job.signing_date or "",
    })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tpl.save(str(output_path))

    # Step 2: python-docx opens the result and appends the per-tract sections.
    doc = Document(str(output_path))
    for ta in analyses:
        _render_tract_section(doc, ta, job)
    _render_appendix(doc, analyses)
    doc.save(str(output_path))
    return output_path
