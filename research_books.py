"""
Research: do bookmaker prices (1xBet, Stake, Pinnacle via OddsPapi) know something Polymarket doesn't?

For finished CS2 matches with both bookmaker odds history and Polymarket trades:
  at a decision time before kickoff, compare the bookmakers' margin-free chance for team 1 with Polymarket's last
  traded price for team 1. If the bookmakers are higher by >= THRESHOLD, buy team 1 on Polymarket (and vice versa).
  Also: when a bookmaker moved >= 6 points towards a team in the 12 h before the decision, back that team on
  Polymarket at the Polymarket price.
Costs one billable OddsPapi request (the fixture list); odds history is free (5 s apart).

Run: python research_books.py --days 9
"""

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cs2_watch as C
import oddspapi as O

SPREAD, FEE = 0.01, 0.05


def pnl(price, won, stake=100):
    p = min(price + SPREAD, 0.99)
    cost = p + FEE * p * (1 - p)
    return stake / cost - stake if won else -stake


def roi(rows, label, out):
    if len(rows) < 5:
        out(f"  {label:58} n={len(rows)}")
        return
    xs = [pnl(r["price"], r["won"]) for r in rows]
    rng = random.Random(3)
    m = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(3000))
    out(f"  {label:58} {len(rows):4} bets  won {sum(r['won'] for r in rows) / len(rows):4.0%} at "
        f"{sum(r['price'] for r in rows) / len(rows):.2f}  ROI {sum(xs) / len(xs):+6.1f}%  [{m[150]:+.0f}%, {m[2850]:+.0f}%]")


def last_before(series, t, max_age=None):
    pts = [p for ts, p in series if ts <= t and (max_age is None or ts >= t - max_age)]
    return pts[-1] if pts else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=9)
    a = ap.parse_args()
    out = lambda *x: print(*x, flush=True)
    st = Path("local_state")
    cache = st / "research_books.json"
    data = json.loads(cache.read_text()) if cache.exists() else {}
    now = datetime.now(timezone.utc)
    lo = now - timedelta(days=a.days)
    if not data.get("fixtures"):
        fx = O.call("fixtures", sportId=O.SPORT, **{"from": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                    "to": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
        data["fixtures"] = [dict(id=f["fixtureId"], p1=f.get("participant1Name") or "", p2=f.get("participant2Name") or "",
                                 start=C.iso(f["startTime"]).timestamp()) for f in fx if f.get("hasOdds")]
        cache.write_text(json.dumps(data))
        out(f"fixtures: {len(data['fixtures'])} with odds (1 billable request)")
    hist = data.setdefault("hist", {})
    todo = [f for f in data["fixtures"] if f["id"] not in hist and f["start"] < now.timestamp() - 3 * 3600]
    out(f"odds history to fetch (free, 5 s apart): {len(todo)}")
    for i, f in enumerate(todo):
        if i:
            time.sleep(O.COOLDOWN)
        try:
            hist[f["id"]] = O.series(O.call("historical-odds", fixtureId=f["id"], bookmakers=",".join(O.BOOKS)))
        except Exception as err:
            hist[f["id"]] = {}
            out(f"  {f['id']}: {err}")
        if i % 20 == 19:
            cache.write_text(json.dumps(data))
    cache.write_text(json.dumps(data))

    # Polymarket side
    evs = C.poly_matches(lo - timedelta(hours=6), now - timedelta(hours=3))
    rows = []
    for f in data["fixtures"]:
        books = hist.get(f["id"]) or {}
        if not books:
            continue
        k = C.match_key(f"{f['p1']} vs {f['p2']}")
        e = next((e for e in evs if C.same_match(k, C.match_key(e["title"]))
                  and abs(C.iso(e["startTime"]).timestamp() - f["start"]) < 6 * 3600), None)
        if not e:
            continue
        mw = C.match_winner_market(e)
        outs = json.loads(mw.get("outcomes") or "[]")
        p1 = C.team_for(f["p1"], outs)
        winner = C.winner_of(mw, True)
        if not p1 or not winner:
            continue
        p2 = next(o for o in outs if o != p1)
        trades = [t for t in C.poly_trades(e["id"]) if t["conditionId"] == mw["conditionId"]
                  and (1 - C.SWEEP) < t["price"] < C.SWEEP]
        poly = sorted((t["timestamp"], t["price"] if t["outcome"] == p1 else 1 - t["price"]) for t in trades)
        start = C.iso(e["startTime"]).timestamp()
        rows.append(dict(match=e["title"], start=start, p1=p1, p2=p2, won1=winner == p1, poly=poly, books=books,
                         vtier=C.vtier(e["title"])))
    out(f"\nmatches with both bookmaker history and Polymarket trades: {len(rows)}")

    for when, label in ((30 * 60, "30 min"), (2 * 3600, "2 h")):
        out(f"\n--- decision {label} before kickoff ---")
        gaps = []
        for r in rows:
            t = r["start"] - when
            pp = last_before(r["poly"], t, max_age=6 * 3600)
            bp = [last_before(s, t) for s in r["books"].values()]
            bp = [x for x in bp if x is not None]
            if pp is None or not bp:
                continue
            b = sum(bp) / len(bp)
            gaps.append(dict(r, pp=pp, bp=b, gap=b - pp, t=t))
        out(f"  usable: {len(gaps)}   median |gap| {sorted(abs(g['gap']) for g in gaps)[len(gaps) // 2] * 100 if gaps else 0:.1f} points")
        for thr in (0.03, 0.05, 0.08, 0.12):
            bets = []
            for g in gaps:
                if g["gap"] >= thr:
                    bets.append(dict(price=g["pp"], won=g["won1"]))
                elif g["gap"] <= -thr:
                    bets.append(dict(price=1 - g["pp"], won=not g["won1"]))
            roi(bets, f"bookmakers >= {thr * 100:.0f} pts above Polymarket: buy on Polymarket", out)
        bets = []
        for g in gaps:
            for s in g["books"].values():
                now_p, then_p = last_before(s, g["t"]), last_before(s, g["t"] - 12 * 3600)
                if now_p is None or then_p is None:
                    continue
                mv = now_p - then_p
                if mv >= 0.06:
                    bets.append(dict(price=g["pp"], won=g["won1"]))
                elif mv <= -0.06:
                    bets.append(dict(price=1 - g["pp"], won=not g["won1"]))
                break
        roi(bets, "bookmaker moved >= 6 pts in 12 h: follow it on Polymarket", out)
        roi([dict(price=max(g["pp"], 1 - g["pp"]), won=g["won1"] if g["pp"] >= .5 else not g["won1"]) for g in gaps],
            "control: Polymarket favourite", out)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
