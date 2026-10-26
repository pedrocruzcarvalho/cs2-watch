"""
Collect settled Kalshi CS2 match-winner events with all their trades (before and during the match), for testing
whether big Kalshi orders during a match point to the winner (kalshi_research.py).

Kalshi has no wallets: each trade is one fill with a taker side. One file per event in local_state/kalshi/:
title, start (from the ticker, New York time), the two teams with the winner, and trades
[time, team backed, price paid for that team, usd].

Run: python kalshi_collect.py --days 60 [--series KXCS2GAME]
"""

import argparse
import json
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cs2_watch as C

OUT = Path("local_state/kalshi")


def events(series, days):
    now = datetime.now(timezone.utc)
    out, cursor = [], ""
    while True:
        q = dict(series_ticker=series, status="settled", limit=200, with_nested_markets="true",
                 min_close_ts=int((now - timedelta(days=days)).timestamp()))
        if cursor:
            q["cursor"] = cursor
        d = C.get(f"{C.KALSHI}/events?{urllib.parse.urlencode(q)}")
        out += d.get("events", [])
        cursor = d.get("cursor")
        if not cursor or not d.get("events"):
            return out


def one(e):
    path = OUT / f"{e['event_ticker']}.json"
    if path.exists():
        return "cached"
    start = C.kalshi_start(e["event_ticker"])
    mk = [m for m in e.get("markets") or [] if m.get("result") in ("yes", "no")]
    if not start or len(mk) != 2:
        return "skip"
    teams = [m.get("yes_sub_title") or m["ticker"] for m in mk]
    winner = next((t for t, m in zip(teams, mk) if m["result"] == "yes"), None)
    rows = []
    for team, m in zip(teams, mk):
        other = teams[1] if team == teams[0] else teams[0]
        for t in C.kalshi_trades(m["ticker"], start.timestamp() - 2 * 86400):
            side = t.get("taker_side")
            px = float(t["yes_price_dollars"] if side == "yes" else t["no_price_dollars"])
            ts = C.iso(t["created_time"]).timestamp()
            if ts > start.timestamp() + 6 * 3600 or not 0.01 < px < 0.99:
                continue
            rows.append([round(ts), team if side == "yes" else other, round(px, 4), round(float(t["count_fp"]) * px, 2)])
    rows.sort()
    path.write_text(json.dumps(dict(ticker=e["event_ticker"], title=e.get("title"), start=start.timestamp(),
                                    teams=teams, winner=winner, trades=rows)))
    return "new"


def safe(e):
    try:
        return one(e)
    except Exception as err:
        print(f"  {e.get('event_ticker')}: {err}", flush=True)
        return "error"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=60)
    ap.add_argument("--series", default="KXCS2GAME")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    evs = events(a.series, a.days)
    print(f"{len(evs)} settled {a.series} events", flush=True)
    done = 0
    with ThreadPoolExecutor(4) as ex:
        for _ in ex.map(safe, evs):
            done += 1
            if done % 250 == 0:
                print(f"  {done}/{len(evs)}", flush=True)
    print(f"done: {len(list(OUT.glob('*.json')))} events on disk", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
