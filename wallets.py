"""
Wallet track records: is a flagged or watched wallet a whale that got lucky, a sharp bettor, or a suspect?

Polymarket's public data-api gives each wallet's positions. Winners usually move to /closed-positions once
redeemed, while losers mostly stay in /positions at price 0, so both are read; counting only the closed list
would make every wallet look like a genius.

For every resolved position we know the average price paid (= the chance the market gave it) and whether it won.
  win rate vs price : someone buying at 0.40 should win ~40%; the gap is their edge
  luck score (z)    : (wins - sum of prices) / sqrt(sum p(1-p)), i.e. how many standard deviations their wins
                      are above what the prices predicted; below 1 is ordinary luck, above 2.5 is very unlikely luck
The same numbers are computed for CS2 only and for the bets this scanner flagged.
"""

import json
import math
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DATA = "https://data-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0"}
TTL = 12 * 3600
MAX_PAGES = 30                         # 1,500 closed positions is plenty for a verdict
PER_TICK = 10
MIN_N = 20

BONUS = {"suspect": 5, "sharp": 3, "unclear": 2, "lucky": 1}
VERDICT_TEXT = {
    "suspect": "suspect: wins far more often than the prices allow, in a pattern that looks like inside knowledge",
    "sharp": "sharp bettor: consistently beats the prices across many markets, more like skill than a fix",
    "lucky": "lucky whale / normal bettor: results match the prices they paid",
    "unclear": "unclear: not enough resolved bets, or results in between",
}


def get(url):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read())


def resolved_positions(w):
    """[(price_paid, won, is_cs2, usd_cost)] for every position whose market has resolved."""
    out = []
    for i in range(MAX_PAGES):
        page = get(f"{DATA}/closed-positions?user={w}&limit=50&offset={i * 50}")
        for p in page:
            if p.get("curPrice", 0.5) >= 0.99 or p.get("curPrice", 0.5) <= 0.01:
                out.append((p["avgPrice"], p["curPrice"] >= 0.99, p.get("slug", "").startswith("cs2-"),
                            p["avgPrice"] * p.get("totalBought", 0)))
        if len(page) < 50:
            break
    for i in range(10):
        page = get(f"{DATA}/positions?user={w}&limit=500&offset={i * 500}&sizeThreshold=0")
        for p in page:
            if p.get("redeemable") or p.get("curPrice", 0.5) <= 0.001:  # resolved but never redeemed (mostly losers)
                if p.get("curPrice", 0.5) >= 0.99 or p.get("curPrice", 0.5) <= 0.01:
                    out.append((p["avgPrice"], p["curPrice"] >= 0.99, p.get("slug", "").startswith("cs2-"),
                                p.get("initialValue", 0)))
        if len(page) < 500:
            break
    return [x for x in out if 0 < x[0] < 1]


def record(items):
    """Wins, losses, win rate, average price, edge and luck score for [(price, won)]."""
    n = len(items)
    if not n:
        return dict(n=0)
    wins = sum(1 for p, w in items if w)
    exp = sum(p for p, _ in items)
    var = sum(p * (1 - p) for p, _ in items)
    return dict(n=n, wins=wins, losses=n - wins, win_rate=round(wins / n, 3), avg_price=round(exp / n, 3),
                edge=round(wins / n - exp / n, 3), z=round((wins - exp) / math.sqrt(var), 2) if var > 0 else 0.0)


def flagged_record(auto_entry):
    fl = [f for f in (auto_entry or {}).get("flags", {}).values() if f.get("won") is not None and f.get("price")]
    return record([(f["price"], f["won"]) for f in fl])


def verdict(prof):
    allr, cs, fl = prof["all"], prof["cs2"], prof["flagged"]
    traded = prof.get("traded")
    cs_share = cs.get("n", 0) / allr["n"] if allr.get("n") else 0
    if fl.get("n", 0) >= 2 and fl["wins"] >= 2 and fl["win_rate"] >= 0.75 and fl["edge"] >= 0.20:
        return "suspect", (f"won {fl['wins']} of {fl['n']} flagged bets at an average price of {fl['avg_price']:.2f} "
                           f"({fl['win_rate']:.0%} vs {fl['avg_price']:.0%} expected)")
    if allr.get("n", 0) >= MIN_N and allr["z"] >= 2.5 and cs_share >= 0.7 and (traded or 0) <= 100:
        return "suspect", (f"wins {allr['win_rate']:.0%} at prices that predict {allr['avg_price']:.0%} (luck score "
                           f"{allr['z']:.1f}), almost only on CS2, with few markets ever traded")
    if allr.get("n", 0) >= MIN_N and allr["z"] >= 2.5:
        return "sharp", f"wins {allr['win_rate']:.0%} vs {allr['avg_price']:.0%} expected over {allr['n']} bets (luck score {allr['z']:.1f})"
    if allr.get("n", 0) >= MIN_N and allr["z"] < 1:
        return "lucky", f"wins {allr['win_rate']:.0%} vs {allr['avg_price']:.0%} expected over {allr['n']} bets (luck score {allr['z']:.1f})"
    return "unclear", (f"{allr.get('n', 0)} resolved bets" + (f", luck score {allr['z']:.1f}" if allr.get("n") else ""))


def build(w, auto_entry=None, traded=None):
    items = resolved_positions(w)
    prof = dict(at=time.time(), traded=traded, all=record([(p, won) for p, won, _, _ in items]),
                cs2=record([(p, won) for p, won, cs, _ in items if cs]), flagged=flagged_record(auto_entry),
                volume=round(sum(c for *_, c in items)))
    prof["verdict"], prof["why"] = verdict(prof)
    return prof


def refresh(profiles, wallets, auto, traded, now, log=print):
    """Update stale profiles for `wallets` (most important first), at most PER_TICK per scan; always recompute the
    flagged record and verdict from the latest auto-watch data (no network needed for that)."""
    stale = [w for w in wallets if now - profiles.get(w, {}).get("at", 0) >= TTL][:PER_TICK]

    def one(w):
        try:
            return w, build(w, auto.get(w), traded.get(w))
        except Exception as err:
            log(f"  profile failed for {w[:10]}: {err}")
            return w, None
    with ThreadPoolExecutor(4) as ex:
        for w, prof in ex.map(one, stale):
            if prof:
                profiles[w] = prof
    for w, prof in profiles.items():
        if w in auto:
            prof["flagged"] = flagged_record(auto[w])
            prof["verdict"], prof["why"] = verdict(prof)
    for w in [w for w, p in profiles.items() if now - p.get("at", 0) > 30 * 86400]:
        del profiles[w]
    if stale:
        log(f"wallet records: refreshed {len(stale)} ({sum(1 for w in stale if w in profiles)} ok)")
    return profiles


def summary(prof):
    """One sentence for explanations and the dashboard."""
    if not prof:
        return None
    a, f = prof["all"], prof["flagged"]
    parts = []
    if a.get("n"):
        parts.append(f"overall {a['wins']}W-{a['losses']}L ({a['win_rate']:.0%} won at prices that predicted "
                     f"{a['avg_price']:.0%}, luck score {a['z']:.1f})")
    if prof["cs2"].get("n"):
        c = prof["cs2"]
        parts.append(f"CS2 {c['wins']}W-{c['losses']}L ({c['win_rate']:.0%} vs {c['avg_price']:.0%})")
    if f.get("n"):
        parts.append(f"on flagged bets {f['wins']}W-{f['losses']}L")
    return dict(verdict=prof["verdict"], label=VERDICT_TEXT[prof["verdict"]].split(":")[0], why=prof["why"],
                text="; ".join(parts) or "no resolved bets yet")
