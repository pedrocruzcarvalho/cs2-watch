"""
Deep search for wallets like the suspected fixers Pedro flagged (TopStreamer, 8988shrederd): mostly CS2, big stakes
from their very first bets, and a very high hit rate.

1. Every CS2 event on Polymarket in the last N days; every wallet with a trade of $500+ in one (Data API v2).
2. Each candidate's whole history (all positions, unredeemed losers included).
3. Per wallet, counted per MATCH (a match's markets summed, so hedging a match against its maps doesn't count as a
   loss): CS2 share, matches won, CS2 profit and return, average stake of its first 3 CS2 matches, join date.
Writes local_state/deep_search.json and prints the wallets that fit the pattern.

Run: python deep_search.py --days 60
"""

import argparse
import json
import statistics as S
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cs2_watch as C
import polydata as P

OUT = Path("local_state/deep_search.json")
KNOWN = {"0xdced0269e8432039c98070b98b7afbd43c1dc799": "TopStreamer", "0x7034b332965bc63ae0ff2efd9eaa72cc7400f3ad": "8988shrederd"}


def events(days):
    now = datetime.now(timezone.utc)
    out, seen, lo = [], set(), now - timedelta(days=days)
    while lo < now:
        hi = min(lo + timedelta(days=5), now)
        off = 0
        while True:
            page = C.get(f"{C.GAMMA}/events?series_id={C.CS2_SERIES}&end_date_min={lo:%Y-%m-%dT%H:%M:%SZ}"
                         f"&end_date_max={hi:%Y-%m-%dT%H:%M:%SZ}&limit=100&offset={off}")
            for e in page:
                if e["id"] not in seen:
                    seen.add(e["id"])
                    out.append(e["id"])
            if len(page) < 100 or off >= 900:
                break
            off += 100
        lo = hi
    return out


def big_traders(eid):
    try:
        return [(t["proxyWallet"].lower(), t["size"] * t["price"]) for t in P.event_trades(eid, cap=1000, min_cash=500)
                if t["side"] == "BUY"]
    except Exception:
        return []


def profile(w):
    try:
        ps = P.user_positions(w, statuses=("CLOSED", "REDEEMABLE", "REDEEMABLE_LOST"), cap=3000)
        st = P.user_stats(w)
    except Exception as e:
        return dict(wallet=w, error=str(e))
    cs = [p for p in ps if (p.get("event_slug") or p.get("slug") or "").startswith("cs2-")]
    by = defaultdict(lambda: dict(pnl=0.0, stake=0.0, first=9e18, title=""))
    for p in cs:
        m = by[p.get("event_slug") or p.get("slug")]
        m["pnl"] += p.get("total_pnl") or 0
        m["stake"] += (p.get("avg_price") or 0) * (p.get("total_size") or 0)
        m["first"] = min(m["first"], p.get("first_entry_at") or 9e18)
        m["title"] = m["title"] or (p.get("title") or "")
    matches = sorted(by.values(), key=lambda m: m["first"])
    staked = sum(m["stake"] for m in matches)
    profit = sum(m["pnl"] for m in matches)
    won = sum(1 for m in matches if m["pnl"] > 0)
    first3 = [m["stake"] for m in matches[:3]]
    return dict(wallet=w, name=None, joined=st.get("join_date"), trades=st.get("trades"), positions=len(ps),
                cs2_share=round(len(cs) / max(len(ps), 1), 3), matches=len(matches), won=won,
                win_rate=round(won / max(len(matches), 1), 3), cs2_profit=round(profit), cs2_staked=round(staked),
                roi=round(profit / staked, 3) if staked else 0, first3_stake=round(S.mean(first3)) if first3 else 0,
                median_stake=round(S.median([m["stake"] for m in matches])) if matches else 0,
                all_profit=round(sum(p.get("total_pnl") or 0 for p in ps)),
                recent=[dict(t=m["first"], title=m["title"][:70], stake=round(m["stake"]), pnl=round(m["pnl"])) for m in matches[-6:]])


def fits(p):
    """Mostly CS2 and big from the first bets, then either almost always right (8988shrederd: 11 of 12 matches) or
    big and clearly profitable (TopStreamer: +$40k on CS2, +18%, though only 48% of matches won)."""
    base = p.get("cs2_share", 0) >= 0.7 and p.get("first3_stake", 0) >= 1000 and 4 <= p.get("matches", 0) <= 400
    sharp = p.get("win_rate", 0) >= 0.7 and p.get("roi", 0) >= 0.15
    heavy = p.get("cs2_profit", 0) >= 10000 and p.get("roi", 0) >= 0.15 and p.get("matches", 0) >= 10
    return base and (sharp or heavy)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=60)
    a = ap.parse_args()
    evs = events(a.days)
    print(f"{len(evs)} CS2 events in {a.days:.0f} days", flush=True)
    seen = defaultdict(lambda: [0, 0.0])
    with ThreadPoolExecutor(8) as ex:
        for i, tr in enumerate(ex.map(big_traders, evs)):
            for w, usd in tr:
                seen[w][0] += 1
                seen[w][1] += usd
            if (i + 1) % 500 == 0:
                print(f"  events {i + 1}/{len(evs)}, {len(seen)} wallets so far", flush=True)
    cands = [w for w, (n, usd) in seen.items() if usd >= 1000] + [w for w in KNOWN if w not in seen]
    print(f"{len(cands)} wallets with $1k+ in big CS2 trades; reading their histories", flush=True)
    profs = []
    with ThreadPoolExecutor(6) as ex:
        for i, p in enumerate(ex.map(profile, cands)):
            profs.append(p)
            if (i + 1) % 250 == 0:
                print(f"  wallets {i + 1}/{len(cands)}", flush=True)
    OUT.write_text(json.dumps(profs))
    good = sorted((p for p in profs if fits(p)), key=lambda p: -p["cs2_profit"])
    print(f"\n{len(good)} wallets fit the pattern (70%+ CS2, 4+ matches, 70%+ won, +15% return, first bets $1k+):")
    for p in good[:60]:
        tag = f" <- {KNOWN[p['wallet']]}" if p["wallet"] in KNOWN else ""
        print(f"  {p['wallet']}  joined {time.strftime('%Y-%m-%d', time.gmtime(p['joined'] or 0))}  {p['matches']:3} matches "
              f"won {p['win_rate']:.0%}  CS2 ${p['cs2_profit']:>8,} ({p['roi']:+.0%})  first bets ${p['first3_stake']:,}  "
              f"CS2 {p['cs2_share']:.0%}{tag}")
    for w, nm in KNOWN.items():
        p = next((x for x in profs if x["wallet"] == w), None)
        if p and not fits(p):
            print(f"  (check) {nm} does NOT fit: {({k: p.get(k) for k in ('cs2_share', 'matches', 'win_rate', 'roi', 'first3_stake')})}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
