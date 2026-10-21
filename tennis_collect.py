"""
Collect finished tennis matches on Polymarket (ITF, ATP incl. Challengers, WTA) with all their trades, before and
during the match, for fixing research (tennis_research.py).

One file per match in local_state/tennis/: start, title, level, volume, and per two-outcome market: label, outcomes,
winner, trades [wallet, outcome, B/S, shares, price, time] (sweeps at 0.97+/0.03- removed).

Run: python tennis_collect.py --days 30
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cs2_watch as C

OUT = Path("local_state/tennis")
SERIES = {"itf": 11634, "atp": 10365, "wta": 10366}


def one(args):
    e, tour = args
    path = OUT / f"{e['id']}.json"
    if path.exists():
        return "cached"
    st = C.iso(e.get("startTime") or e.get("startDate"))
    if not st:
        return "no start"
    markets = {}
    for m in e.get("markets") or []:
        outs = json.loads(m.get("outcomes") or "[]")
        if len(outs) != 2:
            continue
        markets[m["conditionId"]] = dict(label=m.get("groupItemTitle") or m.get("question") or "", outcomes=outs,
                                         winner=C.winner_of(m, True), trades=[])
    for t in C.poly_trades(e["id"]):
        mk = markets.get(t["conditionId"])
        if mk is None or not (1 - C.SWEEP) < t["price"] < C.SWEEP:
            continue
        mk["trades"].append([t["proxyWallet"].lower(), t["outcome"], t["side"][0], round(t["size"], 2),
                             round(t["price"], 4), t["timestamp"]])
    path.write_text(json.dumps(dict(id=e["id"], title=e["title"], tour=tour, start=st.timestamp(),
                                    volume=float(e.get("volume") or 0),
                                    markets=[dict(m, cid=c) for c, m in markets.items() if m["trades"]])))
    return "new"


def safe(a):
    try:
        return one(a)
    except Exception as err:
        print(f"  {a[0]['id']}: {err}", flush=True)
        return "error"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    evs, seen = [], set()
    for tour, sid in SERIES.items():
        lo = now - timedelta(days=a.days)
        while lo < now:                                              # 2-day slices: deep paging is refused
            hi = min(lo + timedelta(days=2), now)
            off = 0
            while True:
                page = C.get(f"{C.GAMMA}/events?series_id={sid}&end_date_min={lo:%Y-%m-%dT%H:%M:%SZ}"
                             f"&end_date_max={hi:%Y-%m-%dT%H:%M:%SZ}&limit=100&offset={off}")
                for e in page:
                    st = C.iso(e.get("startTime") or e.get("startDate"))
                    if e["id"] not in seen and st and st < now - timedelta(hours=4) and e.get("markets"):
                        seen.add(e["id"])
                        evs.append((e, tour))
                if len(page) < 100 or off >= 900:
                    break
                off += 100
            lo = hi
        print(f"{tour}: {sum(1 for _, t in evs if t == tour)} finished matches", flush=True)
    done = 0
    with ThreadPoolExecutor(6) as ex:
        for _ in ex.map(safe, evs):
            done += 1
            if done % 500 == 0:
                print(f"  {done}/{len(evs)}", flush=True)
    print(f"done: {len(list(OUT.glob('*.json')))} matches on disk", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
