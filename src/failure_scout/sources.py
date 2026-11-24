"""Data sources (docs/design-details.md, Data sources). Every search function never raises:
on any error it logs and returns [], so one broken source can't kill a run."""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from .models import NewsItem

log = logging.getLogger(__name__)

USER_AGENT = "failure-scout/0.1 (personal research tool)"
TIMEOUT_S = 30
MAX_ITEMS_PER_SEARCH = 50

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "examples.json"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return resp.read()


# ---------------------------------------------------------------- ClinicalTrials.gov

CTGOV_URL = "https://clinicaltrials.gov/api/v2/studies"
CTGOV_FIELDS = ",".join(
    [
        "NCTId",
        "BriefTitle",
        "OverallStatus",
        "WhyStopped",
        "CompletionDate",
        "LastUpdatePostDate",
        "LeadSponsorName",
        "Phase",
        "Condition",
        "InterventionName",
    ]
)


def build_ctgov_url(query: str, since: date, until: date, page_size: int) -> str:
    # The date filter is on LastUpdatePostDate: the record update that sets
    # TERMINATED usually lands near the termination, but not always.
    # Drug / biologic trials only: device-only, procedure, behavioural trials are out of scope.
    advanced = (f"AREA[InterventionType](DRUG OR BIOLOGICAL) AND "
                f"AREA[LastUpdatePostDate]RANGE[{since.isoformat()},{until.isoformat()}]")
    if query.strip():
        advanced = f"AREA[WhyStopped]({query}) AND {advanced}"
    params = {
        "filter.overallStatus": "TERMINATED,SUSPENDED,WITHDRAWN",
        "filter.advanced": advanced,
        "fields": CTGOV_FIELDS,
        "pageSize": str(page_size),
        "sort": "LastUpdatePostDate:desc",
    }
    return CTGOV_URL + "?" + urllib.parse.urlencode(params)


def parse_ctgov(payload: dict) -> list[NewsItem]:
    return [it for it in (study_to_item(s) for s in payload.get("studies", [])) if it]


def study_to_item(study: dict) -> NewsItem | None:
    p = study.get("protocolSection", {})
    ident = p.get("identificationModule", {})
    status = p.get("statusModule", {})
    nct = ident.get("nctId")
    if not nct:
        return None
    sponsor = p.get("sponsorCollaboratorsModule", {}).get("leadSponsor", {}).get("name")
    phases = p.get("designModule", {}).get("phases", [])
    conditions = p.get("conditionsModule", {}).get("conditions", [])
    drugs = [i.get("name") for i in p.get("armsInterventionsModule", {}).get("interventions", [])]
    snippet = "; ".join(
        [
            f"Status: {status.get('overallStatus')}",
            f"Why stopped: {status.get('whyStopped') or 'not given'}",
            f"Sponsor: {sponsor}",
            f"Phase: {', '.join(phases) or 'n/a'}",
            f"Conditions: {', '.join(conditions[:5])}",
            f"Interventions: {', '.join(d for d in drugs[:5] if d)}",
        ]
    )
    updated = status.get("lastUpdatePostDateStruct", {}).get("date")
    completion = status.get("completionDateStruct", {}).get("date")
    # The API also returns anticipated dates (e.g. 2030 for a withdrawn trial). An actual stop
    # date can't be later than the update that reported the stop; withdrawn trials never ran.
    # Dates may be "YYYY-MM": compare on the shared prefix.
    if completion and (status.get("overallStatus") == "WITHDRAWN"
                       or (updated and completion[:len(updated)] > updated[:len(completion)])):
        completion = None
    return NewsItem(
        url=f"https://clinicaltrials.gov/study/{nct}",
        title=ident.get("briefTitle") or nct,
        source="ClinicalTrials.gov",
        published=updated,
        snippet=snippet,
        origin="ctgov",
        nct_id=nct,
        registry_status=status.get("overallStatus"),
        why_stopped=status.get("whyStopped"),
        completion_date=completion,
    )


def fetch_ctgov_study(nct_id: str) -> NewsItem | None:
    """One registry record by NCT id. Raises on network errors (caller handles)."""
    url = f"{CTGOV_URL}/{urllib.parse.quote(nct_id)}?" + urllib.parse.urlencode({"fields": CTGOV_FIELDS})
    return study_to_item(json.loads(_get(url)))


def find_ctgov_by_drug(drug: str, sponsor: str | None, limit: int = 10) -> list[NewsItem]:
    """Registry records whose interventions match `drug` (and sponsor, if given), newest first.
    Raises on network errors (caller handles)."""
    params = {"query.intr": drug, "fields": CTGOV_FIELDS, "pageSize": str(limit),
              "sort": "LastUpdatePostDate:desc"}
    if sponsor:
        params["query.spons"] = sponsor
    found = parse_ctgov(json.loads(_get(CTGOV_URL + "?" + urllib.parse.urlencode(params))))
    if not found and sponsor:  # sponsor names vary a lot ("Roche" vs "Hoffmann-La Roche")
        return find_ctgov_by_drug(drug, None, limit)
    return found


CTGOV_PAGE_SIZE = 100
CTGOV_MAX_ITEMS = 500  # all pages up to this cap (March 2026: ~150 stopped drug trials)


