"""
Which wallets really beat CS2 prices over their whole history, and does following them pay on later matches?

Walk-forward: wallets are judged only on their CS2 history before a cutoff day (wallet_history.py: every CS2
position ever, losers included), then followed on the mined matches after it (mine_collect.py trades, so the time of
each bet is known). A wallet qualifies with N+ bets, a luck score of Z+ (wins vs what the prices predicted), and a
profit before the cutoff. When qualifying wallets put $M+ net on one side of a market before kickoff (less than half
of that on the other side), we bet $100 on that side at the price 10 minutes later, +1c spread + fee.
Control: back the favourite on the same markets at the same moment.

Run: python wallet_skill.py
"""

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import fishy_research as F


def day(s):
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def histories():
    out = {}
    for f in Path("local_state/hist").glob("*.json"):
        h = json.loads(f.read_text())
        rows = [(day(r[0]) + 86400, r[3], r[4], r[5]) for r in h["rows"] if 0.03 < r[3] < 0.97 and r[5] >= 5]
        out[h["wallet"]] = dict(rows=rows, pnl=h["pnl"], cs2_pnl=h["cs2_pnl"])
    return out


def record(rows, before, since=0):
    rs = [r for r in rows if since <= r[0] <= before]
    n = len(rs)
    if not n:
        return dict(n=0, z=0, edge=0, profit=0)
    ex = sum(r[1] for r in rs)
    wins = sum(r[2] for r in rs)
    var = sum(r[1] * (1 - r[1]) for r in rs)
    profit = sum((r[3] / r[1] - r[3]) if r[2] else -r[3] for r in rs)
    return dict(n=n, z=(wins - ex) / math.sqrt(var) if var else 0, edge=(wins - ex) / n, profit=profit,
                won=wins / n, px=ex / n)


def follow(markets, qual, cut, M, tiers):
    """Bets after the cutoff following qualifying wallets: [(start, follow pnl, favourite pnl)]."""
    res = []
    for mk in markets:
        if mk["start"] < cut or tiers and mk["vt"] not in tiers:
            continue
        net = defaultdict(float)
        for w, o, side, size, price, ts in mk["trades"]:
            if w not in qual:
                continue
            net[o] += size * price if side == "B" else -size * price
            top = max(net, key=net.get)
            other = sum(v for k, v in net.items() if k != top)
            if net[top] >= M and other < 0.5 * net[top]:
                t = min(ts + F.DELAY, mk["start"] - 1)
                p = F.price_at(mk, top, t)
                o0, o1 = mk["outcomes"]
                q0 = F.price_at(mk, o0, t)
                if p is None or not 0.03 < p < 0.97:
                    break
                fav = o0 if q0 >= 0.5 else o1
                res.append((mk["start"], F.pnl(p, top == mk["winner"]),
                            F.pnl(q0 if fav == o0 else 1 - q0, fav == mk["winner"]), p))
                break
    return res


def load_markets():
    """Every resolved two-outcome market with its pre-kickoff trades (no $1k minimum, unlike fishy_research)."""
    out = []
    for f in Path("local_state/mine").glob("*.json"):
        d = json.loads(f.read_text())
        for m in d["markets"]:
            if not m["winner"] or len(m["outcomes"]) != 2 or not m["trades"]:
                continue
            o0 = m["outcomes"][0]
            tr = sorted(m["trades"], key=lambda t: t[5])
            out.append(dict(start=d["start"], vt=d.get("vtier") or "T6", outcomes=m["outcomes"], winner=m["winner"],
                            trades=tr, times=[t[5] for t in tr], px0=[t[4] if t[1] == o0 else 1 - t[4] for t in tr]))
    return sorted(out, key=lambda m: m["start"])


