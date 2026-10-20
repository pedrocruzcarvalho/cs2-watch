"""
Do wallets that made big money on CS2 (whatever they did elsewhere) keep winning, and is copying them worth it,
before kickoff or in-play?

Winners are picked only from their CS2 history before a cutoff day (wallet_history.py: losers included), by CS2
profit and a minimum number of CS2 bets. Then their bets after the cutoff are copied: pre-match (mine_collect.py
trades) and in-play (mine_live.py trades, $50+), at the price after a 10-minute delay, $100 + 1c + fee.
Control: the favourite at the same moments.

Run: python winners_live.py
"""

import sys
from collections import defaultdict

import fishy_research as F
import live_research as L
import wallet_skill as S


def cs2_profit(rows, before):
    return sum((r[3] / r[1] - r[3]) if r[2] else -r[3] for r in rows if r[0] <= before)


def copy(markets, who, cut, U, delay, tiers, inplay):
    out = []
    for mk in markets:
        if mk["start"] < cut or tiers and mk["vt"] not in tiers:
            continue
        net = defaultdict(float)
        trades = mk["tr"] if inplay else mk["trades"]
        for w, o, side, size, price, ts in trades:
            if w not in who:
                continue
            net[(w, o)] += size * price if side == "B" else -size * price
            if net[(w, o)] < U:
                continue
            other = sum(v for (w2, o2), v in net.items() if o2 != o and v > 0)
            if other >= 0.5 * net[(w, o)]:
                continue
            t = ts + delay if inplay else min(ts + delay, mk["start"] - 1)
            get = L.after if inplay else F.price_at
            p = get(mk, o, t)
            o0, o1 = mk["outcomes"]
            q0 = get(mk, o0, t)
            if p is None or q0 is None or not 0.03 < p < 0.97:
                break
            fav = o0 if q0 >= 0.5 else o1
            out.append((mk["start"], F.pnl(p, o == mk["winner"]), F.pnl(q0 if fav == o0 else 1 - q0, fav == mk["winner"])))
            break
    return out


def main():
    H = S.histories()
    pre = S.load_markets()
    live = L.load()
    t0 = pre[0]["start"]
    for cut_day in (20, 30, 40):
        cut = t0 + cut_day * 86400
        print(f"--- winners picked on CS2 history before day {cut_day}, copied after it")
        for min_profit, min_n in ((10000, 30), (25000, 50), (50000, 50), (100000, 100)):
            who = {w for w, h in H.items()
                   if sum(1 for r in h["rows"] if r[0] <= cut) >= min_n and cs2_profit(h["rows"], cut) >= min_profit}
            for inplay in (False, True):
                for U, tiers in ((200, None), (1000, None), (1000, ("T3", "T4", "T5", "T6"))):
                    r = copy(live if inplay else pre, who, cut, U, 600, tiers, inplay)
                    s, c = F.stats([x[1] for x in r]), F.stats([x[2] for x in r])
                    print(f"  CS2 profit >= ${min_profit:>7,} ({len(who):3} wallets) {'in-play' if inplay else 'pre    '} "
                          f"${U:>5} {'T3-T6' if tiers else 'all  '}: {s['n']:4} bets  copy {s['roi']:+6.1f}% "
                          f"[{s['lo']:+.0f},{s['hi']:+.0f}]  favourite {c['roi']:+6.1f}%")
        print()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
