"""
Collect every pre-kickoff CS2 trade on Polymarket (all wallets, all markets, non-tier-1) for wallet mining.

One compact JSON file per match in local_state/mine/, so the run can be stopped and resumed and never has to be
downloaded twice. Each file: start time, title, Valve tier, and per market: label, outcomes, winner, and the trades
placed before kickoff (wallet, outcome, BUY/SELL, shares, price, time), sweeps removed.

Run: python mine_collect.py --days 60
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cs2_watch as C

OUT = Path("local_state/mine")


def one(e):
    path = OUT / f"{e['id']}.json"
    if path.exists():
        return "cached"
    start = C.iso(e["startTime"]).timestamp()
    markets = {}
    for m in e["markets"]:
        outs = json.loads(m.get("outcomes") or "[]")
        markets[m["conditionId"]] = dict(label=C.poly_market_label(m), outcomes=outs, winner=C.winner_of(m, True),
                                         trades=[])
    for t in C.poly_trades(e["id"]):
        mk = markets.get(t["conditionId"])
        if mk is None or t["timestamp"] >= start or not (1 - C.SWEEP) < t["price"] < C.SWEEP:
            continue
        mk["trades"].append([t["proxyWallet"].lower(), t["outcome"], t["side"][0], round(t["size"], 2),
                             round(t["price"], 4), t["timestamp"]])
    path.write_text(json.dumps(dict(id=e["id"], title=e["title"], start=start, tier=C.tier(e["title"]),
                                    vtier=C.vtier(e["title"]),
                                    markets=[dict(m, cid=c) for c, m in markets.items() if m["trades"]])))
    return "new"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=60)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    C.TEAMS = json.loads(Path("local_state/state_vrs").read_text())
    now = datetime.now(timezone.utc)
    evs, seen, lo = [], set(), now - timedelta(days=a.days)
    while lo < now:                                                  # 5-day slices: deep paging is refused (422)
        hi = min(lo + timedelta(days=5), now)
        off = 0
        while True:
            page = C.get(f"{C.GAMMA}/events?series_id={C.CS2_SERIES}&end_date_min={lo:%Y-%m-%dT%H:%M:%SZ}"
                         f"&end_date_max={hi:%Y-%m-%dT%H:%M:%SZ}&limit=100&offset={off}")
            for e in page:
                st = C.iso(e.get("startTime"))
                if e["id"] not in seen and st and st.timestamp() < (now - timedelta(hours=3)).timestamp()                         and e.get("markets") and C.tier(e["title"]) != "top":
                    seen.add(e["id"])
                    evs.append(e)
            if len(page) < 100:
                break
            off += 100
        lo = hi
    print(f"{len(evs)} finished non-tier-1 CS2 matches in {a.days:.0f} days", flush=True)
    done = 0
    with ThreadPoolExecutor(8) as ex:
        for r in ex.map(lambda e: (one(e) if True else None), evs):
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(evs)}", flush=True)
    print(f"done: {len(list(OUT.glob('*.json')))} matches on disk", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
