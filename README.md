# MoneyMaker

Cross-venue prediction-market arbitrage between Kalshi and Polymarket US. The design and
roadmap are in [docs/DESIGN.md](docs/DESIGN.md). Phase 1 (market data) is implemented.

## Setup

```bash
uv sync
cp .env.example .env   # add Kalshi key ID + PEM path; Polymarket US key ID + secret
```

## Usage

```bash
uv run moneymaker markets kalshi --limit 20
uv run moneymaker book polymarket_us <slug>
uv run moneymaker stream kalshi <ticker> [<ticker> ...]       # needs Kalshi keys
uv run moneymaker stream polymarket_us <slug> [<slug> ...]    # WS with keys, REST polling without
```

## Matching (Phase 2)

```bash
uv run moneymaker match --scope crypto            # find, classify, price; writes data/match_runs/*.jsonl
uv run moneymaker match --scope crypto --no-jev   # candidates + pricing only
uv run moneymaker review                          # approve / reject pairs interactively
uv run moneymaker review --status auto_approved   # audit pairs auto-approved from your templates
```

Pairs whose rules match a template you already approved (same wording with only asset, strike
and dates differing) are stored as `auto_approved` when deterministic checks agree; see
DESIGN.md §4.4.

Needs `TYPESAFE_API_KEY` for Jev and `GEMINI_API_KEY` for `gemini-embedding-2` embeddings (falls
back to a local model without it; set `EMBEDDING_MODEL` to use an OpenRouter model instead). Scopes: crypto, economics, politics, sports, culture, climate, tech.

## Tests

```bash
uv run pytest && uv run ruff check src tests
```
