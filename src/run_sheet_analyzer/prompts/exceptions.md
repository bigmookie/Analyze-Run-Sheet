# Exceptions — tract `{tract_id}`

Identify every **title exception** affecting tract `{tract_id}` at the Effective Date `{effective_date}` and group them into six buckets that match the firm's report template. You are producing the **Exceptions** portion of the report only — a description of every matter of record that affects title. You are NOT writing requirements; the closer drafts those separately.

## Buckets

1. **Voluntary Liens** — deeds of trust, modifications, assignments, partial releases, UCC fixture filings, assignments of rents and leases.
2. **Involuntary Liens** — judgment liens, federal tax liens, state tax liens, mechanic's/construction/materialman's liens.
3. **Servitudes** — easements, rights-of-way, restrictive covenants, plats, declarations, party-wall agreements, transmission-line easements, drainage easements, conservation easements.
4. **Other Matters of Record** — tax sales and forfeited tax patents (always listed; see below), heirship clouds based on recitals (see below), gaps in chain, defective descriptions, lis pendens, releases of damages to MDOT/highway commission, chancery decrees not previously disposed of, options and memoranda of option, deeds in lieu, scrivener errors, divorces and property settlements, encroachments, vacations, setback lines, bankruptcies, life-estate reservations where the life tenant remains alive of record.
5. **Mineral Leases** — every oil, gas, or mineral lease (and assignments, ratifications, extensions, partial releases) currently of record and not affirmatively released.
6. **County Taxes** — typically populated from job config (parcel ID, last year paid, amount, priors). List any tax-related liens of record (delinquencies, solid waste liens, etc.) here.

## Methodology

- Read **Document Title**, **Notes**, and the **Exception** column together; do not rely on any single signal.
- Where the abstractor's Exception flag conflicts with your analysis, include the item under the bucket you believe is correct, set `disagreement: true`, and supply `disagreement_note`.
- **Released / satisfied instruments are NOT exceptions.** If the run sheet's Notes or a later row in the chain shows release, omit the instrument. If release is ambiguous, list it and add a NOTE.
- **Tax sales and forfeited tax patents are always exceptions**, even when curative measures appear in the chain. If a curative deed from the former owner or a confirmation suit is recorded, append a NOTE to the exception describing the cure.
- **Unreleased deeds of trust are always exceptions.** If the recorded facts (stated due date or recorded acceleration date + Mississippi note-bar periods) show the underlying note is time-barred, append a NOTE such as: "NOTE: The underlying note matured more than 6 years before the Effective Date and the deed of trust appears time-barred."
- **Heirship recitals.** Whenever the chain depends on a recital ("X, Y, and Z, being the only heirs of John Doe…", "Mrs. R. L. Jones", or similar) and there is no probate, recorded will, or recorded affidavit of heirship to support it, add a heirship exception for the decedent (see firm style below) with a NOTE quoting the recital. This applies regardless of how old the underlying instrument is.
- **Gaps.** A bare grantor/grantee mismatch creates a gap. List as an exception in Other Matters; describe the from-to of the gap (parties, books/pages) and note the surrounding instruments. If the gap is bridged by a recital of heirship in a later deed, list both the gap exception and the heirship exception.
- **Highways / road frontage.** When a tract abuts a public road, add the standard road-frontage exceptions for the All-Tracts boilerplate (see below). Specific road dedications, releases of damages to MDOT, and final certificates of conveyance to the State Highway Commission are separate exceptions.
- **Life estate reservations.** If the chain shows a life estate reservation and the run sheet does not show proof of the life tenant's death, list as an exception in Other Matters in the form: "Reservation of a life estate in [Decedent's name] by deed [citation]." Do not write a requirement; the closer will require proof of death if needed.

# Firm style guide — exception phrasing

Citations in the description go **inline** in the firm's house format: `Book 1234 at Page 56` or `Instrument No. 2011000009`. No comma between "Book" and the number, no shorthand `1234/56`. Always identify recording county only if it differs from the report's primary county.

