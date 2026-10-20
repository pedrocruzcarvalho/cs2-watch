"""
Is there an edge in big, out-of-place bets ("fishy" bets), and what is the best way to bet them?

Replays every pre-kickoff trade of the mined matches (mine_collect.py) in time order. A signal is the moment one
wallet's net position on one side of a market crosses $U. Each signal carries what was knowable then: its size, its
size against what a whole match of that Valve tier normally draws before kickoff, whether the wallet had ever bet
CS2 before (in the data, after a 10-day warm-up), hours to kickoff, the side's price, tier, market type.

Settings tried (one bet per market, the first signal that passes):
  size $U, size vs tier normal, new wallets only, tiers, market type, underdog/favourite side, follow or fade,
  and when to bet: at the signal (price 10 minutes later, like the 10-minute scan), 10 minutes before kickoff, or the
  signal only if it came in the last N hours.
A bet is $100 at the last traded price + 1c spread + Polymarket's fee. Settings are ranked on the first half of the
days and judged on the second half, which they never saw. Also reported: how many settings that looked good on
the first half stayed positive on the second (if about half, the "edges" are noise).

Run: python fishy_research.py
"""

import itertools
import json
import random
import sys
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

SPREAD, FEE = 0.01, 0.05
DELAY = 600                                       # the cron scans every 10 minutes
TIER_NORMAL = {"T1": 11513, "T2": 8001, "T3": 2371, "T4": 739, "T5": 805, "T6": 375}
WARMUP = 10 * 86400


def pnl(price, won):
    p = min(price + SPREAD, 0.99)
    cost = p + FEE * p * (1 - p)
    return 100 / cost - 100 if won else -100


def stats(xs):
    if not xs:
        return dict(n=0, roi=0.0, lo=0.0, hi=0.0)
    rng = random.Random(3)
    m = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(1000))
    return dict(n=len(xs), roi=sum(xs) / len(xs), lo=m[50], hi=m[950])


def load(src="local_state/mine"):
    files = [json.loads(f.read_text()) for f in Path(src).glob("*.json")]
    files.sort(key=lambda d: d["start"])
    t0 = files[0]["start"]
    seen = {}                                      # wallet -> first match start it bet on
    for d in files:
        for m in d["markets"]:
            for w, *_ in m["trades"]:
                seen.setdefault(w, d["start"])
    markets = []
    for d in files:
        start, vt = d["start"], d.get("vtier") or "T6"
        for m in d["markets"]:
            if not m["winner"] or len(m["outcomes"]) != 2 or not m["trades"]:
                continue
            o0, o1 = m["outcomes"]
            tr = sorted(m["trades"], key=lambda t: t[5])
            times = [t[5] for t in tr]
            px0 = [t[4] if t[1] == o0 else 1 - t[4] for t in tr]   # price of outcome 0 after each trade
            net = defaultdict(float)
            sigs = []
            fired = set()
            for w, o, side, size, price, ts in tr:
                net[(w, o)] += size * price if side == "B" else -size * price
                for U in (1000, 2000, 3000, 5000, 10000, 20000):
                    if net[(w, o)] >= U and (w, o, U) not in fired:
                        fired.add((w, o, U))
                        other = sum(v for (w2, o2), v in net.items() if o2 != o and v >= U / 2)
                        sigs.append(dict(U=U, ts=ts, w=w, o=o, usd=net[(w, o)], other=other,
                                         new=start - t0 >= WARMUP and seen[w] >= start - 3 * 3600))
            if not sigs:
                continue
            markets.append(dict(start=start, vt=vt, mw=m["label"] == "Match Winner", label=m["label"],
                                outcomes=(o0, o1), winner=m["winner"], times=times, px0=px0, sigs=sigs,
                                id=d["id"], title=d["title"]))
    return markets


def price_at(mk, o, ts):
    i = bisect_right(mk["times"], ts) - 1
    if i < 0:
        return None
    p0 = mk["px0"][i]
    return p0 if o == mk["outcomes"][0] else 1 - p0


