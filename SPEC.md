# Run Sheet Analyzer — Specification (v1)

## 1. Goal

Read an abstractor's run sheet (Excel) plus a small per-job config, and produce:
- A consolidated Word (`.docx`) title report following the firm's standard template
- A machine-readable JSON sidecar covering every tract

For each tract the AI analyzes three areas in a single agentic conversation, in order: **surface chain → mineral chain → exceptions**. Each area's output is grounded in the run sheet, with abstractor-flagged columns trusted as hints and disagreements surfaced explicitly.

## 2. High-level architecture

```
            ┌───────────────┐
Excel run ─▶│  Parser       │── normalized rows ──┐
sheet       └───────────────┘                     │
                                                  ▼
job config ─────────────────────────────▶ ┌──────────────────┐
(yaml/json)                               │ Tract builder    │── per-tract event stream ──┐
                                          └──────────────────┘                            │
                                                                                          ▼
optional PDFs / OCR txt ────▶ document store keyed by Book/Page ──────────────┐ ┌────────────────────┐
                                                                              ├▶│  Analysis pipeline │
                                                                              │ │  (Claude API)      │
hash + cache ──────────────────────────────────────────────────────────────── ┘ └─────────┬──────────┘
                                                                                          │
                                                       ┌──────────────────────────────────┤
                                                       ▼                                  ▼
                                                 JSON sidecar                    .docx renderer
```

## 3. Inputs

### 3.1 Run sheet (Excel)

Single worksheet. The parser reads the first non-blank row as the header. Headers are matched **case-insensitive, whitespace-tolerant, and tolerant of common synonyms** (e.g., "Recorded" ↔ "Date Recorded", "Brief Legal Description" ↔ "Brief Description").

**Required columns (12 of 13):**

| Header (canonical)        | Synonyms accepted                          | Type       | Notes                                                                 |
|---------------------------|--------------------------------------------|------------|-----------------------------------------------------------------------|
| Book                      | —                                          | str        | e.g. `1`, `MB E`, `C`                                                 |
| Page                      | —                                          | str        |                                                                       |
| Date Recorded             | `Recorded`                                 | date / str | parsed permissively                                                   |
| Document Title            | `Doc Title`                                | str        |                                                                       |
| Grantor                   | `Grantor(s)`, `Grantors`                   | str        | `|` separates multiple grantors                                       |
| Grantee                   | `Grantee(s)`, `Grantees`                   | str        | `|` separates; fractional interests may appear inline `(450/1250)`    |
| Brief Description         | `Brief Legal Description`, `Legal`         | str        | `|` separates legal descriptions                                      |
| Tract                     | —                                          | str        | comma-separated tract IDs; sentinels `NS`, `LE,<tract>`               |
| Mineral Reservations      | `Reservations`                             | str        | abstractor's prose of reservations                                    |
| Notes                     | —                                          | str        | attorney-significant annotations                                      |
| Exception                 | `Exceptions`                               | str / `x`  | `x` flags as title exception                                          |
| Mineral Transfer          | `Min Transfer`                             | str / `x`  | `x` flags as mineral-conveying instrument                             |

**Optional column:**

| Header (canonical) | Synonyms accepted              | Notes                                                                                |
|--------------------|--------------------------------|--------------------------------------------------------------------------------------|
| Instrument No      | `Inst No`, `Instrument Number` | If present, captured per row; rendered in citations as `Inst. No. <n>` when populated |

**Strict validation.** If any of the 12 required columns is missing from the header row, the parser **aborts with a clear error and a list of missing columns**. The Tkinter UI surfaces this as a message box; the user must fix the run sheet and re-run.

### 3.2 Tract tags

Tract IDs are **user-defined by the abstractor**. The system is tract-agnostic — it discovers tracts at parse time via `SELECT DISTINCT` from column G and treats them as opaque labels. The numbering scheme in the example run sheet (`23.6`, `26.1`) is illustrative; future run sheets may use any convention.

Two sentinel tokens have fixed meaning:

- **`NS`** = Not Subject. Row is kept in the corpus (Claude can see it as context) but never anchored to any tract chain.
- **`LE, <tract>`** = Less and Except, bound to `<tract>`. Row is a conveyance OUT of that base tract; the base tract's running legal description should be reduced by it.

Parsing rules for column G:

1. Split on comma, trim each token.
2. If the token list begins with `LE`, the row is an L&E carve-out. The remaining token(s) are the affected base tract(s). The row is bound to each of those tracts as `le_rows[i]`, never as `rows[i]`.
3. Else if any token is `NS`, the row is not-subject. Stored in the global `not_subject` list.
4. Else every remaining token is treated as a base tract the row belongs to (replicate across all).

