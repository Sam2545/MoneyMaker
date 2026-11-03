# MoneyMaker — Cross-Venue Prediction Market Arbitrage

Status: draft v0.1 (2026-11-02). Phase 1 is implemented and the later phases are designed here.

## 1. Goal and the core trade

The bot buys complementary outcomes of the *same* binary event on Kalshi and Polymarket US whenever
their combined cost, including all fees and slippage, is below the $1.00 settlement payout.

```
Buy YES on venue A at a   +   Buy NO on venue B at b
Payout at settlement: exactly $1.00 per pair (one leg always wins)
Locked edge per pair = 1 − a − b − fee_A(a) − fee_B(b)
```

The trade is only riskless if three things hold:

1. **Equivalence.** Both contracts settle on the same outcome. If their resolution rules diverge,
   both legs can lose, so a wrong match costs up to the full $1 per pair. This is the largest risk
   in the system and it is why matching is semantic, conservative and human-reviewed (§4).
2. **Execution.** Both legs fill. Until the second leg fills, the position is a naked directional bet (§7).
3. **Settlement and solvency.** Both venues pay out on time. Capital is tied up until resolution, so
   the true return is `edge / capital × 365 / days_to_resolution` (§5.3).

Design principle: **Jev handles semantic reasoning, deterministic code handles money, and a
risk-controlled executor handles orders.** The model never computes a price, size or fee, and it
never places an order.

## 2. Architecture

```
            ┌────────────── Phase 1 ───────────────┐
 Kalshi ───►│ KalshiClient (REST + WS, RSA-PSS)    │──┐
            │                                      │  │  normalized Market + OrderBook
 Poly US ──►│ PolymarketUSClient (REST + WS)       │──┤
            └──────────────────────────────────────┘  │
                                                      ▼
   ┌──────────────┐   candidate pairs   ┌────────────────────┐  approved pairs  ┌──────────────┐
   │ Embedding    │────────────────────►│ Jev equivalence    │─────────────────►│ MatchStore   │
   │ retrieval    │                     │ classifier + rules │ (+human review)  │ (Postgres)   │
   └──────────────┘                     └────────────────────┘                  └──────┬───────┘
                                                                                       │
            live books (in-process, mirrored to Redis) ─────────────────────────────┐  │
                                                                                    ▼  ▼
                                                                         ┌─────────────────────┐
                                                                         │ Arb engine          │
                                                                         │ (fees, depth, size) │
                                                                         └─────────┬───────────┘
                                                                                   │ Opportunity
                                                         ┌─────────────────────────┼───────────┐
                                                         ▼                         ▼           ▼
                                                   Paper executor          Live executor   Dashboard
                                                   (Phase 4)               + RiskManager   (Next.js)
                                                                           (Phase 5)
```

Process model: a single asyncio process owns the WebSocket connections and the in-memory books,
because the arb engine must read both books with sub-millisecond latency and no network hop.
Redis receives a throttled mirror of book tops and opportunities for the dashboard. It is not on
the hot path. Postgres stores durable state: markets, matches, opportunities, orders, fills,
positions and the P&L ledger.

## 3. Phase 1 — market data (implemented)

### 3.1 Normalized model (`src/moneymaker/models.py`, `orderbook.py`)

- `Market`: venue, venue id, event id, title, YES label, full rules text, close and expiration
  times, status, tick size. The raw venue payload is kept for auditing matches.
- `OrderBook`: a mutable L2 book stored in **YES-perspective**, with all prices as `Decimal`
  dollars in [0, 1]:
  - `yes_bids`: bids to buy YES
  - `yes_asks`: offers to sell YES. A NO bid at `p` is equivalent to a YES ask at `1 − p`.
  - `buy_levels(Side.NO)` derives the NO-buyer's ladder as `1 − yes_bid`.
  - `walk(side, size, limit)` simulates a marketable buy across levels and returns the VWAP and
    worst price. The arb engine uses it for depth-aware sizing.
- Every price, size and fee uses `Decimal`, never `float`.

### 3.2 Venue adapters (`src/moneymaker/venues/`)