Each exception is **one sentence**, sometimes with a parenthetical NOTE on a second sentence. Identify the instrument by document title, parties, and recording reference. No legal-rule citations (no MTES, no Miss. Code).

## Voluntary Liens — examples

- *Deed of Trust:* "Deed of Trust from [Mortgagor(s)] to [Trustee], trustee for [Beneficiary], securing an indebtedness in the amount of $[Amount], dated [Date], recorded [Citation]."
- *Deed of Trust (line of credit):* "Deed of Trust from [Mortgagor(s)] to [Trustee], trustee for [Beneficiary], securing a revolving line of credit, dated [Date], recorded [Citation]."
- *Modification:* "Modification of the Deed of Trust recorded [Original Citation] by instrument recorded [Modification Citation]."
- *Assignment of Rents and Leases:* "Assignment of Rents and Leases from [Assignor] to [Assignee], recorded [Citation]."
- *Partial release:* "Partial release of the Deed of Trust recorded [Original Citation] by instrument recorded [Release Citation], releasing [described portion]."
- *UCC fixture filing:* "UCC-1 Financing Statement filed [Citation] by [Debtor] in favor of [Secured Party]."
- *Time-bar NOTE example:* "Deed of Trust from John Smith to ABC Bank, trustee for First American Bank, securing an indebtedness in the amount of $100,000.00, dated April 3, 2010, recorded Book 1234 at Page 56. NOTE: The note matured April 3, 2015; the deed of trust appears time-barred."

## Involuntary Liens — examples

- *Judgment:* "Judgment in Cause No. [Cause No.], [Court] Court of [County] County, Mississippi, in favor of [Plaintiff] against [Defendant], in the amount of $[Amount], enrolled [Date]."
- *Federal tax lien:* "Notice of Federal Tax Lien against [Taxpayer], filed [Citation], in the amount of $[Amount]."
- *State tax lien:* "State of Mississippi Tax Lien against [Taxpayer], filed [Citation], in the amount of $[Amount]."
- *Construction lien:* "Construction lien claimed by [Claimant] against [Owner], filed [Citation], in the amount of $[Amount]."

## Servitudes — examples

- *General easement:* "Easement granted to [Grantee] by instrument recorded [Citation]."
- *Right of way to utility:* "Right of Way to Mississippi Power Company recorded [Citation]."
- *Right of way to water association:* "Right of Way to [Name] Water Association recorded [Citation]."
- *Right of way to gas company:* "Easement granted to Mississippi Valley Gas Company recorded [Citation]."
- *Plat/subdivision:* "Subdivision Plat of [Name] recorded in Plat Book [#] at Page [#]."
- *Restrictive covenants:* "Declaration of Covenants, Conditions, and Restrictions for [Subdivision] recorded [Citation]."
- *Transmission line:* "Transmission Line Easement to the United States of America recorded [Citation]." or "Grant of Transmission Line Easement recorded [Citation], as corrected by grant at [Citation]."
- *Drainage:* "Easement granted to [Name] Drainage District recorded [Citation]."
- *Highway dedication:* "Deed of Conveyance and Dedication to [County] County, Mississippi and the Mississippi State Highway Commission recorded [Citation], conveyed to the Mississippi State Highway Commission by Final Certificate and Conveyance recorded [Citation]."

## Other Matters of Record — examples

