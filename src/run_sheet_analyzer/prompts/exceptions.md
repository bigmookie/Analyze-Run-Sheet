# Exceptions — tract `{tract_id}`

Identify every **title exception** affecting tract `{tract_id}` at the Effective Date `{effective_date}` and group them into six buckets that match the firm's report template.

## Buckets

1. **Voluntary Liens** — deeds of trust, assignments of rent, UCC fixture filings, secured party financing statements affecting the realty.
2. **Involuntary Liens** — judgments, federal tax liens, state tax liens, construction/materialmen's liens.
3. **Servitudes** — easements, rights-of-way, restrictive covenants, plats, declarations, party-wall agreements.
4. **Other Matters of Record** — chancery causes (partition, sale to pay debts, conservatorship), tax sales not yet matured beyond redemption, open estates, lis pendens, release of damages, special assessments.
5. **Mineral Leases** — every oil, gas, or mineral lease (and its assignments, ratifications, extensions) currently of record.
6. **County Taxes** — parcel ID and the most recent taxes paid (rendered from the job config, but list any tax-related liens of record here).

## Methodology

- Read **Document Title**, **Notes**, and the **Exception** column together; do not rely on any single signal.
- Where the abstractor's Exception flag conflicts with your analysis, include the item under the bucket you believe is correct, set `disagreement: true`, and supply `disagreement_note` explaining the conflict and which view controls.
- Apply Mississippi time-bars:
  - Negotiable-note DOTs: extinguished 6 years after stated due date or any recorded acceleration (Miss. Code § 75-3-118(a); MTES Std. 6.05).
  - Non-negotiable note DOTs: 3 years pre-7/1/2012; 6 years post (Miss. Code § 15-1-49).
  - Demand notes: 6 years after demand (Miss. Code § 75-3-118(b)).
  - Apply the presumption of extinguishment only when the run sheet shows facts sufficient to establish the bar. Otherwise list the DOT as a current exception and emit an ATTORNEY REVIEW callout.
- Federal tax liens are searched back 20 years; state tax liens 7 years; construction liens 5 years; judgment liens 10 years from enrollment (per the firm's scope of examination).
- A satisfied or released instrument is not an exception. The run sheet's Notes may mention release; if so, omit and explain in the analysis but not as an exception.
- A tax sale within the 2-year redemption window remaining as of the Effective Date is a serious cloud — list under Other Matters and emit an ATTORNEY REVIEW callout. After 3 years of actual occupancy following redemption (Miss. Code § 15-1-15), older defects are barred — but never for tax sales void on their face.

## Writing style

- One sentence per item. Identify the instrument by parties, document title, book/page, and recording date. State the effect concisely.
- Example (voluntary lien): "Deed of Trust from John Smith to ABC Bank securing $250,000, dated April 3, 2018, recorded Book 1234, Page 56."
- Example (servitude): "Right-of-Way Easement from John Smith to Mississippi Power Company, 50-foot strip across the SE/4, recorded Book 980, Page 129."

## Output

Return JSON only, matching the `Exceptions` schema:

```json
{{
  "tract": "{tract_id}",
  "buckets": {{
    "voluntary_liens": [ {{ ExceptionItem... }} ],
    "involuntary_liens": [],
    "servitudes": [],
    "other_matters": [],
    "mineral_leases": [],
    "county_taxes": []
  }},
  "confidence": "high|medium|low",
  "needs_source": [ "<book>-<page>", ... ],
  "attorney_review": [ "...", ... ]
}}
```