def search_ctgov(query: str, since: date, until: date) -> list[NewsItem]:
    """All stopped drug/biologic trials updated in the window, following pages.
    `query`, if given, is matched against the free-text WhyStopped field."""
    items: list[NewsItem] = []
    try:
        url = build_ctgov_url(query, since, until, CTGOV_PAGE_SIZE)
        token = None
        while len(items) < CTGOV_MAX_ITEMS:
            page = json.loads(_get(url + (f"&pageToken={urllib.parse.quote(token)}" if token else "")))
            items.extend(parse_ctgov(page))
            token = page.get("nextPageToken")
            if not token:
                break
        else:
            log.warning("ctgov: stopped at %d items (cap)", CTGOV_MAX_ITEMS)
    except Exception as e:  # never raise; keep pages already fetched
        log.warning("ctgov search failed (%r) after %d items: %s", query, len(items), e)
    return items[:CTGOV_MAX_ITEMS]


# ---------------------------------------------------------------- Google News RSS
# Feed terms: "solely for ... personal, non-commercial use".

GNEWS_URL = "https://news.google.com/rss/search"
_TAG_RE = re.compile(r"<[^>]+>")


def build_gnews_url(query: str, since: date, until: date) -> str:
    # `before:` is exclusive, so add a day to make `until` inclusive.
    q = f"{query} after:{since.isoformat()} before:{(until + timedelta(days=1)).isoformat()}"
    params = {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    return GNEWS_URL + "?" + urllib.parse.urlencode(params)


def _rfc822_to_iso(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_gnews(xml_bytes: bytes) -> list[NewsItem]:
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        src_el = it.find("source")
        publisher = (src_el.text if src_el is not None and src_el.text else "").strip() or "unknown"
        publisher_url = src_el.get("url") if src_el is not None else None
        # Titles look like "Headline - Publisher"; drop the suffix.
        suffix = f" - {publisher}"
        if title.endswith(suffix):
            title = title[: -len(suffix)]
        desc = html.unescape(_TAG_RE.sub(" ", it.findtext("description") or ""))
        desc = re.sub(r"\s+", " ", desc).strip()
        items.append(
            NewsItem(
                url=link,
                title=title,
                source=publisher,
                published=_rfc822_to_iso(it.findtext("pubDate")),
                publisher_url=publisher_url,
                # Google's description is usually just "title publisher": treat as empty.
                snippet="" if not desc or desc.startswith(title) else desc,
                origin="gnews",
            )
        )
    return items[:MAX_ITEMS_PER_SEARCH]


def search_gnews(query: str, since: date, until: date) -> list[NewsItem]:
    """`query` uses Google News syntax: quotes for phrases, OR between terms."""
    try:
        return parse_gnews(_get(build_gnews_url(query, since, until)))
    except Exception as e:  # never raise
        log.warning("gnews search failed (%r): %s", query, e)
        return []


# ---------------------------------------------------------------- publisher RSS feeds
# Full feeds, no keywords: every article goes to the classifier. Feeds keep only the latest
# few days (BioPharma Dive ~2 days, STAT biotech ~1 week), so they help regular runs, not
# backfills. FierceBiotech and Endpoints answer 403 to non-browser clients; not used.

FEEDS = {
    "BioPharma Dive": "https://www.biopharmadive.com/feeds/news/",
    "STAT": "https://www.statnews.com/category/biotech/feed/",
}


def parse_feed(xml_bytes: bytes, publisher: str) -> list[NewsItem]:
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        desc = html.unescape(_TAG_RE.sub(" ", it.findtext("description") or ""))
        items.append(NewsItem(
            url=link,  # a real article URL, unlike Google News
            title=title,
            source=publisher,
            published=_rfc822_to_iso(it.findtext("pubDate")),
            snippet=re.sub(r"\s+", " ", desc).strip()[:1000],
            origin="rss",
        ))
    return items


def search_feeds(query: str, since: date, until: date) -> list[NewsItem]:
    """Every article from FEEDS published inside the window. `query` is ignored."""
    out = []
    for publisher, url in FEEDS.items():
        try:
            for it in parse_feed(_get(url), publisher):
                if it.published and since.isoformat() <= it.published <= until.isoformat():
                    out.append(it)
        except Exception as e:  # never raise; skip this feed
            log.warning("feed %s failed: %s", publisher, e)
    return out


# ---------------------------------------------------------------- offline cache


LABEL_KEYS = {"label", "expect"}  # answers for `scout eval`, not part of the item


def load_fixtures(path: Path = FIXTURE_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def fixture_item(row: dict) -> NewsItem:
    return NewsItem(**{k: v for k, v in row.items() if k not in LABEL_KEYS})


def search_fixture(query: str, since: date, until: date) -> list[NewsItem]:
    """Offline source: hand-labelled examples, filtered by date only."""
    try:
        out = []
        for row in load_fixtures():
            item = fixture_item(row)
            if item.published:
                d = datetime.strptime(item.published, "%Y-%m-%d").date()
                if not (since <= d <= until):
                    continue
            out.append(item)
        return out
    except Exception as e:  # never raise
        log.warning("fixture load failed: %s", e)
        return []


SOURCES = {"ctgov": search_ctgov, "gnews": search_gnews, "rss": search_feeds}
PLANNER_SOURCES = ["gnews"]  # the only source where model-chosen keywords add anything

# Run by code on every run, one or two per event type (5 in total), before the planner adds its own queries.
# Google News returns only ~10-20 items per query, so one broad OR-query per run starves
# whole event types (2026-11-06: a run with one combined query found 0 CRLs, the next 6).
FIXED_GNEWS_QUERIES = {
    "missed_endpoint": '"primary endpoint" trial "did not meet" OR "failed to meet" OR misses OR fails',
    "stopped_futility_safety": 'trial stopped OR halted OR discontinued futility OR "interim analysis" OR safety',
    "clinical_hold": '"clinical hold" FDA',
    "regulatory_rejection": '"complete response letter"',
    "business_discontinuation": "biotech discontinues OR deprioritizes OR scraps drug program",
}

