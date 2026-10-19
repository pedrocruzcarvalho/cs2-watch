"""
Do some Polymarket wallets keep winning low-tier CS2 bets far more often than the prices allow, and does following
them pay on matches they haven't been judged on yet? Walk-forward, no hindsight.

1. Every wallet's pre-kickoff position on every resolved market (net buys, average price paid, won or lost).
2. Walk forward match by match. A wallet "qualifies" using only its bets on matches that started 3+ hours before
   this one (so they had resolved): at least N bets, a luck score of at least Z (wins vs what the prices predicted),
   and an average edge of at least E.
3. When qualifying wallets bet a market (net $U+, more on one side than the other), follow that side with $100 at the
   market's last pre-kickoff price + 1c + fee. One bet per market.
Compared with: following any wallet with N+ bets (no skill filter), and backing the favourite.
Also lists "rings": wallets that kept backing the same side of the same low-tier matches.

Run: python mine_analyze.py
"""

import json
import math
import random
import sys
from collections import defaultdict
from itertools import combinations, product
from pathlib import Path

SPREAD, FEE = 0.01, 0.05
MIN_POS = 20


def pnl(price, won):
    p = min(price + SPREAD, 0.99)
    cost = p + FEE * p * (1 - p)
    return 100 / cost - 100 if won else -100


def summary(bets, label):
    if len(bets) < 5:
        return f"  {label:62} n={len(bets)}"
    xs = [pnl(b[0], b[1]) for b in bets]
    rng = random.Random(11)
    m = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(2000))
    return (f"  {label:62} {len(xs):5} bets  won {sum(b[1] for b in bets) / len(xs):4.0%} at "
            f"{sum(b[0] for b in bets) / len(xs):.2f}  ROI {sum(xs) / len(xs):+6.1f}%  [{m[100]:+.0f}%, {m[1900]:+.0f}%]")


def load():
    matches = []
    for f in Path("local_state/mine").glob("*.json"):
        d = json.loads(f.read_text())
        mk = []
        for m in d["markets"]:
            if not m["winner"] or len(m["outcomes"]) != 2:
                continue
            pos, last = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0]), None   # shares bought, usd bought, shares sold, usd sold
            for w, o, side, size, price, ts in m["trades"]:
                p = pos[(w, o)]
                if side == "B":
                    p[0] += size
                    p[1] += size * price
                else:
                    p[2] += size
                    p[3] += size * price
                if last is None or ts >= last[0]:
                    last = (ts, o, price)
            if not last:
                continue
            o0 = m["outcomes"][0]
            px0 = last[2] if last[1] == o0 else 1 - last[2]
            prices = {o0: px0, m["outcomes"][1]: 1 - px0}
            bets = []
            for (w, o), (sb, ub, ss, us) in pos.items():
                net_sh, net_usd = sb - ss, ub - us
                if net_usd < MIN_POS or sb <= 0:
                    continue
                avg = ub / sb
                if 0.03 < avg < 0.97:
                    bets.append(dict(w=w, o=o, usd=net_usd, avg=avg, won=o == m["winner"]))
            mk.append(dict(label=m["label"], outcomes=m["outcomes"], winner=m["winner"], prices=prices, bets=bets))
        if mk:
            matches.append(dict(id=d["id"], title=d["title"], start=d["start"], vtier=d.get("vtier"), markets=mk))
    return sorted(matches, key=lambda m: m["start"])


def walk(matches, N, Z, E, U, scope, tiers, mode="skill"):
    """Follow qualifying wallets. mode 'skill' = luck-score filter; 'any' = any wallet with N+ prior bets."""
    stats = defaultdict(lambda: [0, 0, 0.0, 0.0])                     # n, wins, sum p, sum p(1-p)
    queue, qi, out = [], 0, []
    for m in matches:
        for mk in m["markets"]:
            for b in mk["bets"]:
                queue.append((m["start"], b))
    queue.sort(key=lambda x: x[0])
    first = matches[0]["start"] if matches else 0
    for m in matches:
        while qi < len(queue) and queue[qi][0] <= m["start"] - 3 * 3600:  # resolved before this match
            _, b = queue[qi]
            s = stats[b["w"]]
            s[0] += 1
            s[1] += b["won"]
            s[2] += b["avg"]
            s[3] += b["avg"] * (1 - b["avg"])
            qi += 1
        if m["start"] - first < 20 * 86400 or (tiers == "low" and m["vtier"] in ("T1", "T2")):
            continue                                                   # 20 days to build records first
        for mk in m["markets"]:
            if scope == "match" and mk["label"] != "Match Winner":
                continue
            money = defaultdict(float)
            for b in mk["bets"]:
                s = stats.get(b["w"])
                if not s or s[0] < N:
                    continue
                if mode == "skill":
                    z = (s[1] - s[2]) / math.sqrt(s[3]) if s[3] > 0 else 0
                    if z < Z or (s[1] - s[2]) / s[0] < E:
                        continue
                money[b["o"]] += b["usd"]
            if not money:
                continue
            side = max(money, key=money.get)
            if money[side] < U or sum(v for o, v in money.items() if o != side) >= 0.5 * money[side]:
                continue
            price = mk["prices"].get(side)
            if price is None or not 0.03 < price < 0.97:
                continue
            out.append((price, side == mk["winner"], m["start"]))
    return out