A bare `LE` with no base tract is a parse error — surfaced to the user as a parser warning at load time, not deferred to the LLM.

### 3.3 Per-job config (YAML)

`job.yaml` in the same directory as the run sheet:

```yaml
effective_date: "2026-03-13"      # appears on certificate, vesting cutoff
addressee: "Acme Pipeline LLC"
county: "Simpson"
state: "Mississippi"
parcels:                          # optional, per tract
  "23.6":
    parcel_id: "123-456-789"
    last_year_taxes_paid: "2024"
    last_year_amount: "$842.13"
    priors_paid: true
# any field omitted → rendered blank in the report
```

All fields are optional. Missing fields render as blanks in the template.

### 3.4 Optional document attachments

Folder `docs/` keyed by `<book>-<page>.pdf` or `<book>-<page>.txt`. When Claude flags an entry as ambiguous, the analysis pipeline attaches the matching file to a follow-up turn (PDF via multimodal, txt inline). v1 attachment trigger: an entry the LLM marks `needs_source: true`.

## 4. Domain model

```python
@dataclass
class RunSheetRow:
    row_index: int
    book: str
    page: str
    recorded: date | None
    doc_title: str
    grantors: list[str]
    grantees: list[str]
    tracts: list[str]          # ["23.1", "23.2"] or ["LE"]
    is_less_except: bool
    is_not_subject: bool
    legal: str
    mineral_reservations: str
    notes: str
    exception_flag: bool       # column K == "x"
    mineral_transfer_flag: bool # column L == "x"

@dataclass
class Tract:
    id: str                    # "23.6"
    rows: list[RunSheetRow]    # rows where tract id is in tracts
    le_rows: list[RunSheetRow] # L&E rows bound to this tract
```

Fractional interests use `fractions.Fraction` end-to-end. Decimals are forbidden in the analysis layer; they may appear only in the rendered report when a Fraction is unrecognizable or attorney-formatted.

## 5. Parsing pipeline

1. Load Excel with `openpyxl`, `data_only=True`.
2. Read header row, build header→column map (case- and whitespace-insensitive).
3. For each data row:
   - Split grantors/grantees by `|`, trim, strip empties.
   - Split `Tract` by `,`, normalize tokens. Recognize `NS`, `LE`. A token `LE` adjacent to a numeric token → mark `is_less_except=True` and bind to that numeric tract. Bare `LE` → `is_less_except=True`, `tracts=[]`.
   - Parse `recorded` permissively (`MM/DD/YYYY`, `YYYY-MM-DD HH:MM:SS`, ISO).
   - Coerce `exception_flag`/`mineral_transfer_flag` from any non-empty value (typically `x`).
4. Build a `Tract` per distinct numeric tract id. Multi-tract rows are **replicated** into each tract (no allocation).
5. `NS` rows are kept in a separate `not_subject` list, available as context but never in `Tract.rows`.

## 6. Analysis pipeline

### 6.1 Per-tract agentic conversation

For each tract, one Claude conversation:

