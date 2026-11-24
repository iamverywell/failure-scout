"""merge node: fold several reports of the same real-world event into one event.

1. Rule: same drug and same company (normalised), dates within RULE_DAYS -> same event.
2. LLM: events are cut into overlapping date windows; Haiku groups the reports in each
   window that describe the same occurrence. Registry records (ClinicalTrials.gov) join a
   window when their company appears in its news.
Groups from all windows are combined with union-find. Two events with different NCT ids are
never merged (different trials). Duplicates keep their row with `merged_into` set."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import anthropic
from pydantic import BaseModel

from .companies import company_key, drug_key
from .db import Store
from .models import CrlReason, EventType, FailureType
from .usage import UsageLog

log = logging.getLogger(__name__)

RULE_DAYS = 30
WINDOW_DAYS = 14
WINDOW_STEP = 7
REGISTRY_DAYS = 60  # registry stop dates can trail the news by weeks
MAX_PER_CALL = 60
NEWS_ORIGINS = {"gnews", "rss", "fixture"}


class Cluster(BaseModel):
    members: list[int]
    event_type: EventType
    failure_type: FailureType | None
    crl_reason: CrlReason | None


class ClusterResult(BaseModel):
    clusters: list[Cluster]


SYSTEM = """You deduplicate a database of drug-development events. Each line is one stored event, built from one news report or one trial-registry record.

Group lines that describe the same real-world occurrence: the same drug or program and the same announcement, e.g. one trial readout, one complete response letter, one clinical hold, one decision to drop a program. Coverage of that occurrence by different outlets, or follow-up stories within days, belongs in the same group even when one report names the drug and another only the company, or when the reports were labelled with different event types.

Keep separate: different trials of the same drug (different NCT ids), different drugs of the same company, and separate occurrences such as a clinical hold and its later lifting, or a first and a second CRL. A registry line joins a news group only if it is the registry record of the very trial the news is about.

Return only groups with two or more lines. For each group give the event_type, failure_type (trial_failure only) and crl_reason (regulatory_rejection only) that best describe the occurrence, judging from all its lines. If nothing should be merged, return an empty list."""


class _UnionFind:
    def __init__(self, ncts: dict[str, str | None]):
        self.parent = {k: k for k in ncts}
        self.ncts = {k: ({v} if v else set()) for k, v in ncts.items()}

    def find(self, k: str) -> str:
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if self.ncts[ra] and self.ncts[rb] and self.ncts[ra] != self.ncts[rb]:
            return False  # two different trials
        self.parent[rb] = ra
        self.ncts[ra] |= self.ncts[rb]
        return True


def _d(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s[:10]) if s and len(s) >= 10 else None
    except ValueError:
        return None


def _line(i: int, r) -> str:
    kind = r["event_type"] + (f"/{r['failure_type']}" if r["failure_type"] else "") + \
        (f"/{r['crl_reason']}" if r["crl_reason"] else "")
    src = "registry" if r["origin"] == "ctgov" else "news"
    return (f"[{i}] {r['event_date'] or '?'} | {kind} | drug: {r['drug'] or '-'} | "
            f"company: {r['sponsor'] or '-'} | {r['nct_id'] or 'no NCT'} | {src}: "
            f"{(r['headline'] or '')[:140]} | quote: {(r['evidence_quote'] or '')[:160]}")


def _ask(client, model, rows, usage) -> list[Cluster]:
    try:
        resp = client.messages.parse(
            model=model, max_tokens=8000, system=SYSTEM,
            messages=[{"role": "user", "content": "\n".join(_line(i, r) for i, r in enumerate(rows))}],
            output_format=ClusterResult,
        )
    except anthropic.APIError as e:
        usage.record("merge", model, error=e)
        log.warning("merge call failed: %s", e)
        return []
    usage.record("merge", model, resp=resp)
    if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
        return []
    return [c for c in resp.parsed_output.clusters
            if len(set(c.members)) >= 2 and all(0 <= m < len(rows) for m in c.members)]


def merge_events(store: Store, client: anthropic.Anthropic, model: str, usage: UsageLog,
                 since: date | None = None, until: date | None = None) -> tuple[int, int]:
    """Returns (events merged into another, groups formed)."""
    pad = timedelta(days=WINDOW_DAYS + REGISTRY_DAYS)
    rows = store.merge_candidates((since - pad).isoformat() if since else None,
                                  (until + pad).isoformat() if until else None)
    by_key = {r["event_key"]: r for r in rows}
    uf = _UnionFind({r["event_key"]: r["nct_id"] for r in rows})
    decisions: list[tuple[set[str], Cluster]] = []

    # 1. Rule merges: same drug + same company, close dates.
    seen: dict[tuple[str, str], list] = {}
    for r in rows:
        dk, ck = drug_key(r["drug"]), r["company_key"] or company_key(r["sponsor"])
        if not (dk and ck):
            continue
        for other in seen.get((dk, ck), []):
            d1, d2 = _d(r["event_date"]), _d(other["event_date"])
            if d1 and d2 and abs((d1 - d2).days) <= RULE_DAYS:
                uf.union(other["event_key"], r["event_key"])
        seen.setdefault((dk, ck), []).append(r)

    # 2. LLM merges over overlapping windows of news events.
    news = [r for r in rows if r["origin"] in NEWS_ORIGINS and _d(r["event_date"])]
    registry = [r for r in rows if r["origin"] == "ctgov" and _d(r["event_date"])]
    if news:
        start, last = _d(news[0]["event_date"]), _d(news[-1]["event_date"])
        while start <= last:
            end = start + timedelta(days=WINDOW_DAYS - 1)
            win = [r for r in news if start <= _d(r["event_date"]) <= end]
            companies = {r["company_key"] for r in win if r["company_key"]}
            win += [r for r in registry if r["company_key"] in companies
                    and start - timedelta(days=REGISTRY_DAYS) <= _d(r["event_date"])
                    <= end + timedelta(days=REGISTRY_DAYS)]
            for i in range(0, len(win), MAX_PER_CALL):
                chunk = win[i:i + MAX_PER_CALL]
                if len(chunk) < 2:
                    continue
                for c in _ask(client, model, chunk, usage):
                    keys = [chunk[m]["event_key"] for m in sorted(set(c.members))]
                    for k in keys[1:]:
                        uf.union(keys[0], k)
                    decisions.append((set(keys), c))
            start += timedelta(days=WINDOW_STEP)

    # 3. Apply each group of two or more.
    groups: dict[str, list[str]] = {}
    for k in by_key:
        groups.setdefault(uf.find(k), []).append(k)
    n_merged = n_groups = 0
    for keys in groups.values():
        if len(keys) < 2:
            continue
        members = [by_key[k] for k in keys]
        # Canonical: the registry record if there is one (stable NCT key), else the most
        # confident report, preferring one that names the drug.
        canon = max(members, key=lambda r: (r["origin"] == "ctgov", r["drug"] is not None,
                                             r["confidence"] or 0))
        # Event type from the LLM group that overlaps this component most.
        best = max((d for d in decisions if d[0] & set(keys)),
                   key=lambda d: len(d[0] & set(keys)), default=None)
        overrides = {}
        if best:
            c = best[1]
            overrides = {"event_type": c.event_type,
                         "failure_type": c.failure_type if c.event_type == "trial_failure" else None,
                         "crl_reason": c.crl_reason if c.event_type == "regulatory_rejection" else None}
        store.merge_events(canon["event_key"], [k for k in keys if k != canon["event_key"]], overrides)
        n_merged += len(keys) - 1
        n_groups += 1
    store.commit()
    return n_merged, n_groups
