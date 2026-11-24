"""plan_query + search nodes: a hand-written tool-use loop (docs/design-details.md, Pipeline steps, step 1).

1. Code pulls the full sources without keywords: every stopped drug trial on
   ClinicalTrials.gov in the window (all pages) and every article in the publisher feeds.
2. Code runs FIXED_GNEWS_QUERIES (one or two Google News queries per event type): Google News caps
   each query at ~10-20 results, so it can't be pulled in full.
3. The model may add up to MAX_EXTRA_QUERIES_PER_SOURCE Google News queries for gaps it sees.
Classification happens later, on everything fetched."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import anthropic

from .models import NewsItem
from .sources import FIXED_GNEWS_QUERIES, PLANNER_SOURCES, SOURCES
from .usage import UsageLog

log = logging.getLogger(__name__)

MAX_RETRIES_PER_SOURCE = 2
MAX_EXTRA_QUERIES_PER_SOURCE = 2  # model-chosen searches with results, per source
MAX_LLM_CALLS = 8  # circuit breaker for the whole planning loop

SEARCH_TOOL = {
    "name": "search_news",
    "description": (
        "Search Google News for failed-trial events in the run's date window (the window is "
        "applied automatically). Returns the number of results and a few sample titles. "
        "`query` uses Google search syntax: quoted phrases, OR between alternatives, e.g. "
        "'\"did not meet\" primary endpoint phase 2'. Results are capped at ~10-20 per query, "
        "so keep each query on one event type."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "enum": PLANNER_SOURCES},
            "query": {"type": "string"},
        },
        "required": ["source", "query"],
        "additionalProperties": False,
    },
}

SYSTEM = f"""You plan searches for a tool that collects failed clinical trial events into a database. Event types wanted: trials that missed their primary endpoint or stopped for futility or safety; FDA clinical holds; FDA complete response letters (any reason); programs discontinued for business reasons. Drugs and biologics only, not medical devices.

ClinicalTrials.gov and the publisher feeds have already been fetched in full, and one or two Google News searches per event type have already been run (two for trial failures: missed endpoint, and stopped for futility or safety); the user message lists them with result counts and sample titles.

You may run up to {MAX_EXTRA_QUERIES_PER_SOURCE} more Google News searches, only for an event type whose fixed search looks thin or off-topic. If nothing looks thin, run none. If a search returns 0 results, try different keywords. When done, stop calling tools and reply with one line summarising what you added."""


@dataclass
class SearchLog:
    since: date
    until: date
    items: dict[str, NewsItem] = field(default_factory=dict)  # url -> item
    queries: list[tuple[str, str, int]] = field(default_factory=list)  # (source, query, n)
    empty_retries: dict[str, int] = field(default_factory=dict)
    extra: dict[str, int] = field(default_factory=dict)  # model searches with results, per source

    def searched(self, source: str) -> bool:
        return any(s == source for s, _, _ in self.queries)

    def _search(self, source: str, query: str) -> str:
        found = SOURCES[source](query, self.since, self.until)
        new = 0
        for it in found:
            if it.url not in self.items:
                self.items[it.url] = it
                new += 1
        self.queries.append((source, query, len(found)))
        sample = "\n".join(f"- {it.title}" for it in found[:5])
        return f"{len(found)} results ({new} new this run).\n{sample}".strip()

    def run_fixed(self, label: str, source: str, query: str) -> str:
        """Code-chosen search: no caps."""
        return f"[{label}] {source}: {query}\n" + self._search(source, query)

    def run(self, source: str, query: str) -> str:
        """Model-chosen search, with caps."""
        if source not in PLANNER_SOURCES:
            raise ValueError(f"unknown source {source!r}")
        if self.extra.get(source, 0) >= MAX_EXTRA_QUERIES_PER_SOURCE:
            raise ValueError(f"{source}: limit of {MAX_EXTRA_QUERIES_PER_SOURCE} extra searches reached; stop")
        result = self._search(source, query)
        if self.queries[-1][2] == 0:
            self.empty_retries[source] = self.empty_retries.get(source, 0) + 1
            if self.empty_retries[source] > MAX_RETRIES_PER_SOURCE:
                self.extra[source] = MAX_EXTRA_QUERIES_PER_SOURCE  # give up on this source
        else:
            self.extra[source] = self.extra.get(source, 0) + 1
        return result


def plan_and_search(client: anthropic.Anthropic, model: str, since: date, until: date,
                    usage: UsageLog) -> SearchLog:
    slog = SearchLog(since=since, until=until)
    slog.run_fixed("all stopped drug trials", "ctgov", "")
    slog.run_fixed("all feed articles", "rss", "")
    fixed = [slog.run_fixed(label, "gnews", q) for label, q in FIXED_GNEWS_QUERIES.items()]
    messages = [{
        "role": "user",
        "content": (f"Date window: {since.isoformat()} to {until.isoformat()}.\n\n"
                    "Fixed Google News searches already run:\n\n" + "\n\n".join(fixed)
                    + "\n\nNow run your searches."),
    }]

    # Server-side refusal fallback exists for Opus / Sonnet / Fable, not Haiku.
    fallback_args = {} if "haiku" in model else {
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",  # re-run on a fallback model if this one declines
    }

    for _ in range(MAX_LLM_CALLS):
        try:
            resp = client.beta.messages.create(
                model=model,
                max_tokens=16000,
                system=SYSTEM,
                tools=[SEARCH_TOOL],
                messages=messages,
                output_config={"effort": "low"},
                **fallback_args,
            )
        except anthropic.APIError as e:
            usage.record("planner", model, error=e)
            log.warning("planner call failed: %s", e)
            break
        usage.record("planner", model, resp=resp)

        if resp.stop_reason in ("refusal", "max_tokens"):
            log.warning("planner stopped: %s", resp.stop_reason)
            break
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            break  # end_turn: the model thinks it's done

        results = []
        for tu in tool_uses:  # every tool_use gets a result, in one user message
            try:
                content, is_error = slog.run(tu.input["source"], tu.input["query"]), False
            except (ValueError, KeyError) as e:
                content, is_error = f"Error: {e}", True
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    # Every source was already searched by code above, so the model skipping its turn is fine.
    return slog
