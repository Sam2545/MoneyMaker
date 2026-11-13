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

## Running on a VPS (Oracle Cloud Always Free)

The monitor runs as the systemd service `moneymaker-monitor` under a dedicated `moneymaker` user,
starts at boot without anyone logging in and restarts 30 s after it exits. Its output goes to
journald; daily summaries still go to `data/logs/opportunities-YYYY-MM-DD.{json,md}`. AI
discovery (`moneymaker match`) is not scheduled, as on the Mac mini.

**One main copy of `data/matches.db`.** Once the VM runs, it holds the main database: grade pairs
(`review`) and run `match` there over SSH. Do not keep writing a separate `data/matches.db` on the
laptop; the two would diverge with no way to merge them.

1. **Create the VM.** Sign up with **US East (Ashburn)** as the home region (Always Free resources
   only exist in the home region). Compute → Instances → Create instance: image **Canonical Ubuntu
   24.04** (aarch64), shape **VM.Standard.A1.Flex** with **2 OCPU / 12 GB** (inside the free
   4 OCPU / 24 GB), a public subnet with a public IPv4 address, and paste your SSH public key
   (`~/.ssh/id_ed25519.pub`). If A1 reports *Out of host capacity*, retry later or in another
   availability domain, or fall back to the AMD **VM.Standard.E2.1.Micro** (x86_64, 1 GB RAM; add a
   swap file before `uv sync`). Oracle may reclaim Always Free instances that stay nearly idle for 7
   days; upgrading the account to Pay As You Go (still free within the limits) avoids that.
2. **Lock it down.** Log in with `ssh ubuntu@<public-ip>`.
   - In the VCN's security list (Networking → Virtual cloud networks → subnet → security list),
     restrict the TCP 22 ingress rule's source from `0.0.0.0/0` to `<your-ip>/32`, or install
     [Tailscale](https://tailscale.com/kb/1031/install-linux) and remove the port 22 rule
     altogether. Add no other ingress rules: the monitor only makes outbound connections.
   - Keys only. This is a manual step: keep a second SSH session open while you do it, and test a
     fresh login before closing it, or you can lock yourself out. Create
     `/etc/ssh/sshd_config.d/99-hardening.conf` with
     ```
     PasswordAuthentication no
     KbdInteractiveAuthentication no
     PermitRootLogin no
     ```
     then `sudo sshd -t && sudo systemctl reload ssh`.
   - Security updates: `sudo apt install unattended-upgrades && sudo dpkg-reconfigure -plow
     unattended-upgrades` (enabled by default on Ubuntu cloud images; this confirms it).
   - Optional: `sudo timedatectl set-timezone America/New_York` so the daily files roll over at
     local midnight rather than UTC.
3. **Get the code onto the VM.** Either way, `setup_vps.sh` places it at
   `/home/moneymaker/MoneyMaker`.
   - *Deploy key* (lets `update` run `git pull`): copy just the script, then install from the SSH
     URL. The first run creates `/home/moneymaker/.ssh/id_ed25519` and prints its public key; add it
     on GitHub under the repo's Settings → Deploy keys with write access **off**, then rerun the
     same command.
     ```bash
     scp scripts/setup_vps.sh ubuntu@<ip>:
     ssh ubuntu@<ip> sudo bash setup_vps.sh install git@github.com:<you>/MoneyMaker.git
     ```
   - *rsync from the laptop* (no GitHub access from the VM):
     ```bash
     rsync -a --exclude .venv --exclude data --exclude .env --exclude secrets ./ ubuntu@<ip>:MoneyMaker/
     ssh ubuntu@<ip> sudo MoneyMaker/scripts/setup_vps.sh install MoneyMaker --move
     ```
     Without `--move` the checkout is copied instead.

   `install` installs git, curl, sqlite3 and rsync, creates the `moneymaker` user, installs uv for
   it, runs `uv sync`, creates `data/logs` and `secrets` (mode 700), writes
   `/etc/systemd/system/moneymaker-monitor.service` and enables it. It starts the service only
   once `.env` exists. It is safe to rerun. To change the monitor arguments, run it as
   `sudo MONITOR_ARGS='--scope crypto --match btc|eth' scripts/setup_vps.sh install`, or add an
   `Environment="MONITOR_ARGS=..."` override with `sudo systemctl edit moneymaker-monitor`.
4. **Copy the files that are not in git.** From the laptop, with a consistent database snapshot:
   ```bash
   sqlite3 data/matches.db ".backup /tmp/matches.db"
   scp .env secrets/kalshi.pem /tmp/matches.db ubuntu@<ip>:
   ```
   Then on the VM (stop the service first if it is already running):
   ```bash
   R=/home/moneymaker/MoneyMaker
   sudo install -m 600 -o moneymaker -g moneymaker ~/.env $R/.env
   sudo install -m 600 -o moneymaker -g moneymaker ~/kalshi.pem $R/secrets/kalshi.pem
   sudo install -m 600 -o moneymaker -g moneymaker ~/matches.db $R/data/matches.db
   rm ~/.env ~/kalshi.pem ~/matches.db
   sudo $R/scripts/setup_vps.sh restart
   ```
   Keep `KALSHI_PRIVATE_KEY_PATH=./secrets/kalshi.pem` in `.env`; the service runs from the repo.
5. **Operate it** (`S=/home/moneymaker/MoneyMaker/scripts/setup_vps.sh`):
   - `sudo $S status`: service state and the last 20 journal lines; `sudo $S logs` follows them.
   - Opportunities and grading, as the service user:
     ```bash
     sudo -u moneymaker -H bash -lc 'cd ~/MoneyMaker && uv run moneymaker opportunities --hours 24'
     sudo -u moneymaker -H bash -lc 'cd ~/MoneyMaker && uv run moneymaker review'
     ```
     From the laptop, prefix with `ssh -t ubuntu@<ip>`.
   - `sudo $S update` runs `git pull`, `uv sync` and restarts; with the rsync setup, rsync the
     laptop checkout to `~/MoneyMaker` again and run `sudo $S update ~/MoneyMaker` (keeps `.env`,
     `secrets`, `data` and `.venv`).
   - `sudo $S restart`; `sudo systemctl stop moneymaker-monitor` stops it until the next boot;
     `sudo $S uninstall` stops, disables and removes the unit but keeps the repo and data.

## Tests

```bash
uv run pytest && uv run ruff check src tests
```
