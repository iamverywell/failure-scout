"""The `dashboard` tab of the Google Sheet, rebuilt on every sync.

Layout (0-based rows/cols in the code, A1 notation here):
  A1        title, A2 last-sync line
  A4:E6     KPI cards: label / total / last 30 days, per event type
  A8        stacked column chart: events per month (last 12 months) by event type
  H8        bar chart: top companies
  A26       latest events table with clickable links
  R1...     chart source data (the charts read from here)

Numbers come from the v_events view (human edits applied, excluded rows hidden). The tab is
fully regenerated: values cleared, charts deleted and re-added, so edits made on this tab are
not kept (edit the events tab instead)."""

from __future__ import annotations

from datetime import date, timedelta

from .db import Store

DASHBOARD_TAB = "dashboard"
TYPES = ["trial_failure", "clinical_hold", "regulatory_rejection", "business_discontinuation"]
TYPE_LABELS = {"trial_failure": "Trial failures", "clinical_hold": "Clinical holds",
               "regulatory_rejection": "CRLs", "business_discontinuation": "Business discontinuations"}
N_MONTHS, N_COMPANIES, N_LATEST = 12, 15, 30

KPI_ROW = 3          # A4
CHART_ROW = 7        # A8
TABLE_ROW = 25       # A26
DATA_COL = 17        # column R: chart source data
TABLE_HEADER = ["date", "type", "detail", "drug", "company", "headline", "reports", "link"]
LEGEND_ROW = TABLE_ROW + N_LATEST + 3  # below the latest-events table

# Shown under the dashboard: what each label means. (term, meaning)
LEGEND = [
    ["What the labels mean", ""],
    ["term", "meaning"],
    ["trial_failure", "A trial missed its primary endpoint, or was stopped early for futility or safety."],
    ["  · missed_primary_endpoint", "The trial ran to its readout and the primary endpoint was not met."],
    ["  · futility", "Stopped early (usually at an interim analysis) because success had become unlikely or efficacy was lacking."],
    ["  · safety", "Stopped because of toxicity, deaths or other safety signals."],
    ["clinical_hold", "A regulator (usually FDA) halted all or part of a trial; partial = only some arms or new enrolment."],
    ["regulatory_rejection", "FDA complete response letter (CRL): the application was not approved. The detail gives the reason:"],
    ["  · efficacy", "Evidence of benefit judged insufficient (e.g. one trial, single-arm design, surrogate endpoint)."],
    ["  · safety", "Safety concerns."],
    ["  · cmc", "Manufacturing / quality problems (chemistry, manufacturing and controls), not the trial data."],
    ["  · labeling / other", "Labelling, packaging or device-use issues; or another stated reason."],
    ["business_discontinuation", "A trial or program dropped for business, strategic, funding or enrolment reasons, not because the data failed."],
    ["", ""],
    ["origin: news / registry", "news = news report; registry = ClinicalTrials.gov record (date = actual stop date when the record gives one)."],
    ["reports", "How many reports were merged into this event."],
    ["Scope", "Drugs and biologics only; medical devices excluded. Rows marked exclude in the events tab are not counted."],
]


def _months_back(today: date, n: int) -> list[str]:
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def build(store: Store, today: date | None = None) -> dict:
    """Everything the tab shows, as plain Python values (tested offline)."""
    today = today or date.today()
    since30 = (today - timedelta(days=30)).isoformat()
    q = store.conn.execute
    total = q("SELECT COUNT(*) FROM v_events").fetchone()[0]
    recent = q("SELECT COUNT(*) FROM v_events WHERE event_date >= ?", (since30,)).fetchone()[0]
    by_type = dict(q("SELECT event_type, COUNT(*) FROM v_events GROUP BY event_type").fetchall())
    recent_type = dict(q("SELECT event_type, COUNT(*) FROM v_events WHERE event_date >= ?"
                         " GROUP BY event_type", (since30,)).fetchall())
    kpis = [("All events", total, recent)] + [
        (TYPE_LABELS[t], by_type.get(t, 0), recent_type.get(t, 0)) for t in TYPES]

    months = _months_back(today, N_MONTHS)
    counts = {(m, t): n for m, t, n in q(
        "SELECT month, event_type, SUM(n_events) FROM v_monthly WHERE month >= ? GROUP BY 1, 2",
        (months[0],)).fetchall()}
    monthly = [["month", *[TYPE_LABELS[t] for t in TYPES]]] + [
        [m, *[counts.get((m, t), 0) for t in TYPES]] for m in months]

    companies = [["company", "events"]] + [
        [r[0] or r[1], r[2]] for r in q(
            "SELECT company, company_key, n_events FROM v_companies"
            " ORDER BY n_events DESC, latest_event DESC LIMIT ?", (N_COMPANIES,)).fetchall()]

    latest = [list(r) for r in q(
        "SELECT event_date, event_type, COALESCE(detail, ''), COALESCE(drug, ''),"
        " COALESCE(sponsor, ''), COALESCE(headline, ''), n_reports, first_url"
        " FROM v_events ORDER BY event_date DESC, event_key LIMIT ?", (N_LATEST,)).fetchall()]

    return {"kpis": kpis, "monthly": monthly, "companies": companies, "latest": latest,
            "total": total}


