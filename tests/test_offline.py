"""Offline tests: no network, no API calls."""

import json

from datetime import date

from failure_scout.classify import quote_is_verbatim
from failure_scout.db import Store, make_event_key
from failure_scout.models import FailureEvent, NewsItem
from failure_scout.sources import build_ctgov_url, build_gnews_url, fixture_item, load_fixtures, parse_ctgov, parse_gnews

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Acme drug misses primary endpoint - FierceBiotech</title>
<link>https://news.google.com/rss/articles/abc</link>
<pubDate>Tue, 10 Mar 2026 14:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Acme drug misses primary endpoint&lt;/a&gt;</description>
<source url="https://www.fiercebiotech.com">FierceBiotech</source></item>
</channel></rss>"""

CTGOV = {"studies": [{"protocolSection": {
    "identificationModule": {"nctId": "NCT01234567", "briefTitle": "A Study of X"},
    "statusModule": {"overallStatus": "TERMINATED", "whyStopped": "Futility at interim",
                     "lastUpdatePostDateStruct": {"date": "2026-03-13"}},
    "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Acme"}},
    "designModule": {"phases": ["PHASE3"]},
}}]}


def item(url="u1", title="t", snippet="s"):
    return NewsItem(url=url, title=title, source="x", published="2026-03-10", snippet=snippet, origin="gnews")


def event(key, url, conf=70):
    return FailureEvent(event_key=key, event_type="trial_failure", failure_type="futility",
                        crl_reason=None, partial=False, drug="X", sponsor="Acme", indication=None,
                        phase=None, nct_id=None, event_date="2026-03-10", evidence_quote="q",
                        quote_verified=True, confidence=conf, source_url=url)


def test_parse_gnews_strips_publisher_and_dates():
    [it] = parse_gnews(RSS)
    assert it.title == "Acme drug misses primary endpoint"
    assert it.source == "FierceBiotech"
    assert it.published == "2026-03-10"
    assert it.snippet == ""  # description only repeated the title
    assert it.publisher_url == "https://www.fiercebiotech.com"


def test_parse_ctgov():
    [it] = parse_ctgov(CTGOV)
    assert it.nct_id == "NCT01234567"
    assert "Why stopped: Futility at interim" in it.snippet
    assert it.url.endswith("NCT01234567")


def test_urls_carry_date_window():
    u = build_ctgov_url("futility", date(2026, 3, 1), date(2026, 3, 31), 10)
    assert "RANGE%5B2026-03-01%2C2026-03-31%5D" in u and "WhyStopped" in u and "InterventionType" in u
    assert "before%3A2026-04-01" in build_gnews_url("x", date(2026, 3, 1), date(2026, 3, 31))


def test_event_key():
    assert make_event_key("NCT01234567", "x", "y", "trial_failure", "u") == "NCT01234567"
    assert make_event_key(None, "Epcoritamab ", "Genmab", "trial_failure", "u") == \
        make_event_key(None, "epcoritamab", "GENMAB", "trial_failure", "v")
    assert make_event_key(None, None, None, "trial_failure", "u") == "url|u"


def test_quote_check():
    it = item(title="Acme drug misses   primary endpoint", snippet="More text.")
    assert quote_is_verbatim("misses primary endpoint", it)
    assert not quote_is_verbatim("failed badly", it)
    assert not quote_is_verbatim("", it)


def test_store_dedupe_and_merge(tmp_path):
    s = Store(tmp_path / "t.db")
    run = s.start_run("2026-03-01", "2026-03-31", "p", "c")
    a, b = item("u1"), item("u2")
    assert [i.url for i in s.unseen([a, a, b])] == ["u1", "u2"]
    assert s.upsert_event(event("k", "u1", 60), run) is True
    assert s.upsert_event(event("k", "u2", 90), run) is False  # second report, same event
    s.mark_seen(a, run, "k")
    s.commit()
    assert [i.url for i in s.unseen([a, b])] == ["u2"]
    [row] = s.list_events()
    assert row["confidence"] == 90 and row["source_urls"] == '["u1", "u2"]'
    s.finish_run(run, 2, 2, 1, [])
    assert s.last_until() == "2026-03-31"


def test_fixtures_are_valid():
    rows = load_fixtures()
    assert len(rows) >= 20
    for r in rows:
        fixture_item(r)


def test_usage_log_cost_and_storage(tmp_path):
    from types import SimpleNamespace
    from failure_scout.usage import UsageLog

    log = UsageLog()
    resp = SimpleNamespace(model="claude-haiku-5-5", stop_reason="end_turn", _request_id="req_1",
                           usage=SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000,
                                                 cache_read_input_tokens=0, cache_creation_input_tokens=0))
    log.record("classify", "claude-haiku-5-5", resp=resp)
    log.record("planner", "claude-haiku-5-5", error=RuntimeError("boom"))
    assert abs(log.total_cost() - 0.60) < 1e-9  # $0.10 in + $0.50 out
    assert "1 errors" in log.summary()

    s = Store(tmp_path / "t.db")
    run = s.start_run("2026-03-01", "2026-03-31", "p", "c")
    s.save_calls(run, log.calls)
    [row] = s.list_runs()
    assert row["n_calls"] == 2 and row["n_errors"] == 1 and abs(row["cost"] - 0.60) < 1e-9


def test_verify_fields_and_registry_source():
    from failure_scout.verify import check_registry, unverified_fields

    it = item(title="Roche's giredestrant fails in Phase III", snippet="Hoffmann-La Roche said ...")
    ev = event("k", "u1")
    ev.drug, ev.sponsor = "giredestrant", "F. Hoffmann-La Roche Ltd"
    assert unverified_fields(ev, it) == []
    ev.drug = "tamoxifen"
    assert unverified_fields(ev, it) == ["drug"]
    ev.drug, ev.sponsor = "giredestrant + palbociclib", "Pfizer"
    assert unverified_fields(ev, it) == ["drug", "sponsor"]

    [reg] = parse_ctgov(CTGOV)
    ev2 = event("NCT01234567", reg.url)
    check_registry(ev2, reg)  # registry record: no network call
    assert ev2.registry_match == "source" and ev2.registry_status == "TERMINATED"
    assert not ev2.registry_conflict

    ev3 = event("k3", "u3")
    ev3.event_type, ev3.crl_reason = "regulatory_rejection", "cmc"
    check_registry(ev3, it)
    assert ev3.registry_match == "skipped"


def test_store_migrates_and_keeps_publisher_urls(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)  # a v0 database without the verification columns
    con.executescript("CREATE TABLE events (event_key TEXT PRIMARY KEY, event_type TEXT NOT NULL,"
                      " failure_type TEXT, crl_reason TEXT, partial INTEGER, drug TEXT, sponsor TEXT,"
                      " indication TEXT, phase TEXT, nct_id TEXT, event_date TEXT, evidence_quote TEXT,"
                      " quote_verified INTEGER, confidence INTEGER, source_urls TEXT,"
                      " first_run INTEGER, updated_run INTEGER);")
    con.close()
    s = Store(path)
    run = s.start_run("a", "b", "p", "c")
    e1 = event("k", "u1"); e1.publisher_url = "https://www.biospace.com"
    e2 = event("k", "u2", conf=90)
    s.upsert_event(e1, run); s.upsert_event(e2, run); s.commit()
    r = s.get_event("k")
    assert r["publisher_urls"] == '["https://www.biospace.com", null]'
    assert r["registry_match"] == "pending" and r["confidence"] == 90


def test_ctgov_drops_anticipated_completion_dates():
    import copy
    def with_dates(status, completion, updated="2026-03-02"):
        d = copy.deepcopy(CTGOV)
        sm = d["studies"][0]["protocolSection"]["statusModule"]
        sm.update(overallStatus=status, completionDateStruct={"date": completion},
                  lastUpdatePostDateStruct={"date": updated})
        return parse_ctgov(d)[0].completion_date
    assert with_dates("TERMINATED", "2025-10-16") == "2025-10-16"
    assert with_dates("TERMINATED", "2026-03") == "2026-03"      # month only, same month
    assert with_dates("TERMINATED", "2028-12-30") is None       # anticipated
    assert with_dates("WITHDRAWN", "2025-01-01") is None        # never ran


def test_searchlog_fixed_queries_and_caps(monkeypatch):
    from failure_scout import planner

    calls = []
    def fake(query, since, until):
        calls.append(query)
        return [] if query == "empty" else [item(url=f"u-{query}")]
    monkeypatch.setitem(planner.SOURCES, "gnews", fake)
    slog = planner.SearchLog(since=date(2026, 3, 1), until=date(2026, 3, 31))
    for label, q in planner.FIXED_GNEWS_QUERIES.items():
        slog.run_fixed(label, "gnews", q)
    assert len(slog.items) == len(planner.FIXED_GNEWS_QUERIES)  # every event type searched

    slog.run("gnews", "extra1")
    slog.run("gnews", "empty")  # empty results don't use up the extra-search budget
    slog.run("gnews", "extra2")
    try:
        slog.run("gnews", "extra3")
        raise AssertionError("cap not enforced")
    except ValueError:
        pass
    assert "extra3" not in calls


FEED = b"""<?xml version="1.0"?><rss><channel>
<item><title>Acme&#8217;s drug fails Phase 3</title><link>https://www.biopharmadive.com/news/acme-fails/1/</link>
<pubDate>Tue, 10 Mar 2026 14:00:00 -0500</pubDate><description>&lt;p&gt;Acme said the trial missed.&lt;/p&gt;</description></item>
<item><title>Old story</title><link>https://www.biopharmadive.com/news/old/2/</link>
<pubDate>Tue, 10 Feb 2026 14:00:00 -0500</pubDate></item>
</channel></rss>"""


def test_feeds_real_urls_and_window(monkeypatch):
    from failure_scout import sources
    [a, b] = sources.parse_feed(FEED, "BioPharma Dive")
    assert a.title == "Acme’s drug fails Phase 3" and a.url.startswith("https://www.biopharmadive.com/")
    assert a.snippet == "Acme said the trial missed." and a.origin == "rss"
    monkeypatch.setattr(sources, "FEEDS", {"BioPharma Dive": "x"})
    monkeypatch.setattr(sources, "_get", lambda url: FEED)
    assert [i.title for i in sources.search_feeds("", date(2026, 3, 1), date(2026, 3, 31))] == [a.title]


def test_ctgov_follows_pages(monkeypatch):
    import json as _json
    from failure_scout import sources
    pages = [dict(CTGOV, nextPageToken="t2"), CTGOV]
    seen = []
    def fake_get(url):
        seen.append("pageToken=t2" in url)
        return _json.dumps(pages[len(seen) - 1]).encode()
    monkeypatch.setattr(sources, "_get", fake_get)
    assert len(sources.search_ctgov("", date(2026, 3, 1), date(2026, 3, 31))) == 2
    assert seen == [False, True]


def test_company_key():
    from failure_scout.companies import company_key
    assert company_key("F. Hoffmann-La Roche Ltd") == company_key("Roche") == "roche"
    assert company_key("Genentech, Inc.") == "roche"
    assert company_key("Aldeyra Therapeutics, Inc.") == company_key("Aldeyra") == "aldeyra"
    assert company_key("Bright Minds'") == company_key("Bright Minds Biosciences")
    assert company_key("Kite, A Gilead Company") == "gilead"
    assert company_key("Kanyos Bio, Inc., a wholly owned subsidiary of Anokion SA") == "kanyos"
    assert company_key(None) is None


def _merge_store(tmp_path):
    s = Store(tmp_path / "m.db")
    run = s.start_run("2026-03-01", "2026-03-31", "p", "c")
    def add(key, url, origin, drug, sponsor, etype, date_, nct=None, conf=70, title="t"):
        ev = event(key, url, conf)
        ev.drug, ev.sponsor, ev.event_type, ev.event_date, ev.nct_id = drug, sponsor, etype, date_, nct
        ev.failure_type = "futility" if etype == "trial_failure" else None
        s.upsert_event(ev, run)
        s.mark_seen(NewsItem(url=url, title=title, source="x", published=date_, snippet="",
                             origin=origin), run, key)
    add("efti|immutep", "u1", "gnews", "eftilagimod alfa", "Immutep", "trial_failure", "2026-03-13", conf=80)
    add("url|u2", "u2", "gnews", None, "Immutep Ltd", "business_discontinuation", "2026-03-14")
    add("url|u3", "u3", "gnews", None, None, "trial_failure", "2026-03-13", title="TACTI-004 stopped")
    add("rep|ald", "u4", "gnews", "reproxalap", "Aldeyra", "regulatory_rejection", "2026-03-17")
    add("rep|aldt", "u5", "gnews", "Reproxalap", "Aldeyra Therapeutics", "regulatory_rejection", "2026-03-17")
    add("NCT00000001", "r1", "ctgov", "X", "Acme", "business_discontinuation", "2026-03-10", nct="NCT00000001")
    add("NCT00000002", "r2", "ctgov", "X", "Acme", "business_discontinuation", "2026-03-11", nct="NCT00000002")
    s.commit()
    return s


class _FakeClient:
    """messages.parse returns one cluster: all Immutep lines."""
    def __init__(self):
        from types import SimpleNamespace as NS
        self.NS, self.calls = NS, []
        self.messages = NS(parse=self.parse)

    def parse(self, **kw):
        from failure_scout.merge import Cluster, ClusterResult
        lines = kw["messages"][0]["content"].split("\n")
        self.calls.append(lines)
        idx = [i for i, l in enumerate(lines) if "Immutep" in l or "TACTI" in l or "eftilagimod" in l]
        out = ClusterResult(clusters=[Cluster(members=idx, event_type="trial_failure",
                                              failure_type="futility", crl_reason=None)] if len(idx) > 1 else [])
        return self.NS(stop_reason="end_turn", parsed_output=out, model="claude-haiku-5-5",
                       usage=self.NS(input_tokens=10, output_tokens=10), _request_id="r")


def test_merge_rule_llm_and_nct_guard(tmp_path):
    from failure_scout.merge import merge_events
    from failure_scout.usage import UsageLog
    s = _merge_store(tmp_path)
    n_merged, n_groups = merge_events(s, _FakeClient(), "claude-haiku-5-5", UsageLog())
    assert (n_merged, n_groups) == (3, 2)  # Immutep 3 -> 1 (LLM), Aldeyra 2 -> 1 (rule)
    shown = {r["event_key"]: r for r in s.list_events(limit=100)}
    assert "efti|immutep" in shown and "url|u2" not in shown and "url|u3" not in shown
    imm = shown["efti|immutep"]
    assert imm["event_type"] == "trial_failure" and imm["failure_type"] == "futility"
    urls = json.loads(imm["source_urls"])
    assert urls[0] == "u1" and sorted(urls) == ["u1", "u2", "u3"]
    assert len([k for k in shown if k.startswith("rep|")]) == 1
    assert "NCT00000001" in shown and "NCT00000002" in shown  # different trials stay apart
    assert s.get_event("url|u2")["merged_into"] == "efti|immutep"
    [row] = [c for c in s.list_companies() if c["company_key"] == "immutep"]
    assert row["n"] == 1 and row["n_trial_failure"] == 1


def test_views_apply_edits(tmp_path):
    s = _merge_store(tmp_path)
    s.set_edit("rep|ald", "business_discontinuation", False, "actually a pipeline cut")
    s.set_edit("NCT00000001", None, True, None)
    s.commit()
    cols, rows = s.view("v_events_all")
    by = {r[cols.index("event_key")]: dict(zip(cols, r)) for r in rows}
    assert by["rep|ald"]["event_type"] == "business_discontinuation"
    assert by["rep|ald"]["model_event_type"] == "regulatory_rejection"
    assert by["rep|ald"]["detail"] is None  # model's detail no longer applies
    assert by["NCT00000001"]["exclude"] == 1
    _, visible = s.view("v_events")
    assert len(visible) == len(rows) - 1
    s.set_edit("NCT00000001", None, False, None)  # all empty = edit removed
    assert s.conn.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 1


def test_pull_edits_maps_merged_keys_and_validates(tmp_path):
    from failure_scout.sync import EVENT_COLUMNS, pull_edits
    s = _merge_store(tmp_path)
    s.merge_events("efti|immutep", ["url|u2"], {})
    s.commit()
    h = EVENT_COLUMNS
    def row(key, ct="", ex="FALSE", notes=""):
        r = [""] * len(h)
        r[h.index("event_key")], r[h.index("corrected_type")] = key, ct
        r[h.index("exclude")], r[h.index("notes")] = ex, notes
        return r
    grid = [h, row("url|u2", ct="clinical_hold"), row("rep|ald", ex="TRUE"),
            row("NCT00000001", ct="nonsense"), row("no-such-key", notes="x"), row("NCT00000002")]
    applied, warnings = pull_edits(s, grid)
    assert applied == 2
    assert len(warnings) == 2  # invalid type + unknown key
    edits = {r[0]: r for r in s.conn.execute("SELECT event_key, corrected_type, exclude FROM edits")}
    assert edits["efti|immutep"][1] == "clinical_hold"  # url|u2 was merged into it
    assert edits["rep|ald"][2] == 1
    assert "NCT00000001" not in edits


class _FakeWS:
    def __init__(self, title, rows, cols, sheet_id):
        self.title, self.row_count, self.col_count, self.id = title, rows, cols, sheet_id
        self.values = []
    def get_all_values(self): return [list(map(lambda v: str(v).upper() if isinstance(v, bool) else str(v), r)) for r in self.values]
    def batch_clear(self, ranges): self.values = []
    def update(self, values, range_name, raw=True): self.writes = getattr(self, "writes", []) + [(range_name, values, raw)]; self.values = [list(r) for r in values] if range_name == "A1" else self.values
    def batch_update(self, data, raw=True): self.writes = getattr(self, "writes", []) + [(d["range"], d["values"], raw) for d in data]
    def freeze(self, rows): pass
    def resize(self, rows, cols): self.row_count, self.col_count = rows, cols


class _FakeSheet:
    def __init__(self): self.tabs, self.requests = {}, []
    def worksheet(self, title):
        import gspread
        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]
    def add_worksheet(self, title, rows, cols, index=None):
        self.tabs[title] = _FakeWS(title, rows, cols, len(self.tabs))
        return self.tabs[title]
    def fetch_sheet_metadata(self): return {"sheets": []}
    def batch_update(self, body): self.requests.extend(body["requests"])


def test_sync_round_trip(tmp_path, monkeypatch):
    from failure_scout import sync as sync_mod
    s = _merge_store(tmp_path)
    sheet = _FakeSheet()
    monkeypatch.setattr(sync_mod, "_open", lambda sid, cp: sheet)
    msg = sync_mod.sync(s, sheet_id="x", creds_path="y")
    assert "7 events" in msg and set(sheet.tabs) == {"events", "companies", "monthly", "about", "dashboard"}
    assert sum("addChart" in r for r in sheet.requests) == 2
    ev = sheet.tabs["events"]
    assert ev.values[0] == sync_mod.EVENT_COLUMNS
    assert any("setDataValidation" in r for r in sheet.requests)

    # A colleague marks a row excluded and writes a note in the sheet ...
    h = ev.values[0]
    target = next(r for r in ev.values[1:] if r[h.index("event_key")] == "rep|ald")
    target[h.index("exclude")], target[h.index("notes")] = True, "duplicate of press release"
    # ... the next sync keeps it, and the database view hides the row.
    msg = sync_mod.sync(s, sheet_id="x", creds_path="y")
    assert "1 rows of edits pulled" in msg
    row = next(r for r in sheet.tabs["events"].values[1:] if r[h.index("event_key")] == "rep|ald")
    assert row[h.index("exclude")] is True and row[h.index("notes")] == "duplicate of press release"
    assert "rep|ald" not in {r[0] for r in s.view("v_events")[1]}


def test_dashboard_build(tmp_path):
    from failure_scout import dashboard
    s = _merge_store(tmp_path)
    s.set_edit("NCT00000001", None, True, None); s.commit()
    d = dashboard.build(s, today=date(2026, 3, 31))
    labels = [k[0] for k in d["kpis"]]
    assert labels[0] == "All events" and d["kpis"][0][1] == d["total"] == 6  # 7 minus 1 excluded
    assert d["monthly"][0][0] == "month" and d["monthly"][-1][0] == "2026-03" and len(d["monthly"]) == 13
    march = d["monthly"][-1]
    assert sum(march[1:]) == 6
    assert d["companies"][0] == ["company", "events"]
    assert len(d["latest"]) == 6 and d["latest"][0][0] >= d["latest"][-1][0]
    assert dashboard._a1(25, 0) == "A26" and dashboard._a1(0, 17) == "R1" and dashboard._a1(0, 27) == "AB1"
    assert dashboard._hyperlink('https://x.com/a"b') == '=HYPERLINK("https://x.com/a%22b", "open")'


def test_dashboard_legend():
    from failure_scout import dashboard
    from failure_scout.models import CrlReason, EventType, FailureType
    from typing import get_args
    rows = dashboard.legend_grid()
    terms = " ".join(r[0] for r in rows)
    for t in [*get_args(EventType)[:-1], *get_args(FailureType), "efficacy", "cmc"]:
        assert t in terms, t  # every label shown on the dashboard is explained
    assert all(len(r) in (1, 3) for r in rows)
    reqs = dashboard.format_requests(0, 30)
    assert all("repeatCell" in r or "updateDimensionProperties" in r for r in reqs)
