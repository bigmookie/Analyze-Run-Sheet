# Mineral chain — tract `{tract_id}`

Construct the **mineral chain of title** for tract `{tract_id}` from a valid root of title at least 100 years old through the Effective Date `{effective_date}`.

## Method

1. Begin at the patent (or other valid root of title at least 100 years old). The root holder owns 100% of the mineral estate.
2. Use the surface chain you just produced as the spine. Every conveyance of the surface carries the minerals **not previously severed** unless the deed itself reserves or excepts minerals.
3. For every row affecting minerals — Mineral Transfer column is `x`, OR Mineral Reservations cell is non-empty, OR the document title indicates a mineral-affecting instrument — record an entry.
4. Track every reservation and conveyance with **exact fractions** (strings like `"1/2"`, `"1/8"`, `"5/8"`). Do not use decimals.
5. Reconcile the total mineral fee interest to **exactly 1** at the Effective Date. The reconciler in the calling code will verify this; if your fractions do not sum to 1, the system will return your output to you with the imbalance shown.
6. List every current cotenant of the mineral estate with their fractional share and the instrument that gave them that share.

## Mineral transfers — the two main forms

Mineral interests are created or moved in two main forms; recognize and handle each:

**A. Reservation in a conveyance of the surface estate.** The grantor of a warranty/quitclaim/special warranty deed retains some interest in minerals while conveying the surface. Examples:
  - *"Grantor reserves an undivided one-half (1/2) of all oil, gas and other minerals."* — surface conveyed; grantor keeps 1/2 of the mineral fee.
  - *"Grantor reserves all oil, gas and other minerals."* — surface only; full mineral fee retained.
  - *"Grantor reserves a non-participating royalty interest of 1/16 of all production."* — surface AND mineral fee conveyed, but a royalty burden is created (see Royalty below).
  - *"Grantor reserves the executive right to lease minerals."* — surface and mineral fee conveyed; grantor retains the right to execute oil and gas leases on the minerals.

**B. Conveyance of minerals (or a sub-interest) only.** A separate mineral deed, royalty deed, mineral right and royalty transfer, or assignment that moves a mineral interest without touching the surface. Examples:
  - Mineral Deed transferring 1/4 of the mineral fee from A to B.
  - Royalty Deed transferring a 1/32 non-participating royalty.
  - Assignment of an oil and gas lease (transfers the leasehold/working interest, NOT the mineral fee).

## Types of mineral interests to recognize

| Interest | Nature | Notation in the chain |
|---|---|---|
| **Mineral fee** | Corporeal estate in the minerals in place. Carries (unless severed): right to develop, right to lease (executive right), right to bonus, royalty, and surface use reasonably necessary for extraction. | Track as a fraction of the mineral fee. |
| **Non-participating royalty interest (NPRI)** | Carved out of the mineral fee; receives a stated share of production free of cost; **no** executive right, **no** bonus, **no** surface use, **no** delay rentals. | Track separately; does NOT count against the mineral fee fraction. List under "reservations" with a `notes` field flagging it as NPRI. |
| **Overriding royalty interest (ORRI)** | Carved out of the **lessee's working interest** under an existing oil and gas lease; expires with the lease. Created by assignment, not by reservation in a fee deed. | Encumbers a specific lease — note in the chain entry for the assignment, not as a fee event. |
| **Working interest / leasehold** | The lessee's interest under an OGML — the right to drill and produce, bearing costs in proportion. | Not a mineral fee event. Appears in Exceptions, not in the mineral chain (unless an assignment materially affects mineral fee ownership). |
| **Term mineral interest** | A mineral fee interest "for a term of X years and so long thereafter as oil, gas, or other minerals are produced." Reverts to the grantor (or successors) when production ceases after the primary term. | Track as a fee fraction; add a `notes` field flagging the term and any known production status. |
| **Executive right** | The right to execute oil and gas leases. May be severed from the mineral fee. | Where severed, note separately under `notes`; current_mineral_owners may need to identify both the fee owner and the executive-right holder. |
| **Specific-substance interest** | A reservation or conveyance limited to specific minerals (e.g., "all coal, lignite, and iron ore" but not oil and gas). | Track only the substances actually severed; explain in `notes` whether the remaining mineral fee passed with the surface. |

## Mississippi-specific rules

- A reservation of "minerals" — without further specification — is in Mississippi a reservation of all oil, gas and other minerals, including non-fugacious minerals, unless context indicates otherwise.
- A general warranty deed of the surface conveys the minerals not previously severed. Quitclaim and special warranty deeds also pass minerals not previously severed **by the grantor** but do not carry after-acquired title.
- Tax sale of the surface does **not** convey separately-assessed mineral interests; the mineral chain is unaffected.
- An oil and gas mineral lease (OGML) is **not** a conveyance of the mineral fee — it is an encumbrance. Leases go in Exceptions (Mineral Leases bucket), not the mineral chain. Note ratifications, extensions, and lease cancellations alongside the lease entry there.
- Every fractional mineral owner is a tenant-in-common; any one cotenant may execute an OGML for the whole interest without the others' consent. This means small fractional owners are still material to surface-use waivers.
- When the surface owner also owns 100% of the unsevered mineral fee, note "as surface owner" in their cotenant entry for clarity (firm house style — see sample reports).

## Output

Return JSON only, matching the `MineralChain` schema:

```json
{{
  "tract": "{tract_id}",
  "chain": [ {{ ChainEntry... }} ],
  "reservations": [ {{ "book": "...", "page": "...", "fraction_reserved": "1/2", "reserver": "...", "notes": "..." }} ],
  "current_mineral_owners": [ {{ "name": "...", "share": "1/4", "source_book_page": "...", "note": "as surface owner" }} ],
  "reconciliation": {{ "total": "1", "ok": true }},
  "confidence": "high|medium|low",
  "needs_source": [ "<book>-<page>", ... ],
  "attorney_review": [ "...", ... ]
}}
```

`current_mineral_owners` should list every fractional cotenant of the mineral fee at the Effective Date. NPRIs and other carved interests go in `reservations` (with explanatory `notes`), not in `current_mineral_owners`. Working interests under active OGMLs are not listed here — they are exceptions.

Set `reconciliation.ok` to `true` only if your `current_mineral_owners` shares (mineral fee only) sum to exactly 1. If they do not, set `ok: false`, leave the imbalance for the system to compute, and emit a specific `attorney_review` callout naming the missing or duplicated fraction.