def legend_grid() -> list[list[str]]:
    """LEGEND as sheet rows: title, then one row per entry (term, blank, meaning)."""
    rows = [[LEGEND[0][0]]]
    for term, meaning in LEGEND[1:]:
        rows.append([term, "", meaning] if (term or meaning) else [""])
    return rows


def _hyperlink(url: str | None) -> str:
    if not url:
        return ""
    return '=HYPERLINK("{}", "open")'.format(url.replace('"', "%22"))


def _a1(row: int, col: int) -> str:
    """0-based (row, col) -> A1."""
    letters, c = "", col + 1
    while c:
        c, rem = divmod(c - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row + 1}"


def _grid(sheet_id: int, r0: int, r1: int, c0: int, c1: int) -> dict:
    return {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
            "startColumnIndex": c0, "endColumnIndex": c1}


def chart_requests(sheet_id: int, n_months: int, n_companies: int) -> list[dict]:
    m0, m1 = 0, n_months + 1          # header + months, at column DATA_COL..
    c0, c1 = 0, n_companies + 1       # header + companies, at column DATA_COL + 6..
    mcol, ccol = DATA_COL, DATA_COL + 6
    monthly = {"addChart": {"chart": {
        "spec": {"title": "Events per month, last 12 months",
                 "basicChart": {
                     "chartType": "COLUMN", "stackedType": "STACKED",
                     "legendPosition": "BOTTOM_LEGEND", "headerCount": 1,
                     "axis": [{"position": "BOTTOM_AXIS", "title": "month"},
                              {"position": "LEFT_AXIS", "title": "events"}],
                     "domains": [{"domain": {"sourceRange": {"sources": [
                         _grid(sheet_id, m0, m1, mcol, mcol + 1)]}}}],
                     "series": [{"series": {"sourceRange": {"sources": [
                         _grid(sheet_id, m0, m1, mcol + 1 + i, mcol + 2 + i)]}},
                         "targetAxis": "LEFT_AXIS"} for i in range(len(TYPES))]}},
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": sheet_id, "rowIndex": CHART_ROW, "columnIndex": 0},
            "widthPixels": 640, "heightPixels": 340}}}}}
    companies = {"addChart": {"chart": {
        "spec": {"title": f"Top {N_COMPANIES} companies by events",
                 "basicChart": {
                     "chartType": "BAR", "legendPosition": "NO_LEGEND", "headerCount": 1,
                     "axis": [{"position": "BOTTOM_AXIS", "title": "events"}],
                     "domains": [{"domain": {"sourceRange": {"sources": [
                         _grid(sheet_id, c0, c1, ccol, ccol + 1)]}}}],
                     "series": [{"series": {"sourceRange": {"sources": [
                         _grid(sheet_id, c0, c1, ccol + 1, ccol + 2)]}},
                         "targetAxis": "BOTTOM_AXIS"}]}},
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": sheet_id, "rowIndex": CHART_ROW, "columnIndex": 7},
            "widthPixels": 520, "heightPixels": 340}}}}}
    return [monthly, companies]