def run(markets, cfg):
    """[(start, pnl)] for one setting."""
    out = []
    for mk in markets:
        if cfg["tiers"] and mk["vt"] not in cfg["tiers"]:
            continue
        if cfg["mw"] and not mk["mw"]:
            continue
        sig = None
        for s in mk["sigs"]:
            if s["U"] != cfg["U"]:
                continue
            if cfg["rel"] and s["usd"] / TIER_NORMAL.get(mk["vt"], 375) < cfg["rel"]:
                continue
            if cfg["new"] and not s["new"]:
                continue
            if cfg["window"] and mk["start"] - s["ts"] > cfg["window"] * 3600:
                continue
            if s["other"] >= 0.5 * s["usd"]:
                continue
            sig = s
            break
        if not sig:
            continue
        side = sig["o"] if cfg["dir"] == "follow" else next(o for o in mk["outcomes"] if o != sig["o"])
        if cfg["entry"] == "now":
            t = min(sig["ts"] + DELAY, mk["start"] - 1)
        else:                                       # 10 minutes before kickoff (or at the signal, if later)
            t = max(min(sig["ts"] + DELAY, mk["start"] - 1), mk["start"] - 600)
        p = price_at(mk, side, t)
        if p is None or not 0.03 < p < 0.97:
            continue
        sp = price_at(mk, sig["o"], sig["ts"])
        if cfg["px"] == "dog" and sp >= 0.5 or cfg["px"] == "fav" and sp < 0.5:
            continue
        out.append((mk["start"], pnl(p, side == mk["winner"]), p, side == mk["winner"]))
    return out


GRID = dict(U=[1000, 2000, 3000, 5000, 10000, 20000], rel=[0, 1, 3], new=[False, True],
            tiers=[None, ("T3", "T4", "T5", "T6"), ("T4", "T5", "T6")], mw=[False, True], px=["any", "dog", "fav"],
            dir=["follow", "fade"], entry=["now", "k10"], window=[0, 6, 1])


def main():
    markets = load()
    starts = sorted(m["start"] for m in markets)
    split = starts[len(starts) // 2]
    print(f"{len(markets)} markets with a $1k+ position, split at day {(split - starts[0]) / 86400:.0f} of "
          f"{(starts[-1] - starts[0]) / 86400:.0f}\n")
    keys = list(GRID)
    res = []
    for combo in itertools.product(*GRID.values()):
        cfg = dict(zip(keys, combo))
        bets = run(markets, cfg)
        a = [x[1] for x in bets if x[0] < split]
        b = [x[1] for x in bets if x[0] >= split]
        if len(a) >= 25 and len(b) >= 15:
            res.append((cfg, stats(a), stats(b), bets))
    print(f"{len(res)} settings with enough bets in both halves")
    good = [r for r in res if r[1]["roi"] > 0]
    print(f"positive on the first half: {len(good)}; of those, positive on the second half too: "
          f"{sum(r[2]['roi'] > 0 for r in good)} ({sum(r[2]['roi'] > 0 for r in good) / max(len(good), 1):.0%})")
    strong = [r for r in res if r[1]["lo"] > 0]
    print(f"clearly positive on the first half (whole 90% range above 0): {len(strong)}; positive on the second: "
          f"{sum(r[2]['roi'] > 0 for r in strong)}; median second-half ROI "
          f"{sorted(r[2]['roi'] for r in strong)[len(strong) // 2] if strong else 0:+.1f}%\n")

    def show(r):
        c, a, b, _ = r
        name = " ".join(f"{k}={v}" for k, v in c.items() if v not in (False, None, 0, "any"))
        return (f"  {name:78} first {a['n']:4} {a['roi']:+6.1f}% [{a['lo']:+.0f},{a['hi']:+.0f}]   "
                f"second {b['n']:4} {b['roi']:+6.1f}% [{b['lo']:+.0f},{b['hi']:+.0f}]")

    print("Top 25 on the first half, with what they did on the second:")
    for r in sorted(res, key=lambda r: -r[1]["roi"])[:25]:
        print(show(r))
    print("\nTop 15 on both halves combined (lowest of the two halves):")
    for r in sorted(res, key=lambda r: -min(r[1]["roi"], r[2]["roi"]))[:15]:
        print(show(r))
    print("\nSimple reference settings:")
    for cfg in [dict(U=u, rel=0, new=False, tiers=None, mw=False, px="any", dir=d, entry=e, window=0)
                for u in (3000, 10000) for d in ("follow", "fade") for e in ("now", "k10")]:
        r = next((x for x in res if x[0] == cfg), None)
        if r:
            print(show(r))
    # each choice on its own: average over all settings that share it (how much does it matter, both halves)
    print("\nEach choice on its own (median ROI over all settings with that choice, first / second half):")
    for k in keys:
        for v in GRID[k]:
            rs = [r for r in res if r[0][k] == v]
            if rs:
                fa = sorted(r[1]["roi"] for r in rs)[len(rs) // 2]
                fb = sorted(r[2]["roi"] for r in rs)[len(rs) // 2]
                print(f"  {k:7} {str(v):30} {len(rs):5} settings   {fa:+6.1f}% / {fb:+6.1f}%")
    Path("local_state/fishy_research.json").write_text(json.dumps(
        [dict(cfg=r[0], first=r[1], second=r[2]) for r in res], default=str))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
