"""
History for the hunter (hunter.py): every finished CS2 and Dota 2 match on Polymarket in the last N days, with every
wallet's buys of $200+ per team market (pre-match and in-play split), the winner, the market's money, and each big
wallet's account creation date. Feeds hunt_research.py.

Run: python hunt_collect.py --days 60        (writes local_state/hunt/*.json; resumable)
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cs2_watch as C
import polydata as P

OUT = Path(__import__("os").environ.get("HUNT_OUT", "local_state/hunt"))
GAMES = {"cs2": 10310, "dota": 10309, "lol": 10311}
MIN_TRADE = 20


def events(series, days):
    now = datetime.now(timezone.utc)
    out, seen, lo = [], set(), now - timedelta(days=days)
    while lo < now:
        hi = min(lo + timedelta(days=3), now)
        off = 0
        while True:
            page = C.get(f"{C.GAMMA}/events?series_id={series}&end_date_min={lo:%Y-%m-%dT%H:%M:%SZ}"
                         f"&end_date_max={hi:%Y-%m-%dT%H:%M:%SZ}&limit=100&offset={off}")
            for e in page:
                if e["id"] not in seen:
                    seen.add(e["id"])
                    out.append(e)
            if len(page) < 100 or off >= 1900:
                break
            off += 100
        lo = hi
    return out


def winner(m):
    try:
        prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        outs = json.loads(m.get("outcomes") or "[]")
    except Exception:
        return None
    if len(prices) == 2 and max(prices) >= 0.99 and min(prices) <= 0.01:
        return outs[prices.index(max(prices))]
    if len(prices) == 2 and prices == [0.5, 0.5]:
        return "void"
    return None


def team_markets(e):
    """The binary team-vs-team markets: series winner and map/game winners (not totals/handicaps)."""
    out = []
    for m in e.get("markets") or []:
        outs = json.loads(m.get("outcomes") or "[]")
        if len(outs) != 2 or set(o.lower() for o in outs) & {"yes", "no", "over", "under"}:
            continue
        q = m.get("question") or ""
        if "handicap" in q.lower() or "o/u" in q.lower():
            continue
        kind = "map" if any(k in q for k in ("Map ", "Game ")) else "match"
        out.append(dict(cid=m["conditionId"], q=q, kind=kind, outcomes=outs, winner=winner(m),
                        vol=float(m.get("volume") or 0)))
    return out


def one(e, game):
    f = OUT / game / f"{e['id']}.json"
    if f.exists():
        return
    st = e.get("startTime") or (e.get("markets") or [{}])[0].get("gameStartTime") or e.get("startDate")
    try:
        start = C.iso(st.replace(" ", "T").replace("+00", "+00:00") if "+00" in st and "+00:" not in st else st).timestamp()
    except Exception:
        return
    mk = team_markets(e)
    if not mk:
        return
    try:
        trades = P.event_trades(e["id"], cap=8000, min_cash=MIN_TRADE)
    except Exception as x:
        print("trades failed", e["slug"], x)
        return
    by = {m["cid"]: m for m in mk}
    pos = {}
    for t in trades:
        if t["conditionId"] not in by:
            continue
        k = (t["proxyWallet"].lower(), t["conditionId"], t["outcome"])
        p = pos.setdefault(k, dict(w=k[0], cid=k[1], o=k[2], name=t["name"] or t["pseudonym"], pre=0.0, pre_sh=0.0,
                                   live=0.0, live_sh=0.0, first=t["timestamp"], last=t["timestamp"],
                                   sold=0.0, sold_sh=0.0))
        usd = t["size"] * t["price"]
        if t["side"] == "SELL":                         # money back before the end: counts in the wallet's real P&L
            p["sold"] += usd
            p["sold_sh"] += t["size"]
            continue
        if t["timestamp"] < start:
            p["pre"] += usd; p["pre_sh"] += t["size"]
        else:
            p["live"] += usd; p["live_sh"] += t["size"]
        p["first"] = min(p["first"], t["timestamp"]); p["last"] = max(p["last"], t["timestamp"])
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(dict(id=e["id"], slug=e["slug"], title=e["title"], start=start, game=game, markets=mk,
                                 positions=list(pos.values()), capped=len(trades) >= 4000)))


def created(w, cache):
    if w in cache:
        return
    try:
        prof = C.get(f"{C.GAMMA}/public-profile?address={w}", tries=2)
        cache[w] = C.iso(prof["createdAt"]).timestamp() if prof.get("createdAt") else None
    except Exception:
        cache[w] = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    for game, series in GAMES.items():
        evs = [e for e in events(series, a.days) if e.get("closed")]
        print(game, len(evs), "closed events", flush=True)
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda e: one(e, game), evs))
    cf = OUT / "created.json"
    cache = json.loads(cf.read_text()) if cf.exists() else {}
    big = set()
    for f in OUT.glob("*/*.json"):
        for p in json.loads(f.read_text())["positions"]:
            if p["pre"] + p["live"] >= 1000:
                big.add(p["w"])
    todo = [w for w in big if w not in cache]
    print(len(big), "big wallets,", len(todo), "to look up", flush=True)
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda w: created(w, cache), todo))
    cf.write_text(json.dumps(cache))
    print("done")


if __name__ == "__main__":
    main()
