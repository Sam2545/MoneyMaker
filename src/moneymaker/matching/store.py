from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from moneymaker.models import Market

SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    text_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (text_hash, model)
);
CREATE TABLE IF NOT EXISTS markets (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (venue, market_id)
);
CREATE TABLE IF NOT EXISTS classifications (
    kalshi_id TEXT NOT NULL,
    pm_id TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    answers TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (kalshi_id, pm_id, inputs_hash, model, prompt_version)
);
CREATE TABLE IF NOT EXISTS matches (
    kalshi_id TEXT NOT NULL,
    pm_id TEXT NOT NULL,
    status TEXT NOT NULL,
    relation TEXT,
    polarity TEXT,
    direction TEXT,
    inputs_hash TEXT NOT NULL,
    similarity REAL,
    scores TEXT,
    flags TEXT,
    reasons TEXT,
    decided_by TEXT NOT NULL,
    note TEXT,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT,
    PRIMARY KEY (kalshi_id, pm_id)
);
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    kalshi_id TEXT NOT NULL,
    pm_id TEXT NOT NULL,
    label TEXT NOT NULL,
    contracts REAL NOT NULL,
    net REAL NOT NULL,
    net_per_contract REAL NOT NULL,
    top_marginal REAL,
    capital REAL NOT NULL,
    annualized REAL
);
CREATE INDEX IF NOT EXISTS observations_pair ON observations (kalshi_id, pm_id, observed_at);
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kalshi_id TEXT NOT NULL,
    pm_id TEXT NOT NULL,
    label TEXT NOT NULL,
    asset TEXT,
    template TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_s REAL NOT NULL,
    max_cents_per_contract REAL NOT NULL,
    max_contracts REAL NOT NULL,
    max_net REAL NOT NULL,
    capital_at_max REAL NOT NULL,
    annualized_at_max REAL,
    updates INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS opportunities_started ON opportunities (started_at);
"""

OPPORTUNITY_FIELDS = (
    "kalshi_id", "pm_id", "label", "asset", "template", "started_at", "ended_at", "duration_s",
    "max_cents_per_contract", "max_contracts", "max_net", "capital_at_max", "annualized_at_max",
    "updates",
)

APPROVED = ("approved_equivalent", "approved_implication")
USER_STATUSES = (*APPROVED, "rejected", "uncertain")


def now_iso() -> str:
    """Returns the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Routed:
    """A routing decision for one pair, before it is merged with any stored user decision."""

    status: str
    relation: str | None = None
    polarity: str | None = None
    direction: str | None = None
    scores: dict[str, Any] | None = None
    flags: list[str] | None = None
    reasons: list[str] | None = None