def informed_rule(markets, H, hist_filter, tiers=("T3", "T4", "T5", "T6")):
    """The live informed rule (informed.py: 21-day record, 5+ bets, luck 2.5+, edge 10+ pts, $200+ one-sided,
    copied 10 minutes after the signal), optionally also requiring a full-history CS2 record that passes
    hist_filter(record before this match). Returns [(start, follow pnl, favourite pnl)] after a 21-day warm-up."""
    done = []                                                   # (start, wallet, won, avg) of earlier markets
    for mk in markets:
        net, sh = defaultdict(float), defaultdict(float)
        for w, o, side, size, price, ts in mk["trades"]:
            net[(w, o)] += size * price if side == "B" else -size * price
            if side == "B":
                sh[(w, o)] += size
        mk["_pos"] = [(w, o, u, (u + 0.0) / sh[(w, o)] if sh[(w, o)] else 0) for (w, o), u in net.items() if u >= 20]
    t0 = markets[0]["start"]
    stats = defaultdict(list)
    qi, out = 0, []
    for mk in markets:
        while qi < len(markets) and markets[qi]["start"] <= mk["start"] - 3 * 3600:
            m2 = markets[qi]
            for w, o, u, _ in m2["_pos"]:
                buys = [t for t in m2["trades"] if t[0] == w and t[1] == o and t[2] == "B"]
                sz = sum(t[3] for t in buys)
                avg = sum(t[3] * t[4] for t in buys) / sz if sz else 0
                if 0.03 < avg < 0.97:
                    stats[w].append((m2["start"], o == m2["winner"], avg))
            qi += 1
        if mk["start"] - t0 < 21 * 86400 or mk["vt"] not in tiers:
            continue
        qual = set()
        for w in {t[0] for t in mk["trades"]}:
            rs = [r for r in stats.get(w, []) if r[0] >= mk["start"] - 21 * 86400]
            if len(rs) < 5:
                continue
            ex, wn = sum(r[2] for r in rs), sum(r[1] for r in rs)
            var = sum(r[2] * (1 - r[2]) for r in rs)
            if var <= 0 or (wn - ex) / math.sqrt(var) < 2.5 or (wn - ex) / len(rs) < 0.10:
                continue
            if hist_filter and not hist_filter(record(H[w]["rows"], mk["start"] - 86400) if w in H else None):
                continue
            qual.add(w)
        if not qual:
            continue
        res = follow([mk], qual, 0, 200, None)
        out += res
    return out


def main():
    H = histories()
    markets = load_markets()
    if "--informed" in sys.argv:
        mid = markets[len(markets) // 2]["start"]
        for name, f in (("21-day rule as live", None),
                        ("+ whole CS2 history luck >= 0", lambda r: r is not None and r["n"] >= 20 and r["z"] >= 0),
                        ("+ whole CS2 history profitable", lambda r: r is not None and r["n"] >= 20 and r["profit"] > 0),
                        ("+ whole CS2 history luck >= 1", lambda r: r is not None and r["n"] >= 20 and r["z"] >= 1),
                        ("+ whole history luck < 0 (losers only)", lambda r: r is not None and r["n"] >= 20 and r["z"] < 0),
                        ("+ no history found (new wallets)", lambda r: r is None or r["n"] < 20)):
            res = informed_rule(markets, H, f)
            s, c = F.stats([x[1] for x in res]), F.stats([x[2] for x in res])
            a, b = F.stats([x[1] for x in res if x[0] < mid]), F.stats([x[1] for x in res if x[0] >= mid])
            print(f"  {name:42} {s['n']:4} bets  follow {s['roi']:+6.1f}% [{s['lo']:+.0f},{s['hi']:+.0f}]  "
                  f"halves {a['roi']:+6.1f}/{b['roi']:+6.1f}  favourite {c['roi']:+6.1f}%")
        return
    t0, t1 = markets[0]["start"], markets[-1]["start"]
    print(f"{len(H)} wallet histories, {len(markets)} markets, {(t1 - t0) / 86400:.0f} days\n")
    for cut_day in (20, 30, 40):
        cut = t0 + cut_day * 86400
        print(f"--- wallets judged on history before day {cut_day}, followed on days {cut_day}-{(t1 - t0) / 86400:.0f}")
        for N, Z, need_profit in ((30, 2.0, True), (30, 2.5, True), (50, 2.5, True), (30, 3.0, True), (100, 2.5, True),
                                  (30, 2.5, False), (5, 2.5, False)):
            qual = {}
            for w, h in H.items():
                r = record(h["rows"], cut)
                if r["n"] >= N and r["z"] >= Z and (r["profit"] > 0 or not need_profit):
                    qual[w] = r
            for M, tiers in ((200, None), (200, ("T3", "T4", "T5", "T6")), (50, None)):
                res = follow(markets, qual, cut, M, tiers)
                s, c = F.stats([x[1] for x in res]), F.stats([x[2] for x in res])
                print(f"  {N:>3}+ bets luck>={Z} {'profit' if need_profit else '      '}: {len(qual):4} wallets | "
                      f"${M} {'T3-T6' if tiers else 'all  '}: {s['n']:4} bets  follow {s['roi']:+6.1f}% "
                      f"[{s['lo']:+.0f},{s['hi']:+.0f}]  favourite {c['roi']:+6.1f}%")
        print()
    # today's view: who looks skilled over the whole history
    best = sorted(((w, record(h["rows"], 1e12), h) for w, h in H.items()), key=lambda x: -x[1]["z"])
    print("Best whole-history CS2 records (30+ bets):")
    for w, r, h in [b for b in best if b[1]["n"] >= 30][:30]:
        print(f"  {w}  {r['n']:5} bets  won {r['won']:.0%} at {r['px']:.2f}  luck {r['z']:+.1f}  "
              f"CS2 profit ${h['cs2_pnl']:>9,}  all markets ${h['pnl']:>10,}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
