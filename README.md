# cs2-watch (Fix Hunter)

Every 10 minutes (a Cloudflare cron worker starts the GitHub Actions scan) the **hunter** reads every CS2, Dota 2 and
League of Legends match on Polymarket, adds up each wallet's money per side (before kickoff and in-play apart) and
paper-bets the signals that passed a 60-day test, on low-tier events only. One paper account, started at $63.47.
A phone push (ntfy) goes out for each new bet, and when the scan fails or recovers.

The **mirror** tab paper-copies one wallet (@cutjalenhurts) in every market it trades, buys and sells, with $100,
for one week, without pushes.

- `hunter.py`        the rules (`classify`), scan, paper account, page data
- `hunt_collect.py`  60 days of finished matches and big wallets (local research data)
- `hunt_research.py` tests the rules on that data (`bankroll()` replays the hunter's own rules)
- `mirror.py`        the copy account
- `cloud_run.py`     one tick: hunter + mirror, state in Cloudflare KV, page data, pushes
- `notify.py`        phone alerts through ntfy.sh; needs the NTFY_TOPIC secret
- `site/` + `functions/`  dashboard on Cloudflare Pages (behind Cloudflare Access)
- `legacy_run.py` and the older modules: the previous multi-account scanner (radar, live money, odd shifts, tennis),
  no longer run

A flag is a reason to look closer, not proof of anything.
