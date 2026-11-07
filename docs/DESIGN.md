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

## 4. Phase 2 — matching (implemented: `moneymaker match` / `moneymaker review`)

Matching is the main source of loss. A false positive turns a riskless arb into two correlated
losing bets, so the pipeline is tuned for **precision**: missing a match only costs opportunity.
**Nothing is tradable until a human approves it, or it matches a template the human approved
(§4.4).**

```
fetch (scoped) → candidates (embeddings + shape checks) → Jev (cached) → routing → pricing → report
                                                                              ↓
                                                         moneymaker review → your decisions
```

### 4.1 Fetch and scope (`matching/pipeline.py`)

`--scope` maps to PM US `categories[]` and Kalshi series categories, e.g. `crypto` →
(`crypto`, `Crypto`). Kalshi markets are fetched per series, which also yields each series'
`fee_multiplier` for the fee model. Only `OPEN` markets are kept. Every market's snapshot is saved
to the store so the review screen works offline.

### 4.2 Candidate search (`matching/candidates.py`, `embed.py`, `extract.py`)

1. **Settle-date window:** Kalshi markets whose expiration (or close) is within ±30 days of the
   PM market's. The window is wide because PM `endDate` is sometimes a placeholder (e.g. the 2026
   BTC year-end ranges carry 2027-01-15).
2. **Semantic search:** `title + yes_label + first 300 chars of rules` is embedded. For each PM
   market, up to 300 Kalshi neighbours are scanned in cosine order and the first **5 that pass the
   shape checks** below are kept, so the right rung of a strike ladder isn't lost to its siblings.
   - `EMBEDDING_MODEL` (default `gemini-embedding-2`) picks the model. `gemini-*` models use the
     Gemini API (`GEMINI_API_KEY`), 100 texts per batch, 768 dimensions, with the
     `task: sentence similarity` prefix (`gemini-embedding-001` uses `task_type`
     `SEMANTIC_SIMILARITY`). Provider-prefixed models (e.g. `qwen/qwen3-embedding-8b`) use
     OpenRouter (`OPENROUTER_API_KEY`). OpenRouter's free Nemotron endpoint was rejecting batches
     on 2026-11-05.
   - Without the key, or if the remote model fails mid-run, the whole run uses local
     `BAAI/bge-small-en-v1.5` via fastembed.
   - Vectors are cached in SQLite per (text hash, model), so each market is embedded once per model.
3. **Shape checks (deterministic):** the strike structure of both titles is parsed:
   - range vs range must match exactly; range vs threshold is rejected;
   - same-direction thresholds at different strikes become **implication candidates** (the lower
     up-strike / higher down-strike is the broader market);
   - plain titles with disjoint strike numbers are rejected;
   - an "X vs Y" matchup must share a participant with the other market.
   - rules that average the settlement price differently (simple average vs trimmed mean) are
     rejected, by user decision on 2026-11-06.

### 4.3 Classification (`matching/jev.py`)

One `POST https://api.typesafe.ai/v1/systemone` per pair (Bearer `TYPESAFE_API_KEY`), model pinned
to `jev-1.14.0` (`TYPESAFE_MODEL`). `state` holds both markets (Kalshi = `market_a`): title, YES
outcome, rules (≤ 6k chars), close and expiration times. Questions (`PROMPT_VERSION = 3`). Listed dates are labelled as possible placeholders, and the
rules text is authoritative for timing:

| id | type | question |
|---|---|---|
| `same_event` / `same_event_swapped` | noul | same subject, outcome/range and settlement moment? Source and method differences ignored (both orders) |
| `polarity` / `polarity_swapped` | choice | which outcome of B is A's YES: `same` / `inverted` / `neither` (both orders; edge cases left to `divergence`) |
| `same_source` | noul | same or equivalent resolution source? |
| `deadline` | choice | `identical` / `A_window_longer` / `B_window_longer` / `incompatible` |
| `divergence` | noul | any plausible scenario where they resolve inconsistently? |
| `divergence_kind` | choice | `cutoff_time`, `resolution_source`, `data_revision`, `postponement`, `overtime`, `tie_handling`, `threshold_boundary`, `measurement_definition`, `announced_vs_occurs`, `early_resolution`, `scope`, `void_or_refund`, `other`, `none` |

Answers are cached by (pair, inputs hash, model, prompt version). The inputs hash covers
everything Jev sees, so an edited market is reclassified automatically. Cost is about $0.0001/pair.

### 4.4 Routing (`matching/routing.py`, thresholds in `matching/thresholds.toml`)

| Status | When |
|---|---|
| `rejected` | `same_event < 0.25`, or `P(polarity = neither) ≥ 0.80` (shape-check rejects never reach Jev) |
| `uncertain` | swap test disagrees; `measurement_definition` / `scope` ≥ 0.50; any approve gate missed; all implication candidates |
| `pending_review` | every gate passes: `same_event ≥ 0.97`, polarity ≥ 0.98 and not `neither`, `divergence ≤ 0.03`, `same_source ≥ 0.90`, `deadline = identical` ≥ 0.95, `S = same_event × p(polarity) × (1 − divergence) ≥ 0.95` |
| `auto_approved` | an `uncertain` / `pending_review` pair with no user decision that matches a user-approved template and passes the template checks below; stored as an equivalent with the template's polarity |
| `approved_equivalent` / `approved_implication` | **only** set by `moneymaker review` |
| `suspended` | an approved pair whose inputs hash changed; back in the review queue |
| `unclassified` | no Jev key, Jev error, or `--jev-limit` reached |

