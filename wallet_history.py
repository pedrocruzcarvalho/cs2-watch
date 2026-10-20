"""
Download the full Polymarket history of every wallet that bet low/mid-tier CS2 in the mined data (mine_collect.py).

A 21-day record picks hot streaks: the first "informed" wallets were all break-even or losing over their whole CS2
history. This keeps, per wallet, every CS2 position ever (match date from the slug, average price, won or lost,
money in) and the all-markets profit, so wallets can be judged on hundreds of bets and the choice tested walk-forward.

Losing positions are often never redeemed: they stay under /positions at price 0, not under /closed-positions.
Both are read.

One file per wallet in local_state/hist/. Run: python wallet_history.py --min-bets 5 --min-usd 500
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA = "https://data-api.polymarket.com"
OUT = Path("local_state/hist")
MAX_CLOSED = 5000                       # enough for any judgement; a few market-making bots have far more
DATE = re.compile(r"-(\d{4}-\d{2}-\d{2})")


def get(url, tries=8):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 + 3 * k)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 + 3 * k)
    raise RuntimeError(f"gave up on {url}")


def pages(base, step, cap):
    out, off = [], 0
    while off < cap:
        p = get(f"{base}&limit={step}&offset={off}")
        out += p
        if len(p) < step:
            break
        off += step
    return out


def cs2_row(x, won):
    slug = x.get("eventSlug") or x.get("slug") or ""
    if not slug.startswith("cs2-"):
        return None
    d = DATE.search(slug)
    if not d:
        return None
    usd = x.get("initialValue") or (x.get("totalBought") or 0) * (x.get("avgPrice") or 0)
    return [d.group(1), slug, x.get("title", ""), round(x.get("avgPrice") or 0, 4), won, round(usd, 2)]


def one(w):
    path = OUT / f"{w}.json"
    if path.exists():
        return "cached"
    closed = pages(f"{DATA}/closed-positions?user={w}&sortBy=TIMESTAMP&sortDirection=DESC", 50, MAX_CLOSED)
    openp = pages(f"{DATA}/positions?user={w}&sizeThreshold=0", 500, 20000)
    dead = [x for x in openp if x.get("curPrice") == 0]
    rows = [r for r in (cs2_row(x, x.get("realizedPnl", 0) > 0) for x in closed) if r]
    rows += [r for r in (cs2_row(x, False) for x in dead) if r]
    pnl = sum(x.get("realizedPnl", 0) for x in closed) + sum(x.get("cashPnl", 0) for x in dead)
    cs2_pnl = sum(x.get("realizedPnl", 0) for x in closed if (x.get("eventSlug") or "").startswith("cs2-")) + \
        sum(x.get("cashPnl", 0) for x in dead if (x.get("eventSlug") or "").startswith("cs2-"))
    path.write_text(json.dumps(dict(wallet=w, pnl=round(pnl), cs2_pnl=round(cs2_pnl), n_closed=len(closed),
                                    truncated=len(closed) >= MAX_CLOSED, rows=rows)))
    return "new"


def safe(w):
    try:
        return one(w)
    except Exception as e:                      # one bad wallet must not stop the run; it is retried next time
        print(f"  {w}: {e}", flush=True)
        return "error"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-bets", type=int, default=5)
    ap.add_argument("--min-usd", type=float, default=500)
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args()
    from mine_analyze import load
    n, usd = Counter(), Counter()
    for m in load():
        for mk in m["markets"]:
            for b in mk["bets"]:
                n[b["w"]] += 1
                usd[b["w"]] += b["usd"]
    ws = sorted((w for w in n if n[w] >= a.min_bets and usd[w] >= a.min_usd), key=lambda w: -n[w])
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"{len(ws)} wallets to fetch", flush=True)
    done = 0
    with ThreadPoolExecutor(a.threads) as ex:
        for r in ex.map(safe, ws):
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(ws)}", flush=True)
    print(f"done: {len(list(OUT.glob('*.json')))} wallets on disk", flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
