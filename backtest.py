"""
Replay the current algorithm on finished matches, using only trades placed before each kickoff, and check whether
each rule earns its place.

For every market of every finished low/mid-tier match: the level the algorithm gives it at kickoff, the side the
flagged money backed, that side's last pre-kickoff price, and who won. A bet is $100 at that price + 1c spread and
Polymarket's fee. Reported: ROI by level, the control (backing the favourite on unflagged matches), and each rule on
its own (flags the throw rule dropped, flags raised by unusual volume, side markets vs match winner).

Limits: wallet facts (age, markets traded) are looked up today, so they are slightly optimistic; the watchlist,
bookmaker check and line moves need live history and are not replayed.

Run: python backtest.py --days 30
"""

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cs2_watch as C
import teams as TM

SPREAD, FEE = 0.01, 0.05


def pnl(price, won, stake=100):
    p = min(price + SPREAD, 0.99)
    cost = p + FEE * p * (1 - p)
    return stake / cost - stake if won else -stake


def band(xs, n=4000):
    rng = random.Random(7)
    m = sorted(sum(rng.choice(xs) for _ in xs) / len(xs) for _ in range(n))
    return m[int(.05 * n)], m[int(.95 * n)]


def line(name, rows, out):
    rows = [r for r in rows if r["price"] and r["won"] is not None]
    if not rows:
        out(f"  {name:46} {'-':>5}")
        return
    xs = [pnl(r["price"], r["won"]) for r in rows]
    a, b = band(xs)
    won = sum(r["won"] for r in rows) / len(rows)
    px = sum(r["price"] for r in rows) / len(rows)
    out(f"  {name:46} {len(rows):5}  won {won:4.0%} at {px:.2f}  ROI {sum(xs) / len(xs):+6.1f}%  [{a:+.0f}%, {b:+.0f}%]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--state", default="local_state")
    a = ap.parse_args()
    out = lambda *x: print(*x, flush=True)
    st = Path(a.state)
    C.DIR = st / "bt"
    C.DIR.mkdir(parents=True, exist_ok=True)
    C.TEAMS = json.loads((st / "state_vrs").read_text())
    baseline = json.loads((st / "state_baseline").read_text())
    now = datetime.now(timezone.utc)
    flags, rows, infos = C.scan(now - timedelta(days=a.days), now - timedelta(hours=4), ["poly"], baseline, log=out,
                                prematch_only=True)
    ms = C.build_matches(infos, rows)
    recs = []
    for m in ms:
        side = m["fix_side"] or m["tracked"]
        dropped = any(x.get("kind") == "throw" for x in m.get("adjust") or [])
        if not m["winner"] or not side or side not in m["prices"]:
            continue
        recs.append(dict(level=m["level"], kind=m["kind"], vtier=m.get("vtier"), price=m["prices"][side],
                         won=m["winner"] == side, flagged=bool(m["fix_side"]), dropped=dropped,
                         volume=any(x.get("kind") == "volume" for x in m.get("adjust") or []),
                         base=m.get("base_level")))
    (st / "backtest.json").write_text(json.dumps(recs))
    out(f"\n{len(recs)} resolved markets, {a.days:.0f} days, low/mid tier, pre-kickoff data only. $100 per bet, fees in.\n")
    out(f"  {'':46} {'bets':>5}")
    for lvl in ("HIGH", "MEDIUM", "LOW"):
        line(f"level {lvl}", [r for r in recs if r["level"] == lvl], out)
    line("any flag (LOW+)", [r for r in recs if r["level"]], out)
    line("MEDIUM+HIGH", [r for r in recs if r["level"] in ("MEDIUM", "HIGH")], out)
    line("CONTROL: favourite, unflagged match winner", [r for r in recs if not r["flagged"] and r["kind"] == "match"], out)
    out("\n  rule checks")
    line("dropped by the throw rule (top-40 team throws)", [r for r in recs if r["dropped"]], out)
    line("raised by unusual volume", [r for r in recs if r["volume"]], out)
    line("flagged, no unusual volume", [r for r in recs if r["level"] and not r["volume"]], out)
    line("flagged match winner", [r for r in recs if r["level"] and r["kind"] == "match"], out)
    line("flagged side markets (maps, totals, rounds)", [r for r in recs if r["level"] and r["kind"] != "match"], out)
    line("flagged, flagged side was the underdog (<0.5)", [r for r in recs if r["level"] and r["price"] < 0.5], out)
    line("flagged, flagged side was the favourite", [r for r in recs if r["level"] and r["price"] >= 0.5], out)
    for vt in ("T2", "T3", "T4", "T5", "T6"):
        line(f"flagged, match tier {vt}", [r for r in recs if r["level"] and r["vtier"] == vt], out)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