All adapters implement the `VenueClient` protocol (`list_markets`, `get_orderbook`,
`stream_orderbooks`). The API details are in §3.3 and §3.4.

Stream behavior that is common to both adapters:
- On connect, the stream subscribes and rebuilds each book from the venue's snapshot.
- It applies deltas in order. On a sequence gap it drops the book and resubscribes, or refetches the snapshot.
- On a disconnect it reconnects with exponential backoff plus jitter, capped at
  `reconnect_max_backoff_s`. Every book is marked **stale** until its snapshot is reloaded.
- Each book carries `updated_ts`. The arb engine ignores any book older than `max_book_age_ms`.


### 3.3 Kalshi (verified against docs.kalshi.com, 2026-11-02)

| Item | Value |
|---|---|
| REST | `https://external-api.kalshi.com/trade-api/v2` (demo: `external-api.demo.kalshi.co`) |
| WS | `wss://external-api-ws.kalshi.com/trade-api/ws/v2`. Auth is required, even for market data |
| Auth | Headers `KALSHI-ACCESS-KEY/-TIMESTAMP(ms)/-SIGNATURE`. Signs `ts + METHOD + /trade-api/v2/path`, with no query string. The key is RSA-PSS SHA-256 (salt = digest length) or Ed25519; the adapter detects which |
| Markets | `GET /markets?status=open&mve_filter=exclude&limit=1000`, cursor-paged. Prices are `*_dollars` strings, quantities `*_fp`. The tick grid comes from `price_ranges` |
| Book | `GET /markets/{t}/orderbook` returns `orderbook_fp.{yes_dollars,no_dollars}`: **bids only**, ascending. A NO bid at p maps to a YES ask at 1−p |
| WS book | `orderbook_delta` channel. Each market gets an `orderbook_snapshot` (keys `yes_dollars_fp`/`no_dollars_fp`), then deltas (`price_dollars`, `delta_fp`, `side`). `seq` is contiguous **per subscription (sid)**, not per market |
| Gap handling | Phase 1 reconnects and resubscribes, which refreshes every snapshot. Phase 6 can switch to `update_subscription{action:get_snapshot}` |
| Limits | Basic tier: reads 200 tokens/s (about 20 GETs/s, burst 600 tokens); writes 100 tokens/s (about 10 orders/s); 429 has no Retry-After header |
| Fees | Set per series: `fee_type` ∈ {quadratic, quadratic_with_maker_fees, flat, …} plus `fee_multiplier`, with event-level overrides. Base taker formula is commonly `0.07·C·P·(1−P)`. **Unverified:** the fee-schedule PDF couldn't be fetched, and fees accrue to $0.000001 with a separate rounding fee. See §5.2 |

### 3.4 Polymarket US (verified against docs.polymarket.us and the `polymarket-us` SDK v2.4.1)

| Item | Value |
|---|---|
| Public REST | `https://gateway.polymarket.us`: markets, events, book and BBO, with no key |
| Auth REST / WS | `https://api.polymarket.us` and `wss://api.polymarket.us/v1/ws/markets`. **Market-data WS needs a key** |
| Auth | Headers `X-PM-Access-Key/-Timestamp(ms)/-Signature`. Ed25519 over `ts + METHOD + path` with no query string; the base64 secret's first 32 bytes are the seed; ±30 s clock window |
| Markets | `GET /v1/markets?active=true&closed=false&limit&offset`, offset-paged. Fields: `slug` (id), `question` + `title`, `description` (rules), `endDate`, `orderPriceMinTickSize` (often 0.001), `feeCoefficient`, `marketSides[]` |
| Book | `GET /v1/markets/{slug}/book` returns `marketData.{bids,offers}` with levels `{px:{value}, qty}`. **One instrument per market (YES)**; NO = 1 − YES. Prices are dollar strings |
| WS book | Subscribe `{subscribe:{requestId, subscriptionType:"SUBSCRIPTION_TYPE_MARKET_DATA", marketSlugs}}`. Every `marketData` message is a **full snapshot** with no seq numbers, so the adapter replaces the whole book each time. Heartbeats arrive as `{heartbeat:{}}`; a 30 s silence triggers a reconnect |
| No-key fallback | `poll_orderbooks` cycles through REST `/book` within the shared 25 req/s per-IP budget (client uses 20/s). That is fine for tens of markets, not hundreds |
| Fees | Taker `Θ·C·p·(1−p)` with Θ = `feeCoefficient` (0.0695 by default), banker's-rounded to $0.01. Maker rebates by volume tier; tier 1 gets none. No per-contract cap |
| Orders | `POST /v1/orders` with `marketSlug, type, price{value,currency}, quantity, tif, intent` (`ORDER_INTENT_BUY_LONG` / `BUY_SHORT` / …). **Open question:** whether a `BUY_SHORT` price is quoted in YES or NO terms. Verify with a 1-contract order before Phase 5 |

