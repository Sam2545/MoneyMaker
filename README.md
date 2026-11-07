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
uv run moneymaker match-templates --scope crypto  # approve + price new template instances, no AI
```

## Opportunity monitor

```bash
uv run moneymaker monitor --scope crypto --match "btc|bitcoin|eth|sol|xrp"   # needs both venues' keys
uv run moneymaker opportunities --hours 24        # summary of recorded episodes
```

The monitor reruns `match-templates` every 5 minutes (`--refresh-min`), streams both legs of every
approved pair and records each fee-adjusted opportunity episode in the `opportunities` table, with
daily summaries in `data/logs/opportunities-YYYY-MM-DD.{json,md}`; see DESIGN.md §5.4.

Pairs whose rules match a template you already approved (same wording with only asset, strike
and dates differing) are stored as `auto_approved` when deterministic checks agree; see
DESIGN.md §4.4.

Needs `TYPESAFE_API_KEY` for Jev and `GEMINI_API_KEY` for `gemini-embedding-2` embeddings (falls
back to a local model without it; set `EMBEDDING_MODEL` to use an OpenRouter model instead). Scopes: crypto, economics, politics, sports, culture, climate, tech.

## Running on a Mac mini

The monitor runs unattended as the LaunchAgent `com.moneymaker.monitor` (started at login and
restarted if it exits). AI discovery is **not** scheduled: run
`uv run moneymaker match --scope crypto --match "btc|bitcoin|eth|sol|xrp"` by hand when you want
to look for new templates, then `moneymaker review` to grade them; the monitor picks up new
approvals at its next refresh.

1. **Keep it awake and running.** System Settings → Energy: turn on *Prevent automatic sleeping
   when the display is off* and *Start up automatically after a power failure* (or
   `sudo pmset -a sleep 0 disksleep 0 autorestart 1`). Turn on automatic login for the user that
   runs the agent (System Settings → Users & Groups), since LaunchAgents only start in a
   logged-in session.
2. **FileVault.** With FileVault on, automatic login is disabled and the mini waits at the unlock
   screen after every reboot, so the monitor stays down until someone logs in. Either keep
   FileVault on and log in after reboots (use `sudo fdesetup authrestart` for planned restarts),
   or turn it off and accept that the keys below sit unencrypted on disk.
3. **Install.** Install [uv](https://docs.astral.sh/uv/) (it lands in `~/.local/bin`), clone the
   repo and run `uv sync`. Copy these from your laptop, which are not in git:
   - `.env` (API keys),
   - `secrets/kalshi.pem` (`chmod 600`),
   - `data/matches.db`, your decisions and templates. Copy a consistent snapshot with
     `sqlite3 data/matches.db ".backup /tmp/matches.db"` and move that file, not the live
     `-wal` / `-shm` files.
4. **Start.** `scripts/install_launchd.sh install` writes
   `~/Library/LaunchAgents/com.moneymaker.monitor.plist` with this repo's path and starts it
   with `launchctl bootstrap gui/$UID`. `scripts/install_launchd.sh render DIR` only writes the
   plist to DIR for inspection.
5. **Check.** `scripts/install_launchd.sh status` shows the agent state and the last heartbeat.
   Logs go to `data/logs/monitor-YYYY-MM-DD.log`, daily summaries to
   `data/logs/opportunities-YYYY-MM-DD.{json,md}`, and `uv run moneymaker opportunities` prints
   the last 24 hours.
6. **Update.** `git pull && uv sync && scripts/install_launchd.sh install` restarts the agent on
   the new code. `scripts/install_launchd.sh uninstall` stops and removes it.

`scripts/run_job.sh <command> [args...]` runs any `moneymaker` command the same way the agent does,
appending to `data/logs/<command>-YYYY-MM-DD.log`.

## Tests

```bash
uv run pytest && uv run ruff check src tests
```
