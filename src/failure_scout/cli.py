"""Entry point.

  scout run [--since YYYY-MM-DD] [--until YYYY-MM-DD]   fetch from the web -> classify -> SQLite
  scout run --offline                                   same, using fixtures/examples.json only
  scout list [--type trial_failure]                     show stored events
  scout list --urls                                     ... with each event's first report URL
  scout show NCT04676360                                one event: all fields, verification, URLs
  scout companies [--min 2]                             companies with event counts by type
  scout list --company Roche                            events of one company
  scout merge                                           re-run duplicate merging over the whole DB
  scout sync                                            Google Sheet: pull human edits, push events
                                                        (scout run does this too when SCOUT_SHEET_ID is set)
  scout runs                                      past runs with API calls and estimated cost
  scout eval                                           classifier accuracy on the labelled fixtures
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, timedelta

import anthropic

from .classify import classify_all
from .db import Store
from .models import NewsItem
from .planner import plan_and_search
from .sources import fixture_item, load_fixtures, search_fixture
from .usage import UsageLog
from .companies import company_key
from .merge import merge_events
from .sync import SyncError, sync
from .verify import verify_all

PLANNER_MODEL = os.environ.get("SCOUT_PLANNER_MODEL", "claude-haiku-5-5")
CLASSIFIER_MODEL = os.environ.get("SCOUT_MODEL", "claude-haiku-5-5")
DEFAULT_DB = os.environ.get("SCOUT_DB", "scout.db")
MAX_ITEMS_PER_RUN = 500  # cost circuit breaker for classification (~$0.10 at Haiku prices)
FIRST_RUN_LOOKBACK_DAYS = 7


def _date(s: str) -> date:
    return date.fromisoformat(s)


def cmd_run(args: argparse.Namespace) -> None:
    store = Store(args.db)
    until = args.until or date.today()
    if args.since:
        since = args.since
    elif store.last_until():
        since = _date(store.last_until())
    else:
        since = until - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
    if since > until:
        sys.exit(f"--since {since} is after --until {until}")

    client = anthropic.Anthropic()
    usage = UsageLog()
    run_id = store.start_run(since.isoformat(), until.isoformat(),
                             "offline" if args.offline else PLANNER_MODEL, CLASSIFIER_MODEL)
    print(f"run {run_id}: {since} .. {until}")

    if args.offline:
        fetched = search_fixture("", since, until)
        queries = [("fixture", "", len(fetched))]
    else:
        slog = plan_and_search(client, PLANNER_MODEL, since, until, usage)
        fetched, queries = list(slog.items.values()), slog.queries
        for source, q, n in queries:
            print(f"  search {source:<6} {n:>3}  {q}")

    new_items = store.unseen(fetched)
    if len(new_items) > MAX_ITEMS_PER_RUN:
        print(f"  {len(new_items)} new items; classifying the first {MAX_ITEMS_PER_RUN} "
              "(the rest stay unseen for the next run)")
        new_items = new_items[:MAX_ITEMS_PER_RUN]

    results = classify_all(client, CLASSIFIER_MODEL, new_items, usage)
    store.save_calls(run_id, usage.calls)
    events = [(item, ev) for item, ev in results if ev is not None]
    verify_all(events)
    n_new_events = 0
    types = Counter()
    for item, ev in results:
        if ev is not None:
            n_new_events += store.upsert_event(ev, run_id)
            types[ev.event_type] += 1
        store.mark_seen(item, run_id, ev.event_key if ev else None)
    store.commit()
    store.finish_run(run_id, len(fetched), len(new_items), n_new_events, queries)

    unclassified = len(new_items) - len(results)
    print(f"  fetched {len(fetched)}, new {len(new_items)}, classified {len(results)}"
          + (f" ({unclassified} failed, retried next run)" if unclassified else ""))
    print(f"  events in reports: {dict(types) or 'none'}; new events stored: {n_new_events}")
    matches = Counter(ev.registry_match for _, ev in events)
    n_fields = sum(1 for _, ev in events if ev.unverified_fields)
    n_conflict = sum(1 for _, ev in events if ev.registry_conflict)
    print(f"  verify: registry {dict(matches) or 'none'}; unverified fields in {n_fields}; "
          f"registry conflicts {n_conflict}")
    n_calls_before = len(usage.calls)
    n_merged, n_groups = merge_events(store, client, CLASSIFIER_MODEL, usage, since, until)
    store.save_calls(run_id, usage.calls[n_calls_before:])
    print(f"  merge: {n_merged} duplicate reports folded into {n_groups} events")
    print(f"  API: {usage.summary()} (cost is an estimate)")
    if not args.no_sync and os.environ.get("SCOUT_SHEET_ID"):
        _sync_and_report(store)
    store.close()


def _sync_and_report(store: Store) -> None:
    try:
        print(f"  {sync(store)}")
    except SyncError as e:
        print(f"  sheet sync skipped: {e}")


def cmd_sync(args: argparse.Namespace) -> None:
    store = Store(args.db)
    try:
        print(sync(store))
    except SyncError as e:
        sys.exit(f"sync failed: {e}")
    finally:
        store.close()


def cmd_merge(args: argparse.Namespace) -> None:
    """Re-run merging over the whole database (runs also merge their own window)."""
    store = Store(args.db)
    usage = UsageLog()
    n_merged, n_groups = merge_events(store, anthropic.Anthropic(), CLASSIFIER_MODEL, usage)
    store.save_calls(None, usage.calls)
    print(f"merge: {n_merged} duplicate reports folded into {n_groups} events")
    print(f"API: {usage.summary()} (cost is an estimate)")
    store.close()


def cmd_companies(args: argparse.Namespace) -> None:
    store = Store(args.db)
    print(f"{'company':<34} {'events':>6} {'fail':>5} {'hold':>5} {'CRL':>5} {'biz':>5}  latest      drugs")
    for r in store.list_companies(args.min):
        names = {d.strip() for d in (r["drugs"] or "").split(",") if d.strip()}
        drugs = ", ".join(sorted(names, key=str.lower))[:60]
        print(f"{(r['name'] or r['company_key'])[:34]:<34} {r['n']:>6} {r['n_trial_failure']:>5} "
              f"{r['n_clinical_hold']:>5} {r['n_regulatory_rejection']:>5} {r['n_business']:>5}  "
              f"{r['latest'] or '':<10}  {drugs}")
    print("\nEvents of one company: scout list --company <name>")
    store.close()


def cmd_runs(args: argparse.Namespace) -> None:
    store = Store(args.db)
    for r in store.list_runs(args.limit):
        errs = f"  {r['n_errors']} API errors" if r["n_errors"] else ""
        print(f"run {r['id']:>3}  {r['started_at'][:16]}  {r['since']}..{r['until']}  "
              f"fetched {r['n_fetched'] or 0:>3}  new {r['n_new'] or 0:>3}  "
              f"events {r['n_events'] or 0:>3}  calls {r['n_calls']:>3}  ${r['cost']:.4f}{errs}")
    store.close()


REGISTRY_MARK = {"source": "reg", "nct": "reg", "drug": "reg~", "drug_ambiguous": "reg?",
                 "not_found": "-", "skipped": "n/a", "error": "err", "pending": "",
                 None: ""}


def _flags(r) -> str:
    flags = []
    if not r["quote_verified"]:
        flags.append("quote?")
    bad = json.loads(r["unverified_fields"] or "[]")
    if bad:
        flags.append(",".join(bad) + "?")
    if r["registry_conflict"]:
        flags.append("REGISTRY CONFLICT")
    return f" [{'; '.join(flags)}]" if flags else ""


def cmd_list(args: argparse.Namespace) -> None:
    store = Store(args.db)
    company = company_key(args.company) if args.company else None
    for r in store.list_events(args.type, args.limit, company):
        detail = r["failure_type"] or r["crl_reason"] or ""
        reg = REGISTRY_MARK.get(r["registry_match"], "")
        print(f"{r['event_date'] or '????-??-??'}  {r['event_type']:<24} {detail:<23} "
              f"{(r['drug'] or '-')[:26]:<26} {(r['sponsor'] or '-')[:22]:<22} "
              f"c={r['confidence']:<3} {reg:<4}{_flags(r)}")
        if args.urls:
            print(f"            key={r['event_key']}  {json.loads(r['source_urls'])[0]}")
    store.close()


def cmd_show(args: argparse.Namespace) -> None:
    store = Store(args.db)
    r = store.get_event(args.key)
    if r is None:
        sys.exit(f"no event {args.key!r}; keys are shown by `scout list --urls`")
    if r["merged_into"]:
        print(f"(duplicate report, merged into {r['merged_into']})\n")
        r = store.get_event(r["merged_into"])
    for f in ("event_key", "event_type", "failure_type", "crl_reason", "partial", "drug",
              "sponsor", "indication", "phase", "nct_id", "event_date", "confidence",
              "evidence_quote", "quote_verified", "unverified_fields"):
        print(f"{f:<22} {r[f]}")
    print("\nregistry (ClinicalTrials.gov)")
    for f in ("registry_match", "registry_nct", "registry_status", "registry_why_stopped",
              "registry_stop_date", "registry_candidates", "registry_conflict"):
        print(f"  {f:<20} {r[f]}")
    if r["registry_nct"]:
        print(f"  {'url':<20} https://clinicaltrials.gov/study/{r['registry_nct']}")
    print("\nreports")
    pubs = json.loads(r["publisher_urls"] or "null") or []
    for i, url in enumerate(json.loads(r["source_urls"])):
        pub = pubs[i] if i < len(pubs) and pubs[i] else ""
        print(f"  {url}" + (f"\n    publisher: {pub}" if pub else ""))
    store.close()


def cmd_eval(args: argparse.Namespace) -> None:
    rows = load_fixtures()
    items = [fixture_item(r) for r in rows]
    by_url = {r["url"]: r for r in rows}
    usage = UsageLog()
    results = classify_all(anthropic.Anthropic(), CLASSIFIER_MODEL, items, usage)

    # field -> [n_correct, n_scored]; secondary fields are scored only where `expect` gives them
    score = {f: [0, 0] for f in ("event_type", "failure_type", "crl_reason", "partial", "quote_verified")}
    for item, ev in results:
        row = by_url[item.url]
        got_type = ev.event_type if ev else "not_an_event"
        misses = []
        checks = {"event_type": (got_type, row["label"])}
        for f, want in row.get("expect", {}).items():
            checks[f] = (getattr(ev, f) if ev else None, want)
        if ev:
            checks["quote_verified"] = (ev.quote_verified, True)
        for f, (got, want) in checks.items():
            score[f][1] += 1
            if got == want:
                score[f][0] += 1
            else:
                misses.append(f"{f}: want {want}, got {got}")
        mark = "ok " if not misses else "BAD"
        print(f"{mark} {got_type:<24} {item.title[:55]}" + ("".join(f"\n      {m}" for m in misses)))

    print()
    for f, (ok, n) in score.items():
        print(f"{f:<15} {ok}/{n}")
    if len(results) < len(items):
        print(f"({len(items) - len(results)} items not classified)")
    print(f"API: {usage.summary()} (cost is an estimate)")


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="scout", description="Collect failed clinical trial events.")
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file (default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="fetch, classify and store")
    r.add_argument("--since", type=_date, help="default: end of last run, or 7 days ago")
    r.add_argument("--until", type=_date, help="default: today")
    r.add_argument("--offline", action="store_true", help="use fixtures instead of the web")
    r.add_argument("--no-sync", action="store_true",
                   help="don't update the Google Sheet (it syncs when SCOUT_SHEET_ID is set)")
    r.set_defaults(func=cmd_run)

    ls = sub.add_parser("list", help="show stored events")
    ls.add_argument("--type", help="filter by event_type")
    ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--urls", action="store_true", help="print each event's first report URL")
    ls.add_argument("--company", help="only this company (any spelling, e.g. 'Roche')")
    ls.set_defaults(func=cmd_list)

    co = sub.add_parser("companies", help="companies with their event counts")
    co.add_argument("--min", type=int, default=1, help="only companies with at least N events")
    co.set_defaults(func=cmd_companies)

    mg = sub.add_parser("merge", help="re-run duplicate merging over the whole database")
    mg.set_defaults(func=cmd_merge)

    sy = sub.add_parser("sync", help="two-way sync with the Google Sheet (pull edits, push events)")
    sy.set_defaults(func=cmd_sync)

    sh = sub.add_parser("show", help="all details, verification and URLs of one event")
    sh.add_argument("key", help="event_key, e.g. NCT04676360")
    sh.set_defaults(func=cmd_show)

    rs = sub.add_parser("runs", help="past runs with API calls and estimated cost")
    rs.add_argument("--limit", type=int, default=20)
    rs.set_defaults(func=cmd_runs)

    ev = sub.add_parser("eval", help="classifier accuracy on labelled fixtures")
    ev.set_defaults(func=cmd_eval)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