- **System prompt:** assigns the role of a **sophisticated Mississippi real estate attorney** examining title for marketability. Embeds the methodology from §6.5 (MS-specific rules). Embeds tone guidance: **concise, declarative, attorney-grade prose** — never bloated, never hedged unnecessarily, but always calling out the necessary information (parties, instrument, book/page, fractional interest, effect on title). Embeds the JSON schemas for the three areas. Marked `cache_control: ephemeral`.
- **User message 1 (cached):** full normalized run sheet serialized as a markdown table, plus the per-tract event stream as a focused subsection (`### TRACT <id> — events`), plus the L&E rows bound to this tract and the global NS rows. Marked `cache_control: ephemeral`.
- **User message 2 (cached, per-tract):** retrieved reference-doc context — top-K chunks from the **whole** `refs/` library scored against a per-tract query (see §6.4). Rendered as a "Reference Standards" block with citations. Marked `cache_control: ephemeral` so all three area turns reuse it.
- **User message 3 — surface:** "Construct the **surface chain of title** for tract `<id>` through the Effective Date `<effective_date>`. Identify the root of title per MTES Std. 2.02 (warranty deed, qualified quitclaim, state patent excluding forfeited tax land, or probate proceeding) reaching back at least 32 years (residential) or 50 years (commercial); walk forward through every conveyance affecting surface; flag any gaps, wild deeds, homestead joinder defects, and L&E carve-outs. Return JSON per `surface_chain.schema`. Set `confidence: high|medium|low` and `needs_source` for any entry you would want to read in full."
- **User message 4 — minerals:** "Construct the **mineral chain of title** for tract `<id>` **from sovereignty (the original land patent)** through the Effective Date. Use your surface chain as the spine; surface conveyances carry minerals unless severed. Track every reservation and mineral conveyance with **Fraction** arithmetic; reconcile total mineral interest at the Effective Date to exactly 1. Output the per-owner fractional table. Forfeited tax land patents do not sever minerals from a separately-assessed mineral estate. Return JSON per `mineral_chain.schema`."
- **User message 5 — exceptions:** "Identify all **title exceptions** affecting tract `<id>` at the Effective Date, grouped into six buckets per the firm's report template: (1) Voluntary Liens, (2) Involuntary Liens, (3) Servitudes, (4) Other Matters of Record, (5) Mineral Leases, (6) County Taxes. Apply MS-specific time-bars: unreleased deeds of trust whose underlying note is barred by SOL (6 yrs negotiable; 3 yrs pre-2012 / 6 yrs post non-negotiable per MTES Std. 6.05 & Miss. Code §§ 75-3-118, 15-1-49) may be treated as extinguished; explain when you do. Evaluate Document Title, Notes, and the Exception column together; do not rely on any single signal. Where the abstractor's flags disagree with your analysis, include both and mark `disagreement: true`. Return JSON per `exceptions.schema`."

If any area returns `confidence: low`, the pipeline re-asks that area with model bumped from Sonnet 4.6 → Opus 4.7 (same conversation continues, so prior turns provide context; the model parameter is updated on the new request).

### 6.2 Model & caching

- Default model: `claude-sonnet-4-6` with extended thinking enabled (`thinking: { type: "enabled", budget_tokens: 8000 }`).
- Escalation model: `claude-opus-4-7` (1M context).
- Prompt caching: system prompt + the full run-sheet markdown table get `cache_control: ephemeral`. The full run sheet is cached **once per process run** and reused across all tracts in the same invocation. Per-tract user messages are not cached.

### 6.3 Output schemas (JSON, condensed)

All schema examples below use `<tract_id>` as a placeholder; the actual tract IDs come from the run sheet at runtime.

```jsonc
// surface_chain.schema
{
  "tract": "<tract_id>",
  "chain": [
    {
      "seq": 1,
      "book": "<book>", "page": "<page>",
      "recorded": "<yyyy-mm-dd>",
      "doc_title": "<doc_title>",
      "grantors": ["..."],
      "grantees": ["..."],
      "interest_after": "1",            // Fraction as string
      "cotenants_after": [
        {"name": "...", "share": "1"}
      ],
      "notes": "...",
      "authorities": ["MTES § 7.02"],   // optional citations from refs/
      "gaps": [],
      "disagreement_with_abstractor": false
    }
  ],
  "current_vesting": [
    {"name": "...", "share": "1/2"},
    {"name": "...", "share": "1/2"}
  ],
  "confidence": "high",
  "needs_source": [],
  "attorney_review_callouts": []
}

// mineral_chain.schema — same shape, plus:
{
  "reservations": [
    {"book": "<book>", "page": "<page>", "fraction_reserved": "1/2",
     "reserver": "...", "notes": "..."}
  ],
  "current_mineral_owners": [
    {"name": "...", "share": "1/4", "source_book_page": "<book>-<page>"}
  ],
  "reconciliation": {"total": "1", "ok": true}
}

// exceptions.schema
{
  "tract": "<tract_id>",
  "buckets": {
    "voluntary_liens": [...],
    "involuntary_liens": [...],
    "servitudes": [...],
    "other_matters": [...],
    "mineral_leases": [...],
    "county_taxes": [...]
  },
  "disagreements": [
    {"book": "...", "page": "...",
     "abstractor": "flagged as exception",
     "claude": "released by Book/Page X",
     "resolution": "remove from exceptions"}
  ]
}
```

Every chain/exception entry **must** cite source `book` + `page`. The renderer enforces this.

### 6.4 Reference docs / retrieval (external dependency)

Authoritative legal reference content (Mississippi Title Examination Standards, abstractor manuals, firm memos) is built and queried by a separate companion project — **"Embeddings DB Creator"** at `C:\Users\gardn\Documents\Projects\Embeddings DB Creator`, exposing the Python package `doc_embed`. The run sheet analyzer treats this library as a black-box dependency.

