# Tract {tract_id}

Below are every recorded instrument affecting tract `{tract_id}`, in chronological order. Assemble the per-tract section of the draft title report in the exact format and style specified in your system prompt.

**Effective Date:** {effective_date}

{commentary}

## Events for tract {tract_id}

{events}

## L&E carve-outs bound to this tract

{le_rows}

## Job context

{job_context}

## Mineral chain analysis (already completed — use verbatim)

A separate, more thorough mineral-estate analysis has already been performed for this tract. **Do not redo the mineral fractional analysis.** Incorporate the block below as follows:

- Use its **MINERAL VESTING** text as the `Minerals:` line under VESTING, verbatim.
- Place its **MINERAL EXCEPTIONS** items under Exceptions bucket 4 (Other Matters of Record).
- Place its **MINERAL LEASES** items under Exceptions bucket 5 (Mineral Leases).
- Fold its **MINERAL ATTORNEY REVIEW** items into your ATTORNEY REVIEW section.

```
{mineral_analysis}
```

# Task

Assemble the report section for tract `{tract_id}` per the output format in your system prompt. Begin with `=== TRACT {tract_id} ===` and end without commentary.

Requirements:
- The CHAIN OF TITLE comes first and lists **every event above, oldest first**. One line per instrument. Each line must include the recording date, document title, grantor, grantee, and Book/Page (or Instrument No.), plus a **brief** effect clause. Keep each entry to a single line — do not write a paragraph per instrument.
- Determine the **surface** vesting and the legal description yourself.
- Use the supplied mineral analysis for everything about the mineral estate (do not recompute fractions).
- Be concise throughout: convey the necessary facts without over-explaining.
