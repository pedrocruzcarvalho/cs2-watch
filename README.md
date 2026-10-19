# cs2-watch

Every 10 minutes (a Cloudflare cron worker starts the GitHub Actions scan) this checks every Counter-Strike
market on Polymarket (match, maps, totals, handicaps, rounds) for out-of-place bets: size, share of the money,
fresh wallets, split bets across fresh wallets, line moves, low-tier events. A paper bankroll ($300) bets the
flagged side when first flagged ($40 HIGH / $20 MEDIUM / $10 LOW), tops up on escalation, and fills every bet
against the real order book. Kalshi support remains in cs2_watch.py but is off in the cloud scan.

- `cs2_watch.py`  scanner, scoring, prediction ledger
- `bankroll.py`   paper staking plan
- `cloud_run.py`  one tick: scan, ledger, bankroll, publish (state in Cloudflare KV, not in this repo)
- `explain.py`    plain-English explanation of every bet
- `cron/`         Cloudflare worker that triggers the scan every 10 minutes
- `site/` + `functions/`  dashboard on Cloudflare Pages (behind Cloudflare Access)

A flag is a reason to look closer, not proof of anything.
