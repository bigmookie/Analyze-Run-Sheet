# Run Sheet Analyzer

Reads an abstractor's run sheet (Excel) and produces a draft Mississippi title report (Word `.docx`). Each tract is analyzed by Claude as a sophisticated Mississippi real estate attorney, with the firm's house-style guide and Mississippi title-examination methodology embedded directly in the prompts.

For the full design rationale, see [SPEC.md](./SPEC.md). This README covers setup and day-to-day use.

---

## Requirements

- Python 3.10 or newer.
- An **Anthropic** API key (for Claude analysis).

---

## First-time setup

1. **Create a virtual environment** and activate it.

   ```powershell
   cd "C:\Users\gardn\Documents\Projects\Run Sheet Analyzer"
   python -m venv .venv
   .venv\Scripts\activate
   ```

   On WSL/macOS/Linux: `source .venv/bin/activate`.

2. **Install runtime dependencies and this project.**

   ```powershell
   pip install -r requirements.txt
   pip install -e .
   ```

3. **Set the API key.** Copy `.env.example` to `.env` and fill in:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

4. **Confirm the firm template is in the project root.** The renderer reads `Abstract - Report - Minerals.docx` from the root and regenerates `templates/title-report.docx` automatically whenever the source changes.

---

## Run an analysis

```powershell
python analyze_run_sheet.py
```

1. A file picker opens — choose the run sheet `.xlsx`.
2. The parser validates the column schema (see below). Missing required columns abort the run.
3. If a `job.yaml` sits next to the run sheet, its fields pre-populate the certificate. A small dialog lets you fill in or override `addressee`, `effective_date`, `county`, and `signing_date`.
4. A confirmation dialog lists the discovered tracts; uncheck any you don't want to analyze.
5. A progress window streams per-tract status as Claude works.
6. When finished, `<run sheet stem> - Draft Report.docx` lands next to the run sheet and opens automatically. Per-tract JSON outputs go into `out/`.

To force a full re-analysis (ignoring cached per-tract JSON), set the env var `ANALYZER_REBUILD=1` before launching.

---

## Run sheet schema

The first row of the worksheet is treated as the header. Header matching is **case-insensitive, whitespace-tolerant, and accepts common synonyms** (e.g., `Recorded` ↔ `Date Recorded`, `Brief Legal Description` ↔ `Brief Description`).

**Required columns (12):**

| Canonical header     | Accepted synonyms                          |
|----------------------|--------------------------------------------|
| Book                 | —                                          |
| Page                 | —                                          |
| Date Recorded        | `Recorded`                                 |
| Document Title       | `Doc Title`                                |
| Grantor              | `Grantor(s)`, `Grantors`                   |
| Grantee              | `Grantee(s)`, `Grantees`                   |
| Brief Description    | `Brief Legal Description`, `Legal`         |
| Tract                | —                                          |
| Mineral Reservations | `Reservations`                             |
| Notes                | —                                          |
| Exception            | `Exceptions`                               |
| Mineral Transfer     | `Min Transfer`                             |

**Optional column:**

| Canonical header | Accepted synonyms                        |
|------------------|------------------------------------------|
| Instrument No    | `Inst No`, `Instrument Number`           |

If any required column is missing, the run aborts with a dialog listing what's missing.

### Tract tagging

- **Numeric / opaque tract IDs**: any label you want (`23.6`, `Lot 4`, `Parcel-A`). Discovered automatically.
- **Multiple tracts per row**: comma-separate (`23.1, 23.2, 23.3`). The row is replicated into each tract's chain.
- **`NS`** = Not Subject. Kept as context but never anchored.
- **`LE, <tract>`** = Less and Except. Carve-out from `<tract>`'s base description; appears as a "LESS AND EXCEPT" clause in that tract's legal description.

### Cell conventions

- Multiple grantors / grantees: separate with `|`.
- Multiple legal descriptions: separate with `|` or newlines.
- `Exception` / `Mineral Transfer` columns: any non-empty value (typically `x`) means flagged.