class MatchStore:
    """SQLite persistence for embeddings, Jev answers, match statuses, and edge observations."""

    def __init__(self, path: Path | str) -> None:
        """Opens or creates the database at path and applies the schema."""
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        """Closes the database connection."""
        self.db.close()

    def get_embeddings(self, hashes: Iterable[str], model: str) -> dict[str, np.ndarray]:
        """Returns cached vectors for the given text hashes under model."""
        out: dict[str, np.ndarray] = {}
        hashes = list(hashes)
        for i in range(0, len(hashes), 500):
            chunk = hashes[i : i + 500]
            rows = self.db.execute(
                f"SELECT text_hash, vector FROM embeddings WHERE model = ? "
                f"AND text_hash IN ({','.join('?' * len(chunk))})",
                [model, *chunk],
            )
            for row in rows:
                out[row["text_hash"]] = np.frombuffer(row["vector"], dtype=np.float32)
        return out

    def put_embeddings(self, vectors: dict[str, np.ndarray], model: str) -> None:
        """Stores vectors keyed by text hash under model."""
        self.db.executemany(
            "INSERT OR REPLACE INTO embeddings (text_hash, model, vector) VALUES (?, ?, ?)",
            [(h, model, v.astype(np.float32).tobytes()) for h, v in vectors.items()],
        )
        self.db.commit()

    def put_markets(self, markets: Iterable[Market]) -> None:
        """Stores a JSON snapshot of each market for later review."""
        ts = now_iso()
        self.db.executemany(
            "INSERT OR REPLACE INTO markets (venue, market_id, snapshot, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [(str(m.venue), m.venue_market_id, m.model_dump_json(), ts) for m in markets],
        )
        self.db.commit()

    def get_market(self, venue: str, market_id: str) -> Market | None:
        """Returns the stored snapshot of a market, or None."""
        row = self.db.execute(
            "SELECT snapshot FROM markets WHERE venue = ? AND market_id = ?", (venue, market_id)
        ).fetchone()
        return Market.model_validate_json(row["snapshot"]) if row else None

    def get_classification(
        self, kalshi_id: str, pm_id: str, inputs_hash: str, model: str, prompt_version: int
    ) -> dict[str, Any] | None:
        """Returns cached Jev answers for this exact pair, inputs, model and prompt version."""
        row = self.db.execute(
            "SELECT answers FROM classifications WHERE kalshi_id = ? AND pm_id = ? "
            "AND inputs_hash = ? AND model = ? AND prompt_version = ?",
            (kalshi_id, pm_id, inputs_hash, model, prompt_version),
        ).fetchone()
        return json.loads(row["answers"]) if row else None

    def put_classification(
        self,
        kalshi_id: str,
        pm_id: str,
        inputs_hash: str,
        model: str,
        prompt_version: int,
        answers: dict[str, Any],
    ) -> None:
        """Stores Jev answers for a pair."""
        self.db.execute(
            "INSERT OR REPLACE INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kalshi_id, pm_id, inputs_hash, model, prompt_version, json.dumps(answers), now_iso()),
        )
        self.db.commit()

    def get_match(self, kalshi_id: str, pm_id: str) -> sqlite3.Row | None:
        """Returns the stored match row for a pair, or None."""
        return self.db.execute(
            "SELECT * FROM matches WHERE kalshi_id = ? AND pm_id = ?", (kalshi_id, pm_id)
        ).fetchone()

    def upsert_routed(
        self,
        kalshi_id: str,
        pm_id: str,
        inputs_hash: str,
        similarity: float,
        routed: Routed,
        *,
        commit: bool = True,
    ) -> str:
        """Merges a routing decision into the stored match and returns the resulting status.

        A user decision on unchanged inputs is kept. An approved pair whose inputs changed
        becomes suspended. Otherwise the routed status replaces the stored one.
        """
        existing = self.get_match(kalshi_id, pm_id)
        status, decided_by = routed.status, "auto"
        reasons = list(routed.reasons or [])
        relation, polarity, direction = routed.relation, routed.polarity, routed.direction
        if existing is not None and existing["decided_by"] == "user":
            if existing["inputs_hash"] == inputs_hash:
                status, decided_by = existing["status"], "user"
                relation = existing["relation"]
                polarity = existing["polarity"]
                direction = existing["direction"]
            elif existing["status"] in APPROVED:
                status = "suspended"
                reasons.append("market rules or dates changed after approval")
        self.db.execute(
            """
            INSERT INTO matches (kalshi_id, pm_id, status, relation, polarity, direction,
                inputs_hash, similarity, scores, flags, reasons, decided_by, note, updated_at,
                reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)
            ON CONFLICT (kalshi_id, pm_id) DO UPDATE SET
                status = excluded.status, relation = excluded.relation,
                polarity = excluded.polarity, direction = excluded.direction,
                inputs_hash = excluded.inputs_hash, similarity = excluded.similarity,
                scores = excluded.scores, flags = excluded.flags, reasons = excluded.reasons,
                decided_by = excluded.decided_by, updated_at = excluded.updated_at
            """,
            (
                kalshi_id, pm_id, status, relation, polarity, direction, inputs_hash,
                similarity, json.dumps(routed.scores or {}), json.dumps(routed.flags or []),
                json.dumps(reasons), decided_by, now_iso(),
            ),
        )
        if commit:
            self.db.commit()
        return status

    def record_decision(
        self,
        kalshi_id: str,
        pm_id: str,
        status: str,
        *,
        relation: str | None = None,
        polarity: str | None = None,
        direction: str | None = None,
        note: str | None = None,
    ) -> None:
        """Stores a user review decision on an existing match."""
        if status not in USER_STATUSES:
            raise ValueError(f"invalid review status {status!r}")
        ts = now_iso()
        cur = self.db.execute(
            "UPDATE matches SET status = ?, relation = ?, polarity = ?, direction = ?, "
            "note = COALESCE(?, note), decided_by = 'user', reviewed_at = ?, updated_at = ? "
            "WHERE kalshi_id = ? AND pm_id = ?",
            (status, relation, polarity, direction, note, ts, ts, kalshi_id, pm_id),
        )
        if cur.rowcount != 1:
            raise KeyError(f"no match for {kalshi_id} / {pm_id}")
        self.db.commit()

    def add_observation(
        self, run_id: str, kalshi_id: str, pm_id: str, edge: Any, *, commit: bool = True
    ) -> None:
        """Stores one priced edge for a pair."""
        self.db.execute(
            "INSERT INTO observations (run_id, observed_at, kalshi_id, pm_id, label, contracts, "
            "net, net_per_contract, top_marginal, capital, annualized) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, now_iso(), kalshi_id, pm_id, edge.label, float(edge.contracts),
                float(edge.net), float(edge.net_per_contract),
                None if edge.top_marginal is None else float(edge.top_marginal),
                float(edge.capital), None if edge.annualized is None else float(edge.annualized),
            ),
        )
        if commit:
            self.db.commit()

    def review_queue(self, statuses: Iterable[str], limit: int | None = None) -> list[sqlite3.Row]:
        """Returns matches in the given statuses, highest latest net edge first."""
        statuses = list(statuses)
        sql = f"""
            SELECT m.*, o.label AS obs_label, o.contracts AS obs_contracts, o.net AS obs_net,
                   o.net_per_contract AS obs_npc, o.top_marginal AS obs_top,
                   o.annualized AS obs_ann, o.observed_at AS obs_at
            FROM matches m
            LEFT JOIN observations o ON o.id = (
                SELECT id FROM observations
                WHERE kalshi_id = m.kalshi_id AND pm_id = m.pm_id
                ORDER BY id DESC LIMIT 1
            )
            WHERE m.status IN ({','.join('?' * len(statuses))})
            ORDER BY COALESCE(o.net, -1e9) DESC, COALESCE(o.top_marginal, -1e9) DESC,
                     m.similarity DESC
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(self.db.execute(sql, statuses))

    def status_counts(self) -> dict[str, int]:
        """Returns the number of matches in each status."""
        return {
            r["status"]: r["n"]
            for r in self.db.execute("SELECT status, COUNT(*) AS n FROM matches GROUP BY status")
        }

    def add_opportunity(self, row: dict[str, Any]) -> None:
        """Stores one closed opportunity episode given as a dict of OPPORTUNITY_FIELDS."""
        self.db.execute(
            f"INSERT INTO opportunities ({', '.join(OPPORTUNITY_FIELDS)}) "
            f"VALUES ({', '.join('?' * len(OPPORTUNITY_FIELDS))})",
            [row[k] for k in OPPORTUNITY_FIELDS],
        )
        self.db.commit()

    def opportunities_between(self, start: str, end: str) -> list[dict[str, Any]]:
        """Returns episodes whose ISO start time is in [start, end), oldest first."""
        rows = self.db.execute(
            "SELECT * FROM opportunities WHERE started_at >= ? AND started_at < ? "
            "ORDER BY started_at",
            (start, end),
        )
        return [dict(r) for r in rows]
