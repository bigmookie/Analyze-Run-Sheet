"""Render the consolidated draft report.

  1. docxtpl fills the certificate page from templates/title-report.docx.
  2. python-docx appends each tract's report-section text (as produced by
     the analyzer) one paragraph at a time, with light formatting.
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor
from docxtpl import DocxTemplate

from .analyzer import JobConfig


CALLOUT_RED = RGBColor(0xC0, 0x00, 0x00)


def _add_heading_run(doc, text: str, size: int = 14):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = True
    r.font.size = Pt(size)
    return p


def _add_paragraph(doc, text: str, *, bold: bool = False, italic: bool = False, color=None):
    p = doc.add_paragraph()
    r = p.add_run(text)
    r.bold = bold
    r.italic = italic
    if color is not None:
        r.font.color.rgb = color
    return p


def _render_county_taxes(doc, tract_id: str, parcel: dict):
    """The model is told to leave 6. County Taxes for the renderer; we fill it
    from job config."""
    _add_paragraph(doc, "6. County Taxes:", bold=True)
    parcel_id = parcel.get("parcel_id", "")
    last_year = parcel.get("last_year", "")
    amount = parcel.get("amount", "")
    priors_paid = parcel.get("priors_paid", False)
    if parcel_id:
        _add_paragraph(doc, f"Parcel ID: {parcel_id}")
    if last_year and amount:
        _add_paragraph(doc, f"{last_year} Taxes Paid in the Amount of {amount}")
    if priors_paid:
        _add_paragraph(doc, "Priors paid")
    if not parcel:
        _add_paragraph(
            doc,
            f"ATTORNEY REVIEW — tax data not supplied for tract {tract_id} in job.yaml.",
            bold=True, color=CALLOUT_RED,
        )


def _render_tract_section(doc, tract_id: str, text: str, job: JobConfig):
    """Render one tract's report section into the doc.

    The model produces a plain-text section in the firm's house style. We
    dump it line by line, applying basic formatting:
      - Lines that look like section headers ("VESTING", "DESCRIPTION",
        "EXCEPTIONS", "ATTORNEY REVIEW", numbered bucket headers) get bold.
      - Lines starting with "ATTORNEY REVIEW" or "NOTE:" or "*** " get red.
      - The bracketed "[See parcel data ...]" placeholder is replaced with
        the actual tax block from job config.
      - Everything else renders as a normal paragraph.
    """
    doc.add_page_break()
    _add_heading_run(doc, f"TITLE REPORT — Tract {tract_id}", size=14)

    parcel = job.for_tract(tract_id)
    section_bold = {
        "CHAIN OF TITLE", "VESTING", "DESCRIPTION", "EXCEPTIONS", "ATTORNEY REVIEW",
    }

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        # Skip the model's "=== TRACT X ===" header — we render our own heading.
        if line.startswith("=== TRACT") and line.endswith("==="):
            continue

        # Replace the tax-block placeholder with rendered tax info.
        if "[See parcel data" in line:
            _render_county_taxes(doc, tract_id, parcel)
            continue

        if not line.strip():
            doc.add_paragraph("")
            continue

        stripped = line.strip()

        # Section headers
        if stripped in section_bold:
            _add_paragraph(doc, stripped, bold=True)
            continue

        # Numbered bucket headers (e.g. "1. Voluntary Liens (...):")
        if stripped[:2].rstrip(".").isdigit() and stripped.rstrip().endswith(":"):
            _add_paragraph(doc, stripped, bold=True)
            continue

        # Attorney-review / callout lines render in red bold.
        if stripped.startswith("ATTORNEY REVIEW") or stripped.startswith("*** "):
            _add_paragraph(doc, stripped, bold=True, color=CALLOUT_RED)
            continue

        # Inline NOTE: keep regular formatting but italicize the NOTE portion?
        # Simple: render as plain paragraph; the "NOTE:" prefix is visible
        # enough in the firm's style.
        doc.add_paragraph(stripped)


def render_report(
    *,
    template_path: Path,
    output_path: Path,
    job: JobConfig,
    tract_sections: list[tuple[str, str]],   # [(tract_id, report_text), ...]
) -> Path:
    # Certificate page from the prepared template.
    tpl = DocxTemplate(str(template_path))
    tpl.render({
        "addressee": job.addressee or "",
        "effective_date": job.effective_date or "",
        "county": job.county or "",
        "signing_date": job.signing_date or "",
    })
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tpl.save(str(output_path))

    # Append per-tract sections.
    doc = Document(str(output_path))
    for tract_id, text in tract_sections:
        _render_tract_section(doc, tract_id, text, job)
    doc.save(str(output_path))
    return output_path
