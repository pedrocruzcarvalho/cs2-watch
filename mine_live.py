"""
Collect the in-play trades of the mined matches (mine_collect.py keeps only pre-kickoff ones), for testing whether
suspicious bets placed during the match say more than pre-match ones.

Only trades of $50+ are fetched (the trades API stops at 3,000 per match), from kickoff to 5 hours after it.
One file per match in local_state/mine_live/: [conditionId, wallet, outcome, B/S, shares, price, time].

Run: python mine_live.py
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cs2_watch as C

SRC, OUT = Path("local_state/mine"), Path("local_state/mine_live")
MIN_CASH = 50


def one(f):
    path = OUT / f.name
    if path.exists():
        return "cached"
    d = json.loads(f.read_text())
    start = d["start"]
    rows = []
    for off in range(0, 3000, 500):
        page = C.get(f"{C.DATA}/trades?eventId={d['id']}&limit=500&offset={off}&filterType=CASH&filterAmount={MIN_CASH}")
        for t in page:
            if start <= t["timestamp"] < start + 5 * 3600:
                rows.append([t["conditionId"], t["proxyWallet"].lower(), t["outcome"], t["side"][0],
                             round(t["size"], 2), round(t["price"], 4), t["timestamp"]])
        if len(page) < 500:
            break
    path.write_text(json.dumps(rows))
    return "new"


def safe(f):
    try:
        return one(f)
    except Exception as e:
        print(f"  {f.name}: {e}", flush=True)
        return "error"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    files = sorted(SRC.glob("*.json"))
    print(f"{len(files)} matches", flush=True)
    done = 0
    with ThreadPoolExecutor(3) as ex:
        for _ in ex.map(safe, files):
            done += 1
            if done % 250 == 0:
                print(f"  {done}/{len(files)}", flush=True)
    print(f"done: {len(list(OUT.glob('*.json')))} on disk", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
