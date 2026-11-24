"""SQLite storage (docs/design-details.md, Pipeline steps, step 5). Stores results only; fetching happens elsewhere."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .companies import company_key
from .models import FailureEvent, NewsItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    since TEXT NOT NULL,
    until TEXT NOT NULL,
    n_fetched INTEGER,
    n_new INTEGER,
    n_events INTEGER,
    planner_model TEXT,
    classifier_model TEXT,
    queries TEXT              -- JSON list of [source, query, n_results]
);
CREATE TABLE IF NOT EXISTS seen_items (
    url TEXT PRIMARY KEY,
    title TEXT,
    source TEXT,
    origin TEXT,
    published TEXT,
    first_run INTEGER REFERENCES runs(id),
    event_key TEXT            -- NULL when classified as not_an_event
);
CREATE TABLE IF NOT EXISTS events (
    event_key TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    failure_type TEXT,
    crl_reason TEXT,
    partial INTEGER,
    drug TEXT,
    sponsor TEXT,
    indication TEXT,
    phase TEXT,
    nct_id TEXT,
    event_date TEXT,
    evidence_quote TEXT,
    quote_verified INTEGER,
    confidence INTEGER,
    source_urls TEXT,         -- JSON list; first entry = first report seen
    first_run INTEGER REFERENCES runs(id),
    updated_run INTEGER REFERENCES runs(id)
    -- verification columns are added by EVENT_COLUMNS below
);
CREATE TABLE IF NOT EXISTS edits (   -- human corrections, synced from the Google Sheet
    event_key TEXT PRIMARY KEY,
    corrected_type TEXT,      -- replaces event_type in the v_events* views
    exclude INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id),
    step TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_usd REAL,            -- estimate from usage.PRICES
    stop_reason TEXT,
    request_id TEXT,
    error TEXT
);
"""


# Read-only views for analysis and the Google Sheet. Dropped and recreated on
# every open so a changed definition always takes effect. Human edits (table `edits`) are
# applied here: corrected_type overrides the model's event_type, exclude hides the row.
VIEWS = """
DROP VIEW IF EXISTS v_monthly;
DROP VIEW IF EXISTS v_companies;
DROP VIEW IF EXISTS v_events;
DROP VIEW IF EXISTS v_events_all;

CREATE VIEW v_events_all AS
SELECT
    e.event_key,
    e.event_date,
    substr(e.event_date, 1, 7)                       AS month,
    COALESCE(x.corrected_type, e.event_type)         AS event_type,
    CASE WHEN x.corrected_type IS NULL OR x.corrected_type = e.event_type
         THEN COALESCE(e.failure_type, e.crl_reason) END AS detail,
    e.partial,
    e.drug,
    e.sponsor,
    e.company_key,
    e.indication,
    e.phase,
    e.nct_id,
    e.confidence,
    json_array_length(e.source_urls)                 AS n_reports,
    json_extract(e.source_urls, '$[0]')              AS first_url,
    s.title                                          AS headline,
    s.source                                         AS publisher,
    CASE s.origin WHEN 'ctgov' THEN 'registry' ELSE 'news' END AS origin,
    e.evidence_quote,
    e.quote_verified,
    e.unverified_fields,
    e.registry_match,
    e.registry_nct,
    e.registry_status,
    e.registry_why_stopped,
    e.registry_stop_date,
    e.registry_conflict,
    e.event_type                                     AS model_event_type,
    x.corrected_type,
    COALESCE(x.exclude, 0)                           AS exclude,
    x.notes,
    e.first_run,
    e.updated_run
FROM events e
LEFT JOIN edits x ON x.event_key = e.event_key
LEFT JOIN seen_items s ON s.url = json_extract(e.source_urls, '$[0]')
WHERE e.merged_into IS NULL;

CREATE VIEW v_events AS
SELECT * FROM v_events_all WHERE exclude = 0;

CREATE VIEW v_companies AS
SELECT
    company_key,
    (SELECT sponsor FROM v_events v2 WHERE v2.company_key = v.company_key
      GROUP BY sponsor ORDER BY COUNT(*) DESC, LENGTH(sponsor) LIMIT 1) AS company,
    COUNT(*)                                          AS n_events,
    SUM(event_type = 'trial_failure')                 AS n_trial_failure,
    SUM(event_type = 'clinical_hold')                 AS n_clinical_hold,
    SUM(event_type = 'regulatory_rejection')          AS n_regulatory_rejection,
    SUM(event_type = 'business_discontinuation')      AS n_business_discontinuation,
    MIN(event_date)                                   AS first_event,
    MAX(event_date)                                   AS latest_event,
    GROUP_CONCAT(DISTINCT drug)                       AS drugs
FROM v_events v
WHERE company_key IS NOT NULL
GROUP BY company_key;

CREATE VIEW v_monthly AS
SELECT month, event_type, origin, COUNT(*) AS n_events
FROM v_events
WHERE month IS NOT NULL
GROUP BY month, event_type, origin;
"""

