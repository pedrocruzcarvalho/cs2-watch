"""
Is the algorithm just too trigger-happy? Re-score the same 30 days of pre-kickoff positions under many settings,
tune on the first 20 days, and check the best settings on the last 10 days they never saw.

Settings varied: money floors, points needed, fresh wallet required, the top-40 throw rule, which side (favourite /
underdog / any), match winner only or all markets, lowest level bet. A bet is $100 at the flagged side's last
pre-kickoff Polymarket price + 1c spread + fee. Polymarket data only.

Run: python sweep.py local_state/dump30.json
"""

import itertools
import json
import random
import sys
from collections import defaultdict

import cs2_watch as C
import teams as TM

SPREAD, FEE = 0.01, 0.05
LEVELS = ["HIGH", "MEDIUM", "LOW", ""]


def pnl(price, won):
    p = min(price + SPREAD, 0.99)
    cost = p + FEE * p * (1 - p)
    return 100 / cost - 100 if won else -100


def stats(bets):
    if not bets:
        return dict(n=0, roi=None)
    xs = [pnl(p, w) for p, w in bets]
    rng = random.Random(5)
    m = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(1500))
    return dict(n=len(xs), roi=sum(xs) / len(xs), lo=m[75], hi=m[1425], won=sum(w for _, w in bets) / len(xs),
                px=sum(p for p, _ in bets) / len(xs))


def fresh(w, start):
    w = w or {}
    if w.get("traded") is not None and w["traded"] >= 500:
        return False
    return (w.get("created") is not None and start - w["created"] < 7 * 86400) or \
        (w.get("traded") is not None and w["traded"] <= 5)


def level(pts, usd, share, cfg):
    fl, fm, fh = cfg["floors"]
    pl, pm, ph = cfg["points"]
    if pts >= ph and (usd >= fh or (usd >= fh / 2 and share >= 0.3)):
        return "HIGH"
    if pts >= pm and (usd >= fm or (usd >= fm * 0.6 and share >= 0.3)):
        return "MEDIUM"
    return "LOW" if pts >= pl and usd >= fl else ""


def prepare(dump):
    """Group positions with their market once: [(start, outcomes, prices, winner, safe_thrower, rows)]."""
    groups = defaultdict(lambda: dict(rows=[], info=None))
    for i in dump["infos"]:
        if i.get("venue") != "poly":
            continue
        k = ("-".join(sorted(C.match_key(i["match"]))), round(i["start"] / 600), i.get("market", "Match Winner"))
        groups[k]["info"] = i
    for r in dump["rows"]:
        if r.get("venue") != "poly":
            continue
        k = ("-".join(sorted(C.match_key(r["match"]))), round(r["start"] / 600), r.get("market", "Match Winner"))
        if k in groups:
            groups[k]["rows"].append(r)
    out = []
    for (mk, _, market), g in groups.items():
        i = g["info"]
        if not i or not i.get("winner") or not g["rows"]:
            continue
        outs = i["teams"]
        safe = {o: TM.wont_throw(C.TEAMS, o)[0] for o in outs} if C.TEAMS.get("teams") else {}
        out.append(dict(start=i["start"], outcomes=outs, prices=i["prices"], winner=C.team_for(i["winner"], outs),
                        safe=safe, market=market, rows=g["rows"], tier=i.get("tier")))
    return out


def run(groups, cfg, split):
    train, test = [], []
    for g in groups:
        if cfg["markets"] == "match" and g["market"] != "Match Winner":
            continue
        money, best = defaultdict(float), {}
        for r in g["rows"]:
            if cfg["fresh"] and not (r.get("cluster") or fresh(r.get("wallet_info"), r["start"])):
                continue
            if not r.get("pre_match"):
                continue
            lv = level(r["pts"], r["usd"], r.get("share") or 0, cfg)
            if not lv:
                continue
            t = C.team_for(r["outcome"], g["outcomes"])
            if not t:
                continue
            money[t] += r["usd"]
            if t not in best or LEVELS.index(lv) < LEVELS.index(best[t]):
                best[t] = lv
        if not money:
            continue
        side = max(money, key=money.get)
        lv = best[side]
        if sum(v for t, v in money.items() if t != side) >= 0.5 * money[side]:
            lv = LEVELS[min(LEVELS.index(lv) + 1, 3)]
        thrower = next((o for o in g["outcomes"] if o != side), None)
        if cfg["throw"] and g["safe"].get(thrower):
            continue
        if not lv or LEVELS.index(lv) > LEVELS.index(cfg["min_level"]):
            continue
        price = g["prices"].get(side)
        if price is None or not 0.02 < price < 0.98:
            continue
        if cfg["side"] == "fav" and price < 0.5 or cfg["side"] == "dog" and price >= 0.5:
            continue
        (train if g["start"] < split else test).append((price, g["winner"] == side))
    return stats(train), stats(test)


