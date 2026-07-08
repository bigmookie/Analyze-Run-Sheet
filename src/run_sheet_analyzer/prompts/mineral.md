# Mineral chain analysis — Tract {tract_id}

You are performing the **mineral estate** portion of the title report for tract `{tract_id}`. Analyze the mineral estate only — surface, liens, and servitudes are handled separately.

**Effective Date:** {effective_date}

{commentary}

## Events for tract {tract_id} (chronological, oldest first)

{events}

## L&E carve-outs bound to this tract

{le_rows}

# Task

Trace the mineral estate from the root of title (customarily the original land patent) forward to the Effective Date. Apply the Mississippi mineral methodology in your system prompt:

- Surface conveyances carry the minerals they have not previously severed.
- Track every reservation, mineral deed, royalty transfer, and severance as an **exact fraction**.
- Reconcile current mineral-fee ownership to **exactly 1** at the Effective Date.
- Distinguish mineral fee from NPRI / ORRI / working interest / term mineral / executive right (see the interest-type table in your system prompt).
- A tax sale of the surface does not convey separately-assessed minerals.
- Oil, gas and mineral leases are encumbrances, not fee conveyances.

# Output

Plain text, no preamble, no markdown fences. Use these exact headings so the assembler can find each part:

```
MINERAL VESTING:
- <Holder> (<fraction>) — <brief basis and citation, e.g. "as surface owner; Book 100 at Page 2" or "Mineral Right and Royalty Transfer, Book 110 at Page 3">
- <one holder per line — never combine multiple holders on one line>
Total <sum> — reconciles.
<If the fractions cannot be reconciled to 1, replace the Total line with: Total <sum> — MINERAL RECONCILIATION ISSUE: <brief description>.>

MINERAL EXCEPTIONS:
- <Each outstanding mineral reservation, severance, NPRI, ORRI, term-mineral, or executive-right severance affecting title, one per line, with citation. Or "None.">

MINERAL LEASES:
- <Each oil, gas and mineral lease of record and its assignments/ratifications/extensions not shown released, one per line, with citation. Or "None.">

MINERAL ATTORNEY REVIEW:
- <Each mineral-specific issue the closer should address (gaps in the mineral chain, unresolved fractions, ambiguous reservations), one per line. Omit this whole block if there are none.>
```

Be concise. Identify each instrument by parties, document title, and recording reference; state the effect in a short clause. Do not restate full legal descriptions.
