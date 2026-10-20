"""
Snipers: wallets that look like the ones shyux calls out (YourBookmaker: CS2 only, new, a few big bets on T4-T6
matches, often in-play, almost always right). Can they be found from their history alone, and does copying them pay?

Walk-forward. For each market, a wallet's profile uses only its CS2 positions on days before the match
(wallet_history.py): number of CS2 bets, win rate, edge over its prices, average size, and how much of all its
Polymarket activity is CS2 (whole-history share, the one small look-ahead). When a profiled wallet puts $M+ on one
side (pre-match or in-play), copy it 10 minutes later at the price then, $100 + 1c + fee. Control: favourite then.

Run: python sniper_research.py
"""

import sys
from collections import defaultdict

import fishy_research as F
import live_research as L
import wallet_skill as S


def profile(h, before):
    rs = [r for r in h["rows"] if r[0] <= before]
    if not rs:
        return None
    n, wins = len(rs), sum(r[2] for r in rs)
    return dict(n=n, won=wins / n, edge=(wins - sum(r[1] for r in rs)) / n, size=sum(r[3] for r in rs) / n,
                share=len(h["rows"]) / max(h["n_closed"] + h.get("n_dead", 0), len(h["rows"]), 1))


def run(markets, H, cfg, inplay, get):
    out = []
    for mk in markets:
        if cfg["tiers"] and mk["vt"] not in cfg["tiers"]:
            continue
        net = defaultdict(float)
        prof = {}
        for w, o, side, size, price, ts in (mk["tr"] if inplay else mk["trades"]):
            if w not in H:
                continue
            if w not in prof:
                prof[w] = profile(H[w], mk["start"] - 86400)
            p = prof[w]
            if not p or not (cfg["n"][0] <= p["n"] <= cfg["n"][1]) or p["won"] < cfg["won"] or p["edge"] < cfg["edge"] \
                    or p["size"] < cfg["size"] or p["share"] < cfg["share"]:
                continue
            net[(w, o)] += size * price if side == "B" else -size * price
            if net[(w, o)] < cfg["M"]:
                continue
            t = ts + 600 if inplay else min(ts + 600, mk["start"] - 1)
            px = get(mk, o, t)
            o0, o1 = mk["outcomes"]
            q0 = get(mk, o0, t)
            if px is None or q0 is None or not 0.03 < px < 0.97:
                break
            fav = o0 if q0 >= 0.5 else o1
            out.append((mk["start"], F.pnl(px, o == mk["winner"]), F.pnl(q0 if fav == o0 else 1 - q0, fav == mk["winner"]), w))
            break
    return out


def main():
    H = S.histories()
    import json
    from pathlib import Path
    for f in Path("local_state/hist").glob("*.json"):
        h = json.loads(f.read_text())
        if h["wallet"] in H:
            H[h["wallet"]]["n_closed"] = h["n_closed"]
    pre, live = S.load_markets(), L.load()
    mid = pre[len(pre) // 2]["start"]
    print(f"{len(H)} wallets\n")
    rows = []
    for n in ((3, 40), (5, 80), (3, 200)):
        for won in (0.6, 0.7):
            for edge in (0.05, 0.15):
                for size in (100, 500):
                    for share in (0.0, 0.5, 0.8):
                        for M in (300, 1000):
                            for tiers in (None, ("T3", "T4", "T5", "T6")):
                                cfg = dict(n=n, won=won, edge=edge, size=size, share=share, M=M, tiers=tiers)
                                for inplay in (False, True):
                                    r = run(live if inplay else pre, H, cfg, inplay, L.after if inplay else F.price_at)
                                    if len(r) < 20:
                                        continue
                                    a = F.stats([x[1] for x in r if x[0] < mid])
                                    b = F.stats([x[1] for x in r if x[0] >= mid])
                                    s, c = F.stats([x[1] for x in r]), F.stats([x[2] for x in r])
                                    rows.append((cfg, inplay, s, a, b, c, len({x[3] for x in r})))
    good = [x for x in rows if x[3]["roi"] > 0]
    print(f"{len(rows)} settings with 20+ bets; positive in the first half: {len(good)}, of those also in the second: "
          f"{sum(x[4]['roi'] > 0 for x in good)}")
    for label, sel in (("in-play", True), ("pre-match", False)):
        rs = [x for x in rows if x[1] == sel]
        if not rs:
            continue
        print(f"\n{label}: median ROI over {len(rs)} settings {sorted(x[2]['roi'] for x in rs)[len(rs) // 2]:+.1f}%; "
              f"top 12 by the worse half:")
        for cfg, _, s, a, b, c, nw in sorted(rs, key=lambda x: -min(x[3]["roi"], x[4]["roi"]))[:12]:
            print(f"  bets {cfg['n'][0]}-{cfg['n'][1]} won>={cfg['won']} edge>={cfg['edge']} size>=${cfg['size']} "
                  f"cs2share>={cfg['share']} ${cfg['M']} {'T3-6' if cfg['tiers'] else 'all '}: {s['n']:4} bets ({nw} wallets) "
                  f"{s['roi']:+6.1f}% [{s['lo']:+.0f},{s['hi']:+.0f}] halves {a['roi']:+.0f}/{b['roi']:+.0f}  fav {c['roi']:+.1f}%")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