### 3.5 Phase 1 CLI

```
uv run moneymaker markets {kalshi|polymarket_us} [--limit N]
uv run moneymaker book    {kalshi|polymarket_us} <market_id> [--depth N]
uv run moneymaker stream  {kalshi|polymarket_us} <market_id> [<market_id> ...]
```

## 4. Phase 2 — matching with Jev

Matching is the main source of loss. A false positive turns a riskless arb into two correlated
losing bets, so the pipeline is tuned for **precision**: missing a match only costs opportunity.

### 4.1 Candidate retrieval (cheap, recall-oriented)

1. **Hard filters:** both markets are open, close dates fall within ±3 days (configurable), and the
   categories are compatible (sports/politics/econ/crypto, mapped per venue).
2. **Embeddings:** each `Market.title + yes_label + first 512 chars of rules` is embedded with a
   local sentence-embedding model (e.g. `bge-small-en-v1.5`; Jev does not do embeddings). The
   embeddings go in pgvector, and each market keeps its top-k=10 cross-venue neighbours above a
   cosine floor.
3. **Entity and number guardrails:** a deterministic check that extracted numbers (strikes such as
   "3.75%" or "45+", dates, scores) and named entities (teams, people) agree. This catches
   look-alike pairs such as "45+ points" vs "40+ points" that embeddings score as near-identical.

### 4.2 Equivalence classification (Jev, `POST https://api.typesafe.ai/v1/systemone`)

Each candidate pair is one request (Bearer `TYPESAFE_API_KEY`). The model is pinned to
`jev-1.14.0`, not `jev-latest`, so calibrated thresholds don't drift when the alias moves.
`state` holds both markets' structured fields: title, yes_label, full rules, close and
expiration times, and the resolution source. All questions go in the same request because they
are evaluated in parallel at almost no extra latency:

| id | type | question |
|---|---|---|
| `same_event` | noul | Do A and B refer to the same real-world event and measurement? |
| `polarity` | choice | `same` (A-YES ≡ B-YES) / `inverted` (A-YES ≡ B-NO) / `neither` |
| `same_source` | noul | Do both resolve from the same (or an equivalent) official source? |
| `deadline` | choice | `identical` / `A_window_longer` / `B_window_longer` / `incompatible` |
| `divergence` | noul | Is there any plausible scenario (tie, postponement, overtime, void, revision, "announced" vs "occurs", time zone) where A and B resolve differently? |
| `divergence_kind` | choice | Which of those scenarios applies (fixed list + `none`) |

Example request shape:

```json
{"model": "jev-1.14.0",
 "state": {"market_a": {"venue": "kalshi", "title": "...", "rules": "...", "close_time": "..."},
           "market_b": {"venue": "polymarket_us", "title": "...", "rules": "...", "close_time": "..."}},
 "questions": {"same_event": {"type": "noul", "instructions": "Do `market_a` and `market_b` ..."},
               "polarity": {"type": "choice", "instructions": "...",
                            "criteria": {"same": "...", "inverted": "...", "neither": "..."}}}}
```

The decision is made **in code** from the probabilities and the `confidence` values:

- **Approve:** `same_event ≥ 0.97`, `polarity.confidence ≥ 0.9` and not `neither`,
  `divergence ≤ 0.05`, `deadline ∈ {identical}`, and the guardrails from §4.1 pass.
- **Review queue:** everything between approve and reject, ranked by expected opportunity.
  A human sees a side-by-side rules diff and approves or rejects.
