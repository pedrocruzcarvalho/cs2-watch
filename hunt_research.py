"""
Which big bets on CS2 / Dota 2 match markets actually win more than their price says? (data: hunt_collect.py)

Each unit is one wallet's buys on one side of one finished team market. Profit is per $1 at the wallet's own
average price (copying it later costs a little more). Sliced by game, size, account age, phase (pre-match /
in-play), market size (a stand-in for the tier) and the bet's share of the market's money.

Run: python hunt_research.py [--min 1000] [--kind match]
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

OUT = Path("local_state/hunt")
DAY = 86400


def units(min_usd, kind):
    import cs2_watch as C
    import hunter
    if not C.TEAMS and Path("local_state/state_vrs").exists():
        C.TEAMS = json.loads(Path("local_state/state_vrs").read_text())
    created = json.loads((OUT / "created.json").read_text()) if (OUT / "created.json").exists() else {}
    out = []
    for f in OUT.glob("*/*.json"):
        if f.name == "created.json":
            continue
        e = json.loads(f.read_text())
        mk = {m["cid"]: m for m in e["markets"]}
        evol = max([m["vol"] for m in e["markets"] if m["kind"] == "match"] or [0])
        both = defaultdict(float)                       # wallet's money on each side of a market (hedge check)
        for p in e["positions"]:
            both[(p["w"], p["cid"], p["o"])] += p["pre"] + p["live"]
        for p in e["positions"]:
            m = mk[p["cid"]]
            if kind != "all" and m["kind"] != kind or m["winner"] in (None, "void"):
                continue
            other = next(o for o in m["outcomes"] if o != p["o"])
            for phase, usd, sh in (("pre", p["pre"], p["pre_sh"]), ("live", p["live"], p["live_sh"])):
                if usd < min_usd or not sh:
                    continue
                px = usd / sh
                if not 0.03 < px < 0.97:
                    continue
                c = created.get(p["w"])
                age = (p["first"] - c) / DAY if c else None
                out.append(dict(game=e["game"], ev=e["id"], title=e["title"], slug=e["slug"], q=m["q"], outcomes=m["outcomes"],
                                winner=m["winner"], o=p["o"], last=p["last"], start=e["start"], cid=p["cid"],
                                w=p["w"], name=p["name"], phase=phase, first=p["first"] if phase == "pre" else max(p["first"], e["start"]), usd=usd, px=px, won=p["o"] == m["winner"],
                                age=age, vol=evol, mvol=m["vol"], share=usd / m["vol"] if m["vol"] else 0,
                                hedge=both[(p["w"], p["cid"], other)] >= 0.3 * usd, kind=m["kind"],
                                top=hunter.top_tier(e["game"], e["title"], evol), tier=hunter.tier_of(e["game"], e["title"], evol)))
    return out


def roi(us):
    if not us:
        return "      -"
    n = len(us)
    w = sum(u["won"] for u in us)
    pnl = sum((1 / u["px"] - 1) if u["won"] else -1 for u in us)
    edge = w / n - sum(u["px"] for u in us) / n
    half = sorted(us, key=lambda u: u["start"])
    a, b = half[:n // 2], half[n // 2:]
    r = lambda g: sum((1 / u["px"] - 1) if u["won"] else -1 for u in g) / len(g) if g else 0
    return f"n={n:4} win {w / n:4.0%} avg px {sum(u['px'] for u in us) / n:.2f} edge {edge * 100:+5.1f}pp ROI {pnl / n:+6.1%}  halves {r(a):+.0%}/{r(b):+.0%}"


def table(name, us, key, order=None):
    print(f"\n== {name}")
    g = defaultdict(list)
    for u in us:
        g[key(u)].append(u)
    for k in (order or sorted(g)):
        if k in g:
            print(f"  {str(k):22} {roi(g[k])}")


def age_b(u):
    a = u["age"]
    return "?" if a is None else "<3d" if a < 3 else "3-14d" if a < 14 else "14-60d" if a < 60 else "60d+"


def size_b(u):
    return "1-3k" if u["usd"] < 3000 else "3-10k" if u["usd"] < 10000 else "10k+"


def vol_b(u):
    v = u["vol"]
    return "<20k" if v < 20000 else "20-60k" if v < 60000 else "60-200k" if v < 200000 else "200k+"


def px_b(u):
    return "<0.35" if u["px"] < 0.35 else "0.35-0.55" if u["px"] < 0.55 else "0.55-0.75" if u["px"] < 0.75 else "0.75+"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=1000)
    ap.add_argument("--kind", default="match")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    us = [u for u in units(a.min, a.kind) if not u["hedge"]]
    print(f"{len(us)} bets of ${a.min:,.0f}+ ({a.kind}), hedgers left out")
    for game in ("cs2", "dota"):
        for phase in ("pre", "live"):
            g = [u for u in us if u["game"] == game and u["phase"] == phase]
            print(f"\n######## {game} {phase}: {roi(g)}")
            table("account age", g, age_b, ["<3d", "3-14d", "14-60d", "60d+", "?"])
            table("size", g, size_b, ["1-3k", "3-10k", "10k+"])
            table("market money", g, vol_b, ["<20k", "20-60k", "60-200k", "200k+"])
            table("price", g, px_b, ["<0.35", "0.35-0.55", "0.55-0.75", "0.75+"])
            table("share of market", g, lambda u: "<10%" if u["share"] < .1 else "10-25%" if u["share"] < .25 else "25%+",
                  ["<10%", "10-25%", "25%+"])
            fresh = [u for u in g if u["age"] is not None and u["age"] < 14]
            table("fresh (<14d) x size", fresh, size_b, ["1-3k", "3-10k", "10k+"])
            table("fresh (<14d) x market money", fresh, vol_b, ["<20k", "20-60k", "60-200k", "200k+"])


if __name__ == "__main__":
    main()


def sim(us, rule, slip=0.03, name=""):
    """One bet per match (the first qualifying wallet bet, by time), filled `slip` worse than the wallet's price."""
    picks, seen = [], set()
    for u in sorted(us, key=lambda u: u["first"]):
        if u["ev"] in seen or not rule(u):
            continue
        seen.add(u["ev"])
        picks.append(dict(u, px=min(0.97, u["px"] + slip)))
    print(f"  {name:44} {roi(picks)}")
    return picks