def fmt(s):
    if not s["n"]:
        return f"{'-':>27}"
    return f"{s['n']:4} bets {s['roi']:+6.1f}% [{s['lo']:+4.0f},{s['hi']:+4.0f}]"


def main(path):
    dump = json.load(open(path, encoding="utf-8"))
    C.TEAMS = json.load(open("local_state/state_vrs"))
    groups = prepare(dump)
    starts = sorted(g["start"] for g in groups)
    split = starts[0] + (starts[-1] - starts[0]) * 2 / 3
    print(f"{len(groups)} resolved markets with scored positions; tune on the first 20 days, check on the last 10\n")
    grid = dict(floors=[(1000, 1500, 5000), (2000, 3000, 7500), (3000, 5000, 10000), (5000, 8000, 15000),
                        (10000, 15000, 25000)],
                points=[(4, 6, 9), (6, 8, 11), (8, 10, 13)], fresh=[False, True], throw=[True, False],
                side=["any", "fav", "dog"], markets=["match", "all"], min_level=["LOW", "MEDIUM"])
    keys = list(grid)
    results = []
    for combo in itertools.product(*grid.values()):
        cfg = dict(zip(keys, combo))
        tr, te = run(groups, cfg, split)
        results.append((cfg, tr, te))
    print(f"{len(results)} settings tried.\n")
    named = {"original (Oct 19 morning): $1k/$1.5k/$5k, 4/6/9 pts, no throw rule":
             dict(floors=(1000, 1500, 5000), points=(4, 6, 9), fresh=False, throw=False, side="any", markets="match",
                  min_level="LOW"),
             "current: $3k/$5k/$10k, 4/6/9 pts, throw rule, all markets":
             dict(floors=(3000, 5000, 10000), points=(4, 6, 9), fresh=False, throw=True, side="any", markets="all",
                  min_level="LOW")}
    print(f"{'':70} {'first 20 days (tuning)':>32}   {'last 10 days (unseen)':>30}")
    for name, cfg in named.items():
        tr, te = run(groups, cfg, split)
        print(f"{name:70} {fmt(tr)}   {fmt(te)}")
    ok = [x for x in results if x[1]["n"] >= 25]
    ok.sort(key=lambda x: -x[1]["roi"])
    print(f"\nbest 12 settings on the tuning days (25+ bets), and how they did on the unseen days:")
    for cfg, tr, te in ok[:12]:
        d = (f"${cfg['floors'][0] // 1000}k/{cfg['floors'][1] // 1000}k/{cfg['floors'][2] // 1000}k "
             f"pts {cfg['points'][0]}/{cfg['points'][1]}/{cfg['points'][2]} fresh={'Y' if cfg['fresh'] else 'n'} "
             f"throw={'Y' if cfg['throw'] else 'n'} {cfg['side']:3} {cfg['markets']:5} min {cfg['min_level']}")
        print(f"  {d:68} {fmt(tr)}   {fmt(te)}")
    te_all = [x[2]["roi"] for x in ok if x[2]["n"] >= 10]
    print(f"\nacross all {len(te_all)} settings with 10+ unseen bets: median unseen ROI {sorted(te_all)[len(te_all) // 2]:+.1f}%, "
          f"{sum(1 for v in te_all if v > 0)} of {len(te_all)} positive")
    for side in ("fav", "dog", "any"):
        sub = [x for x in ok if x[0]["side"] == side and x[2]["n"] >= 10]
        if sub:
            v = sorted(x[2]["roi"] for x in sub)
            print(f"  side={side}: median unseen ROI {v[len(v) // 2]:+.1f}% over {len(v)} settings")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main(sys.argv[1])
