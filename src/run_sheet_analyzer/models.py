"""Pydantic models for the JSON the analyzer requests from Claude.

Fractions are exchanged as strings ("1/2", "5/8", "1"). The reconciler is the
single place that parses them to fractions.Fraction.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]


class OwnerShare(BaseModel):
    name: str
    share: str                               # Fraction as string, e.g. "1/2"
    note: str | None = None                  # life estate, etc.
    source_book_page: str | None = None      # how we know


class ChainEntry(BaseModel):
    seq: int
    book: str
    page: str
    instrument_no: str | None = None
    recorded: str | None = None              # ISO date string
    doc_title: str
    grantors: list[str]
    grantees: list[str]
    summary: str = ""                        # concise attorney-style summary
    interest_after: str | None = None        # total interest held by the chain after this event
    cotenants_after: list[OwnerShare] = Field(default_factory=list)
    authorities: list[str] = Field(default_factory=list)   # MTES § X, Miss. Code § Y
    notes: str = ""
    disagreement_with_abstractor: bool = False


class SurfaceChain(BaseModel):
    tract: str
    chain: list[ChainEntry] = Field(default_factory=list)
    current_vesting: list[OwnerShare] = Field(default_factory=list)
    legal_description: str = ""              # synthesized from base + L&E
    less_and_except: list[dict] = Field(default_factory=list)   # {description, book, page, recorded}
    confidence: Confidence = "medium"
    needs_source: list[str] = Field(default_factory=list)
    attorney_review: list[str] = Field(default_factory=list)


class MineralReservation(BaseModel):
    book: str
    page: str
    fraction_reserved: str
    reserver: str
    notes: str = ""


class MineralChain(BaseModel):
    tract: str
    chain: list[ChainEntry] = Field(default_factory=list)
    reservations: list[MineralReservation] = Field(default_factory=list)
    current_mineral_owners: list[OwnerShare] = Field(default_factory=list)
    reconciliation: dict = Field(default_factory=lambda: {"total": "1", "ok": True})
    confidence: Confidence = "medium"
    needs_source: list[str] = Field(default_factory=list)
    attorney_review: list[str] = Field(default_factory=list)


class ExceptionItem(BaseModel):
    description: str                         # concise attorney-style description
    book: str
    page: str
    instrument_no: str | None = None
    recorded: str | None = None
    disagreement: bool = False
    disagreement_note: str | None = None
    authorities: list[str] = Field(default_factory=list)


class ExceptionBuckets(BaseModel):
    voluntary_liens: list[ExceptionItem] = Field(default_factory=list)
    involuntary_liens: list[ExceptionItem] = Field(default_factory=list)
    servitudes: list[ExceptionItem] = Field(default_factory=list)
    other_matters: list[ExceptionItem] = Field(default_factory=list)
    mineral_leases: list[ExceptionItem] = Field(default_factory=list)
    county_taxes: list[ExceptionItem] = Field(default_factory=list)


class Exceptions(BaseModel):
    tract: str
    buckets: ExceptionBuckets = Field(default_factory=ExceptionBuckets)
    confidence: Confidence = "medium"
    needs_source: list[str] = Field(default_factory=list)
    attorney_review: list[str] = Field(default_factory=list)


class TractAnalysis(BaseModel):
    """Combined per-tract output, also the shape of out/<tract>.json."""
    model_config = ConfigDict(protected_namespaces=())

    tract: str
    surface: SurfaceChain
    mineral: MineralChain
    exceptions: Exceptions
    input_hash: str = ""
    model_used: dict = Field(default_factory=dict)
    generated_at: str = ""
