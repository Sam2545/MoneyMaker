from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from moneymaker.matching.extract import Check
from moneymaker.matching.jev import Answers
from moneymaker.matching.store import Routed


@dataclass(frozen=True)
class Thresholds:
    """Routing thresholds loaded from thresholds.toml."""

    data: dict[str, Any]

    @classmethod
    def load(cls, path: Path | None = None) -> Thresholds:
        """Loads thresholds from path, or from the packaged thresholds.toml."""
        if path is not None:
            return cls(tomllib.loads(path.read_text()))
        text = resources.files("moneymaker.matching").joinpath("thresholds.toml").read_text()
        return cls(tomllib.loads(text))

    def __getitem__(self, key: str) -> dict[str, Any]:
        """Returns one section of the thresholds."""
        return self.data[key]


def route(answers: Answers, check: Check, t: Thresholds) -> Routed:
    """Turns Jev answers and the shape check into a status, relation, polarity and reasons.

    Only pairs that pass every approve gate become pending_review. Clear mismatches become
    rejected, implication pairs and everything in between become uncertain.
    """
    same_event = answers.noul("same_event")
    same_event_sw = answers.noul("same_event_swapped")
    polarity, p_polarity, polarity_probs = answers.choice("polarity")
    polarity_sw, _, _ = answers.choice("polarity_swapped")
    same_source = answers.noul("same_source")
    deadline, p_deadline, _ = answers.choice("deadline")
    divergence = answers.noul("divergence")
    _, _, kind_probs = answers.choice("divergence_kind")

    combined = same_event * p_polarity * (1 - divergence)
    list_min = t["divergence_kinds"]["list_min"]
    flags = sorted(
        (k for k, p in kind_probs.items() if k != "none" and p >= list_min),
        key=lambda k: -kind_probs[k],
    )
    scores = {
        "same_event": same_event,
        "same_event_swapped": same_event_sw,
        "polarity": polarity,
        "polarity_p": p_polarity,
        "polarity_swapped": polarity_sw,
        "same_source": same_source,
        "deadline": deadline,
        "deadline_p": p_deadline,
        "divergence": divergence,
        "divergence_kinds": {k: kind_probs[k] for k in flags},
        "combined": combined,
        "model": answers.model,
    }
    pol = polarity if polarity in ("same", "inverted") else None

    def out(status: str, reasons: list[str], relation: str | None = "equivalent") -> Routed:
        """Builds a Routed result with the shared scores and flags."""
        return Routed(status, relation, pol, None, scores, flags, reasons)

    reject = t["reject"]
    if same_event < reject["same_event_max"]:
        return out("rejected", [f"same_event {same_event:.2f} < {reject['same_event_max']}"], None)
    if polarity_probs.get("neither", 0.0) >= reject["polarity_neither_min"]:
        return out("rejected", ["polarity is neither"], None)

    if check.result == "implication":
        return Routed(
            "uncertain", "implication_candidate", "same", f"{check.broader}_broader",
            scores, flags, [f"implication: {check.reason}"],
        )
    if deadline in ("A_window_longer", "B_window_longer"):
        broader = "kalshi" if deadline == "A_window_longer" else "pm"
        return Routed(
            "uncertain", "implication_candidate", pol, f"{broader}_broader",
            scores, flags, [f"implication: deadline {deadline} ({p_deadline:.2f})"],
        )

    reasons: list[str] = []
    if polarity != polarity_sw:
        reasons.append(f"swap test: polarity {polarity} vs {polarity_sw}")
    if abs(same_event - same_event_sw) > t["swap"]["same_event_max_diff"]:
        reasons.append(f"swap test: same_event {same_event:.2f} vs {same_event_sw:.2f}")
    kinds = t["divergence_kinds"]
    for kind in kinds["always_uncertain"]:
        if kind_probs.get(kind, 0.0) >= kinds["always_uncertain_min"]:
            reasons.append(f"divergence kind {kind} ({kind_probs[kind]:.2f})")

    a = t["approve"]
    gates = [
        (same_event >= a["same_event_min"], f"same_event {same_event:.2f} < {a['same_event_min']}"),
        (pol is not None and p_polarity >= a["polarity_min"],
         f"polarity {polarity} {p_polarity:.2f} < {a['polarity_min']}"),
        (divergence <= a["divergence_max"], f"divergence {divergence:.2f} > {a['divergence_max']}"),
        (same_source >= a["same_source_min"],
         f"same_source {same_source:.2f} < {a['same_source_min']}"),
        (deadline == "identical" and p_deadline >= a["deadline_identical_min"],
         f"deadline {deadline} {p_deadline:.2f}"),
        (combined >= a["combined_min"], f"combined {combined:.2f} < {a['combined_min']}"),
    ]
    reasons += [msg for ok, msg in gates if not ok]
    if reasons:
        return out("uncertain", reasons)
    return out("pending_review", ["passed all approve gates"])
