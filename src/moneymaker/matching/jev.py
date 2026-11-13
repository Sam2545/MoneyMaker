from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from moneymaker.backoff import backoff_delay
from moneymaker.models import Market

log = structlog.get_logger(__name__)

API_URL = "https://api.typesafe.ai"
PROMPT_VERSION = 3
RULES_CHARS = 6000
DATES_NOTE = (
    "Listing dates from the venue; they can be placeholders. The rules text is authoritative "
    "for timing."
)

DIVERGENCE_KINDS = {
    "cutoff_time": "Different cut-off times or time zones for when the event must happen",
    "resolution_source": "Different data sources or reporting bodies decide the outcome",
    "data_revision": "One uses an initial figure and the other a revised or final figure",
    "postponement": "Postponed, delayed, or rescheduled events are handled differently",
    "overtime": "Overtime, extra time, or penalties are counted differently",
    "tie_handling": "Ties or draws are handled differently",
    "threshold_boundary": "A value exactly at the strike resolves differently",
    "measurement_definition": "The measured quantity is defined or computed differently",
    "announced_vs_occurs": "One requires an announcement and the other the event itself",
    "early_resolution": "One can resolve early and the other waits for a fixed date",
    "scope": "Different participants, places, or entities count toward the outcome",
    "void_or_refund": "One can be voided, cancelled, or settled at a fair price",
    "other": "Some other scenario makes them resolve inconsistently",
    "none": "No scenario makes them resolve inconsistently",
}


def market_state(market: Market) -> dict[str, Any]:
    """Returns the fields of a market that Jev sees."""
    return {
        "venue": str(market.venue),
        "title": market.title,
        "yes_outcome": market.yes_label,
        "no_outcome": market.no_label,
        "rules": market.rules[:RULES_CHARS],
        "listed_close_time": market.close_time.isoformat() if market.close_time else None,
        "listed_expiration_time": (
            market.expiration_time.isoformat() if market.expiration_time else None
        ),
        "dates_note": DATES_NOTE,
    }


def inputs_hash(kalshi: Market, pm: Market) -> str:
    """Returns a hash of everything Jev sees for a pair, so edits invalidate cached answers."""
    blob = json.dumps([market_state(kalshi), market_state(pm)], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _same_event(first: str, second: str) -> dict[str, Any]:
    """Builds the same_event noul with first/second as the market references."""
    return {
        "type": "noul",
        "instructions": (
            f"Do `{first}` and `{second}` settle on the same real-world outcome: the same "
            "subject, the same outcome or value range, and the same settlement moment? Ignore "
            "differences in wording, data source, or calculation method; other questions judge "
            "those. Use each market's rules text to determine the settlement moment; listed "
            "dates can be placeholders."
        ),
        "criteria": {
            "true": "Same subject, same outcome or strike/range, same settlement moment, even if "
            "the data source or calculation method differs",
            "false": "Different subjects, different strikes or ranges, or a different settlement "
            "date or window",
        },
    }


def _polarity(first: str, second: str) -> dict[str, Any]:
    """Builds the polarity choice with first/second as the market references."""
    return {
        "type": "choice",
        "instructions": (
            f"Which outcome of `{second}` is the same real-world outcome as YES on `{first}`? "
            "Judge only which outcomes correspond; another question judges edge cases where they "
            "might settle differently."
        ),
        "criteria": {
            "same": f"YES on `{second}` is the same outcome as YES on `{first}`",
            "inverted": f"NO on `{second}` is the same outcome as YES on `{first}`; YES on "
            f"`{second}` is the opposite outcome",
            "neither": f"No outcome of `{second}` corresponds to YES on `{first}`; they are about "
            "different things",
        },
    }


def build_questions() -> dict[str, Any]:
    """Returns the Jev question set, including swapped-order copies for the consistency check."""
    return {
        "same_event": _same_event("market_a", "market_b"),
        "same_event_swapped": _same_event("market_b", "market_a"),
        "polarity": _polarity("market_a", "market_b"),
        "polarity_swapped": _polarity("market_b", "market_a"),
        "same_source": {
            "type": "noul",
            "instructions": "Do `market_a` and `market_b` resolve from the same source, or from "
            "sources that will always report the same value?",
        },
        "deadline": {
            "type": "choice",
            "instructions": "How do the time windows of `market_a` and `market_b` compare? "
            "Use each market's rules text; listed dates can be placeholders.",
            "criteria": {
                "identical": "Same event date, time window, and cut-off",
                "A_window_longer": "`market_a`'s window contains `market_b`'s and extends past it",
                "B_window_longer": "`market_b`'s window contains `market_a`'s and extends past it",
                "incompatible": "The windows differ and neither contains the other",
            },
        },
        "divergence": {
            "type": "noul",
            "instructions": (
                "Given how `market_a` and `market_b` correspond, is there any plausible scenario "
                "in which their resolutions are inconsistent with each other? Consider ties, "
                "postponements, cancellations, overtime, data revisions, differing sources, "
                "cut-off times and time zones, values exactly at a strike, early resolution, "
                "and voids."
            ),
        },
        "divergence_kind": {
            "type": "choice",
            "instructions": "Which scenario most plausibly makes `market_a` and `market_b` "
            "resolve inconsistently?",
            "criteria": DIVERGENCE_KINDS,
        },
    }


@dataclass(frozen=True)
class Answers:
    """Parsed Jev answers for one pair."""

    model: str
    raw: dict[str, Any]

    def noul(self, qid: str) -> float:
        """Returns the yes-probability of a noul answer."""
        return float(self.raw[qid]["noul"])

    def choice(self, qid: str) -> tuple[str, float, dict[str, float]]:
        """Returns (top option, its probability, all probabilities) for a choice answer."""
        a = self.raw[qid]
        probs = {k: float(v) for k, v in a["probabilities"].items()}
        top = a.get("choice") or max(probs, key=probs.get)  # type: ignore[arg-type]
        return top, probs.get(top, 0.0), probs


class JevClient:
    """Calls TypeSafe's System One endpoint to classify Kalshi/PM market pairs."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = API_URL,
        concurrency: int = 4,
        timeout_s: float = 60.0,
    ) -> None:
        """Creates an authenticated HTTP client limited to `concurrency` requests at once."""
        self.model = model
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._sem = asyncio.Semaphore(concurrency)

    def request_body(self, kalshi: Market, pm: Market) -> dict[str, Any]:
        """Returns the System One request for a pair, with Kalshi as market_a."""
        return {
            "model": self.model,
            "state": {"market_a": market_state(kalshi), "market_b": market_state(pm)},
            "questions": build_questions(),
        }

    async def classify(self, kalshi: Market, pm: Market) -> Answers:
        """Classifies a pair, retrying 429, 529 and 5xx responses with backoff up to 6 times."""
        body = self.request_body(kalshi, pm)
        async with self._sem:
            for attempt in range(7):
                resp = await self._http.post("/v1/systemone", json=body)
                if resp.status_code in (429, 529) or resp.status_code >= 500:
                    if attempt == 6:
                        resp.raise_for_status()
                    retry_after = resp.headers.get("retry-after")
                    delay = float(retry_after) if retry_after else max(1.0, backoff_delay(attempt))
                    log.warning("jev.retry", status=resp.status_code, delay=round(delay, 2))
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return Answers(model=data.get("model", self.model), raw=data["answers"])
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        """Closes the underlying HTTP client."""
        await self._http.aclose()
