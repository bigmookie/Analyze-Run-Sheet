# Mineral chain — tract `{tract_id}`

Construct the **mineral chain of title** for tract `{tract_id}` **from sovereignty (the original land patent) through the Effective Date `{effective_date}`**.

## Method

1. Begin at the patent. The patentee holds 100% of the mineral estate.
2. Use the surface chain you just produced as the spine; each surface conveyance carries the minerals it had not previously severed.
3. For every row whose Mineral Transfer column is `x`, OR whose Mineral Reservations cell is non-empty, OR which otherwise affects minerals (oil/gas/mineral deeds, leases, ratifications, assignments, mineral lease cancellations), record an entry.
4. Track every reservation and conveyance with **exact fractions** (strings like `"1/2"`, `"1/8"`, `"5/8"`). Do not use decimals.
5. Reconcile the total mineral interest to **exactly 1** at the Effective Date. The reconciler in the calling code will verify this; if your fractions do not sum to 1, the system will return your output to you with the imbalance shown.
6. List every current cotenant of the mineral estate with their fractional share and the instrument that gave them that share.

## Mississippi-specific rules

- Tax sale of the surface does **not** convey separately-assessed mineral interests (Miss. Code §§ 27-31-73, 27-35-51). Such a tax sale leaves the mineral chain intact.
- A general warranty deed of the surface conveys minerals not previously severed; quitclaim and special warranty deeds also pass minerals not previously severed by the grantor but do not carry after-acquired title.
- An oil-and-gas mineral lease is **not** a conveyance of the mineral interest — it is an encumbrance. Leases belong in Exceptions, not in the mineral chain (unless an assignment of the leasehold materially affects ownership of the mineral fee, in which case explain).
- A reservation of "minerals" without further specification in Mississippi is a reservation of all oil, gas, and other minerals — including non-fugacious minerals — unless context indicates otherwise.

## Output

Return JSON only, matching the `MineralChain` schema:

```json
{{
  "tract": "{tract_id}",
  "chain": [ {{ ChainEntry... }} ],
  "reservations": [ {{ "book": "...", "page": "...", "fraction_reserved": "1/2", "reserver": "...", "notes": "..." }} ],
  "current_mineral_owners": [ {{ "name": "...", "share": "1/4", "source_book_page": "..." }} ],
  "reconciliation": {{ "total": "1", "ok": true }},
  "confidence": "high|medium|low",
  "needs_source": [ "<book>-<page>", ... ],
  "attorney_review": [ "...", ... ]
}}
```

Set `reconciliation.ok` to `true` only if your `current_mineral_owners` shares sum to exactly 1. If they do not, set `ok: false` and leave the imbalance for the system to compute; emit an ATTORNEY REVIEW callout explaining where the gap is.