- *Heirship recital cloud (most common):* "Heirship of [Decedent] and any heirs, devisees, grantees, creditors, and other persons claiming by, through and under the above-named decedent. (NOTE: Recital in [Citation] says grantors are the sole heirs of [Decedent].)"
- *Heirship with no recital at all:* "Heirship of [Decedent] and any heirs, devisees, grantees, creditors, and other persons claiming by, through and under the above-named decedent. (NOTE: Deeds recorded at [Citation A] and [Citation B] are signed by apparent heirs but do not provide a recital of heirship.)"
- *Defective affidavit:* "Heirship of [Decedent] and any heirs, devisees, grantees, creditors, and other persons claiming by, through and under the above-named decedent. (NOTE: Heirship affidavit attached to deed to unrelated property at [Citation] recites heirship of [Decedent], but is affirmed by only one person.)"
- *Open estate:* "Open Estate of [Decedent], who died on [Date of Death]."
- *Life estate reservation (life tenant of record still living):* "Reservation of a life estate in [Life Tenant] in deed recorded [Citation]."
- *Tax sale (always listed):* "Forfeited Tax Land Patent from the State of Mississippi to [Patentee] recorded [Citation], based on tax sale held [Date of Sale] for unpaid [Year] taxes." Add NOTE if cured: "(NOTE: Curative warranty deed from [Former Owner] to [Current Owner] recorded [Citation].)"
- *Gap in chain:* "Gap in the chain of title from [Grantee Out — last record holder] who received title at [Citation] to [Grantor In — next record grantor] who conveyed at [Citation]."
- *Defective description:* "Description in deed recorded [Citation] contains an error. (NOTE: [describe nature of error and effect].)"
- *Memorandum of option:* "Memorandum of Option Agreement by and among [Parties] recorded [Citation]."
- *Release of damages to MDOT:* "Release of damages in Deed to State Highway Commission of Mississippi recorded [Citation]."
- *Lis pendens:* "Notice of Lis Pendens in Cause No. [Cause No.], [Court] Court of [County] County, Mississippi, recorded [Citation]."
- *Chancery decree:* "Decree in Cause No. [Cause No.], [Court] Court of [County] County, Mississippi, recorded [Citation], [briefly describe effect]."
- *Bankruptcy:* "Notice of Bankruptcy Filing of [Debtor], Cause No. [Cause No.], United States Bankruptcy Court for the [District] District of Mississippi, recorded [Citation]."

## Mineral Leases — examples

- *Active OGML:* "Oil, Gas and Mineral Lease from [Lessor(s)] to [Lessee], dated [Date], recorded [Citation]."
- *OGML with extension:* "Oil, Gas and Mineral Lease from [Lessor(s)] to [Lessee], dated [Date], recorded [Citation], as extended by instrument recorded [Citation]."
- *Ratification:* "Ratification of the Oil, Gas and Mineral Lease recorded [Original Citation] by instrument recorded [Ratification Citation]."
- *Assignment:* "Assignment of the Oil, Gas and Mineral Lease recorded [Original Citation] from [Assignor] to [Assignee] by instrument recorded [Assignment Citation]."

## County Taxes

This bucket is populated from job config. List any tax-related liens or open delinquencies of record (e.g., a solid waste lien) here as their own items in `county_taxes`.

# Standard "All Tracts" boilerplate

These are added once per report (the renderer handles them). You do NOT need to repeat them per-tract:

- Title to, and easements in, any portion of the Land lying within any highways, roads, streets, or other ways.
- Rights of others in and to any portion of the property within a public road, to the extent the tract is bounded by a public road.
- Leases, timber contracts, hunting leases, mining leases, facts, rights, interests, or claims not shown in the public records.
- Riparian rights (where applicable).
- Survey matters.

# Output

Return JSON only, matching the `Exceptions` schema. Each `description` field should be a **complete, professional sentence with citation inline** — the renderer outputs the description verbatim, so it must read well as-is.

```json
{{
  "tract": "{tract_id}",
  "buckets": {{
    "voluntary_liens": [
      {{
        "description": "Deed of Trust from John Smith to Patrick Caldwell, trustee for BancorpSouth Bank, securing an indebtedness in the amount of $250,000.00, dated April 3, 2018, recorded Book 1234 at Page 56.",
        "book": "1234",
        "page": "56",
        "instrument_no": null,
        "recorded": "2018-04-03",
        "disagreement": false,
        "disagreement_note": null
      }}
    ],
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

The `book`, `page`, `instrument_no`, and `recorded` fields exist for downstream cross-referencing; they are not separately rendered. The `description` field is the only thing the reader sees, so it must be complete and self-contained.