def rings(matches, min_shared=3):
    """Pairs of wallets that backed the same side of the same low-tier match (T3-T6) at least `min_shared` times."""
    seen = defaultdict(list)
    for m in matches:
        if m["vtier"] in ("T1", "T2"):
            continue
        for mk in m["markets"]:
            if mk["label"] != "Match Winner":
                continue
            by_side = defaultdict(set)
            for b in mk["bets"]:
                if b["usd"] >= 100:
                    by_side[b["o"]].add(b["w"])
            for o, ws in by_side.items():
                if len(ws) > 40:
                    continue                                           # popular side: co-betting means nothing
                for a, b in combinations(sorted(ws), 2):
                    seen[(a, b)].append((m["title"], o == mk["winner"], mk["prices"].get(o)))
    return sorted(((k, v) for k, v in seen.items() if len(v) >= min_shared), key=lambda kv: -len(kv[1]))


def main():
    out = lambda *x: print(*x, flush=True)
    ms = load()
    nb = sum(len(mk["bets"]) for m in ms for mk in m["markets"])
    days = (ms[-1]["start"] - ms[0]["start"]) / 86400 if ms else 0
    out(f"{len(ms)} matches, {nb} wallet positions over {days:.0f} days; first 20 days only build records\n")
    out("baselines (same matches, after day 20):")
    fav = []
    for m in ms:
        if m["start"] - ms[0]["start"] < 20 * 86400:
            continue
        for mk in m["markets"]:
            if mk["label"] == "Match Winner":
                o = max(mk["prices"], key=mk["prices"].get)
                if 0.03 < mk["prices"][o] < 0.97:
                    fav.append((mk["prices"][o], o == mk["winner"]))
    out(summary(fav, "back the favourite (match winner)"))
    for N in (5, 10):
        out(summary(walk(ms, N, 0, -1, 0, "match", "all", mode="any"), f"follow any wallet with {N}+ past bets"))
    out("\nfollow 'too good' wallets (walk-forward):")
    res = []
    for N, Z, E, U, scope, tiers in product((5, 10, 20), (2.0, 2.5, 3.0), (0.10, 0.20), (0, 300), ("match", "all"),
                                             ("all", "low")):
        bets = walk(ms, N, Z, E, U, scope, tiers)
        res.append(((N, Z, E, U, scope, tiers), bets))
    for (N, Z, E, U, scope, tiers), bets in res:
        if (scope, tiers, U) == ("match", "all", 0) and E == 0.10:
            out(summary(bets, f"N>={N} bets, luck score>={Z}, edge>={E:.2f}"))
    good = [(k, b) for k, b in res if len(b) >= 20]
    if good:
        rois = sorted(sum(pnl(p, w) for p, w, _ in b) / len(b) for k, b in good)
        out(f"\n  across {len(good)} settings with 20+ bets: median ROI {rois[len(rois) // 2]:+.1f}%, "
            f"{sum(r > 0 for r in rois)} of {len(rois)} positive")
        k, b = max(good, key=lambda kb: sum(pnl(p, w) for p, w, _ in kb[1]) / len(kb[1]))
        mid = sorted(t for _, _, t in b)[len(b) // 2]
        out(summary([(p, w) for p, w, _ in b], f"best: {k}"))
        out(summary([(p, w) for p, w, t in b if t < mid], "   ...its first half"))
        out(summary([(p, w) for p, w, t in b if t >= mid], "   ...its second half"))
    out("\nrings: wallet pairs that backed the same side of the same T3-T6 match 3+ times")
    rs = rings(ms)
    out(f"  {len(rs)} pairs")
    for (a, b), v in rs[:12]:
        won = sum(x[1] for x in v)
        px = sum(x[2] or 0 for x in v) / len(v)
        out(f"  {a[:10]} + {b[:10]}: {len(v)} matches together, won {won}/{len(v)} at avg price {px:.2f}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