def bankroll(us, start=63.47, slip=0.03, log=True):
    """The hunter's own rules (hunter.classify) over history: one bet per match, stake = the rule's share of the
    balance at the time, filled `slip` worse. Settled at kickoff+3h (close enough for the order of events)."""
    import hunter
    picks, seen = [], set()
    for u in sorted(us, key=lambda u: u["first"]):
        if u["ev"] in seen or u["hedge"]:
            continue
        r, _ = hunter.classify(u)
        if not r:
            continue
        seen.add(u["ev"])
        px, won = (1 - u["px"], not u["won"]) if r == "D" else (u["px"], u["won"])
        picks.append(dict(u, rule=r, bpx=min(0.95, px + slip), bwon=won))
    bal, peak, dd, open_ = start, start, 0.0, []
    by = defaultdict(lambda: [0, 0, 0.0, 0.0])
    for p in picks:
        t = p["first"]
        for q in [q for q in open_ if q[0] <= t]:
            bal += q[1]
            open_.remove(q)
        stake = max(1.0, bal * hunter.stake_pct(p))
        if stake > bal:
            continue
        bal -= stake
        ret = stake / p["bpx"] if p["bwon"] else 0.0
        open_.append((max(t, p["start"]) + 3 * 3600, ret))
        b = by[(p["rule"], p["game"])]
        b[0] += 1; b[1] += p["bwon"]; b[2] += stake; b[3] += ret - stake
        peak = max(peak, bal + sum(q[1] for q in open_)); dd = max(dd, peak - bal)
    bal += sum(q[1] for q in open_)
    if log:
        for k in sorted(by):
            n, w, st, pnl = by[k]
            print(f"  rule {k[0]} {k[1]:5} n={n:4} won {w / n:4.0%} staked ${st:8.2f} pnl ${pnl:+8.2f} ROI {pnl / st:+.1%}")
        print(f"  ${start:.2f} -> ${bal:.2f} over {len(picks)} bets; worst dip ${dd:.2f}")
    return bal
