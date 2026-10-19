# cs2-watch

Every 10 minutes (GitHub Actions) this scans Counter-Strike match markets on Polymarket and Kalshi for
out-of-place bets (size, share of the match's money, fresh wallets, low-tier events), and keeps a paper
bankroll that bets the flagged side at kickoff: $40 HIGH / $20 MEDIUM / $10 LOW from a $300 start.

- `cs2_watch.py`  scanner, scoring, prediction ledger
- `bankroll.py`   paper staking plan
- `cloud_run.py`  one tick: scan, ledger, bankroll, publish (state in Cloudflare KV, not in this repo)
- `site/` + `functions/`  dashboard on Cloudflare Pages (behind Cloudflare Access)

A flag is a reason to look closer, not proof of anything.