The `refs/` directory at the project root contains one subdirectory per built DB; each subdir is a self-contained 4-file unit (`manifest.json`, `chunks.jsonl`, `embeddings.npy`, `bm25.pkl`).

**Public contract the analyzer relies on:**

```python
from doc_embed import RefLibrary, RetrievedChunk
from dotenv import load_dotenv
load_dotenv()  # picks up VOYAGE_API_KEY from project-local .env

lib = RefLibrary.load("./refs")             # loads every DB under ./refs

# What's loaded:
stats: dict = lib.stats()                   # {doc_name: {n_chunks, embed_model, built_at, ...}}

# Hybrid retrieval (dense + BM25 fused via RRF, optional Voyage rerank):
hits: list[RetrievedChunk] = lib.retrieve(
    query="mineral severance fractional reservation Mississippi",
    k=8,
    rerank=True,                            # ~$0.0015/query when enabled
    docs=None,                              # or ["mississippi-title-examination-standards"] to scope
)

# RetrievedChunk fields (all available):
#   .id                stable chunk id, e.g. "mtes:7.02:0"
#   .doc               source DB name (the subdir under refs/)
#   .section           section identifier within the doc (e.g. "7.02", "root" for txt/pdf)
#   .title             section heading text (NOT the document title)
#   .citation          rendered per the DB's citation_template
#   .text              full chunk text
#   .score             higher = better, comparable within one retrieve() call
#   .parent_hierarchy  list of ancestor heading strings
```

**Multi-model handling:** DBs in `refs/` may use different embed models (the current set mixes `voyage-law-2` and `voyage-3-large`). The library re-embeds queries per-model as needed and fuses across DBs — the analyzer does not need to worry about model coordination.

**Usage in the analysis pipeline:**

1. At process start, load the library once: `lib = RefLibrary.load(refs_path)`. If `lib.stats()` is empty, log a warning and continue without retrieval (Claude proceeds on system prompt + run sheet alone). If `--refs` is omitted entirely, skip loading altogether.
2. Per tract, build a focused query string from the tract's facts (doc-title histogram, presence of mineral reservations / OGMLs / chancery causes / tax sales / patents), retrieve top-K chunks once, and render them as a cached "Reference Standards" user message that all three area turns reuse.
3. If `--per-area-retrieval` is enabled, each area turn may issue an additional targeted retrieval (e.g., the minerals turn appends "fractional mineral reservation, after-acquired title, severance").
4. `--rerank` toggles the Voyage reranker on retrieval calls. Recommend on by default for legal work.
5. The `docs=` filter can be used to scope retrieval — e.g., legal-authority queries (vesting rules, mineral severance doctrine) can pin to `["mississippi-title-examination-standards"]`, while procedural questions (how to read a chancery decree, abstractor conventions) can use the manuals. v1 default: no filter; retrieve across the whole library.

**`VOYAGE_API_KEY` requirement:** must be present in the environment when retrieval is performed (every retrieve() embeds the query). The analyzer loads `./.env` via `python-dotenv` at startup. If the key is missing and `--refs` is non-empty, the analyzer hard-fails with a clear error rather than producing a report without authority grounding.

**No build-side code lives in this project.** Building the `refs/` directory (chunking, embedding, indexing) is owned by the Embeddings DB Creator project; see its README for usage.

### 6.5 Mississippi title examination methodology (system-prompt content)

The system prompt embeds the following MS-specific rules, drawn from MTES and the firm's reference manuals (verified by retrieval against `refs/`). Claude is instructed to **apply these rules by default but consult the retrieved Reference Standards block** when an edge case appears.

**Period of examination (MTES Std. 2.02; Miss. Code §§ 1-3-27, 15-1-13):**
- Residential (1–4 family): minimum **32 years** to root of title.
- Commercial: minimum **50 years**.
- **Minerals: back to original land patent (sovereignty).** Always.
- A "root of title" is a warranty deed (general or special), a qualified quitclaim, a state patent (excluding forfeited tax land patents), or a probate proceeding identifying the property.

**Mineral severance (MTES; First American Agents Manual):**
- Surface and minerals are separate, distinct estates once severed.
- Severance can occur by reservation in a deed, conveyance of minerals only, mineral lease, or mortgage of the mineral estate.
- Mineral estate is dominant over surface in MS; lessee may use as much surface as reasonably necessary.
- Tax sale of the surface does **not** convey separately-assessed mineral interests (Miss. Code §§ 27-31-73, 27-35-51).

