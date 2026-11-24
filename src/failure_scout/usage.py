"""Local record of every Claude API call: step, tokens, estimated cost, request id.

Collected in memory (thread-safe, classification runs in threads) and written to the
`llm_calls` table by the main thread at the end of a run."""

from __future__ import annotations

import threading
from dataclasses import dataclass

# USD per million tokens (input, output), first-party API, prompts under 100K tokens.
# Estimate only: check current prices before relying on the totals.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
CACHE_READ_FACTOR = 0.1  # cache reads ~10% of the input price
CACHE_WRITE_FACTOR = 1.25  # 5-minute cache writes ~125% of the input price


@dataclass
class LlmCall:
    step: str  # "planner" | "classify" | "merge"
    model: str  # model that served the request (may differ after a fallback)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    stop_reason: str | None = None
    request_id: str | None = None
    error: str | None = None

    @property
    def cost_usd(self) -> float | None:
        price = next((p for k, p in PRICES.items() if self.model.startswith(k)), None)
        if price is None:
            return None
        p_in, p_out = price
        return (
            self.input_tokens * p_in
            + self.cache_read_tokens * p_in * CACHE_READ_FACTOR
            + self.cache_write_tokens * p_in * CACHE_WRITE_FACTOR
            + self.output_tokens * p_out
        ) / 1e6


class UsageLog:
    def __init__(self) -> None:
        self.calls: list[LlmCall] = []
        self._lock = threading.Lock()

    def record(self, step: str, model: str, resp=None, error: Exception | None = None) -> None:
        call = LlmCall(step=step, model=model)
        if resp is not None:
            u = resp.usage
            call.model = getattr(resp, "model", None) or model
            call.input_tokens = u.input_tokens or 0
            call.output_tokens = u.output_tokens or 0
            call.cache_read_tokens = getattr(u, "cache_read_input_tokens", None) or 0
            call.cache_write_tokens = getattr(u, "cache_creation_input_tokens", None) or 0
            call.stop_reason = resp.stop_reason
            call.request_id = getattr(resp, "_request_id", None)
        if error is not None:
            call.error = f"{type(error).__name__}: {error}"[:500]
            call.request_id = getattr(error, "request_id", None)
        with self._lock:
            self.calls.append(call)

    def total_cost(self) -> float:
        return sum(c.cost_usd or 0.0 for c in self.calls)

    def summary(self) -> str:
        by_step: dict[str, list[LlmCall]] = {}
        for c in self.calls:
            by_step.setdefault(c.step, []).append(c)
        parts = []
        for step, calls in by_step.items():
            cost = sum(c.cost_usd or 0.0 for c in calls)
            errs = sum(1 for c in calls if c.error)
            tokens = sum(c.input_tokens + c.output_tokens for c in calls)
            parts.append(f"{step}: {len(calls)} calls, {tokens} tokens, ${cost:.4f}"
                         + (f", {errs} errors" if errs else ""))
        return "; ".join(parts) or "no API calls"
