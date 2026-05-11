# Surface chain — tract `{tract_id}`

Construct the **surface chain of title** for tract `{tract_id}` through the Effective Date `{effective_date}`.

## Method

1. Identify the **root of title** per MTES Std. 2.02 reaching back at least 32 years (residential) or 50 years (commercial). Acceptable roots: warranty deed (general or special), qualified quitclaim, state patent **excluding** forfeited tax land patents, probate proceeding identifying the property.
2. Walk forward through every recorded conveyance affecting the surface of this tract.
3. For each event:
   - Capture the grantors, grantees, document title, book/page, recording date, and a one-sentence summary stating the effect on surface title.
   - Update `cotenants_after` with the fractional shares each owner holds in surface after the event.
   - Cite any MTES standard or Miss. Code section that controls (e.g., homestead joinder under § 89-1-29 if both spouses signed).
4. End at the current vesting as of the Effective Date.
5. Flag in `attorney_review`:
   - Gaps in the chain (grantor in row N does not match grantee in row N−1).
   - Wild deeds / strangers to title.
   - Apparent homestead-joinder defects on modern conveyances of what may be a residence.
   - Less-and-Except carve-outs whose legal description cannot be precisely reconciled with the base description.
   - Any instrument that disagrees with the abstractor's Exception flag.

## Less-and-Except

The user message includes a section listing every row tagged `LE, {tract_id}`. Apply each as a deduction from the base description. The synthesized `legal_description` field should read: base description, followed by one "LESS AND EXCEPT" clause per L&E row, each citing the source book/page.

## Output

Return JSON only, matching the `SurfaceChain` schema:

```json
{{
  "tract": "{tract_id}",
  "chain": [ {{ ChainEntry... }} ],
  "current_vesting": [ {{ "name": "...", "share": "1/2", "note": "...", "source_book_page": "..." }} ],
  "legal_description": "<base description, with LESS AND EXCEPT clauses>",
  "less_and_except": [ {{ "description": "...", "book": "...", "page": "...", "recorded": "..." }} ],
  "confidence": "high|medium|low",
  "needs_source": [ "<book>-<page>", ... ],
  "attorney_review": [ "...", ... ]
}}
```