Divergence kinds ≥ 0.15 are listed as flags on the pair. Expected next steps: calibrate the
thresholds against ≥ 200 labelled pairs (target ≥ 99.5% precision in the approve band), and add
code-level checks for `cutoff_time` / `threshold_boundary`.

**Template auto-approval (`matching/autoapprove.py`, `[auto]` in `thresholds.toml`).** A rules
*fingerprint* is the lowercased rules text with URLs, asset names (`<asset>`), CF index names and
codes such as `BRTI` / `ETHUSD_RTI` (`<index>`), month and weekday names, ET/EST/EDT (`<tz>`),
and every number or clock time (`#`) masked. A *template* is a (Kalshi fingerprint, PM
fingerprint) pair; each run builds the map from the user's `approved_equivalent` decisions,
recording the approved polarity, the count and the lowest Jev `same_event` among them. A user
rejection with the same fingerprints (other than bulk `rule:` rejections) or a second polarity
marks the template conflicted. Because assets are masked, an ETH pair inherits an approved BTC
template. A pair is auto-approved only when all of these hold:

- the template is known, not conflicted and has ≥ `min_examples` approvals;
- the shape check is `ok` (not an implication) and both rules name the same assets;
- the UTC settlement instants parsed from both rules (`5 PM EST on Nov 6, 2026`,
  `Dec 31, 2026 at 11:59 PM ET`, ...) are equal and non-empty, so an 11:59 PM vs midnight
  pair is refused;
- `settlement_method` (simple vs trimmed) is equal;
- Jev polarity equals the template's, `polarity_p ≥ polarity_min`, and
  `same_event ≥ max(same_event_floor, template minimum − same_event_margin)`.

User decisions always win: pairs with any user decision are never auto-approved, and a review
decision overwrites `auto_approved`.

### 4.5 Pricing and report

Every non-rejected pair is priced from REST books (`edge.py`, `fees.py`): both directions for
equivalents, only the safe direction for implications, and, for `unclassified` pairs, only when
both YES labels match (labelled unverified). Sizing walks both ladders, uses ≤ 50% of each level,
and stops before a marginal contract earns < 0.2¢. Each priced pair is stored as an
`observation`, and the run writes `data/match_runs/<run>-<scope>.jsonl`.

### 4.6 Review (`matching/review.py`)

`moneymaker review [--status S] [--limit N]` walks the queue (default `pending_review`,
`suspended`, `unclassified`), highest latest edge first. Each screen shows both markets' titles,
YES outcomes, times and rules, Jev scores and flags, the routing reasons, and the latest edge.
Keys: **e** equivalent (confirm polarity), **i** implication (confirm the broader side),
**r** reject, **u** uncertain, **s** skip, **v** full rules, **q** quit. Decisions are saved
immediately and survive reruns unless the market changes.

### 4.7 Store (`data/matches.db`, SQLite, gitignored)

`embeddings`, `markets` (snapshots), `classifications` (Jev answers), `matches` (status, relation,
polarity, direction, scores, flags, reasons, decided_by, note), `observations` (edge per run).

### 4.8 Template matcher without AI (`matching/templates.py`, `moneymaker match-templates`)

Once templates exist, new instances of them (the next hour, day or week, other assets) are
matched deterministically, with **no embedding, Gemini, OpenRouter or Jev calls**. The AI
pipeline (`moneymaker match`) is then only needed, run manually, to discover new templates.

1. **Fetch.** Only Kalshi series that can carry a usable template's Kalshi fingerprint are
   fetched: the series of stored snapshots with such a fingerprint, plus the series of every
   user-approved pair. When no series is known, with `--full`, and every 6 hours inside the
   monitor, every in-scope series is fetched and the series set is rebuilt. PM US is one
   category-filtered listing. A crypto refresh is about 40 Kalshi and 15 PM requests.
2. **Index.** Kalshi markets with a usable fingerprint are grouped by (`rules_assets`,
   `settlement_instants`). Each PM market with a usable PM fingerprint looks up its own key, so
   only markets with the same asset and identical instants are compared.
3. **Match.** A pair needs `compare(describe(k), describe(p)).result == "ok"` and a known,
   non-conflicted (Kalshi, PM) fingerprint template; `AutoApprover.check(require_jev=False)`
   then repeats every template check except the Jev-score gates. Instant equality is strict,
   so the yearly `KXBTCMAXY` template (11:59 PM Dec 31 vs midnight) stays refused.
4. **Store.** Passing pairs become `auto_approved` with the reason "template match (no AI)".
   Rows decided by the user, and `suspended` rows, are never changed, except that a
   user-approved pair whose inputs hash changed is suspended, as in the pipeline.
5. **Price.** Both venues' books come from WebSocket snapshots (REST without keys). PM US
   allows 10 subscriptions of 50 markets per connection, so larger sets are sharded across
   connections. Every priced pair is stored as an observation.

User-approved implications (`relation = implication`) are priced only in their safe direction,
like routed implication candidates.

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
| 2 🚧 | Retrieval + Jev classification + review queue (built; needs Jev key + labels) | ≥ 200 labelled pairs; measured precision |
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