**Tax sales (Miss. Code §§ 27-41-59, 27-45-3, 15-1-15; Miss. Const. art. 4 § 79):**
- 2-year redemption from sale date; minors and persons of unsound mind have extended periods.
- Upon redemption, clerk's release operates as a quitclaim from the state/purchaser.
- 3 years of actual occupancy after the 2-year redemption period cures most defects (§ 15-1-15) — but does **not** cure a tax sale void on its face (e.g., inadequate description).
- Forfeited tax land patents are **not** valid as root of title.

**Deeds of Trust — time bar on the underlying note (MTES Std. 6.05; Miss. Code §§ 75-3-118, 15-1-49, 89-1-49):**
- Negotiable note: action barred 6 years after due date or acceleration date.
- Non-negotiable note: 3 years pre-7/1/2012, 6 years post.
- Demand note: 6 years after demand.
- When the underlying debt is time-barred, the examiner may presume the DOT is extinguished. The analyzer applies this presumption when the recorded due date plus the applicable period is before the Effective Date, with no evidence of acceleration tolling. If facts to apply the time-bar are not in the run sheet, the DOT remains an exception with an ATTORNEY REVIEW callout.

**Heirship affidavits (MTES Std. 12.07; Miss. Code § 89-5-8(3)):**
- Reliable when 3 years + 90 days have passed since the decedent's death.
- Must identify affiant, not be self-serving, recite reasonable basis for facts.
- Two affiants (one corroborating) preferred per First American.

**Homestead joinder (Miss. Code § 89-1-29):**
- Conveyance or DOT on homestead property without non-titled spouse's joinder is **VOID** — not merely voidable. *No subsequent ratification cures it.*
- Five exceptions: (1) non-owner-occupied property, (2) purchase-money mortgages, (3) inter-spousal conveyances, (4) judicial sales, (5) certain corporate conveyances.
- The analyzer flags every modern deed of what may be a residence where only one spouse signed, unless the document title or notes indicate an exception applies.

**Forms of conveyance:**
- General warranty: full covenants, carries after-acquired title via estoppel.
- Special warranty: covenants only against grantor's own acts.
- Quitclaim: no covenants; does **not** carry after-acquired title.
- The analyzer flags after-acquired title issues when a later patent or curative deed conveys an interest the grantor had previously purported to convey by warranty deed.

