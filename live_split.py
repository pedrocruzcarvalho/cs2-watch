"""
Which in-play big bets carry the live edge: fixer-looking wallets, established sharp ones, or bets against the trend?

Replays the live_money.py rule on the 60-day data (T3-T6, one wallet $3k+ in-play, one-sided, copied 10 minutes
later) and labels each bet by the signalling wallet's profile from its CS2 history BEFORE that match
(wallet_history.py) and by the price trend in the 15 minutes before the signal. ROI per group, with the favourite
control and both halves.

Run: python live_split.py
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import fishy_research as F
import live_research as L
import wallet_skill as S

U, DELAY, TIERS = 3000, 600, {"T3", "T4", "T5", "T6"}


def signals(ms):
    out = []
    for mk in ms:
        if mk["vt"] not in TIERS:
            continue
        net = defaultdict(float)
        for w, o, side, size, price, ts in mk["tr"]:
            net[(w, o)] += size * price if side == "B" else -size * price
            if net[(w, o)] < U:
                continue
            other = sum(v for (w2, o2), v in net.items() if o2 != o and v >= U / 2)
            if other >= 0.5 * net[(w, o)]:
                continue
            p = L.after(mk, o, ts + DELAY)
            o0, o1 = mk["outcomes"]
            q0 = L.after(mk, o0, ts + DELAY)
            if p is None or not 0.03 < p < 0.97:
                break
            fav = o0 if q0 >= 0.5 else o1
            before = L.after(mk, o, ts - 900)
            now = L.after(mk, o, ts - 1)
            out.append(dict(start=mk["start"], w=w, o=o, ts=ts, p=p, won=o == mk["winner"],
                            pnl=F.pnl(p, o == mk["winner"]), fav=F.pnl(q0 if fav == o0 else 1 - q0, fav == mk["winner"]),
                            trend=None if before is None or now is None else now - before, vt=mk["vt"],
                            mins=(ts - mk["start"]) / 60, usd=net[(w, o)]))
            break
    return out


def main():
    H = S.histories()
    meta = {}
    for f in Path("local_state/hist").glob("*.json"):
        h = json.loads(f.read_text())
        meta[h["wallet"]] = dict(n_closed=h["n_closed"], first=min((r[0] for r in h["rows"]), default=None))
    sig = signals(L.load())
    mid = sorted(x["start"] for x in sig)[len(sig) // 2]
    for x in sig:
        h = H.get(x["w"])
        rs = [r for r in h["rows"] if r[0] <= x["start"] - 86400] if h else []
        n = len(rs)
        x["n"] = n
        x["edge"] = (sum(r[2] for r in rs) - sum(r[1] for r in rs)) / n if n else None
        first = min((r[0] for r in rs), default=None)
        x["age"] = (x["start"] - first) / 86400 if first else 0
        x["share"] = len(h["rows"]) / max(meta[x["w"]]["n_closed"], len(h["rows"]), 1) if h else None
        x["known"] = h is not None
    print(f"{len(sig)} live signals (T3-T6, ${U}+, copied {DELAY // 60} min later)\n")

    def show(name, xs):
        if not xs:
            print(f"  {name:52}    0")
            return
        s, c = F.stats([x["pnl"] for x in xs]), F.stats([x["fav"] for x in xs])
        a = F.stats([x["pnl"] for x in xs if x["start"] < mid])
        b = F.stats([x["pnl"] for x in xs if x["start"] >= mid])
        print(f"  {name:52} {s['n']:4}  won {sum(x['won'] for x in xs) / len(xs):4.0%} at {sum(x['p'] for x in xs) / len(xs):.2f}  "
              f"ROI {s['roi']:+6.1f}% [{s['lo']:+.0f},{s['hi']:+.0f}]  halves {a['roi']:+.0f}/{b['roi']:+.0f}  fav {c['roi']:+.0f}%")

    show("all", sig)
    print("\nwallet history before the match (CS2 positions):")
    show("no history in the download (new or tiny wallet)", [x for x in sig if not x["known"] or x["n"] == 0])
    show("1-20 CS2 bets before", [x for x in sig if 1 <= x["n"] <= 20])
    show("21-200 CS2 bets before", [x for x in sig if 21 <= x["n"] <= 200])
    show("200+ CS2 bets before (established)", [x for x in sig if x["n"] > 200])
    print("\nhow long betting CS2 before this match:")
    show("under 30 days (or never)", [x for x in sig if x["age"] < 30])
    show("30-120 days", [x for x in sig if 30 <= x["age"] < 120])
    show("120+ days", [x for x in sig if x["age"] >= 120])
    print("\nshare of all its Polymarket bets that are CS2:")
    show("80%+ CS2 (specialist)", [x for x in sig if x["share"] is not None and x["share"] >= 0.8])
    show("under 80% CS2", [x for x in sig if x["share"] is not None and x["share"] < 0.8])
    print("\nits CS2 record before (edge = wins above its prices):")
    show("edge > +5 pts (10+ bets)", [x for x in sig if x["n"] >= 10 and x["edge"] > 0.05])
    show("edge -5..+5 pts (10+ bets)", [x for x in sig if x["n"] >= 10 and -0.05 <= x["edge"] <= 0.05])
    show("edge < -5 pts (10+ bets)", [x for x in sig if x["n"] >= 10 and x["edge"] < -0.05])
    print("\nprice of the backed side in the 15 minutes before the bet:")
    show("was falling 5+ pts (bet against the move)", [x for x in sig if x["trend"] is not None and x["trend"] <= -0.05])
    show("flat (within 5 pts)", [x for x in sig if x["trend"] is not None and abs(x["trend"]) < 0.05])
    show("was rising 5+ pts (bet with the move)", [x for x in sig if x["trend"] is not None and x["trend"] >= 0.05])
    print("\nprice copied at:")
    show("underdog (< 0.45)", [x for x in sig if x["p"] < 0.45])
    show("coin flip (0.45-0.65)", [x for x in sig if 0.45 <= x["p"] < 0.65])
    show("favourite (0.65+)", [x for x in sig if x["p"] >= 0.65])
    print("\ntier and timing:")
    for t in ("T3", "T4", "T5", "T6"):
        show(t, [x for x in sig if x["vt"] == t])
    show("first 45 min of the match", [x for x in sig if x["mins"] < 45])
    show("after 45 min", [x for x in sig if x["mins"] >= 45])
    show("$3k-6k", [x for x in sig if x["usd"] < 6000])
    show("$6k+", [x for x in sig if x["usd"] >= 6000])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
