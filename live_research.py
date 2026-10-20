"""
Do big bets placed DURING a match (in-play) point to the winner more often than the price says?

Uses the in-play trades of $50+ (mine_live.py) and the winners from the mined matches. A signal is the moment one
wallet's net in-play position on one side of a market crosses $U, with less than half of that from other big
wallets on the other side. We bet $100 on that side at the price of the first trade after a delay (1 or 10 minutes),
+1c spread + fee. Control: on the same market at the same moment, back the side that is the favourite then.
Judged on the first and second half of the days separately.

Run: python live_research.py
"""

import json
import sys
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

import fishy_research as F


def load():
    out = []
    for f in Path("local_state/mine").glob("*.json"):
        lf = Path("local_state/mine_live") / f.name
        if not lf.exists():
            continue
        d = json.loads(f.read_text())
        live = json.loads(lf.read_text())
        by = defaultdict(list)
        for r in live:
            by[r[0]].append(r[1:])
        for m in d["markets"]:
            tr = sorted(by.get(m["cid"], []), key=lambda t: t[5])
            if not m["winner"] or len(m["outcomes"]) != 2 or len(tr) < 3:
                continue
            o0 = m["outcomes"][0]
            out.append(dict(start=d["start"], vt=d.get("vtier") or "T6", mw=m["label"] == "Match Winner",
                            outcomes=m["outcomes"], winner=m["winner"], tr=tr, times=[t[5] for t in tr],
                            px0=[t[4] if t[1] == o0 else 1 - t[4] for t in tr]))
    return sorted(out, key=lambda m: m["start"])


def after(mk, o, ts):
    i = bisect_left(mk["times"], ts)
    if i >= len(mk["times"]):
        return None
    p0 = mk["px0"][i]
    return p0 if o == mk["outcomes"][0] else 1 - p0


def run(ms, U, delay, tiers=None, mw=None, px="any"):
    res = []
    for mk in ms:
        if tiers and mk["vt"] not in tiers or mw is not None and mk["mw"] != mw:
            continue
        net = defaultdict(float)
        for w, o, side, size, price, ts in mk["tr"]:
            net[(w, o)] += size * price if side == "B" else -size * price
            if net[(w, o)] < U:
                continue
            other = sum(v for (w2, o2), v in net.items() if o2 != o and v >= U / 2)
            if other >= 0.5 * net[(w, o)]:
                continue
            p_sig = price
            if px == "dog" and p_sig >= 0.5 or px == "fav" and p_sig < 0.5:
                break
            p = after(mk, o, ts + delay)
            o0, o1 = mk["outcomes"]
            q0 = after(mk, o0, ts + delay)
            if p is None or not 0.03 < p < 0.97:
                break
            fav = o0 if q0 >= 0.5 else o1
            pf = q0 if fav == o0 else 1 - q0
            res.append((mk["start"], F.pnl(p, o == mk["winner"]), F.pnl(pf, fav == mk["winner"]), p))
            break
    return res


def main():
    ms = load()
    split = ms[len(ms) // 2]["start"]
    print(f"{len(ms)} markets with in-play trades\n")
    for tiers, tname in ((None, "all tiers"), (("T3", "T4", "T5", "T6"), "T3-T6"), (("T4", "T5", "T6"), "T4-T6")):
        for mw in (None, True):
            for px in ("any", "dog", "fav"):
                for U in (1000, 3000, 10000):
                    for delay in (60, 600):
                        r = run(ms, U, delay, tiers, mw, px)
                        if len(r) < 20:
                            continue
                        a = F.stats([x[1] for x in r if x[0] < split])
                        b = F.stats([x[1] for x in r if x[0] >= split])
                        ca = F.stats([x[2] for x in r if x[0] < split])
                        cb = F.stats([x[2] for x in r if x[0] >= split])
                        print(f"  {tname:9} {'match winner' if mw else 'all markets':12} {px:3} ${U:>5} {delay // 60:>2} min  "
                              f"follow {a['roi']:+6.1f}% / {b['roi']:+6.1f}% (n {a['n']}/{b['n']})   "
                              f"favourite {ca['roi']:+6.1f}% / {cb['roi']:+6.1f}%")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
