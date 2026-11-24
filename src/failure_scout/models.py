"""Data structures shared by every step (docs/design-details.md, FailureEvent)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

EventType = Literal[
    "trial_failure",  # missed primary endpoint, or stopped for futility / safety
    "clinical_hold",  # FDA halted all or part of a trial
    "regulatory_rejection",  # complete response letter (CRL)
    "business_discontinuation",  # dropped for business / strategic reasons
    "not_an_event",  # positive result, opinion piece, unrelated, ...
]
FailureType = Literal["missed_primary_endpoint", "futility", "safety"]
CrlReason = Literal["efficacy", "safety", "cmc", "labeling", "other"]
Origin = Literal["ctgov", "gnews", "rss", "fixture"]
RegistryMatch = Literal[
    "pending",
    "source",  # the item itself is a registry record
    "nct",  # looked up by NCT id
    "drug",  # drug-name search, one candidate
    "drug_ambiguous",  # drug-name search, several candidates; the most recent stopped one is shown
    "not_found",
    "skipped",  # CRLs and events without a drug name: no registry record to check
    "error",
]


class NewsItem(BaseModel):
    """One raw search result, before classification."""

    url: str
    title: str
    source: str  # publisher name, or "ClinicalTrials.gov"
    published: str | None  # YYYY-MM-DD
    snippet: str  # text the classifier sees besides the title
    origin: Origin
    nct_id: str | None = None
    publisher_url: str | None = None  # publisher homepage (Google News gives no direct article URL)
    # Registry fields, filled for ClinicalTrials.gov records only
    registry_status: str | None = None
    why_stopped: str | None = None
    completion_date: str | None = None  # actual stop date for TERMINATED trials


class Classification(BaseModel):
    """What the model extracts from one NewsItem (structured output)."""

    index: int  # position of the item inside the batch
    event_type: EventType
    failure_type: FailureType | None  # only for trial_failure
    crl_reason: CrlReason | None  # only for regulatory_rejection
    partial: bool  # one arm only, partial hold, primary missed but secondary met
    drug: str | None
    sponsor: str | None
    indication: str | None
    phase: str | None
    nct_id: str | None
    event_date: str | None  # YYYY-MM-DD if stated
    evidence_quote: str  # verbatim sentence from the title/snippet
    confidence: int  # 0-100


class ClassificationBatch(BaseModel):
    results: list[Classification]


class FailureEvent(BaseModel):
    """A classified event as stored in the `events` table."""

    event_key: str
    event_type: EventType
    failure_type: FailureType | None
    crl_reason: CrlReason | None
    partial: bool
    drug: str | None
    sponsor: str | None
    indication: str | None
    phase: str | None
    nct_id: str | None
    event_date: str | None
    evidence_quote: str
    quote_verified: bool  # code-checked: quote really appears in the source text
    confidence: int
    source_url: str
    publisher_url: str | None = None
    # Detail verification (verify.py)
    unverified_fields: list[str] = []  # extracted fields not found in the source text
    registry_match: RegistryMatch = "pending"
    registry_nct: str | None = None
    registry_status: str | None = None
    registry_why_stopped: str | None = None
    registry_stop_date: str | None = None
    registry_candidates: int = 0  # trials found by drug-name search
    registry_conflict: bool = False  # news says stopped, registry says still recruiting
