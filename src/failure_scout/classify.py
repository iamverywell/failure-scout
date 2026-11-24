"""classify_extract node: batches of 5 items -> structured output (docs/design-details.md, Pipeline steps, step 3; docs/design-details.md, FailureEvent)."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor

import anthropic

from .db import make_event_key
from .models import ClassificationBatch, FailureEvent, NewsItem
from .usage import UsageLog

log = logging.getLogger(__name__)

BATCH_SIZE = 5

SYSTEM = """You label news items and trial-registry records about drug development.

For each item, decide which event it reports:
- trial_failure: a trial missed its primary endpoint, or was stopped for futility, lack of efficacy, or safety. Set failure_type to missed_primary_endpoint, futility, or safety.
- clinical_hold: a regulator (usually FDA) halted all or part of a trial.
- regulatory_rejection: a complete response letter (CRL) or other refusal to approve. Set crl_reason: efficacy (evidence of benefit judged insufficient), safety, cmc (manufacturing / quality), labeling, or other.
- business_discontinuation: a program or trial dropped for business, strategic, funding, or enrollment reasons, not because the data failed.
- not_an_event: positive results, approvals, opinion or market pieces, trials stopped early because they succeeded, or anything else.

Scope is drugs and biologics only (small molecules, antibodies, vaccines, cell and gene therapies). Medical devices, diagnostics, surgical procedures and behavioural interventions are out of scope: label them not_an_event even if the trial failed.

Registry records (source ClinicalTrials.gov) carry a "Why stopped" text: low enrollment, funding, or sponsor decisions are business_discontinuation; lack of efficacy or futility is trial_failure; toxicity or deaths is trial_failure with failure_type safety. If "Why stopped" is empty or vague, use not_an_event with low confidence.

Set partial=true when only one arm, cohort, or part of a trial is affected, a hold is partial, or the primary endpoint failed while a secondary succeeded.

evidence_quote must be copied verbatim from the item's title or text: the shortest span that supports your label. Never paraphrase it.

drug must be a specific name or code (e.g. "reproxalap", "BMB-101", "PGN-EDODM1"). Descriptions such as "partnered drug", "oral immunology drug", "STAT6 degrader" or "ICI" are not names: use null.

Use null for fields the item does not state. Dates as YYYY-MM-DD. confidence is 0-100: how sure you are of event_type given only this text (headlines alone rarely justify above 80).

Return exactly one result per item, with index matching the item's [index]."""


def _render(batch: list[NewsItem]) -> str:
    parts = []
    for i, it in enumerate(batch):
        parts.append(
            f"[{i}] source: {it.source} | published: {it.published or 'unknown'}\n"
            f"title: {it.title}\n"
            f"text: {it.snippet or '(none)'}"
        )
    return "\n\n".join(parts)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_is_verbatim(quote: str, item: NewsItem) -> bool:
    """Anti-fabrication check done in code, not by the model."""
    q = _squash(quote)
    return bool(q) and q in _squash(f"{item.title} {item.snippet}")


def classify_batch(client: anthropic.Anthropic, model: str, batch: list[NewsItem],
                   usage: UsageLog) -> list[tuple[NewsItem, FailureEvent | None]]:
    """Returns (item, event-or-None) for every item in the batch; None = not an event."""
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=8000,
            system=SYSTEM,
            messages=[{"role": "user", "content": _render(batch)}],
            output_format=ClassificationBatch,
        )
    except anthropic.APIError as e:
        usage.record("classify", model, error=e)
        log.warning("classify batch failed: %s", e)
        return []  # items stay unseen and are retried next run
    usage.record("classify", model, resp=resp)
    if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
        log.warning("classify batch stopped: %s", resp.stop_reason)
        return []

    by_index = {r.index: r for r in resp.parsed_output.results}
    out: list[tuple[NewsItem, FailureEvent | None]] = []
    for i, item in enumerate(batch):
        r = by_index.get(i)
        if r is None:
            continue  # model skipped it; leave unseen so the next run retries
        if r.event_type == "not_an_event":
            out.append((item, None))
            continue
        nct = r.nct_id or item.nct_id
        verified = quote_is_verbatim(r.evidence_quote, item)
        out.append((item, FailureEvent(
            event_key=make_event_key(nct, r.drug, r.sponsor, r.event_type, item.url),
            event_type=r.event_type,
            failure_type=r.failure_type if r.event_type == "trial_failure" else None,
            crl_reason=r.crl_reason if r.event_type == "regulatory_rejection" else None,
            partial=r.partial,
            drug=r.drug, sponsor=r.sponsor, indication=r.indication, phase=r.phase,
            nct_id=nct,
            # Registry records: the actual stop date, not the record's last-update date.
            event_date=(item.completion_date if item.origin == "ctgov" and item.completion_date
                        else r.event_date or item.published),
            publisher_url=item.publisher_url,
            evidence_quote=r.evidence_quote,
            quote_verified=verified,
            # An unverifiable quote caps confidence: we can't show where the label came from.
            confidence=max(0, min(100, r.confidence)) if verified else min(r.confidence, 40),
            source_url=item.url,
        )))
    return out


def classify_all(client: anthropic.Anthropic, model: str, items: list[NewsItem],
                 usage: UsageLog, workers: int = 4) -> list[tuple[NewsItem, FailureEvent | None]]:
    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    results: list[tuple[NewsItem, FailureEvent | None]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(lambda b: classify_batch(client, model, b, usage), batches):
            results.extend(part)
    return results
