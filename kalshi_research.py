"""
Do big Kalshi orders during a CS2 match point to the winner more often than the price says?

Data: kalshi_collect.py (settled KXCS2GAME events with all trades). Kalshi has no wallets, so two signals:
  block   one taker order (fills on the same side in the same second) of $U+ during the match
  burst   $U+ on one side within 10 minutes during the match, with less than half of that on the other side
Each is copied 10 minutes later at the first trade price after that, $100 + 1c + the Polymarket fee (the paper
accounts bet on Polymarket), against the control of backing the favourite at that moment, in both halves.

Run: python kalshi_research.py
"""

import json
import sys
from bisect import bisect_left
from pathlib import Path

import cs2_watch as C
import fishy_research as F

TIERS = {"T3", "T4", "T5", "T6"}


def load():
    C.TEAMS = json.loads(Path("local_state/state_vrs").read_text())
    out = []
    for f in Path("local_state/kalshi").glob("*.json"):
        d = json.loads(f.read_text())
        if not d.get("winner") or len(d["trades"]) < 5:
            continue
        a = d["teams"][0]
        tr = d["trades"]
        out.append(dict(start=d["start"], title=d["title"], teams=d["teams"], winner=d["winner"], trades=tr,
                        times=[t[0] for t in tr], pa=[t[2] if t[1] == a else 1 - t[2] for t in tr],
                        vt=C.vtier("Counter-Strike: " + d["title"])))
    return sorted(out, key=lambda m: m["start"])


def price_after(m, team, ts):
    i = bisect_left(m["times"], ts)
    if i >= len(m["times"]):
        return None
    pa = m["pa"][i]
    return pa if team == m["teams"][0] else 1 - pa


def copy(m, team, ts):
    p = price_after(m, team, ts + 600)
    q = price_after(m, m["teams"][0], ts + 600)
    if p is None or q is None or not 0.05 < p < 0.95:
        return None
    a, b = m["teams"]
    fav = a if q >= 0.5 else b
    return (m["start"], F.pnl(p, team == m["winner"]), F.pnl(q if fav == a else 1 - q, fav == m["winner"]), p, m["vt"])


def blocks(m, U, live=True):
    cur = {}
    for ts, team, px, usd in m["trades"]:
        if (ts >= m["start"]) != live or (live and ts > m["start"] + 5 * 3600):
            continue
        k = (team, ts)
        cur[k] = cur.get(k, 0) + usd
        if cur[k] >= U:
            return copy(m, team, ts)
    return None


def burst(m, U, live=True, window=600):
    win = []
    for ts, team, px, usd in m["trades"]:
        if (ts >= m["start"]) != live or (live and ts > m["start"] + 5 * 3600):
            continue
        win = [w for w in win if w[0] > ts - window] + [(ts, team, usd)]
        mine = sum(w[2] for w in win if w[1] == team)
        other = sum(w[2] for w in win if w[1] != team)
        if mine >= U and other < 0.5 * mine:
            return copy(m, team, ts)
    return None


def show(name, res, mid):
    if len(res) < 10:
        print(f"  {name:44} n={len(res)}")
        return
    s, c = F.stats([x[1] for x in res]), F.stats([x[2] for x in res])
    a, b = F.stats([x[1] for x in res if x[0] < mid]), F.stats([x[1] for x in res if x[0] >= mid])
    print(f"  {name:44} n={s['n']:4}  ROI {s['roi']:+6.1f}% [{s['lo']:+.0f},{s['hi']:+.0f}]  halves {a['roi']:+5.0f}/{b['roi']:+5.0f}"
          f"  favourite {c['roi']:+6.1f}%  avg px {sum(x[3] for x in res) / len(res):.2f}")


def main():
    ms = load()
    mid = ms[len(ms) // 2]["start"]
    from collections import Counter
    print(f"{len(ms)} Kalshi CS2 matches; tiers {dict(Counter(m['vt'] for m in ms))}\n")
    for live in (True, False):
        print("=== " + ("IN-PLAY" if live else "PRE-MATCH"))
        for kind, fn in (("block", blocks), ("burst 10 min", burst)):
            for U in (500, 1000, 2000, 4000, 8000):
                for tname, tiers in (("T3-T6", TIERS), ("T3", {"T3"}), ("T4-T6", {"T4", "T5", "T6"}), ("T1-T2", {"T1", "T2"})):
                    res = [r for r in (fn(m, U, live) for m in ms if m["vt"] in tiers) if r]
                    show(f"{kind} ${U:,}+ {tname}", res, mid)
                    if tname == "T3-T6" and res:
                        show(f"   ...price < 0.65", [r for r in res if r[3] < 0.65], mid)
                        show(f"   ...price 0.65+", [r for r in res if r[3] >= 0.65], mid)
            print()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