---

## Per-job config (`job.yaml`)

Place next to the run sheet. All fields are optional — the dialog fills in anything missing.

```yaml
effective_date: "March 13, 2026"
addressee: "Acme Pipeline LLC"
county: "Simpson"
signing_date: "May 11, 2026"
parcels:
  "23.6":
    parcel_id: "123-456-789"
    last_year: "2024"
    amount: "$842.13"
    priors_paid: true
```

---

## Project layout

```
Run Sheet Analyzer/
├── analyze_run_sheet.py            # entry point — Tkinter app
├── pyproject.toml
├── .env / .env.example
├── SPEC.md / README.md
├── Abstract - Report - Minerals.docx   # firm template (never modified)
├── templates/title-report.docx          # generated automatically
├── refs/                                # consumed read-only; built by Embeddings DB Creator
├── docs/                                # optional source PDFs / OCR txt (gitignored)
├── out/                                 # per-tract JSON cache + smoke artifacts
├── src/run_sheet_analyzer/
│   ├── cli.py             # Tkinter orchestration
│   ├── parser.py          # strict-schema Excel parser
│   ├── models.py          # pydantic JSON schemas
│   ├── analyzer.py        # agentic Claude conversation
│   ├── reconciler.py      # Fraction math validator
│   ├── template_builder.py # programmatic template prep
│   ├── renderer.py        # docx rendering
│   ├── cache.py           # per-tract hash cache
│   └── prompts/
│       ├── system.md        # attorney role + MS methodology
│       ├── surface.md       # surface chain turn
│       ├── mineral.md       # mineral chain turn
│       └── exceptions.md    # exception classification turn
├── scripts/research.py     # ad-hoc retrieval over refs/ for spec research
└── tests/fixtures/
```

---

## Customization

- **Prompts**: edit anything in `src/run_sheet_analyzer/prompts/`. Changes apply on next run; no rebuild required.
- **Firm template**: edit `Abstract - Report - Minerals.docx` in Word. The builder detects the SHA-256 change and regenerates `templates/title-report.docx` on the next run.
- **Adding a reference doc**: build a new DB with Embeddings DB Creator (`python create_embeddings_db.py`) targeting `refs/`. The analyzer picks it up automatically — no code change.
- **Force-rebuild cached analyses**: `setx ANALYZER_REBUILD 1` (or set it once in the shell). Unset to resume cached behavior.
- **Disable Opus escalation** (useful while iterating on prompts): `setx ANALYZER_NO_ESCALATE 1`.

---

## Cost ballpark per run

A 15-tract run sheet of typical complexity:

- Claude (Sonnet 4.6 with extended thinking, occasional Opus 4.7 escalation): **$1–3**.
- Voyage retrieval + reranking: **a few cents**.

Per-tract input is ~50K tokens on the first turn (system prompt + run sheet + tract context + retrieved standards). Subsequent area turns within the same tract reuse the cached prefix at ~10% of normal input cost.

---

## Troubleshooting

**"Reference library missing"** — `refs/` is empty or unreadable. Build at least one DB with the Embeddings DB Creator project.

**"Missing required column(s): X"** — the run sheet header is missing one of the 12 required columns. Use the table above (or a documented synonym) and re-run.

**"Missing API keys"** — copy `.env.example` to `.env` and fill in both `VOYAGE_API_KEY` and `ANTHROPIC_API_KEY`.

**Mineral fractions don't reconcile to 1** — the analyzer asks Claude to correct itself once and escalates to Opus. If it still can't reconcile, the report renders with an ATTORNEY REVIEW callout naming the imbalance. Inspect the chain in the appendix to find the missing or duplicated fractional event.

**Output looks wrong but no error** — JSON output is in `out/<tract>.json`. Diff that against expectations to localize the issue. The full Claude conversation isn't persisted; bump the prompts and re-run with `ANALYZER_REBUILD=1` to iterate.

**Template changes don't take effect** — delete `templates/title-report.docx` and `templates/title-report.source.sha256` to force a rebuild.
