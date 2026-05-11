"""Validate that mineral ownership fractions sum to exactly 1 at the Effective Date.

The analyzer asks Claude for the final per-owner fractional table; we sum it
here in pure Python and report any imbalance so the analyzer can feed it back
to Claude for a re-ask.
"""
from __future__ import annotations

from fractions import Fraction

from .models import MineralChain, OwnerShare


def parse_fraction(s: str | None) -> Fraction | None:
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        return Fraction(s)
    except (ValueError, ZeroDivisionError):
        # Tolerate "1/2 of 1/2" style — but only the simple case.
        return None


def reconcile(chain: MineralChain) -> dict:
    """Return a reconciliation dict suitable for chain.reconciliation.

    Shape: {"total": "5/4", "ok": false, "imbalance": "1/4", "per_owner": [...]}
    """
    total = Fraction(0)
    per_owner: list[dict] = []
    for owner in chain.current_mineral_owners:
        f = parse_fraction(owner.share)
        per_owner.append({"name": owner.name, "share": owner.share, "parsed": f is not None})
        if f is None:
            return {
                "total": str(total),
                "ok": False,
                "imbalance": "unparseable",
                "bad_owner": owner.name,
                "bad_share": owner.share,
                "per_owner": per_owner,
            }
        total += f
    ok = total == Fraction(1)
    out = {
        "total": str(total),
        "ok": ok,
        "per_owner": per_owner,
    }
    if not ok:
        out["imbalance"] = str(total - Fraction(1))
    return out
