"""Two-way sync between SQLite (the source of truth) and a shared Google Sheet.

1. Pull: read the human columns (corrected_type, exclude, notes) of the `events` tab and
   store them in the `edits` table. Rows are matched by event_key; a key that was merged
   into another event carries its edit over to the canonical event.
2. Push: rewrite the `events`, `companies` and `monthly` tabs from the v_* views, with the
   human columns filled from `edits`, so a correction survives every later sync; then
   rebuild the `dashboard` tab (dashboard.py).

Config: SCOUT_SHEET_ID (the id in the sheet's URL) and
SCOUT_GOOGLE_CREDENTIALS (path to the service-account JSON key)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import get_args

from . import dashboard
from .db import Store
from .models import EventType

log = logging.getLogger(__name__)

EVENTS_TAB, COMPANIES_TAB, MONTHLY_TAB, ABOUT_TAB = "events", "companies", "monthly", "about"
HUMAN_COLUMNS = ["corrected_type", "exclude", "notes"]
# Columns shown in the events tab, in order (human columns last, after a visual gap).
EVENT_COLUMNS = [
    "event_key", "event_date", "month", "event_type", "detail", "partial", "drug", "sponsor",
    "indication", "phase", "nct_id", "origin", "n_reports", "headline", "publisher", "first_url",
    "evidence_quote", "confidence", "quote_verified", "unverified_fields", "registry_match",
    "registry_status", "registry_why_stopped", "registry_stop_date", "registry_conflict",
    "model_event_type", *HUMAN_COLUMNS,
]
VALID_TYPES = [t for t in get_args(EventType) if t != "not_an_event"]
TRUE = {"true", "yes", "1", "x", "y", "✓"}


class SyncError(RuntimeError):
    pass


# ---------------------------------------------------------------- pure helpers (tested offline)


def pull_edits(store: Store, values: list[list[str]]) -> tuple[int, list[str]]:
    """Apply the human columns of an events-tab grid to `edits`.
    Returns (rows that carry an edit, warnings)."""
    if not values:
        return 0, []
    header = [h.strip() for h in values[0]]
    if "event_key" not in header:
        return 0, ["events tab has no event_key column; nothing pulled"]
    col = {name: header.index(name) for name in ["event_key", *HUMAN_COLUMNS] if name in header}
    warnings, applied = [], 0

    def cell(row, name):
        i = col.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    for n, row in enumerate(values[1:], start=2):
        raw_key = cell(row, "event_key")
        if not raw_key:
            continue
        key = store.canonical_key(raw_key)
        if key is None:
            warnings.append(f"row {n}: unknown event_key {raw_key!r}, skipped")
            continue
        corrected = cell(row, "corrected_type") or None
        if corrected and corrected not in VALID_TYPES:
            warnings.append(f"row {n}: corrected_type {corrected!r} is not one of {VALID_TYPES}; ignored")
            corrected = None
        exclude = cell(row, "exclude").lower() in TRUE
        notes = cell(row, "notes") or None
        store.set_edit(key, corrected, exclude, notes)  # all empty = edit cleared in the sheet
        applied += bool(corrected or exclude or notes)
    store.commit()
    return applied, warnings


def _cell(v):
    if v is None:
        return ""
    return v


def events_grid(store: Store) -> list[list]:
    names, rows = store.view("v_events_all", order_by="event_date DESC, event_key")
    idx = [names.index(c) for c in EVENT_COLUMNS]
    out = [EVENT_COLUMNS]
    for r in rows:
        vals = [_cell(r[i]) for i in idx]
        vals[EVENT_COLUMNS.index("exclude")] = bool(r[names.index("exclude")])
        for flag in ("partial", "quote_verified", "registry_conflict"):
            vals[EVENT_COLUMNS.index(flag)] = bool(r[names.index(flag)])
        out.append(vals)
    return out


def view_grid(store: Store, view: str, order_by: str) -> list[list]:
    names, rows = store.view(view, order_by=order_by)
    return [names] + [[_cell(v) for v in r] for r in rows]


# ---------------------------------------------------------------- Google Sheets I/O


def _open(sheet_id: str, creds_path: str):
    try:
        import gspread
    except ImportError as e:
        raise SyncError("gspread is not installed: uv pip install --python .venv/bin/python gspread") from e
    if not os.path.exists(creds_path):
        raise SyncError(f"credentials file not found: {creds_path} (see Usage in README.md)")
    try:
        return gspread.service_account(filename=creds_path).open_by_key(sheet_id)
    except Exception as e:  # gspread raises several types; all mean "can't reach this sheet"
        raise SyncError(f"cannot open sheet {sheet_id}: {e}. Is it shared with the service "
                        "account's email as Editor?") from e


def _tab(sh, title: str, rows: int, cols: int):
    import gspread
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=max(rows, 10), cols=max(cols, 5)), True
    if ws.row_count < rows or ws.col_count < cols:
        ws.resize(rows=max(ws.row_count, rows), cols=max(ws.col_count, cols))
    return ws, False


def _write(ws, grid: list[list]) -> None:
    ws.batch_clear(["A:ZZ"])
    ws.update(values=grid, range_name="A1")  # raw=True: text is never parsed as a formula
    ws.freeze(rows=1)


def _format_events_tab(sh, ws, n_rows: int) -> None:
    """Dropdown for corrected_type, checkbox for exclude, warning on machine columns."""
    first_human = EVENT_COLUMNS.index("corrected_type")
    ct, ex = first_human, EVENT_COLUMNS.index("exclude")
    rng = lambda c0, c1: {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": max(n_rows, 2),
                          "startColumnIndex": c0, "endColumnIndex": c1}
    requests = [
        {"setDataValidation": {"range": rng(ct, ct + 1), "rule": {
            "condition": {"type": "ONE_OF_LIST",
                          "values": [{"userEnteredValue": t} for t in VALID_TYPES]},
            "showCustomUi": True, "strict": False}}},
        {"setDataValidation": {"range": rng(ex, ex + 1), "rule": {"condition": {"type": "BOOLEAN"}}}},
        {"repeatCell": {"range": {**rng(first_human, len(EVENT_COLUMNS)), "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 0.95, "blue": 0.7}}},
                        "fields": "userEnteredFormat.backgroundColor"}},
    ]
    # Machine columns: warn (not block) before editing; they are rewritten on every sync.
    existing = sh.fetch_sheet_metadata().get("sheets", [])
    has_protection = any(p.get("description") == "scout-machine-columns"
                         for s in existing if s["properties"]["sheetId"] == ws.id
                         for p in s.get("protectedRanges", []))
    if not has_protection:
        requests.append({"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": ws.id, "startColumnIndex": 0, "endColumnIndex": first_human},
            "description": "scout-machine-columns", "warningOnly": True}}})
    sh.batch_update({"requests": requests})


def sync(store: Store, sheet_id: str | None = None, creds_path: str | None = None) -> str:
    sheet_id = sheet_id or os.environ.get("SCOUT_SHEET_ID")
    creds_path = creds_path or os.environ.get("SCOUT_GOOGLE_CREDENTIALS",
                                              os.path.expanduser("~/.config/failure-scout/google.json"))
    if not sheet_id:
        raise SyncError("SCOUT_SHEET_ID is not set (see Usage in README.md)")
    sh = _open(sheet_id, creds_path)

    # 1. Pull human edits first, so the push below writes them back unchanged.
    applied, warnings = 0, []
    try:
        applied, warnings = pull_edits(store, sh.worksheet(EVENTS_TAB).get_all_values())
    except Exception as e:  # missing tab on first sync is normal
        if type(e).__name__ != "WorksheetNotFound":
            raise
    for w in warnings:
        log.warning("sheet: %s", w)

    # 2. Push fresh tabs.
    grid = events_grid(store)
    ws, _ = _tab(sh, EVENTS_TAB, len(grid) + 50, len(EVENT_COLUMNS))
    _write(ws, grid)
    _format_events_tab(sh, ws, len(grid) + 50)

    companies = view_grid(store, "v_companies", "n_events DESC, latest_event DESC")
    ws, _ = _tab(sh, COMPANIES_TAB, len(companies) + 10, len(companies[0]))
    _write(ws, companies)

    monthly = view_grid(store, "v_monthly", "month DESC, event_type")
    ws, _ = _tab(sh, MONTHLY_TAB, len(monthly) + 10, len(monthly[0]))
    _write(ws, monthly)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    dashboard.write(sh, store, now.replace("T", " ").replace("+00:00", ""))

    about = [
        ["failure-scout sheet", ""],
        ["last sync (UTC)", now],
        ["events", len(grid) - 1],
        ["", ""],
        ["How to edit", "Only the yellow columns of the events tab are kept: corrected_type "
                        "(dropdown), exclude (checkbox), notes. All other columns are rewritten "
                        "from the database on every sync."],
        ["Dashboard", "The dashboard tab is rebuilt on every sync from non-excluded events; "
                      "anything typed there is overwritten."],
        ["Filtering", "events tab: Data > Create a filter, then filter any column "
                      "(event_type, sponsor, month ...)."],
    ]
    ws, _ = _tab(sh, ABOUT_TAB, 20, 2)
    _write(ws, about)

    return (f"sheet synced: {len(grid) - 1} events, {len(companies) - 1} companies; "
            f"{applied} rows of edits pulled" + (f", {len(warnings)} warnings" if warnings else ""))
