"""
Tennis: do big or informed bets on Polymarket point to the winner more often than the price says?

Data: tennis_collect.py (30 days of ITF, ATP incl. Challengers, WTA; all trades, before and during matches).
Each match's winner market (the one whose outcomes are the two players). Levels: ITF; "small" ATP/WTA (event
volume under the tour's median, mostly Challengers / qualifying / small WTA); "big" ATP/WTA.

Tests, each copied 10 minutes after the signal at the price then, $100 + 1c + Polymarket fee, against the control
of backing the favourite at the same moment, and in both halves of the month:
  pre    one wallet's net pre-match position on one side reaches $U (less than half of that from big wallets on
         the other side)
  live   the same, bought during the match
  hot    wallets whose last 21 days of tennis bets (5+, $20+, before the match) won far more than their prices
         (luck 2.5+, edge 10+ pts) put $M+ on one side before the match

Run: python tennis_research.py
"""

import json
import math
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path

import fishy_research as F

DELAY = 600


def load():
    out = []
    for f in Path("local_state/tennis").glob("*.json"):
        d = json.loads(f.read_text())
        players = [p.strip() for p in d["title"].split(":", 1)[-1].split(" vs ")]
        mk = None
        for m in d["markets"]:
            if not m["winner"] or len(m["outcomes"]) != 2 or set(m["outcomes"]) & {"Yes", "No", "Over", "Under"}:
                continue
            if mk is None or len(m["trades"]) > len(mk["trades"]):
                mk = m
        if not mk:
            continue
        o0 = mk["outcomes"][0]
        tr = sorted(mk["trades"], key=lambda t: t[5])
        out.append(dict(id=d["id"], title=d["title"], tour=d["tour"], start=d["start"], volume=d["volume"],
                        outcomes=mk["outcomes"], winner=mk["winner"], trades=tr, times=[t[5] for t in tr],
                        px0=[t[4] if t[1] == o0 else 1 - t[4] for t in tr], players=players))
    med = {}
    for tour in ("atp", "wta"):
        v = sorted(m["volume"] for m in out if m["tour"] == tour)
        med[tour] = v[len(v) // 2] if v else 0
    for m in out:
        m["level"] = "ITF" if m["tour"] == "itf" else ("small " if m["volume"] < med[m["tour"]] else "big ") + m["tour"].upper()
        m["lvl"] = "ITF" if m["tour"] == "itf" else ("small" if m["volume"] < med[m["tour"]] else "big")
    return sorted(out, key=lambda m: m["start"])


def price(mk, o, ts, live):
    if live:
        i = bisect_left(mk["times"], ts)
        if i >= len(mk["times"]):
            return None
    else:
        i = bisect_right(mk["times"], min(ts, mk["start"] - 1)) - 1
        if i < 0:
            return None
    p0 = mk["px0"][i]
    return p0 if o == mk["outcomes"][0] else 1 - p0


def follow(mk, side, ts, live):
    p = price(mk, side, ts + DELAY, live)
    o0, o1 = mk["outcomes"]
    q0 = price(mk, o0, ts + DELAY, live)
    if p is None or q0 is None or not 0.03 < p < 0.97:
        return None
    fav = o0 if q0 >= 0.5 else o1
    return (mk["start"], F.pnl(p, side == mk["winner"]), F.pnl(q0 if fav == o0 else 1 - q0, fav == mk["winner"]), p)


def big(ms, U, live, levels):
    out = []
    for mk in ms:
        if mk["lvl"] not in levels:
            continue
        net = defaultdict(float)
        for w, o, side, size, px, ts in mk["trades"]:
            if (ts >= mk["start"]) != live:
                continue
            net[(w, o)] += size * px if side == "B" else -size * px
            if net[(w, o)] < U:
                continue
            other = sum(v for (w2, o2), v in net.items() if o2 != o and v >= U / 2)
            if other >= 0.5 * net[(w, o)]:
                continue
            r = follow(mk, o, ts, live)
            if r:
                out.append(r)
            break
    return out


def hot(ms, M, levels, Z=2.5, E=0.10, N=5):
    recs = defaultdict(list)                                   # wallet -> [(start, won, avg)]
    done, out = 0, []
    for mk in ms:
        while done < len(ms) and ms[done]["start"] <= mk["start"] - 4 * 3600:
            m2 = ms[done]
            buys = defaultdict(lambda: [0.0, 0.0, 0.0])
            for w, o, side, size, px, ts in m2["trades"]:
                if ts >= m2["start"]:
                    continue
                b = buys[(w, o)]
                if side == "B":
                    b[0] += size
                    b[1] += size * px
                else:
                    b[2] += size * px
            for (w, o), (sz, ub, us) in buys.items():
                if ub - us >= 20 and sz and 0.03 < ub / sz < 0.97:
                    recs[w].append((m2["start"], o == m2["winner"], ub / sz))
            done += 1
        if mk["lvl"] not in levels or mk["start"] - ms[0]["start"] < 10 * 86400:
            continue
        net = defaultdict(float)
        for w, o, side, size, px, ts in mk["trades"]:
            if ts >= mk["start"]:
                break
            rs = [r for r in recs.get(w, []) if r[0] >= mk["start"] - 21 * 86400]
            if len(rs) < N:
                continue
            ex, wn = sum(r[2] for r in rs), sum(r[1] for r in rs)
            var = sum(r[2] * (1 - r[2]) for r in rs)
            if var <= 0 or (wn - ex) / math.sqrt(var) < Z or (wn - ex) / len(rs) < E:
                continue
            net[o] += size * px if side == "B" else -size * px
            top = max(net, key=net.get)
            if net[top] >= M and sum(v for k, v in net.items() if k != top) < 0.5 * net[top]:
                r = follow(mk, top, ts, False)
                if r:
                    out.append(r)
                break
    return out


def show(name, res, mid):
    if len(res) < 10:
        print(f"  {name:46} n={len(res)}")
        return
    s, c = F.stats([x[1] for x in res]), F.stats([x[2] for x in res])
    a, b = F.stats([x[1] for x in res if x[0] < mid]), F.stats([x[1] for x in res if x[0] >= mid])
    print(f"  {name:46} {s['n']:5} bets  ROI {s['roi']:+6.1f}% [{s['lo']:+.0f},{s['hi']:+.0f}]  halves "
          f"{a['roi']:+5.0f}/{b['roi']:+5.0f}  favourite {c['roi']:+6.1f}%  avg px {sum(x[3] for x in res) / len(res):.2f}")


def main():
    ms = load()
    mid = ms[len(ms) // 2]["start"]
    from collections import Counter
    print(f"{len(ms)} matches with a winner market: {dict(Counter(m['level'] for m in ms))}\n")
    for levels, name in (({"ITF"}, "ITF"), ({"small"}, "small ATP/WTA"), ({"big"}, "big ATP/WTA"),
                         ({"ITF", "small"}, "ITF + small")):
        print(f"--- {name}")
        for live in (False, True):
            for U in (300, 1000, 3000, 10000):
                show(f"{'in-play' if live else 'pre-match'} one wallet ${U:,}+", big(ms, U, live, levels), mid)
        for M in (100, 500):
            show(f"hot wallets ${M}+ pre-match", hot(ms, M, levels), mid)
        print()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