# Added after v0; created by migration so existing databases keep working.
EVENT_COLUMNS = {
    "publisher_urls": "TEXT",        # JSON list, parallel to source_urls (null for registry)
    "unverified_fields": "TEXT",     # JSON list, e.g. ["drug"]
    "registry_match": "TEXT",
    "registry_nct": "TEXT",
    "registry_status": "TEXT",
    "registry_why_stopped": "TEXT",
    "registry_stop_date": "TEXT",
    "registry_candidates": "INTEGER",
    "registry_conflict": "INTEGER",
    "company_key": "TEXT",           # companies.company_key(sponsor)
    "merged_into": "TEXT",           # set on duplicate reports; NULL on canonical events
}

# Fields replaced when a more confident report of the same event arrives.
REPLACE_FIELDS = ["failure_type", "crl_reason", "partial", "drug", "sponsor", "indication", "phase",
                  "nct_id", "event_date", "evidence_quote", "quote_verified", "confidence",
                  "unverified_fields", "registry_match", "registry_nct", "registry_status",
                  "registry_why_stopped", "registry_stop_date", "registry_candidates",
                  "registry_conflict"]


def _event_row(ev: FailureEvent) -> dict:
    return {
        "event_key": ev.event_key, "event_type": ev.event_type, "failure_type": ev.failure_type,
        "crl_reason": ev.crl_reason, "partial": int(ev.partial), "drug": ev.drug,
        "sponsor": ev.sponsor, "indication": ev.indication, "phase": ev.phase,
        "nct_id": ev.nct_id, "event_date": ev.event_date, "evidence_quote": ev.evidence_quote,
        "quote_verified": int(ev.quote_verified), "confidence": ev.confidence,
        "unverified_fields": json.dumps(ev.unverified_fields), "registry_match": ev.registry_match,
        "registry_nct": ev.registry_nct, "registry_status": ev.registry_status,
        "registry_why_stopped": ev.registry_why_stopped, "registry_stop_date": ev.registry_stop_date,
        "registry_candidates": ev.registry_candidates, "registry_conflict": int(ev.registry_conflict),
        "company_key": company_key(ev.sponsor),
    }


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def make_event_key(nct_id: str | None, drug: str | None, sponsor: str | None,
                   event_type: str, fallback_url: str) -> str:
    """Same event reported by several outlets -> same key."""
    if nct_id and re.fullmatch(r"NCT\d{8}", nct_id.strip()):
        return nct_id.strip()
    if _norm(drug):
        return f"{_norm(drug)}|{_norm(sponsor)}|{event_type}"
    return f"url|{fallback_url}"


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(events)")}
        for col, typ in EVENT_COLUMNS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
        # Backfill company_key for rows stored before the column existed.
        for key, sponsor in self.conn.execute(
                "SELECT event_key, sponsor FROM events WHERE company_key IS NULL AND sponsor IS NOT NULL"
        ).fetchall():
            self.conn.execute("UPDATE events SET company_key=? WHERE event_key=?",
                              (company_key(sponsor), key))
        self.conn.executescript(VIEWS)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- runs
    def last_until(self) -> str | None:
        row = self.conn.execute("SELECT until FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return row["until"] if row else None

    def start_run(self, since: str, until: str, planner_model: str, classifier_model: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, since, until, planner_model, classifier_model) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), since, until,
             planner_model, classifier_model),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, n_fetched: int, n_new: int, n_events: int,
                   queries: list) -> None:
        self.conn.execute(
            "UPDATE runs SET n_fetched=?, n_new=?, n_events=?, queries=? WHERE id=?",
            (n_fetched, n_new, n_events, json.dumps(queries, ensure_ascii=False), run_id),
        )
        self.conn.commit()

    # ---- seen items
    def unseen(self, items: list[NewsItem]) -> list[NewsItem]:
        """Drop items already classified in an earlier run, and in-batch duplicates."""
        out, urls = [], set()
        for it in items:
            if it.url in urls:
                continue
            urls.add(it.url)
            if self.conn.execute("SELECT 1 FROM seen_items WHERE url=?", (it.url,)).fetchone():
                continue
            out.append(it)
        return out

    def mark_seen(self, item: NewsItem, run_id: int, event_key: str | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_items (url, title, source, origin, published, first_run, event_key)"
            " VALUES (?,?,?,?,?,?,?)",
            (item.url, item.title, item.source, item.origin, item.published, run_id, event_key),
        )

    # ---- events
    def upsert_event(self, ev: FailureEvent, run_id: int) -> bool:
        """Insert a new event, or attach this report to an existing one. Returns True if new."""
        row = self.conn.execute(
            "SELECT source_urls, publisher_urls, confidence FROM events WHERE event_key=?",
            (ev.event_key,),
        ).fetchone()
        data = _event_row(ev)
        if row is None:
            data |= {"source_urls": json.dumps([ev.source_url]),
                     "publisher_urls": json.dumps([ev.publisher_url]),
                     "first_run": run_id, "updated_run": run_id}
            cols = ", ".join(data)
            self.conn.execute(f"INSERT INTO events ({cols}) VALUES ({', '.join('?' * len(data))})",
                              tuple(data.values()))
            return True
        urls = json.loads(row["source_urls"])
        pubs = json.loads(row["publisher_urls"] or "null") or [None] * len(urls)
        if ev.source_url not in urls:
            urls.append(ev.source_url)
            pubs.append(ev.publisher_url)
        if ev.confidence > row["confidence"]:
            # A more confident report replaces the extracted fields, keeps the URL history.
            sets = ", ".join(f"{f}=?" for f in REPLACE_FIELDS)
            self.conn.execute(f"UPDATE events SET {sets} WHERE event_key=?",
                              tuple(data[f] for f in REPLACE_FIELDS) + (ev.event_key,))
        self.conn.execute(
            "UPDATE events SET source_urls=?, publisher_urls=?, updated_run=? WHERE event_key=?",
            (json.dumps(urls), json.dumps(pubs), run_id, ev.event_key),
        )
        return False

    def get_event(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM events WHERE event_key=?", (key,)).fetchone()

    def commit(self) -> None:
        self.conn.commit()

    # ---- llm calls
    def save_calls(self, run_id: int, calls: list) -> None:
        self.conn.executemany(
            "INSERT INTO llm_calls (run_id, step, model, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, cost_usd, stop_reason, request_id, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, c.step, c.model, c.input_tokens, c.output_tokens, c.cache_read_tokens,
              c.cache_write_tokens, c.cost_usd, c.stop_reason, c.request_id, c.error)
             for c in calls],
        )
        self.conn.commit()

    def list_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT r.id, r.started_at, r.since, r.until, r.n_fetched, r.n_new, r.n_events,"
            " COUNT(c.id) AS n_calls, COALESCE(SUM(c.cost_usd), 0) AS cost,"
            " SUM(c.error IS NOT NULL) AS n_errors"
            " FROM runs r LEFT JOIN llm_calls c ON c.run_id = r.id"
            " GROUP BY r.id ORDER BY r.id DESC LIMIT ?", (limit,)
        ).fetchall()

    def list_events(self, event_type: str | None = None, limit: int = 50,
                    company: str | None = None) -> list[sqlite3.Row]:
        """Canonical events only (duplicates merged into another event are hidden)."""
        sql = "SELECT * FROM events WHERE merged_into IS NULL"
        args: tuple = ()
        if event_type:
            sql += " AND event_type=?"
            args += (event_type,)
        if company:
            sql += " AND company_key=?"
            args += (company,)
        sql += " ORDER BY COALESCE(event_date, '') DESC LIMIT ?"
        return self.conn.execute(sql, args + (limit,)).fetchall()

    # ---- merging
    def merge_candidates(self, since: str | None = None, until: str | None = None) -> list[sqlite3.Row]:
        """Canonical events with the headline of their first report (for the merge step)."""
        sql = ("SELECT e.*, s.title AS headline, s.origin AS origin FROM events e"
               " LEFT JOIN seen_items s ON s.url = json_extract(e.source_urls, '$[0]')"
               " WHERE e.merged_into IS NULL")
        args: tuple = ()
        if since:
            sql += " AND COALESCE(e.event_date, '') >= ?"
            args += (since,)
        if until:
            sql += " AND COALESCE(e.event_date, '') <= ?"
            args += (until,)
        return self.conn.execute(sql + " ORDER BY e.event_date", args).fetchall()

    def merge_events(self, canonical: str, members: list[str], overrides: dict) -> None:
        """Fold `members` into `canonical`: URLs appended, missing fields filled, members hidden."""
        canon = self.get_event(canonical)
        urls = json.loads(canon["source_urls"])
        pubs = json.loads(canon["publisher_urls"] or "null") or [None] * len(urls)
        fill = {}
        for key in members:
            m = self.get_event(key)
            m_urls = json.loads(m["source_urls"])
            m_pubs = json.loads(m["publisher_urls"] or "null") or [None] * len(m_urls)
            for u, p in zip(m_urls, m_pubs):
                if u not in urls:
                    urls.append(u)
                    pubs.append(p)
            for f in ("drug", "sponsor", "company_key", "nct_id", "indication", "phase",
                      "failure_type", "crl_reason"):
                if canon[f] is None and m[f] is not None and f not in fill:
                    fill[f] = m[f]
            self.conn.execute("UPDATE events SET merged_into=? WHERE event_key=?", (canonical, key))
            self.conn.execute("UPDATE seen_items SET event_key=? WHERE event_key=?", (canonical, key))
        fill |= {k: v for k, v in overrides.items() if v is not None or k in ("failure_type", "crl_reason")}
        fill |= {"source_urls": json.dumps(urls), "publisher_urls": json.dumps(pubs)}
        sets = ", ".join(f"{k}=?" for k in fill)
        self.conn.execute(f"UPDATE events SET {sets} WHERE event_key=?", (*fill.values(), canonical))

    # ---- human edits (from the Google Sheet)
    def canonical_key(self, key: str) -> str | None:
        """Follow merged_into to the event a key now lives in (None if unknown)."""
        for _ in range(10):
            row = self.conn.execute("SELECT merged_into FROM events WHERE event_key=?", (key,)).fetchone()
            if row is None:
                return None
            if not row["merged_into"]:
                return key
            key = row["merged_into"]
        return key

    def set_edit(self, key: str, corrected_type: str | None, exclude: bool, notes: str | None) -> None:
        if corrected_type is None and not exclude and not notes:
            self.conn.execute("DELETE FROM edits WHERE event_key=?", (key,))
            return
        self.conn.execute(
            "INSERT INTO edits (event_key, corrected_type, exclude, notes, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(event_key) DO UPDATE SET corrected_type=excluded.corrected_type,"
            " exclude=excluded.exclude, notes=excluded.notes, updated_at=excluded.updated_at",
            (key, corrected_type, int(exclude), notes,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )

    def view(self, name: str, order_by: str = "") -> tuple[list[str], list[tuple]]:
        """(column names, rows) of one v_* view."""
        if not re.fullmatch(r"v_[a-z_]+", name):
            raise ValueError(name)
        cur = self.conn.execute(f"SELECT * FROM {name}" + (f" ORDER BY {order_by}" if order_by else ""))
        return [d[0] for d in cur.description], [tuple(r) for r in cur.fetchall()]

    # ---- companies
    def list_companies(self, min_events: int = 1) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT company_key,"
            " (SELECT sponsor FROM events e2 WHERE e2.company_key = e.company_key"
            "   AND e2.merged_into IS NULL GROUP BY sponsor ORDER BY COUNT(*) DESC, LENGTH(sponsor)"
            "   LIMIT 1) AS name,"
            " COUNT(*) AS n,"
            " SUM(event_type='trial_failure') AS n_trial_failure,"
            " SUM(event_type='clinical_hold') AS n_clinical_hold,"
            " SUM(event_type='regulatory_rejection') AS n_regulatory_rejection,"
            " SUM(event_type='business_discontinuation') AS n_business,"
            " MAX(event_date) AS latest,"
            " GROUP_CONCAT(DISTINCT drug) AS drugs"
            " FROM events e WHERE merged_into IS NULL AND company_key IS NOT NULL"
            " GROUP BY company_key HAVING COUNT(*) >= ?"
            " ORDER BY n DESC, latest DESC", (min_events,)
        ).fetchall()
