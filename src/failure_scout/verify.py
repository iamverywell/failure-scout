"""verify node (docs/design-details.md, Pipeline steps, step 4): check event details after classification.

1. Field check (code only): are the extracted drug / sponsor actually in the source text?
2. Registry check: look the trial up on ClinicalTrials.gov and record its status,
   why-stopped text and stop date; flag a conflict when the news says the trial stopped
   but the registry still lists it as recruiting.
No LLM calls. Never raises: a failed lookup sets registry_match="error"."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

from .models import FailureEvent, NewsItem
from .sources import fetch_ctgov_study, find_ctgov_by_drug

log = logging.getLogger(__name__)

STOPPED = {"TERMINATED", "SUSPENDED", "WITHDRAWN"}
STILL_RUNNING = {"RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION"}
# Generic words that would make a sponsor "match" on almost any text.
SPONSOR_STOPWORDS = {"inc", "ltd", "llc", "corp", "corporation", "company", "pharma",
                     "pharmaceutical", "pharmaceuticals", "therapeutics", "biotech", "biosciences",
                     "bio", "medical", "university", "hospital", "center", "centre", "group",
                     "the", "and", "plc", "gmbh"}


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def unverified_fields(ev: FailureEvent, item: NewsItem) -> list[str]:
    """Fields whose value can't be found in the item's title + text."""
    text = f"{item.title} {item.snippet}"
    flat, words = _squash(text), set(_tokens(text))
    bad = []
    if ev.drug and _squash(ev.drug) not in flat:
        # Combination names ("toripalimab + chidamide"): accept if every part is present.
        parts = [p for p in re.split(r"\s*(?:\+|/|,| and | with | plus )\s*", ev.drug.lower()) if p.strip()]
        if not parts or not all(_squash(p) in flat for p in parts):
            bad.append("drug")
    if ev.sponsor and _squash(ev.sponsor) not in flat:
        # Accept a distinctive word of the sponsor name ("Roche" for "Hoffmann-La Roche").
        key = [w for w in _tokens(ev.sponsor) if len(w) >= 3 and w not in SPONSOR_STOPWORDS]
        if not key or not any(w in words for w in key):
            bad.append("sponsor")
    if ev.nct_id and ev.nct_id not in text and ev.nct_id != item.nct_id:
        bad.append("nct_id")
    return bad


def _claims_stopped(ev: FailureEvent) -> bool:
    return (ev.event_type in ("business_discontinuation", "clinical_hold")
            or ev.failure_type in ("futility", "safety"))


def _apply_registry(ev: FailureEvent, rec: NewsItem, match: str, n_candidates: int = 0) -> None:
    ev.registry_match = match
    ev.registry_nct = rec.nct_id
    ev.registry_status = rec.registry_status
    ev.registry_why_stopped = rec.why_stopped
    ev.registry_candidates = n_candidates
    if rec.registry_status in STOPPED:
        ev.registry_stop_date = rec.completion_date
    # Only a certain match can conflict: an ambiguous drug-name match is often another trial.
    ev.registry_conflict = (match in ("nct", "drug") and _claims_stopped(ev)
                            and rec.registry_status in STILL_RUNNING)


def check_registry(ev: FailureEvent, item: NewsItem) -> None:
    if item.origin == "ctgov":
        _apply_registry(ev, item, "source")
        return
    if ev.event_type == "regulatory_rejection" or not (ev.nct_id or ev.drug):
        ev.registry_match = "skipped"  # a CRL has no registry record of its own
        return
    try:
        if ev.nct_id:
            rec = fetch_ctgov_study(ev.nct_id)
            if rec:
                _apply_registry(ev, rec, "nct")
                return
        if ev.drug:
            found = find_ctgov_by_drug(ev.drug, ev.sponsor)
            if found:
                stopped = [r for r in found if r.registry_status in STOPPED]
                best = stopped[0] if stopped else found[0]
                _apply_registry(ev, best, "drug" if len(found) == 1 else "drug_ambiguous", len(found))
                return
        ev.registry_match = "not_found"
    except Exception as e:  # never raise
        log.warning("registry lookup failed for %s: %s", ev.event_key, e)
        ev.registry_match = "error"


def verify_one(ev: FailureEvent, item: NewsItem) -> FailureEvent:
    ev.unverified_fields = unverified_fields(ev, item)
    check_registry(ev, item)
    return ev


def verify_all(pairs: list[tuple[NewsItem, FailureEvent]], workers: int = 4) -> None:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda p: verify_one(p[1], p[0]), pairs))