- **Reject:** `same_event < 0.5` or `polarity = neither`.
- **Phase 4–5 gate:** while live trading is new, *every* pair needs a human approval. Jev only
  orders the queue. Auto-approval turns on after measuring precision on ≥ 200 labelled pairs
  (the thresholds above are starting points, to be tuned against that set).

Cost: about 1.5–3k input tokens per pair × $0.042/Mtok ≈ $0.0001 per pair, so 100k pairs cost
about $10. Re-classification runs whenever a market's rules hash changes. A rules change on an
approved pair **suspends** it until it is re-approved.

### 4.3 Match record

`match(id, kalshi_market, pm_market, polarity, jev_model, jev_answers jsonb, rules_hash_a,
rules_hash_b, status{pending,approved,rejected,suspended}, approved_by, approved_at)`.

## 5. Phase 3 — arbitrage engine (deterministic)

### 5.1 Opportunity

For an approved pair with polarity `same`, there are two directions:

- **D1:** buy YES on Kalshi + buy NO on PM US. Equivalently, buy Kalshi's YES ask and sell PM's YES bid.
- **D2:** buy NO on Kalshi + buy YES on PM US.

(Polarity `inverted` swaps PM's sides.) On every book update for either leg, the engine walks
both ladders level by level and finds the largest size `C` whose *marginal* contract still clears
the threshold:

```
edge(C) = C − cost_A(C) − cost_B(C) − fee_A(C) − fee_B(C)
trade if  edge(C)/C ≥ min_edge_per_contract  and  edge(C) ≥ min_edge_dollars
```

Then it clamps `C` by: each venue's minimum quantity and tick, the free balance on each venue,
per-pair and per-event position limits, and a depth haircut (use ≤ 50% of visible size at the
last level touched). Books must be `synced` and younger than `max_book_age_ms` (start at 1500 ms).

### 5.2 Fees

The fee model is a per-venue function `fee(venue, market, price, contracts, role)`.

- PM US: `Θ·C·p·(1−p)`, banker's-rounded to $0.01 per order, with Θ taken from `feeCoefficient`.
- Kalshi: `mult·0.07·C·p·(1−p)`, using the series' `fee_type`/`fee_multiplier`, rounded up
  (conservative). **Gate before Phase 5:** reconcile the modelled fees against the actual
  fills from paper or small live trades, to the cent.

Fees are quadratic and peak at p = 0.5, which sets the bar for every trade. Near the midpoint
both legs together cost about **3.5¢ per pair**:

| Kalshi YES | PM NO | Gross | Kalshi fee | PM fee | Net / contract |
|---|---|---|---|---|---|
| 0.45 | 0.52 | 3.0¢ | 1.73¢ | 1.73¢ | **−0.47¢** ✗ |
| 0.45 | 0.50 | 5.0¢ | 1.73¢ | 1.74¢ | **+1.53¢** ✓ |
| 0.10 | 0.87 | 3.0¢ | 0.63¢ | 0.79¢ | **+1.58¢** ✓ |

These are per-contract figures at 100-lot sizes. At small sizes, per-order rounding to the cent
makes it worse: a 1-contract order at p = 0.5 pays 2¢ on each venue. Most visible "arbs" of 1–3¢ near the midpoint are fee mirages. The engine must never alert on
gross spread alone.

### 5.3 Capital efficiency

Capital is locked on **both** venues until settlement, and the two balances can't be netted. The engine
ranks opportunities by annualized return `net_edge / capital_locked × 365 / days_to_settle` and
enforces `min_annualized_return` (for example 15%), so it doesn't tie up capital for months to earn 1¢.

## 6. Phase 4 — paper trading

- The paper executor uses the live books with the same code path as live, swapping in a
  `SimulatedVenue` executor.
- **Latency injection:** a signal fills against the book *as it is N ms later* (N taken from the
  measured order round-trip, starting at 300 ms), not the book that produced the signal. This is
  what reveals whether opportunities survive.
- The simulator also models partial fills, a second leg that moves away, and fees as in §5.2.
- Metrics: signals/day, survival rate after latency, realized-vs-expected edge, second-leg
  slippage, capital-days used, and annualized return. **Exit criterion:** ≥ 2 weeks with positive
  net P&L after fees and ≥ 90% second-leg completion before any live trading.

## 7. Phase 5 — live execution and risk

### 7.1 Leg sequencing

1. Re-validate both books (fresh, synced) and the free balances.
2. Send the **less liquid / more volatile leg first** as an IOC limit at the computed worst price
   (Kalshi limit + IOC; PM US `ORDER_TYPE_LIMIT` + `TIME_IN_FORCE_IMMEDIATE_OR_CANCEL`).
3. Size the second leg to the *actual* first-leg fill. Send it as an IOC limit priced at the
   break-even level (§5.1 with edge = 0), not at the signal price.
4. If the second leg fills only partly or not at all, run the hedge loop: retry up to `k` times
   within `hedge_max_loss_per_contract`. If that fails, unwind the first leg. Every naked contract
   counts against `max_unhedged_notional`.

### 7.2 Risk controls (all enforced in `RiskManager`, outside the strategy)

| Control | Default (start small) |
|---|---|
| Max notional per pair | $50 |
| Max notional per venue | $500 |
| Max unhedged notional (global) | $25. Any breach halts new entries |
| Max daily realized loss | $25. Kill switch: halt and cancel all open orders |
| Stale data | No entries if either book's age > 1.5 s or its WS is reconnecting |
| Clock skew | Halt if local clock vs exchange > 5 s (PM US rejects at 30 s) |
| Rules drift | Suspend a pair whenever either market's rules hash changes |
| Reconciliation | Every 30 s, compare the internal positions/balances with each venue's portfolio API; any mismatch halts |
| Order rate | Client-side token buckets below the venue limits (Kalshi writes 10/s; PM 25/s shared) |
| Manual kill | The `moneymaker halt` CLI, plus a `HALT` file that is checked every loop |

The order-placement code path is **disabled by default** (`LIVE_TRADING=false`). It is enabled per
venue only after: eligibility is verified, PM `BUY_SHORT` price semantics are confirmed, Kalshi fees
are reconciled, and the Phase 4 exit criterion is met.

## 8. Storage and services

- **Postgres:**
  - `markets` (with `rules_hash`, embedding via pgvector)
  - `matches`
  - `book_tops` (sampled, for research)
  - `opportunities`
  - `orders`, `fills`, `positions`
  - `ledger` (double-entry: cash, fees, settlement payouts per venue)
- **Redis:** pub/sub of book tops and opportunities for the dashboard, plus the halt flag. It is
  never the source of truth.
- **FastAPI:** read endpoints for the dashboard (matches review queue, opportunities, positions,
  P&L) and the review approve/reject actions. Bound to localhost / authenticated. It never exposes
  order placement.
- **Next.js dashboard:** match review UI (rules side-by-side + Jev answers), live opportunities, P&L.
- **Docker Compose:** postgres+pgvector, redis, the bot, the api and the dashboard.

## 9. Roadmap and open questions

| Phase | Deliverable | Gate to next |
|---|---|---|
| 1 ✅ | Adapters, normalized books, CLI | Live WS verified on both venues with keys |
| 2 | Retrieval + Jev classification + review queue | ≥ 200 labelled pairs; measured precision |
| 3 | Fee model + opportunity engine + persistence | Fee model reconciled to actual fills |
| 4 | Paper trader with latency injection | 2 weeks net-positive, ≥ 90% leg completion |
| 5 | Live executor + RiskManager, tiny limits | 30 days live within risk limits |
| 6 | Speed (snapshot resync, colocated region), capital recycling, maker-side quoting | — |

Open questions:
1. PM US `BUY_SHORT` price quoting: YES or NO terms? Test with 1 contract.
2. Kalshi fee schedule details: exact per-series multipliers and rounding. Get the PDF and reconcile.
3. PM US WS connection and subscription limits are undocumented. Measure them.
4. PM US `/v1/markets` has no event linkage field in practice (`eventSlug` absent). Event
   grouping may need `/v1/events` with nested markets.
5. Account-level terms: confirm that automated API trading and the planned volume are permitted
   under each venue's member agreement, and check any position limits on the target contracts.
