# NSE Trading Dashboard — Project Memory

## Project
**NSE 4-Strategy Dashboard** — single-file `nse_live_dashboard.html` (~2,600 lines, no framework), Python scripts in `nse-trading-bot/`, GitHub Actions cron, Vercel static + serverless. Repo: `Dushyant-dataanalyst/job-command-center`. Owner: Dushyant, ₹1Cr goal in 2 years, ~₹1.5L MIS capital.

## Key facts
- **4-Strategy** = 4 voters (Inna, Pham, Cianni, Unger) each vote BUY/STRONG_BUY/WATCH/NO_SIGNAL per stock; weighted consensus via `voter_weights.json`. Logic in `equity_scan_core.py` is a *reconstruction* from one-line hints, not a verified spec.
- Every JSON feed at repo root is CI-generated; dashboard fetches client-side. `vercel.json` needs a no-cache route per new JSON file.
- Timestamps: always use `ist_time.now_ist_str()` — bare `datetime.now()` on CI runners is UTC mislabeled as IST (past bug).
- All JSON writes: `write_text(..., encoding="utf-8")` — Windows cp1252 default corrupted files before.
- Emoji in Python source/print crashes Windows cp1252 console — use `[OK]`/`[WARNING]` in scripts.
- Main cron `nse_fo_refresh.yml` (*/5 02:00-10:55 UTC + 15:30 UTC prep): 7 per-run scripts + once-daily (prep-gated) equity scan, voter weights, snapshot. Each step `continue-on-error`, validated JSON before commit, heartbeat ping (healthchecks.io), Telegram alert on **every** run (INFO on success, CRITICAL on any step failure) via `nse-trading-bot/alerts.py`'s `send_alert(message, level)`.
- `concurrency.cancel-in-progress` is `false` (not `true`) — `logs/run_history.json` changes every execution and forces a commit, so commit-count is a reliable execution-count proxy; with `true`, most runs got killed by the next 5-min tick before reaching the commit step. Don't re-flip this without re-checking commit cadence first.
- GitHub's scheduler heavily throttles */5 crons — real cadence can be hours. Known issue, don't re-diagnose from scratch.
- Kite Connect fallback (`kite_fallback.py`): daily manual token via `kite_auth_refresh.yml` workflow_dispatch; session in `kite_session.json` (expires daily). Quote-only — no historical data, no orders.
- `daily-jobs.yml` + `fetch_jobs.py` = unrelated job-search feature in same repo; don't touch.

## Preferences (standing)
- **Never handle credentials** — user adds tokens via GitHub/Vercel UI only; if pasted in chat, tell them to revoke.
- Verify with real data before claiming done; no fabricated numbers; label estimates honestly.
- Signals are educational; SEBI: manual orders only; never enable live trading autonomously.
- Rebase over CI bot commits (`git pull --rebase`, take `--theirs` for data JSONs) before push.
- Light theme on this dashboard (dark premium preferred elsewhere — ask first).
- Responsive pattern: `clamp()` type, `auto-fill` minmax grids, scroll-snap tabs, column-hiding tables.

## Master Brief (Downloads/NSE_Trading_System_Master_Brief.md)
Locked config: risk 1% MIS / 1.5% CNC, min R:R 1.5/2.0, max 20%/position, kill switches 2% daily / 5% weekly, DRY_RUN default — all in `nse-trading-bot/config.py`. These 2%/5% kill switches are Module 3 (not built), and are intentionally distinct from the dashboard's own existing circuit breaker (`DAILY_MAX_LOSS_PCT`/`WEEKLY_MAX_LOSS_PCT` = -3%/-8% in `nse_live_dashboard.html`) — user explicitly said keep the dashboard one as-is, don't reconcile the two. Order code needs static whitelisted IP (SEBI Apr 2026) — cannot run on GH Actions/Vercel. Build gates: audit → approval → backtest FIRST → executor → filters → safety. System B (quality-growth screen) = separate folder, never orders.
- **Module 2 (`nse-trading-bot/trade_filters.py`) built, filters 1-4 only**, per brief's own "build 1-4, pause, then 5-6": market regime (reads `market_regime.json`'s avoid-list), time window (first/last 15min + optional lunch), loss streak breaker (3 consecutive losses), R:R rejection (1.5 MIS / 2.0 CNC). Standalone/testable via `python trade_filters.py` — **not yet wired into any trade-opening code** (e.g. `trade_brain.py`'s `_open_trade()`) since that would change existing paper-trading behavior; needs explicit user go-ahead first. Filters 5 (correlation/exposure) and 6 (liquidity/spread) not built — paused per the brief.