def format_requests(sheet_id: int, n_latest: int) -> list[dict]:
    def cells(r0, r1, c0, c1, fmt, fields):
        return {"repeatCell": {"range": _grid(sheet_id, r0, r1, c0, c1),
                               "cell": {"userEnteredFormat": fmt}, "fields": fields}}
    header_bg = {"red": 0.93, "green": 0.93, "blue": 0.96}
    widths = [90, 170, 150, 160, 170, 420, 60, 50]
    reqs = [
        cells(0, 1, 0, 1, {"textFormat": {"bold": True, "fontSize": 16}}, "userEnteredFormat.textFormat"),
        cells(1, 2, 0, 1, {"textFormat": {"italic": True, "foregroundColor": {"red": .4, "green": .4, "blue": .4}}},
              "userEnteredFormat.textFormat"),
        cells(KPI_ROW, KPI_ROW + 1, 0, 5, {"textFormat": {"bold": True}, "backgroundColor": header_bg},
              "userEnteredFormat(textFormat,backgroundColor)"),
        cells(KPI_ROW + 1, KPI_ROW + 2, 0, 5, {"textFormat": {"bold": True, "fontSize": 20},
                                               "horizontalAlignment": "LEFT"},
              "userEnteredFormat(textFormat,horizontalAlignment)"),
        cells(KPI_ROW + 2, KPI_ROW + 3, 0, 5, {"textFormat": {"foregroundColor": {"red": .35, "green": .35, "blue": .35}}},
              "userEnteredFormat.textFormat"),
        cells(TABLE_ROW - 1, TABLE_ROW, 0, 1, {"textFormat": {"bold": True, "fontSize": 12}},
              "userEnteredFormat.textFormat"),
        cells(TABLE_ROW, TABLE_ROW + 1, 0, len(TABLE_HEADER), {"textFormat": {"bold": True}, "backgroundColor": header_bg},
              "userEnteredFormat(textFormat,backgroundColor)"),
        cells(TABLE_ROW + 1, TABLE_ROW + 1 + n_latest, 5, 6, {"wrapStrategy": "CLIP"}, "userEnteredFormat.wrapStrategy"),
        cells(0, 60, DATA_COL, DATA_COL + 9, {"textFormat": {"foregroundColor": {"red": .6, "green": .6, "blue": .6}}},
              "userEnteredFormat.textFormat"),
    ]
    # Legend: title row, then one row per entry (term bold).
    n = len(legend_grid())
    reqs += [
        cells(LEGEND_ROW, LEGEND_ROW + 1, 0, 1, {"textFormat": {"bold": True, "fontSize": 12}},
              "userEnteredFormat.textFormat"),
        cells(LEGEND_ROW + 1, LEGEND_ROW + n, 0, 8, {"wrapStrategy": "OVERFLOW_CELL"},
              "userEnteredFormat.wrapStrategy"),
        cells(LEGEND_ROW + 1, LEGEND_ROW + n, 0, 1, {"textFormat": {"bold": True}},
              "userEnteredFormat.textFormat"),
    ]
    for i, w in enumerate(widths):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize"}})
    return reqs


def write(sh, store: Store, synced_at: str, today: date | None = None) -> int:
    """Rebuild the dashboard tab. Returns the number of events it summarises."""
    import gspread
    d = build(store, today)
    try:
        ws = sh.worksheet(DASHBOARD_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=DASHBOARD_TAB, rows=80, cols=30, index=0)

    # Delete the previous run's charts before writing new ones.
    meta = sh.fetch_sheet_metadata()
    old = [c["chartId"] for s in meta.get("sheets", []) if s["properties"]["sheetId"] == ws.id
           for c in s.get("charts", [])]
    if old:
        sh.batch_update({"requests": [{"deleteEmbeddedObject": {"objectId": cid}} for cid in old]})

    ws.batch_clear(["A1:AZ200"])
    head = [["Failed clinical trial tracker"],
            [f"Last sync {synced_at} UTC · {d['total']} events (excluded rows hidden) · "
             "edit corrections in the events tab"]]
    kpi = [[k[0] for k in d["kpis"]], [k[1] for k in d["kpis"]],
           [f"+{k[2]} in last 30 days" for k in d["kpis"]]]
    table = [TABLE_HEADER] + [r[:7] + [""] for r in d["latest"]]
    links = [[_hyperlink(r[7])] for r in d["latest"]]
    ws.batch_update([
        {"range": "A1", "values": head},
        {"range": _a1(KPI_ROW, 0), "values": kpi},
        {"range": _a1(TABLE_ROW - 1, 0), "values": [[f"Latest {N_LATEST} events"]]},
        {"range": _a1(TABLE_ROW, 0), "values": table},
        {"range": _a1(0, DATA_COL), "values": d["monthly"]},
        {"range": _a1(0, DATA_COL + 6), "values": d["companies"]},
        {"range": _a1(LEGEND_ROW, 0), "values": legend_grid()},
    ], raw=True)
    if links:  # only the link column is parsed (as HYPERLINK formulas)
        ws.update(values=links, range_name=_a1(TABLE_ROW + 1, 7), raw=False)

    sh.batch_update({"requests": format_requests(ws.id, len(d["latest"]))
                     + chart_requests(ws.id, len(d["monthly"]) - 1, len(d["companies"]) - 1)})
    return d["total"]