**Tone and reporting style (binding on Claude's output):**
- Write as a sophisticated Mississippi real estate attorney drafting a draft title opinion.
- Every assertion must cite Book/Page (and Instrument No. if available).
- Every Mississippi-specific legal rule must cite either an MTES standard or a Miss. Code section, drawn from the Reference Standards block.
- Concise, declarative sentences. No throat-clearing. No "it appears that," "may possibly," or other unnecessary hedging — when uncertain, emit an ATTORNEY REVIEW callout instead.
- Where the abstractor's flag disagrees with the analysis, both views are stated and the analysis explains which controls.

## 7. Mineral fraction reconciliation

1. Start with `total_mineral = 1` to the original surface owner (often the patent grantee).
2. Walk the per-tract event stream in `recorded` order.
3. For each event:
   - If `mineral_transfer_flag` is set OR `mineral_reservations` is non-empty OR Claude classifies it as mineral-affecting: parse the conveyed/reserved fraction.
   - Update per-owner fractional ledger: subtract from grantor's holdings (proportional if grantor holds less than 1/1 of a reserved interest), add to grantee.
4. At the Effective Date, sum all current owners' shares; assert equality with `Fraction(1)`. Mismatch ⇒ `reconciliation.ok = false` and an attorney callout.
5. The reconciliation algorithm runs **client-side in Python** on Claude's emitted fraction events. Claude proposes fractions; Python validates totals. If totals don't sum, the area is re-asked with the imbalance shown to Claude.

## 8. Output rendering

### 8.1 .docx generation

- Library: `docxtpl` (wraps `python-docx` and adds Jinja templating directly inside the Word document).
- Base template: `Abstract - Report - Minerals.docx` in the project root. The template already contains the firm certificate header, methodology block, and a TITLE REPORT section with the A–C structure (vesting, description, exceptions).
- **Template preparation is automatic.** A module `template_builder.py` reads `Abstract - Report - Minerals.docx`, applies the placeholder substitutions listed in §8.2 programmatically (using `python-docx` paragraph-text replacement and structured insertion of Jinja tags), and saves the result to `templates/title-report.docx`. This runs once at first launch and again whenever the source template's SHA-256 changes. The user never has to edit Word.
- One consolidated document. Structure:
  1. Certificate page (rendered once from job config).
  2. Methodology / Scope of Title Examination block (carried over from template).
  3. For each tract: a `TITLE REPORT` section reproducing the template's A–C blocks (vesting, description, exceptions, mineral leases, taxes).
  4. Appendix: chain-of-title tables (surface and mineral) rendered as Word tables.
- Attorney callouts render as red, bold paragraphs prefixed `ATTORNEY REVIEW —` using docxtpl's `RichText` API.

### 8.2 Template placeholders (the list to apply by hand)

The placeholders below use docxtpl's three tag forms:

- `{{ expr }}` — inline value, no structural effect.
- `{%p ... %}` — paragraph-level control flow; the line that holds the tag is consumed (used for `for`/`endfor`/`if`/`endif` over paragraph blocks).
- `{%tr ... %}` — table-row-level control flow; the row holding the tag is consumed (used for tables).
- `{{ rt_callout }}` — paragraph rendered as `RichText` for ATTORNEY REVIEW callouts (red, bold).

**Top of document — once:**

| Replace in template                                | With                                              |
|----------------------------------------------------|---------------------------------------------------|
| `ADDRESSEE` (line by itself)                       | `{{ addressee }}`                                 |
| `Effective Date: EFFECTIVE DATE`                   | `Effective Date: {{ effective_date }}`            |
| `Chancery Court Records of COUNTY County`          | `Chancery Court Records of {{ county }} County`   |
| `on this the May 11, 2026,`                        | `on this the {{ signing_date }},`                 |

**Per-tract section — wrap the existing "TITLE REPORT" block in a paragraph-level for-loop:**

The single TITLE REPORT block (everything from the heading `TITLE REPORT` through the end of the C.6 County Taxes line) is wrapped in:

```
{%p for tract in tracts %}
TITLE REPORT — Tract {{ tract.id }}
...
{%p endfor %}
```

Inside each tract block, the placeholders are:

**A. Title Vested in**

```
As to the Surface Estate:
{%p for owner in tract.surface_owners %}
{{ owner.name }}, an undivided {{ owner.share }} interest{% if owner.note %} ({{ owner.note }}){% endif %}
{%p endfor %}

As to the Mineral Estate:
{%p for owner in tract.mineral_owners %}
{{ owner.name }}, an undivided {{ owner.share }} interest{% if owner.note %} ({{ owner.note }}){% endif %}
{%p endfor %}
```

**B. Description**

```
{{ tract.legal_description }}
{%p for le in tract.less_and_except %}
LESS AND EXCEPT: {{ le.description }} (Book {{ le.book }}, Page {{ le.page }}, recorded {{ le.recorded }})
{%p endfor %}
```

**C. Title Exceptions** — each of the six numbered subsections uses the same pattern. Example for `C.1 Voluntary Liens`:

```
1.    Voluntary Liens (Deeds of Trust, Assignments of Rent, UCCs, etc.):
{%p for x in tract.voluntary_liens %}
{{ x.description }} (Book {{ x.book }}, Page {{ x.page }}, recorded {{ x.recorded }}){% if x.disagreement %}  [{{ x.disagreement_note }}]{% endif %}
{%p endfor %}{%p if not tract.voluntary_liens %}
None.
{%p endif %}
```

Repeat the same `for`/`if none` pattern for:

- `tract.involuntary_liens` (C.2)
- `tract.servitudes` (C.3)
- `tract.other_matters` (C.4)
- `tract.mineral_leases` (C.5)

**C.6 County Taxes** is single-valued per tract:

```
Parcel ID: {{ tract.taxes.parcel_id }}
{{ tract.taxes.last_year }} Taxes Paid in the Amount of {{ tract.taxes.amount }}
{%p if tract.taxes.priors_paid %}Priors paid{%p else %}Prior years: see attorney review{%p endif %}
```

**Attorney-review callouts** — at the end of each tract block:

```
{%p for c in tract.attorney_review %}
{{ c.rt }}
{%p endfor %}
```

…where the renderer pre-builds each `c.rt` as `RichText("ATTORNEY REVIEW — " + text, color="C00000", bold=True)`.

**Appendix — chain-of-title tables.** Two Word tables per tract, each with a header row already present in the template. The data row gets the `{%tr for row in tract.surface_chain %}` (and similarly `tract.mineral_chain`) wrapper:

```
| Seq | Book/Page | Recorded | Doc Title | Grantor | Grantee | Interest After |
| {%tr for row in tract.surface_chain %}{{ row.seq }} | {{ row.book }}/{{ row.page }} | {{ row.recorded }} | {{ row.doc_title }} | {{ row.grantors_joined }} | {{ row.grantees_joined }} | {{ row.interest_after }}{%tr endfor %} |
```

The renderer is responsible for producing pre-joined strings (`grantors_joined = " | ".join(grantors)`) so the template never does logic beyond loops and conditionals.

**Render-side context shape** (the dict passed to `DocxTemplate.render(...)`):

```python
{
  "addressee": str,
  "effective_date": str,
  "county": str,
  "signing_date": str,
  "tracts": [
    {
      "id": str,
      "surface_owners": [{"name": str, "share": str, "note": str|None}, ...],
      "mineral_owners": [{"name": str, "share": str, "note": str|None}, ...],
      "legal_description": str,
      "less_and_except": [{"description": str, "book": str, "page": str, "recorded": str}, ...],
      "voluntary_liens":   [{"description": str, "book": str, "page": str, "recorded": str, "disagreement": bool, "disagreement_note": str|None}, ...],
      "involuntary_liens": [ ...same shape... ],
      "servitudes":        [ ...same shape... ],
      "other_matters":     [ ...same shape... ],
      "mineral_leases":    [ ...same shape... ],
      "taxes": {"parcel_id": str, "last_year": str, "amount": str, "priors_paid": bool},
      "attorney_review":   [{"rt": RichText}, ...],
      "surface_chain":     [ ...rows... ],
      "mineral_chain":     [ ...rows... ],
    },
    ...
  ],
}
```

Any field that the job config or analysis didn't fill is rendered as an empty string (the renderer normalizes `None → ""` before `render()`).

### 8.3 JSON sidecar

`out/<run-sheet-stem>.analysis.json`:

```jsonc
{
  "job": { "effective_date": "...", "addressee": "...", "county": "...", ... },
  "tracts": {
    "<tract_id>": {
      "surface": { /* surface_chain.schema */ },
      "mineral": { /* mineral_chain.schema */ },
      "exceptions": { /* exceptions.schema */ },
      "input_hash": "sha256:...",
      "model_used": { "surface": "sonnet-4-6", "mineral": "sonnet-4-6", "exceptions": "opus-4-7" },
      "generated_at": "2026-05-11T12:34:56Z"
    }
  }
}
```

## 9. Incremental re-runs

- For each tract compute `input_hash = sha256(canonical_json(tract_rows + le_rows + ns_rows + job_config_subset))`.
- Persist `out/<tract>.json` and `out/<run-sheet-stem>.analysis.json`.
- On re-run: skip tracts whose `input_hash` matches the cached one; re-run only the changed tracts; rebuild the consolidated .docx from the merged set.
- Force-rerun flag: `--rebuild [tract-id|all]`.

## 10. Entry point — `analyze_run_sheet.py`

The system is invoked by running `analyze_run_sheet.py` from the project root. It is a **Tkinter-driven GUI app**, not a flag-heavy CLI.

**Flow:**

1. Loads `./.env` for `VOYAGE_API_KEY` and `ANTHROPIC_API_KEY`. If either is missing, shows an error dialog and exits.
2. Opens a **file picker** for the user to choose the run-sheet `.xlsx`. Default starting directory is the OS user's home or the most-recently-used path (persisted in `~/.run_sheet_analyzer.json`).
3. Looks for `job.yaml` (per-tract config: addressee, effective date, county, parcels) in the same directory as the chosen run sheet. If absent, prompts the user with a small dialog to fill in addressee / effective date / county; remaining template fields are left blank in the output.
4. Parses the run sheet with strict column validation (§3.1). Any missing required column is shown in a message box; the run aborts.
5. Loads the embeddings library from `./refs`. If `refs/` is missing or empty, **hard-fails** with a message box explaining that authority grounding is required.
6. Discovers tracts, presents a confirmation dialog listing them (with row counts) and asks the user to confirm or deselect any.
7. For each tract, runs the agentic Claude conversation (§6.1), writes `out/<tract_id>.json`, and shows a progress line in a small status window.
8. After all tracts complete, renders the consolidated `.docx` report (§8.1) and writes it to the same directory as the run sheet, with filename `<run-sheet-stem> - Draft Report.docx`. Opens the result with the OS default opener on success (best-effort).
9. Shows a final summary dialog: tracts succeeded, tracts with reconciliation failures, tracts with attorney-review callouts, location of the output file.

**No CLI flags** in v1. Defaults are baked in per the open-question resolutions: whole-library retrieval, Voyage reranker on, refs/ hard-fail, programmatic template tagging, Sonnet 4.6 default with Opus 4.7 escalation on low confidence or reconciliation failure.

**Incremental re-runs** (§9) still happen — same hash-based cache — but they are transparent to the user. To force a full rebuild, the user holds **Shift** while clicking "Run" in the confirmation dialog (or deletes the `out/` directory).

Exit codes (useful when the app is automated): `0` clean, `1` parse/API error, `2` reconciliation failure on any tract.

## 11. Project layout

```
Run Sheet Analyzer/
├── SPEC.md                        # this file
├── EMBEDDINGS_SPEC.md              # historical design doc for the embeddings system
│                                  # (now built; canonical reference is the
│                                  # README in Embeddings DB Creator)
├── pyproject.toml                 # deps below
├── .env                           # VOYAGE_API_KEY=... (gitignored)
├── .env.example                   # template, committed
├── job.yaml                       # per-run config (gitignored)
├── Abstract - Report - Minerals.docx   # firm-owned template original (not modified)
├── templates/
│   └── title-report.docx          # docxtpl-prepared copy of the firm template
├── refs/                          # built externally by Embeddings DB Creator
│   ├── mississippi-title-examination-standards/   # voyage-3-large, 264 chunks
│   ├── abstractor-training-manual/                # voyage-law-2, 6344 chunks
│   └── first-american-agents-manual/              # voyage-law-2, 13362 chunks
├── docs/                          # optional per-job source documents (gitignored)
│   └── <book>-<page>.{pdf,txt}
├── out/                           # per-tract cache + final outputs
├── src/run_sheet_analyzer/
│   ├── __init__.py
│   ├── cli.py                     # analyze.py entrypoint
│   ├── parser.py                  # Excel → RunSheetRow / Tract
│   ├── models.py                  # pydantic models for JSON schemas
│   ├── analyzer.py                # agentic conversation orchestration
│   ├── prompts/
│   │   ├── system.md
│   │   ├── surface.md
│   │   ├── mineral.md
│   │   └── exceptions.md
│   ├── reconciler.py              # Fraction math validator
│   ├── renderer.py                # docxtpl rendering
│   └── cache.py                   # hashing + on-disk JSON cache
└── tests/
    └── fixtures/
        └── run_sheet_minimal.xlsx
```

**Dependencies:**

- `anthropic` — Claude API client
- `openpyxl` — Excel parsing
- `python-docx` + `docxtpl` — Word template rendering
- `numpy`, `pydantic`, `pyyaml` — data plumbing
- `python-dotenv` — load `VOYAGE_API_KEY` from project-local `.env`
- The `doc_embed` package, installed editable from the user's existing project:

  ```bash
  pip install -e "C:\Users\gardn\Documents\Projects\Embeddings DB Creator"
  ```

The retrieval-side machinery (Voyage SDK, BM25, chunk loaders) is a transitive dependency through `doc_embed`, not a direct dep here.

## 12. Validation & QA

- `--dry-run` parses the Excel and prints per-tract event counts, distinct tracts, NS/LE counts, flag totals. No API calls.
- Unit tests cover: parser edge cases (multi-tract rows, `|`-separated grantor lists, weird `Recorded` values, `MB E` book), reconciler (fraction math), L&E binding.
- An end-to-end smoke test runs one tract through Claude with a tiny mock run sheet and asserts the JSON validates against the schemas.

## 13. v2 / explicitly out of scope

- Hyperlinks to PDF originals (no hosting yet).
- Persistent SQLite with cross-job history.
- Web UI for interactive review.
- Automated PDF retrieval from county records.
- Streaming token-by-token output in the CLI.

## 14. Resolved decisions

All previously-open questions are resolved:

1. **Template tagging:** **programmatic.** `template_builder.py` inserts the Jinja tags from §8.2 into a copy of `Abstract - Report - Minerals.docx` at first launch; the user never opens Word.
2. **Retrieval scoping:** **whole library.** No `docs=` filter — the analyzer queries every DB in `refs/` and lets RRF fuse the results.
3. **Empty-refs behavior:** **hard-fail.** If `refs/` is missing, empty, or unreadable, the run aborts with a clear dialog explaining that authority grounding is required.
4. **Reranker default:** **on.** The Voyage reranker is always invoked.
5. **Tone / role:** the system prompt assigns the role of a **sophisticated Mississippi real estate attorney**; output is concise, declarative, and always includes citation (book/page; MTES standards or Miss. Code sections for legal rules).
6. **No CLI flags.** The Tkinter app uses fixed defaults; advanced overrides live only via the env (`ANALYZER_FORCE_OPUS=1`, `ANALYZER_REBUILD=1`).
