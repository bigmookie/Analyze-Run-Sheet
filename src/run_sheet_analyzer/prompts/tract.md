# Tract {tract_id}

You are a sophisticated Mississippi real estate attorney working as a landman/abstractor. Below are every recorded instrument affecting tract `{tract_id}`, in chronological order. Produce the corresponding section of the draft title report.

Effective Date: {effective_date}

## Events for tract {tract_id}

{events}

## L&E carve-outs bound to this tract

{le_rows}

## Job context

{job_context}

# Your task

Produce the report section for tract `{tract_id}` in the **exact format below**, replacing placeholders with values you derive from the events above. Use plain text — no JSON, no markdown code fences. Use the headings exactly as written so the renderer can find them.

```
=== TRACT {tract_id} ===

VESTING
Surface: <one-line statement of surface vesting; e.g. "Title to the surface estate is vested in Jane Doe.">
Minerals: <one-line statement of mineral vesting; e.g. "Title to the mineral estate is vested in Jane Doe (1/2, as surface owner), R. F. Reed (1/4), Jesse Burress (1/4)."  If mineral fractions don't reconcile to 1, end the line with " [MINERAL RECONCILIATION ISSUE: <describe>]".>

DESCRIPTION
<Base legal description as one paragraph. Append "LESS AND EXCEPT" clauses (one per paragraph) for each L&E carve-out listed above, each citing the source Book/Page or Instrument No.>

EXCEPTIONS

1. Voluntary Liens (Deeds of Trust, Assignments of Rent, UCCs, etc.):
- <one exception per line; identify parties, document title, amount if applicable, recording reference; e.g. "Deed of Trust from Jane Doe to Patrick Caldwell, trustee for ABC Bank, securing $250,000.00, dated April 3, 2020, recorded Book 1234 at Page 567.">
- If none, write a single line: "None."
- If a DOT appears time-barred by Mississippi statute of limitations (6 yrs negotiable; 3/6 yrs non-negotiable per pre/post 7/1/2012), still list it and append "  NOTE: Underlying note appears time-barred."

2. Involuntary Liens (Judgments, Tax Liens, etc.):
- <one per line, or "None.">

3. Servitudes (Covenants, Restrictions, Plats, Easements, Rights-of-Way, etc.):
- <one per line, or "None.">

4. Other Matters of Record:
- <one per line, or "None.">
- Include: heirship clouds (any chain link relying on a recital like "X, Y, Z, being the only heirs of John Doe" WITHOUT a probate, recorded will, or recorded affidavit of heirship — list as "Heirship of [Decedent] and any heirs, devisees, grantees, creditors, and other persons claiming by, through and under the above-named decedent.  NOTE: Recital in Book X at Page Y says grantors are the sole heirs of [Decedent].")
- Tax sales / forfeited tax patents (ALWAYS list, even if cured — add a NOTE describing any curative deed or confirmation suit)
- Gaps in the chain ("Gap from [last record grantee] (Book X at Page Y) to [next record grantor] (Book A at Page B).")
- Life estate reservations where the life tenant remains alive of record
- Description defects, scrivener errors, options of record, releases of damages, lis pendens, chancery decrees, divorces

5. Mineral Leases:
- <every active OGML and assignments/ratifications/extensions, or "None.">

6. County Taxes:
[See parcel data from job config; the renderer fills this in.]

ATTORNEY REVIEW
- <one issue per line — anything you want flagged for the closer's attention>
- If none, omit the ATTORNEY REVIEW block entirely.
```

# Style and rules

- Cite Book/Page as "Book 1234 at Page 56" — never "1234/56", never "Book 1234, Page 56".
- Cite instrument numbers as "Instrument No. 2018005432".
- Do NOT cite MTES standards or Miss. Code sections in your output. The reference material is for your reasoning only.
- One sentence per exception. Identify by parties, document title, recording reference. State the effect concisely.
- An oil/gas/mineral lease is an exception, not a conveyance — list under Mineral Leases.
- Tax sale of the surface does not convey separately-assessed minerals.
- Default search period assumption: 100 years back from the Effective Date.
- For mineral fractions: track each reservation and conveyance as exact fractions; reconcile to 1 at the Effective Date. If you can't reconcile, list everyone you can verify and flag the imbalance on the Minerals: line.

Begin your response with `=== TRACT {tract_id} ===` and end without commentary.
