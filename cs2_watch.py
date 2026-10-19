"""
CS2 integrity watch (on demand). Looks for out-of-place money on Counter-Strike match markets.

Venues:
  Polymarket - every trade carries a wallet, so we can ask "who is this?" (account age, how many markets
               it has ever traded). Gamma API for matches, data-api for trades and wallet history.
  Kalshi     - public trades are anonymous: only size, price and time. Match + map winner markets.

What counts as "out of place" (each adds points, see score()):
  - size        : one wallet's net buy on one outcome, in USDC, against fixed steps and against the
                  baseline of what positions normally look like (data/cs2/baseline.json, see --calibrate)
  - dominance   : that position's share of all the money on the match
  - fresh wallet: account < 7 / 30 days old, or few markets ever traded, or first ever trade right
                  before this match
  - context     : low-tier event (qualifiers, academies, open cups), pre-match, backing the underdog,
                  (tier-1 matches - big-stage main events or two top teams - are not scanned at all:
                  deep markets where big bets are normal)
                  the line moved the wallet's way
Ignored on purpose: fills at >= 0.97 or <= 0.03. Those are bots sweeping markets whose result is
already known, and they are the bulk of "big" trades on small matches.

A flag is a reason to look closer, not proof. Sharps, syndicates and people with real info (roster
news, a stand-in, a player sick) produce the same footprint.

Run:
  python cs2_watch.py                      matches from 12 h ago to 36 h ahead, both venues
  python cs2_watch.py --past 48 --ahead 0  only matches already played
  python cs2_watch.py --calibrate --days 7 learn what normal looks like + how often flags won
  python cs2_watch.py --jev                also ask Jev for a second opinion on MEDIUM/HIGH flags
Output: console, data/cs2/scan_<time>.json, cs2.html (dashboard)

Validation ledger (data/cs2/ledger.json): every scan records, per upcoming match, the side the flagged money
backed and that side's market price. The record freezes at kickoff and is resolved once the match ends,
so "do flagged sides beat their price?" is measured on predictions that were made before the result.
--calibrate also writes a backtest into the same ledger, using only trades placed before each kickoff.
"""

import argparse
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8")
DIR = Path(__file__).parent / "data" / "cs2"
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CS2_SERIES = 10310
KALSHI_SERIES = ["KXCS2GAME", "KXCS2MAP", "KXCS2TOTALMAPS"]
UA = {"User-Agent": "Mozilla/5.0"}
SWEEP = 0.97                 # fills at >= this (or <= 1 - this) are post-result sweeps
MIN_POSITION = 500           # USDC; smaller positions are never flagged on their own
CLUSTER_MIN = 200            # ...but positions from $200 count towards a split-bet cluster
CLUSTER_WALLETS, CLUSTER_USD = 3, 5000
WATCH = {}                   # wallet -> {"reason": ..., "source": "auto"|"manual"}; set by the caller before scan()
WATCH_MIN, WATCH_MEDIUM, WATCH_HIGH = 100, 500, 3000
STEAM_MOVE, STEAM_WINDOW = 0.12, 6 * 3600
CLOB = "https://clob.polymarket.com"
TOP_EVENTS = ("major", "iem ", "blast", "esl pro league", "esports world cup", "pgl masters", "pgl cluj",
              "pgl astana", "pgl bucharest", "starladder", "perfect world", "thunderpick world", "fissure playground")
LOW_WORDS = ("qualifier", "academy", "open", "contenders", "challengers", "league", "ligaen", "cup",
             "season", "series", "masters regular", "ranked", "group stage")
# roughly the HLTV top 30 (Oct 2026); two of these meeting is a deep market even in a small event
TOP_TEAMS = ("vitality", "spirit", "mouz", "natus vincere", "navi", "falcons", "furia", "the mongolz", "themongolz",
             "aurora", "faze", "g2", "parivision", "astralis", "liquid", "3dmax", "heroic", "virtus.pro", "fnatic",
             "legacy", "b8", "gamerlegion", "pain", "mibr", "betboom", "tyloo", "alliance", "nemiga", "fut", "big",
             "eternal fire", "complexity", "monte", "lynn vision", "ence", "sinners", "9z")
ET = ZoneInfo("America/New_York")


_kalshi_lock, _kalshi_next = threading.Lock(), [0.0]
KALSHI_GAP = 0.25            # public API rate-limits bursts; ~4 req/s across threads never hit 429


def get(url, tries=5):
    for i in range(tries):
        if url.startswith(KALSHI):
            with _kalshi_lock:
                t = max(time.time(), _kalshi_next[0])
                _kalshi_next[0] = t + KALSHI_GAP
            time.sleep(max(0.0, t - time.time()))
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read())
        except Exception as e:
            if i == tries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code not in (429, 500, 502, 503, 504)):
                raise
            time.sleep(2 ** i)


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=timezone.utc) if s else None


def tier(title):
    """'top': big-stage main event, or two top teams. 'mid': one top team (neutral). 'low': the rest."""
    t = title.lower()
    if any(k in t for k in TOP_EVENTS) and not any(k in t for k in ("qualifier", "academy", " open")):
        return "top"
    names = re.sub(r"^counter-strike:\s*", "", t).split(" (")[0]
    top = sum(any(re.fullmatch(rf"(team )?{re.escape(k)}( team| esports| gaming)?", side.strip()) for k in TOP_TEAMS)
              for side in re.split(r"\s+vs\.?\s+", names))
    return "top" if top >= 2 else "mid" if top == 1 else "low"


def norm_team(name):
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("esports", "").replace("gaming", "").replace("team", ""))


# ---------------------------------------------------------------------------------------------- Polymarket

def poly_matches(lo, hi):
    out, off = [], 0
    while True:
        page = get(f"{GAMMA}/events?series_id={CS2_SERIES}&end_date_min={lo:%Y-%m-%dT%H:%M:%SZ}&limit=100&offset={off}")
        out += page
        if len(page) < 100:
            break
        off += 100
    keep = []
    for e in out:
        st = iso(e.get("startTime"))
        if st and lo <= st <= hi and e.get("markets"):
            keep.append(e)
    return keep


def poly_trades(event_id):
    out = []
    for off in range(0, 3000, 500):
        page = get(f"{DATA}/trades?eventId={event_id}&limit=500&offset={off}")
        out += page
        if len(page) < 500:
            break
    return out


def poly_market_label(m):
    """One name per market type, shared with Kalshi: 'Match Winner', 'Map 1 Winner', 'Total Maps O/U 2.5',
    'Map Handicap: ...', 'Map 1 Total Rounds: Over/Under 21.5', 'Map 1 Rounds Handicap: ...'."""
    t, title = m.get("sportsMarketType"), m.get("groupItemTitle") or ""
    if t == "moneyline" or title.lower().startswith("match"):
        return "Match Winner"
    if t == "totals":
        n = re.search(r"([\d.]+)", title)
        return f"Total Maps O/U {n.group(1) if n else '2.5'}"
    return title or "Match Winner"


def market_kind(label):
    if label == "Match Winner":
        return "match"
    if re.fullmatch(r"Map \d+ Winner", label):
        return "map"
    if "Rounds" in label:
        return "rounds"
    return "maps"                                                   # total maps, map handicap


KIND_NAME = {"match": "Match winner", "map": "Map winners", "maps": "Total maps & map handicap",
             "rounds": "Round totals & handicaps"}


def winner_of(market, ended=False):
    """Winner once the market is closed, or once the match has ended and the price has gone to ~1.
    (Polymarket's formal resolution can lag the end of the match by hours.)"""
    try:
        prices = [float(p) for p in json.loads(market.get("outcomePrices") or "[]")]
        outs = json.loads(market.get("outcomes") or "[]")
    except Exception:
        return None
    if (market.get("closed") or ended) and prices and max(prices) >= 0.99:
        return outs[prices.index(max(prices))]
    return None


def poly_positions(event, trades, cutoff=None):
    """Net buy per (wallet, market, outcome), sweeps removed. Returns positions + match-level stats.
    cutoff: ignore trades at or after this time (backtests see only what was known at kickoff)."""
    start = iso(event["startTime"]).timestamp()
    markets = {m["conditionId"]: m for m in event["markets"]}
    real = [t for t in trades if (1 - SWEEP) < t["price"] < SWEEP and t["conditionId"] in markets
            and (cutoff is None or t["timestamp"] < cutoff)]
    notional = [t["size"] * t["price"] for t in real]
    mflow = {}
    for t in real:
        mflow[t["conditionId"]] = mflow.get(t["conditionId"], 0) + t["size"] * t["price"]
    stats = dict(n_trades=len(real), flow=sum(notional), p90=p90(notional),
                 pre_flow=sum(t["size"] * t["price"] for t in real if t["timestamp"] < start))
    pos = {}
    for t in sorted(real, key=lambda t: t["timestamp"]):
        k = (t["proxyWallet"], t["conditionId"], t["outcome"])
        p = pos.setdefault(k, dict(wallet=t["proxyWallet"], name=t.get("name") or t.get("pseudonym") or "",
                                   market=poly_market_label(markets[t["conditionId"]]),
                                   outcome=t["outcome"], buy=0.0, sell=0.0, shares=0.0, fills=0,
                                   first=t["timestamp"], last=t["timestamp"], pre=0.0))
        usd = t["size"] * t["price"]
        if t["side"] == "BUY":
            p["buy"] += usd
            p["shares"] += t["size"]
            if t["timestamp"] < start:
                p["pre"] += usd
        else:
            p["sell"] += usd
        p["fills"] += 1
        p["last"] = t["timestamp"]
    res = []
    for (w, cid, outcome), p in pos.items():
        p["net"] = p["buy"] - p["sell"]
        if p["net"] < CLUSTER_MIN and not (w.lower() in WATCH and p["net"] >= WATCH_MIN):
            continue
        p["avg_price"] = p["buy"] / p["shares"] if p["shares"] else 0
        p["share"] = p["net"] / stats["flow"] if stats["flow"] else 0
        p["mshare"] = p["net"] / mflow[cid] if mflow.get(cid) else 0
        p["market_flow"] = mflow.get(cid, 0)
        p["x_p90"] = p["net"] / stats["p90"] if stats["p90"] else 0
        p["pre_match"] = p["pre"] >= 0.5 * p["buy"]
        p["move"] = line_move(real, cid, outcome, p["first"], start)
        w = winner_of(markets[cid], event.get("ended"))
        p["won"] = None if w is None else w == outcome
        res.append(p)
    return res, stats


def match_winner_market(event):
    ms = event["markets"]
    return next((m for m in ms if m.get("sportsMarketType") == "moneyline"), None) or \
        next((m for m in ms if (m.get("groupItemTitle") or "").lower().startswith("match")), ms[0])


def poly_info(event, trades, cutoff=None):
    """Per market: outcomes, last traded price per outcome, winner once known, money traded. The match-winner
    market's teams also become the match's teams."""
    markets = []
    by_cid = {}
    for t in sorted(trades, key=lambda t: t["timestamp"]):
        if (cutoff is not None and t["timestamp"] >= cutoff) or not (1 - SWEEP) < t["price"] < SWEEP:
            continue
        by_cid.setdefault(t["conditionId"], []).append(t)
    for m in event["markets"]:
        outs = json.loads(m.get("outcomes") or "[]")
        px, flow = {}, 0.0
        for t in by_cid.get(m["conditionId"], []):
            flow += t["size"] * t["price"]
            px[t["outcome"]] = t["price"]
            other = next((x for x in outs if x != t["outcome"]), None)
            if other:
                px[other] = 1 - t["price"]
        if not px and cutoff is None:
            try:
                px = dict(zip(outs, (float(x) for x in json.loads(m.get("outcomePrices") or "[]"))))
            except Exception:
                px = {}
        markets.append(dict(market=poly_market_label(m), cid=m["conditionId"], outcomes=outs, prices=px,
                            winner=winner_of(m, event.get("ended")), flow=round(flow),
                            tokens=dict(zip(outs, json.loads(m.get("clobTokenIds") or "[]")))))
    mw = next((x for x in markets if x["market"] == "Match Winner"), markets[0])
    return dict(teams=mw["outcomes"], prices=mw["prices"], winner=mw["winner"], markets=markets)


def p90(xs):
    xs = sorted(xs)
    return xs[int(0.9 * (len(xs) - 1))] if xs else 0


def line_move(trades, cid, outcome, since, start):
    """Price of `outcome` just before the wallet's first fill vs at the last pre-match (or latest) trade."""
    seq = [(t["timestamp"], t["price"] if t["outcome"] == outcome else 1 - t["price"])
           for t in trades if t["conditionId"] == cid]
    before = [p for ts, p in seq if ts < since]
    after = [p for ts, p in seq if ts >= since and ts < max(start, since + 1)] or [p for ts, p in seq if ts >= since]
    if not before or not after:
        return 0.0
    return after[-1] - before[-1]


_wallets = {}
WALLET_TTL = 6 * 3600        # account age and first trade never change; "markets traded" drifts slowly


def load_wallets():
    path = DIR / "wallets.json"
    if path.exists():
        try:
            _wallets.update(json.loads(path.read_text()))
        except Exception:
            pass


def save_wallets():
    DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 30 * 86400
    keep = {w: i for w, i in _wallets.items() if i.get("fetched", 0) >= cutoff}
    (DIR / "wallets.json").write_text(json.dumps(keep))


def wallet_info(w):
    if w in _wallets and time.time() - _wallets[w].get("fetched", 0) < WALLET_TTL:
        return _wallets[w]
    info = dict(created=None, traded=None, first_trade=None, fetched=time.time())
    try:
        prof = get(f"{GAMMA}/public-profile?address={w}")
        info["created"] = iso(prof.get("createdAt")).timestamp() if prof.get("createdAt") else None
    except Exception:
        pass
    try:
        info["traded"] = get(f"{DATA}/traded?user={w}").get("traded")
    except Exception:
        pass
    try:
        a = get(f"{DATA}/activity?user={w}&limit=1&sortDirection=ASC")
        info["first_trade"] = a[0]["timestamp"] if a else None
    except Exception:
        pass
    _wallets[w] = info
    return info


# ------------------------------------------------------------------------------------------------- Kalshi

def kalshi_start(event_ticker):
    """KXCS2GAME-26OCT190400FORZERG1 -> 2026-10-19 04:00 New York time."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{4})", event_ticker)
    if not m:
        return None
    yy, mon, dd, hm = m.groups()
    d = datetime.strptime(f"20{yy}{mon}{dd}{hm}", "%Y%b%d%H%M").replace(tzinfo=ET)
    return d.astimezone(timezone.utc)


def kalshi_matches(lo, hi):
    out = []
    for s in KALSHI_SERIES:
        for status in ("open", "closed", "settled"):
            cursor = ""
            while True:
                q = dict(series_ticker=s, status=status, limit=200, with_nested_markets="true")
                if status != "open":
                    q["min_close_ts"] = int(lo.timestamp())
                if cursor:
                    q["cursor"] = cursor
                d = get(f"{KALSHI}/events?{urllib.parse.urlencode(q)}")
                for e in d.get("events", []):
                    st = kalshi_start(e["event_ticker"])
                    if st and lo <= st <= hi:
                        e["start"] = st
                        out.append(e)
                cursor = d.get("cursor")
                if not cursor or not d.get("events"):
                    break
    seen, uniq = set(), []
    for e in out:
        if e["event_ticker"] not in seen:
            seen.add(e["event_ticker"])
            uniq.append(e)
    return uniq


def kalshi_trades(ticker, since):
    out, cursor = [], ""
    for _ in range(20):
        q = dict(ticker=ticker, limit=1000, min_ts=int(since))
        if cursor:
            q["cursor"] = cursor
        d = get(f"{KALSHI}/markets/trades?{urllib.parse.urlencode(q)}")
        out += d.get("trades", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    return out


def kalshi_blocks(event, cutoff=None):
    """Kalshi has no wallets, so the unit is one taker order: fills on the same side in the same second."""
    start = event["start"].timestamp()
    totals = event["event_ticker"].startswith("KXCS2TOTALMAPS")
    teams = ["Over", "Under"] if totals else [m.get("yes_sub_title") or m["ticker"] for m in event["markets"]]
    rows = []
    for m in event["markets"]:
        team = "Over" if totals else (m.get("yes_sub_title") or m["ticker"])
        other = next((t for t in teams if t != team), "NOT " + team)
        for t in kalshi_trades(m["ticker"], start - 4 * 86400):
            side = t.get("taker_side")
            px = float(t["yes_price_dollars"] if side == "yes" else t["no_price_dollars"])
            if not (1 - SWEEP) < px < SWEEP:
                continue
            ts = iso(t["created_time"]).timestamp()
            if cutoff is not None and ts >= cutoff:
                continue
            rows.append(dict(ts=ts, team=team if side == "yes" else other, px=px,
                             usd=float(t["count_fp"]) * px, yes_team=team,
                             p_team=float(t["yes_price_dollars"])))
    rows.sort(key=lambda r: r["ts"])
    usd = [r["usd"] for r in rows]
    stats = dict(n_trades=len(rows), flow=sum(usd), p90=p90(usd),
                 pre_flow=sum(r["usd"] for r in rows if r["ts"] < start))
    blocks, by_key = [], {}
    for r in rows:
        b = by_key.get((r["team"], int(r["ts"])))
        if not b:
            b = dict(outcome=r["team"], buy=0.0, shares=0.0, fills=0, first=r["ts"], last=r["ts"], pre=0.0,
                     wallet=None, name="(anonymous)", market=event_label(event), sell=0.0)
            blocks.append(b)
            by_key[(r["team"], int(r["ts"]))] = b
        b["buy"] += r["usd"]
        b["shares"] += r["usd"] / r["px"]
        b["fills"] += 1
        b["last"] = r["ts"]
        if r["ts"] < start:
            b["pre"] += r["usd"]
    res = []
    if totals:
        winners = {"Over": m.get("result") == "yes" for m in event["markets"] if m.get("result")}
        if winners:
            winners["Under"] = not winners["Over"]
    else:
        winners = {m.get("yes_sub_title"): m.get("result") == "yes" for m in event["markets"] if m.get("result")}
    for b in blocks:
        b["net"] = b["buy"]
        if b["net"] < MIN_POSITION:
            continue
        b["avg_price"] = b["buy"] / b["shares"]
        b["share"] = b["net"] / stats["flow"] if stats["flow"] else 0
        b["mshare"], b["market_flow"] = b["share"], stats["flow"]       # a Kalshi event is a single market
        b["x_p90"] = b["net"] / stats["p90"] if stats["p90"] else 0
        b["pre_match"] = b["pre"] >= 0.5 * b["buy"]
        seq = [(r["ts"], r["p_team"] if r["yes_team"] == b["outcome"] else 1 - r["p_team"]) for r in rows
               if b["outcome"] in (r["yes_team"], r["team"])]
        before = [p for ts, p in seq if ts < b["first"]]
        after = [p for ts, p in seq if ts > b["last"]]
        b["move"] = (after[-1] - before[-1]) if before and after else 0.0
        b["won"] = winners.get(b["outcome"]) if winners else None
        res.append(b)
    last = {}
    for r in rows:
        last[r["yes_team"]] = r["p_team"]
    for t in teams:
        o = next((x for x in teams if x != t and x in last), None)
        if t not in last and o:
            last[t] = 1 - last[o]
    stats.update(prices=last, teams=teams, winner=next((k for k, v in winners.items() if v), None))
    stats["markets"] = [dict(market=event_label(event), cid=None, outcomes=teams, prices=last, winner=stats["winner"],
                             flow=round(stats["flow"]))]
    return res, stats


def event_label(e):
    if e.get("event_ticker", "").startswith("KXCS2TOTALMAPS"):
        return "Total Maps O/U 2.5"
    t = e.get("title", "")
    return "Map " + t.split("Map ")[1].split(" ")[0] + " Winner" if "Map " in t else "Match Winner"


# ------------------------------------------------------------------------------------------------ scoring

SIZE_STEPS = [(3000, 1), (10000, 2), (25000, 3)]


def score(p, tier_, wallet, match_start, baseline=None):
    """Points + human-readable reasons. Thresholds are deliberately simple so a reader can argue with them."""
    pts, why = 0, []
    s = max([v for k, v in SIZE_STEPS if p["net"] >= k], default=0)
    if s:
        pts += s
        why.append(f"${p['net']:,.0f} position")
    pct = size_percentile(p["net"], tier_, baseline)
    if pct is not None and pct >= 99:
        pts += 1
        why.append(f"top {100 - pct:.1f}% of {tier_}-tier positions")
    if p["share"] >= 0.4:
        pts += 2
        why.append(f"{p['share']:.0%} of all money on the match")
    elif p["share"] >= 0.2:
        pts += 1
        why.append(f"{p['share']:.0%} of the match's money")
    if p["x_p90"] >= 25:
        pts += 1
        why.append(f"{p['x_p90']:.0f}x a typical big trade on this match (p90)")
    if p.get("market", "Match Winner") != "Match Winner" and p.get("mshare", 0) >= 0.5:
        pts += 1
        why.append(f"{p['mshare']:.0%} of all money in the {p['market']} market")
    if wallet:
        if wallet.get("created"):
            age = (match_start - wallet["created"]) / 86400
            if age < 7:
                pts += 3
                why.append(f"account {max(age, 0):.1f} days old")
            elif age < 30:
                pts += 1
                why.append(f"account {age:.0f} days old")
        if wallet.get("traded") is not None:
            if wallet["traded"] <= 5:
                pts += 2
                why.append(f"only {wallet['traded']} markets ever traded")
            elif wallet["traded"] <= 20:
                pts += 1
                why.append(f"{wallet['traded']} markets ever traded")
            elif wallet["traded"] >= 500:
                pts -= 2
                why.append(f"busy trader ({wallet['traded']} markets) - likely a pro/bot")
        if wallet.get("first_trade") and 0 <= p["first"] - wallet["first_trade"] < 2 * 86400:
            pts += 1
            why.append("first ever trade was within 48 h of this bet")
    if p["pre_match"]:
        pts += 1
        why.append("bet before the match started")
    if p["avg_price"] < 0.40:
        pts += 1
        why.append(f"backed the underdog at {p['avg_price']:.2f}")
    if p["move"] >= 0.10:
        pts += 1
        why.append(f"line moved {p['move'] * 100:+.0f}c its way")
    if tier_ == "low":
        pts += 1
        why.append("low-tier event")
    return pts, why


def level(pts, usd, share):
    """Points alone are not enough. Polymarket stakes are big, so a few thousand dollars is ordinary money; the
    30%-of-the-match route keeps thin matches covered, where $3k can be most of what is traded."""
    if pts >= 9 and (usd >= 10000 or (usd >= 5000 and share >= 0.3)):
        return "HIGH"
    if pts >= 6 and (usd >= 5000 or (usd >= 3000 and share >= 0.3)):
        return "MEDIUM"
    return "LOW" if pts >= 4 and usd >= 3000 else ""


def watch_level(lvl, usd):
    """A watched wallet's bet is at least MEDIUM from WATCH_MEDIUM and HIGH from WATCH_HIGH."""
    floor = "HIGH" if usd >= WATCH_HIGH else "MEDIUM" if usd >= WATCH_MEDIUM else ""
    return min(lvl or "", floor, key=lambda x: LEVELS.index(x)) if floor else lvl


def size_percentile(x, tier_, baseline):
    if not baseline or tier_ not in baseline.get("sizes", {}):
        return None
    xs = baseline["sizes"][tier_]
    return 100 * sum(v <= x for v in xs) / len(xs) if xs else None


# --------------------------------------------------------------------------------------------------- scan

def scan(lo, hi, venues, baseline=None, log=print, prematch_only=False):
    """Returns (flags, every scored position, one info dict per match per venue)."""
    positions_all, infos = [], []

    def do_poly(e):
        trades = poly_trades(e["id"])
        cut = iso(e["startTime"]).timestamp() if prematch_only else None
        pos, stats = poly_positions(e, trades, cut)
        stats.update(poly_info(e, trades, cut))
        return e, pos, stats

    def do_kalshi(e):
        pos, stats = kalshi_blocks(e, e["start"].timestamp() if prematch_only else None)
        return e, pos, stats

    jobs, top = [], []                                              # tier-1 matches are skipped
    if "poly" in venues:
        ev = poly_matches(lo, hi)
        top = [(match_key(e["title"]), iso(e["startTime"]).timestamp()) for e in ev if tier(e["title"]) == "top"]
        keep = [e for e in ev if tier(e["title"]) != "top"]
        log(f"polymarket: {len(ev)} CS2 matches in window, {len(ev) - len(keep)} tier-1 skipped")
        jobs += [("poly", do_poly, e) for e in keep]
    if "kalshi" in venues:
        ev = kalshi_matches(lo, hi)
        keep = [e for e in ev if tier(e["title"]) != "top" and not any(
            same_match(k, match_key(e["title"])) and abs(t - e["start"].timestamp()) < 6 * 3600 for k, t in top)]
        log(f"kalshi: {len(ev)} CS2 match/map events in window, {len(ev) - len(keep)} tier-1 skipped")
        jobs += [("kalshi", do_kalshi, e) for e in keep]

    with ThreadPoolExecutor(8) as ex:
        futs = [(venue, ex.submit(fn, e)) for venue, fn, e in jobs]
        results = []
        for venue, f in futs:
            try:
                results.append((venue, *f.result()))
            except Exception as err:
                log(f"  skip ({venue}): {err}")

    load_wallets()
    wallets_needed = {p["wallet"] for v, e, pos, st in results if v == "poly" for p in pos}
    log(f"looking up {len(wallets_needed)} wallets ...")
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(wallet_info, wallets_needed))
    save_wallets()

    poly_tier = {match_key(e["title"]): (tier(e["title"]), e["title"]) for v, e, _, _ in results if v == "poly"}
    small = []
    for venue, e, pos, stats in results:
        title = e["title"]
        if venue == "poly":
            t = tier(title)
        else:
            t, ptitle = poly_tier.get(match_key(title), ("unknown", None))
            title = f"{title}  [{ptitle.split(' - ', 1)[-1]}]" if ptitle else title
        start = (iso(e["startTime"]) if venue == "poly" else e["start"]).timestamp()
        for mk in stats["markets"]:
            infos.append(dict(venue=venue, match=title, tier=t, start=start, market=mk["market"],
                              teams=mk["outcomes"], prices=mk["prices"], winner=mk["winner"], flow=mk["flow"],
                              cid=mk["cid"], tokens=mk.get("tokens", {}),
                              ref=e["slug"] if venue == "poly" else e["event_ticker"],
                              url=(f"https://polymarket.com/event/{e['slug']}" if venue == "poly"
                                   else f"https://kalshi.com/markets/{e['event_ticker'].split('-')[0].lower()}")))
        for p in pos:
            w = _wallets.get(p["wallet"]) if p["wallet"] else None
            watched = WATCH.get(p["wallet"].lower()) if p["wallet"] else None
            if p["net"] < MIN_POSITION and not watched:
                small.append(dict(match=title, tier=t, start=start, market=p["market"], outcome=p["outcome"],
                                  wallet=p["wallet"], name=p["name"], usd=p["net"], avg_price=p["avg_price"],
                                  first=p["first"], pre=p["pre"], buy=p["buy"], wallet_info=w,
                                  match_flow=stats["flow"], market_flow=p.get("market_flow", 0),
                                  url=f"https://polymarket.com/event/{e['slug']}"))
                continue
            pts, why = score(p, t, w, start, baseline)
            lvl = level(pts, p["net"], p["share"])
            if watched:
                pts += watched.get("bonus", 5)                 # weight depends on the wallet's track record
                why.insert(0, f"watched wallet (+{watched.get('bonus', 5)}): {watched.get('reason') or 'on the watchlist'}"
                              + (f"; record says {watched['verdict']}" if watched.get("verdict") else ""))
                if watched.get("floors", True):
                    lvl = watch_level(lvl, p["net"])
            row = dict(venue=venue, match=title, tier=t, start=start, pts=pts, level=lvl, why=why,
                       watched=watched.get("reason") if watched else None,
                       wallet=p["wallet"], name=p["name"], market=p["market"], outcome=p["outcome"],
                       usd=round(p["net"]), avg_price=round(p["avg_price"], 3), share=round(p["share"], 3),
                       first=p["first"], won=p["won"], x_p90=round(p["x_p90"], 1),
                       mshare=round(p.get("mshare", p["share"]), 3), market_flow=round(p.get("market_flow", 0)),
                       pre_match=p["pre_match"], move=round(p["move"], 3), match_flow=round(stats["flow"]), wallet_info=w,
                       url=(f"https://polymarket.com/event/{e['slug']}" if venue == "poly"
                            else f"https://kalshi.com/markets/{e['event_ticker'].split('-')[0].lower()}"))
            if p["wallet"]:
                row["profile"] = f"https://polymarket.com/profile/{p['wallet']}"
            positions_all.append(row)
    positions_all += find_clusters(small, positions_all, baseline)
    corroborate(positions_all)
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    flags = sorted((r for r in positions_all if r["level"]), key=lambda r: (rank[r["level"]], -r["pts"]))
    return flags, positions_all, infos


def match_key(title):
    title = title.split("  [")[0]                                   # Kalshi titles carry "  [tournament]"
    teams = re.split(r"\s+vs\.?\s+", re.sub(r"^Counter-Strike:\s*", "", title).split(" (")[0].split(":")[0])
    return frozenset(norm_team(t) for t in teams)


def same_match(a, b):
    """Team-name sets match, tolerating venue variants: 'WBT' vs 'WBT Academy', 'GenOne' vs 'Gen One'."""
    if a == b:
        return True
    if len(a) != len(b) or not a:
        return False
    rest = set(b)
    for t in a:
        hit = next((u for u in rest if t and u and (t.startswith(u) or u.startswith(t))), None)
        if not hit:
            return False
        rest.discard(hit)
    return True


def corroborate(rows):
    """Same match + market + team, a real chunk of money on BOTH venues within 6 h -> +2.
    Someone with inside info has every reason to spread the bet across venues."""
    def big(r):
        return r["usd"] >= 2000 and r["share"] >= 0.10
    by = {}
    for r in rows:
        by.setdefault((match_key(r["match"]), r["market"], norm_team(r["outcome"])), []).append(r)
    for r in rows:
        if not big(r):
            continue
        other = [o for o in by[(match_key(r["match"]), r["market"], norm_team(r["outcome"]))]
                 if o["venue"] != r["venue"] and big(o) and abs(o["first"] - r["first"]) < 6 * 3600]
        if other:
            r["pts"] += 2
            r["why"].append(f"same side also hit on {other[0]['venue']} (${other[0]['usd']:,})")
            r["level"] = level(r["pts"], r["usd"], r["share"])


# ------------------------------------------------------------------------------------- matches + ledger

LEVELS = ["HIGH", "MEDIUM", "LOW", ""]
FLAG_KEYS = ("venue", "market", "outcome", "usd", "avg_price", "share", "level", "pts", "why", "name", "wallet",
             "first", "profile", "pre_match", "move", "x_p90", "wallet_info", "mshare", "market_flow", "cluster",
             "members", "watched", "record")


def team_for(outcome, teams):
    o = norm_team(outcome)
    return next((t for t in teams if norm_team(t) == o), None) or \
        next((t for t in teams if o and (norm_team(t).startswith(o) or o.startswith(norm_team(t)))), None)


LIVE_MIN_USD, LIVE_MIN_SHARE = 5000, 0.25            # Polymarket, fresh (or watched) wallet required on top
KALSHI_LIVE_MIN_USD, KALSHI_LIVE_MIN_SHARE = 15000, 0.50
LIVE_PRICE = (0.10, 0.85)


def live_check(r):
    """In-play bets are judged far more strictly than pre-match ones: live traders bet big on the score all
    the time. Every condition must hold. Returns the reasons (a list) or None."""
    if r.get("pre_match", True) or r.get("tier") == "top" or not LIVE_PRICE[0] <= r["avg_price"] <= LIVE_PRICE[1]:
        return None
    if r["venue"] == "kalshi":
        if r["usd"] >= KALSHI_LIVE_MIN_USD and r["share"] >= KALSHI_LIVE_MIN_SHARE:
            return [f"${r['usd']:,} single in-play Kalshi order", f"{r['share']:.0%} of all money on the match"]
        return None
    w = r.get("wallet_info") or {}
    age = r["start"] - w["created"] if w.get("created") else None
    traded = w.get("traded")
    if traded is not None and traded >= 500:
        return None
    fresh = age is not None and age < 7 * 86400
    few = traded is not None and traded <= 5
    watched = r.get("watched")
    if not (fresh or few or watched) or r["usd"] < LIVE_MIN_USD or r["share"] < LIVE_MIN_SHARE:
        return None
    why = [f"${r['usd']:,} placed during the match", f"{r['share']:.0%} of all money on the match"]
    if watched:
        why.append(f"watched wallet ({watched})")
    if fresh:
        why.append(f"account {max(age, 0) / 86400:.1f} days old")
    if few:
        why.append(f"only {traded} markets ever traded")
    why.append(f"price still open at {r['avg_price']:.2f}")
    return why


def build_matches(infos, rows):
    """One record per real-world match and market (Polymarket and Kalshi merged): which outcome the flagged money
    backed in that market. The match-winner market is always kept (it is also the control for validation);
    side markets (maps, totals, handicaps, rounds) only when something in them is flagged."""
    groups = []
    for i in sorted(infos, key=lambda i: (i["venue"] != "poly", i.get("market", "Match Winner") != "Match Winner")):
        g = next((g for g in groups if same_match(g["key"], match_key(i["match"])) and abs(g["start"] - i["start"]) < 6 * 3600),
                 None)
        if not g:
            g = dict(key=match_key(i["match"]), start=i["start"], match=i["match"], tier=i["tier"], teams=i["teams"],
                     markets={})
            groups.append(g)
        g["markets"].setdefault(i.get("market", "Match Winner"), {})[i["venue"]] = i
    out = []
    for g in groups:
        base_id = f"{'-'.join(sorted(g['key']))}|{datetime.fromtimestamp(g['start'], timezone.utc):%Y-%m-%d}"
        mine_all = [r for r in rows if same_match(match_key(r["match"]), g["key"]) and abs(r["start"] - g["start"]) < 6 * 3600]
        for label, venues in g["markets"].items():
            first = next(iter(venues.values()))
            outcomes = (venues.get("poly") or first)["teams"]
            mine = [r for r in mine_all if r.get("market", "Match Winner") == label]
            m = judge_market(mine, outcomes)
            if label != "Match Winner" and not m["level"] and not m["live_side"]:
                continue
            winner = next((team_for(v["winner"], outcomes) for v in venues.values() if v.get("winner")), None)
            prices = next((v["prices"] for v in venues.values() if v.get("prices")), {})
            fav = max(prices, key=prices.get) if prices else None
            tracked = m["fix_side"] or fav
            refs = {v["venue"]: v["ref"] for v in venues.values()}
            if venues.get("poly", {}).get("cid"):
                refs["poly_cid"] = venues["poly"]["cid"]
            out.append(dict(m, tokens=venues.get("poly", {}).get("tokens", {}),
                            id=base_id if label == "Match Winner" else f"{base_id}|{label}", match_id=base_id,
                            market=label, kind=market_kind(label), match=g["match"], tier=g["tier"], start=g["start"],
                            teams=outcomes, winner=winner, tracked=tracked,
                            tracked_is="flagged" if m["fix_side"] else "favorite",
                            price=round(prices[tracked], 3) if tracked in prices else None, prices=prices, refs=refs,
                            urls={v["venue"]: v["url"] for v in venues.values()},
                            flow=sum(v["flow"] for v in venues.values())))
    return sorted(out, key=lambda m: (m["start"], m["kind"] != "match", m["market"]))


def judge_market(mine, outcomes):
    """Flagged side, level and LIVE flag for one market's positions."""
    flagged = sorted((r for r in mine if r["level"]), key=lambda r: (LEVELS.index(r["level"]), -r["pts"]))
    in_cluster = {w for r in flagged if r.get("cluster") for w in r.get("members_wallets", [])}
    money, best = {t: 0.0 for t in outcomes}, {}
    for r in flagged:
        t = team_for(r["outcome"], outcomes)
        if t:
            if r.get("wallet") not in in_cluster:                      # already counted inside its cluster
                money[t] += r["usd"]
            best.setdefault(t, r)                                      # flagged is sorted best-first
    side = max(money, key=money.get) if money and max(money.values()) > 0 else None
    lvl, pts, conflict, base, adjust = "", 0, False, "", []
    if side:
        lvl, pts = best[side]["level"], best[side]["pts"]
        base = lvl                                                     # what the bets alone are worth
        if sum(v for t, v in money.items() if t != side) >= 0.5 * money[side]:
            conflict = True                                            # flagged money on both sides
            lvl = LEVELS[min(LEVELS.index(lvl) + 1, 3)]
            other = next((t for t in money if t != side), "the other side")
            adjust.append(dict(kind="conflict", effect="down", other=other, other_money=round(money[other])))
    live, live_money = [], {}
    for r in mine:
        why = live_check(r)
        t = team_for(r["outcome"], outcomes)
        if why and t:
            live.append({k: r[k] for k in FLAG_KEYS if k in r} | dict(level="LIVE", why=why))
            live_money[t] = live_money.get(t, 0) + r["usd"]
    live_side = max(live_money, key=live_money.get) if live_money else None
    if live_side and sum(v for t, v in live_money.items() if t != live_side) >= 0.5 * live_money[live_side]:
        live_side = None                                               # live money on both sides: no signal
    return dict(level=lvl, pts=pts, conflict=conflict, base_level=base, base_pts=pts, adjust=adjust,
                fix_side=side, money={t: round(v) for t, v in money.items()},
                flags=[{k: r[k] for k in FLAG_KEYS if k in r} for r in flagged],
                live_side=live_side, live_flags=sorted(live, key=lambda f: -f["usd"]),
                live_money={t: round(v) for t, v in live_money.items()})


def load_ledger():
    path = DIR / "ledger.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_ledger(led):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / "ledger.json.tmp"
    tmp.write_text(json.dumps(led))
    tmp.replace(DIR / "ledger.json")


LEDGER_KEYS = ("match", "tier", "start", "teams", "level", "pts", "conflict", "fix_side", "tracked", "tracked_is",
               "price", "refs", "urls", "money", "market", "kind", "match_id", "steam")


def record(led, matches, mode, now):
    """live: overwrite while the match has not started, freeze at kickoff. backtest: written as-is.
    live records also keep `history`: one entry each time the level or the flagged side changes
    (LOW -> MEDIUM -> HIGH ...), with the time and the price of the flagged side at that moment."""
    for m in matches:
        k = f"{mode}|{m['id']}"
        if mode == "live" and now >= m["start"]:
            # after kickoff the record is frozen; only a LIVE flag (one per match) is added to its history
            old = led.get(k)
            if old and m.get("live_side") and not m["winner"] and not any(
                    h["level"] == "LIVE" for h in old.get("history", [])):
                old["history"] = old.get("history", []) + [dict(
                    t=now, level="LIVE", pts=0, side=m["live_side"], price=m["prices"].get(m["live_side"]),
                    asks=book_asks(m.get("tokens", {}).get(m["live_side"])),
                    snap=dict(flags=m["live_flags"][:5], money=m["live_money"], flow=m["flow"], tier=m["tier"],
                              conflict=False, live=True))]
            continue
        if mode == "live" and m["price"] is None:
            continue
        hist = led.get(k, {}).get("history", [])
        if mode == "live":
            last = hist[-1] if hist else None
            if not last or (last["level"], last["side"]) != (m["level"], m["fix_side"]):
                hist = hist + [dict(t=now, level=m["level"], pts=m["pts"], side=m["fix_side"],
                                    price=m["prices"].get(m["fix_side"]) if m["fix_side"] else None,
                                    asks=book_asks(m.get("tokens", {}).get(m["fix_side"])) if m["level"] else None,
                                    # the evidence as it stood at this moment, so a bet can explain itself later
                                    snap=dict(flags=m["flags"][:5], money=m["money"], flow=m["flow"], tier=m["tier"],
                                              conflict=m["conflict"], steam=m.get("steam"), sharp=m.get("sharp"),
                                              books=m.get("books"), base_level=m.get("base_level"),
                                              base_pts=m.get("base_pts"), adjust=m.get("adjust"))
                                    if m["level"] else None)]
        line = list(led.get(k, {}).get("line", []))
        px0 = m["prices"].get(m["teams"][0]) if m.get("teams") else None
        if mode == "live" and px0 is not None and (not line or abs(line[-1][1] - px0) >= 0.005):
            line = (line + [[now, round(px0, 3)]])[-400:]
        led[k] = {f: m.get(f) for f in LEDGER_KEYS} | dict(mode=mode, made=now, history=hist, line=line,
                                                       winner=m["winner"] if mode == "backtest" else None)


def book_asks(token, levels=15):
    """Best asks on Polymarket's public order book: [[price, shares], ...] cheapest first.
    [] means nobody is selling; None means the book couldn't be read."""
    if not token:
        return None
    try:
        b = get(f"{CLOB}/book?token_id={token}")
    except Exception:
        return None
    asks = sorted((float(a["price"]), float(a["size"])) for a in b.get("asks", []))
    return [[p, round(s, 2)] for p, s in asks if p < SWEEP][:levels]


def fresh_wallet(w, start):
    """New account (under 7 days at kickoff) or almost no history (5 or fewer markets), and not a busy pro."""
    w = w or {}
    if w.get("traded") is not None and w["traded"] >= 500:
        return False
    young = w.get("created") is not None and start - w["created"] < 7 * 86400
    few = w.get("traded") is not None and w["traded"] <= 5
    return young or few


def find_clusters(small, rows, baseline=None):
    """Split bets: CLUSTER_WALLETS+ fresh wallets backing the same outcome of the same market, together
    CLUSTER_USD+. Each piece can be small enough to pass unnoticed; together they look like one big bet."""
    pieces = {}
    for x in small + [r for r in rows if r["venue"] == "poly" and not r.get("cluster")]:
        if fresh_wallet(x.get("wallet_info"), x["start"]):
            pieces.setdefault((x["match"], x["start"], x["market"], x["outcome"]), []).append(x)
    out = []
    for (match, start, market, outcome), g in pieces.items():
        usd = sum(x["usd"] for x in g)
        if len({x["wallet"] for x in g}) < CLUSTER_WALLETS or usd < CLUSTER_USD:
            continue
        shares = sum(x["usd"] / x["avg_price"] for x in g if x["avg_price"])
        mflow = max(x.get("market_flow") or 0 for x in g)
        flow = max(x.get("match_flow") or 0 for x in g)
        pre = sum(x["usd"] for x in g if x.get("pre_match", x.get("pre", 0) >= 0.5 * x.get("buy", 1)))
        p = dict(net=usd, share=usd / flow if flow else 0, mshare=usd / mflow if mflow else 0, x_p90=0,
                 pre_match=pre >= 0.5 * usd, avg_price=usd / shares if shares else 0, move=0,
                 first=min(x["first"] for x in g), market=market)
        t = g[0]["tier"]
        pts, why = score(p, t, None, start, baseline)
        pts += 4
        why = [f"{len(g)} fresh wallets backing {outcome}, ${usd:,.0f} combined"] + why + [
            "split across several new wallets: a known way to hide one big bet"]
        out.append(dict(venue="poly", match=match, tier=t, start=start, pts=pts, level=level(pts, usd, p["share"]),
                        why=why, wallet=None, name=f"{len(g)} fresh wallets", market=market, outcome=outcome,
                        usd=round(usd), avg_price=round(p["avg_price"], 3), share=round(p["share"], 3),
                        mshare=round(p["mshare"], 3), market_flow=round(mflow), first=p["first"], won=None, x_p90=0,
                        pre_match=p["pre_match"], move=0, match_flow=round(flow), wallet_info=None,
                        url=g[0].get("url"), cluster=len(g),
                        members=[dict(name=x.get("name") or x["wallet"][:10], usd=round(x["usd"]),
                                      profile=f"https://polymarket.com/profile/{x['wallet']}") for x in
                                 sorted(g, key=lambda x: -x["usd"])][:10],
                        members_wallets=[x["wallet"] for x in g]))
    return out


AUTO_WATCH_MAX, AUTO_WATCH_DAYS = 150, 60


def update_watch(auto, rows, now):
    """Remember every MEDIUM/HIGH-flagged Polymarket wallet and how its flagged bets ended. A wallet goes on the
    watchlist when a flagged bet of its WON, or when it is flagged on 2+ different matches. `auto` is the stored
    dict {wallet: {...}}; returns the wallets currently watched automatically."""
    for r in rows:
        if r.get("venue") != "poly" or not r.get("wallet") or r.get("level") not in ("MEDIUM", "HIGH"):
            continue
        a = auto.setdefault(r["wallet"], dict(name=r.get("name") or "", first_seen=now, flags={}))
        k = f"{match_key_str(r['match'])}|{r['start']:.0f}|{r['market']}|{r['outcome']}"
        f = a["flags"].setdefault(k, dict(match=r["match"], market=r["market"], outcome=r["outcome"], start=r["start"]))
        f.update(level=r["level"], usd=r["usd"], price=r.get("avg_price"),
                 won=r.get("won") if r.get("won") is not None else f.get("won"))
        a["last_seen"] = now
    for w in [w for w, a in auto.items() if now - a.get("last_seen", a["first_seen"]) > AUTO_WATCH_DAYS * 86400]:
        del auto[w]
    watched = {}
    for w, a in auto.items():
        fl = list(a["flags"].values())
        wins = [f for f in fl if f.get("won")]
        matches = {f["match"] for f in fl}
        if wins:
            f = max(wins, key=lambda f: f["usd"])
            reason = f"its flagged ${f['usd']:,} bet on {f['outcome']} ({short_match(f['match'])}) won"
        elif len(matches) >= 2:
            reason = f"flagged on {len(matches)} different matches"
        else:
            continue
        a["watched_since"] = a.get("watched_since") or now
        watched[w] = dict(reason=reason, source="auto", name=a.get("name"), since=a["watched_since"],
                          won=sum(1 for f in fl if f.get("won")), lost=sum(1 for f in fl if f.get("won") is False),
                          flags=len(fl))
    if len(watched) > AUTO_WATCH_MAX:
        keep = sorted(watched, key=lambda w: -watched[w]["since"])[:AUTO_WATCH_MAX]
        watched = {w: watched[w] for w in keep}
    return watched


def match_key_str(title):
    return "-".join(sorted(match_key(title)))


def short_match(title):
    return re.sub(r"^Counter-Strike:\s*", "", title).split(" (")[0].split(" - ")[0]


def apply_line_moves(led, matches, now):
    """Steam: the flagged side's price climbed STEAM_MOVE+ within STEAM_WINDOW before kickoff (from this scanner's
    own 10-minute price history). Money spread over many accounts moves the price even when no single wallet
    stands out, so the market's level goes up one step. Call before record()."""
    for m in matches:
        side = m.get("fix_side")
        r = led.get(f"live|{m['id']}")
        if not side or not r or now >= m["start"] or side not in m["prices"] or not m.get("teams"):
            continue
        flip = side != m["teams"][0]
        pts = [(t, 1 - p if flip else p) for t, p in r.get("line", []) if t >= now - STEAM_WINDOW]
        cur = m["prices"][side]
        if not pts:
            continue
        low_t, low = min(pts, key=lambda x: x[1])
        if cur - low >= STEAM_MOVE and m["level"]:
            m["steam"] = dict(low=round(low, 3), now=round(cur, 3), since=low_t, move=round(cur - low, 3),
                              raised_from=m["level"])
            m.setdefault("adjust", []).append(dict(m["steam"], kind="line", effect="up"))
            m["level"] = LEVELS[max(LEVELS.index(m["level"]) - 1, 0)]
            m["pts"] += 2


def backfill_snapshots(led, matches, now):
    """Level-history entries recorded before evidence snapshots existed get the evidence the scanner sees now,
    marked with when it was captured, so older bets can still explain themselves."""
    by_id = {f"live|{m['id']}": m for m in matches}
    filled = 0
    for k, r in led.items():
        m = by_id.get(k)
        if not m or not m["flags"]:
            continue
        for h in r.get("history", []):
            if h.get("level") and not h.get("snap"):
                h["snap"] = dict(flags=m["flags"][:5], money=m["money"], flow=m["flow"], tier=m["tier"],
                                 conflict=m["conflict"], backfilled_at=now)
                filled += 1
    return filled


def resolve(led, now, log=print):
    """Fill in winners for frozen live predictions whose match should be over."""
    todo = [r for r in led.values() if r["mode"] == "live" and not r.get("winner") and r["start"] < now - 1800]
    for r in todo:
        try:
            if "poly" in r["refs"]:
                e = get(f"{GAMMA}/events?slug={r['refs']['poly']}")[0]
                cid = r["refs"].get("poly_cid")
                mk = next((m for m in e["markets"] if m["conditionId"] == cid), None) if cid else None
                w = winner_of(mk or match_winner_market(e), e.get("ended"))
            else:
                tk = r["refs"]["kalshi"]
                e = get(f"{KALSHI}/events/{tk}?with_nested_markets=true")
                ms = e.get("markets") or e.get("event", {}).get("markets", [])
                if tk.startswith("KXCS2TOTALMAPS"):
                    res = next((m.get("result") for m in ms if m.get("result")), None)
                    w = {"yes": "Over", "no": "Under"}.get(res)
                else:
                    w = next((m.get("yes_sub_title") for m in ms if m.get("result") == "yes"), None)
            if w:
                r["winner"] = team_for(w, r["teams"]) or w
                r["resolved_at"] = now
        except Exception as err:
            log(f"  resolve failed for {r['match'][:50]}: {err}")
    if todo:
        log(f"resolved {sum(1 for r in todo if r.get('winner'))}/{len(todo)} finished predictions")


def validation(led):
    """Per mode and level: how often the tracked side won vs the price it was trading at."""
    out = {}
    for mode in ("live", "backtest"):
        rows = [r for r in led.values() if r["mode"] == mode and r.get("winner") and r.get("price") is not None
                and r.get("market", "Match Winner") == "Match Winner"]
        for lvl in LEVELS:
            g = [r for r in rows if r["level"] == lvl]
            if not g:
                continue
            won = sum(r["winner"] == r["tracked"] for r in g)
            wr, px = won / len(g), sum(r["price"] for r in g) / len(g)
            out.setdefault(mode, []).append(dict(level=lvl or "none", n=len(g), won=won, win_rate=round(wr, 3),
                                                 price=round(px, 3), edge=round(wr - px, 3),
                                                 se=round(math.sqrt(max(wr * (1 - wr), 0.01) / len(g)), 3)))
    return out


# ---------------------------------------------------------------------------------------------- calibrate

def calibrate(days, log=print):
    """Score every closed match of the last `days`: the size distribution per tier is 'normal', and the
    win rate of flagged vs unflagged positions tells us whether the flags carry any information."""
    now = datetime.now(timezone.utc)
    flags, rows, infos = scan(now - timedelta(days=days), now - timedelta(hours=6), ["poly", "kalshi"], log=log,
                              prematch_only=True)
    sizes = {}
    for r in rows:
        sizes.setdefault(r["tier"], []).append(r["usd"])
    base = dict(made=now.isoformat(), days=days, sizes={k: sorted(v) for k, v in sizes.items()})
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / "baseline.json").write_text(json.dumps(base))
    log(f"\nbaseline from {len(rows)} positions >= ${MIN_POSITION} over {days} days")
    for k, v in sizes.items():
        v.sort()
        q = lambda f: v[min(len(v) - 1, int(f * len(v)))]
        log(f"  {k:4} tier: n={len(v):5}  median ${q(.5):,.0f}  p90 ${q(.9):,.0f}  p99 ${q(.99):,.0f}  max ${v[-1]:,.0f}")
    rescored = []
    for r in rows:
        p = dict(net=r["usd"], share=r["share"], x_p90=r["x_p90"], pre_match=r["pre_match"],
                 avg_price=r["avg_price"], move=r["move"], first=r["first"])
        pts, _ = score(p, r["tier"], r["wallet_info"], r["start"], base)
        rescored.append((level(pts, r["usd"], r["share"]) or "none", r))
    log("\nwin rate by flag level (resolved positions only). Compare against the price paid:")
    for lv in ("HIGH", "MEDIUM", "LOW", "none"):
        g = [r for l, r in rescored if l == lv and r["won"] is not None]
        if g:
            wr = sum(r["won"] for r in g) / len(g)
            implied = sum(r["avg_price"] for r in g) / len(g)
            log(f"  {lv:6} n={len(g):4}  won {wr:6.1%}  avg price paid {implied:.2f}  edge {wr - implied:+.2f}")
    log("  (edge = win rate minus price paid; small n is noise, a few hundred per bucket before it means anything)")
    led = load_ledger()
    record(led, build_matches(infos, rows), "backtest", now.timestamp())
    save_ledger(led)
    log("\nbacktest per MATCH (side the flagged money backed, last price before kickoff):")
    for v in validation(led).get("backtest", []):
        log(f"  {v['level']:6} n={v['n']:4}  won {v['win_rate']:6.1%}  price {v['price']:.2f}"
            f"  edge {v['edge']:+.2f} (+/-{v['se']:.2f})")
    return base


# ---------------------------------------------------------------------------------------------------- jev

JEV_Q = {"verdict": {"type": "choice", "instructions": (
    "You are an esports betting integrity analyst. Below is one bet on a Counter-Strike 2 match on a "
    "prediction market. Judge whether it looks like someone with knowledge that the match is fixed, or "
    "like ordinary trading (a sharp, a fan, a market maker, public roster news)."),
    "criteria": {
        "suspicious": "Suspicious: size, timing, wallet history and context are hard to explain without inside knowledge.",
        "ordinary": "Ordinary: a sharp bettor, a big fan, a market maker or public information explains it."}}}


def jev_state(r):
    w = r.get("wallet_info") or {}
    age = f"{(r['start'] - w['created']) / 86400:.1f} days" if w.get("created") else "unknown"
    return "\n".join([
        f"Match: {r['match']} ({'tier-1 event' if r['tier'] == 'top' else 'lower-tier event' if r['tier'] == 'low' else 'tier unknown'})",
        f"Venue: {r['venue']}. Market: {r['market']}. Total money traded on this match: ${r['match_flow']:,}.",
        f"Bet: ${r['usd']:,} on {r['outcome']} at average price {r['avg_price']:.2f} "
        f"({r['share']:.0%} of the match's money), placed {'before' if r['pre_match'] else 'during'} the match.",
        f"Price moved {r['move'] * 100:+.0f} cents in the bet's direction afterwards.",
        (f"Account age at match time: {age}. Markets ever traded: {w.get('traded', 'unknown')}."
         if r["venue"] == "poly" else "Kalshi trades are anonymous; no account history."),
    ])


def jev_opinions(rows, log=print):
    import engine as E
    jev = E.Jev(E.load_api_key(), rate=5.0, cache=str(DIR / "jev_cache.jsonl"))
    todo = [r for r in rows if r["level"] in ("HIGH", "MEDIUM")]
    log(f"asking Jev about {len(todo)} flags ...")

    def one(r):
        key = f"{r['venue']}|{r['match']}|{r['market']}|{r['outcome']}|{r['wallet']}|{r['usd']}"
        try:
            a = jev.ask(jev_state(r), JEV_Q, cache_key=key)["verdict"]
            r["jev"] = round(float(a["probabilities"]["suspicious"]), 3)
        except Exception as err:
            r["jev"] = None
            log(f"  jev failed: {err}")
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, todo))


# -------------------------------------------------------------------------------------------------- output

def show(flags, limit, log=print):
    if not flags:
        log("\nnothing out of place in this window.")
        return
    log(f"\n{len(flags)} flagged positions (HIGH >= 9 pts, MEDIUM >= 6, LOW >= 4)\n")
    for r in flags[:limit]:
        when = datetime.fromtimestamp(r["start"], timezone.utc).strftime("%m-%d %H:%M")
        res = {True: "  -> WON", False: "  -> lost", None: ""}[r["won"]]
        log(f"[{r['level']:6} {r['pts']:2}] {r['venue']:6} {when}Z  {r['match'][:80]}")
        log(f"           {r['market']}: ${r['usd']:,} on {r['outcome']} @ {r['avg_price']:.2f}"
            f"  (match total ${r['match_flow']:,}){res}")
        if r.get("profile"):
            log(f"           wallet {r['name'] or '?'}  {r['profile']}")
        log(f"           {r['url']}")
        log("           " + "; ".join(r["why"]))
        if r.get("jev") is not None:
            log(f"           Jev: {r['jev']:.0%} suspicious")
        log("")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--past", type=float, default=12, help="hours back (match start)")
    ap.add_argument("--ahead", type=float, default=36, help="hours ahead (match start)")
    ap.add_argument("--venue", default="poly,kalshi")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--jev", action="store_true", help="second opinion from Jev on MEDIUM/HIGH flags")
    a = ap.parse_args()
    if a.calibrate:
        calibrate(a.days)
        return
    base_path = DIR / "baseline.json"
    baseline = json.loads(base_path.read_text()) if base_path.exists() else None
    if not baseline:
        print("(no baseline yet - run --calibrate once so sizes are judged against what's normal)")
    now = datetime.now(timezone.utc)
    flags, rows, infos = scan(now - timedelta(hours=a.past), now + timedelta(hours=a.ahead), a.venue.split(","),
                              baseline)
    if a.jev and flags:
        jev_opinions(flags)
    show(flags, a.limit)
    matches = build_matches(infos, rows)
    led = load_ledger()
    apply_line_moves(led, matches, now.timestamp())
    record(led, matches, "live", now.timestamp())
    resolve(led, now.timestamp())
    save_ledger(led)
    DIR.mkdir(parents=True, exist_ok=True)
    out = DIR / f"scan_{now:%Y%m%d_%H%M}.json"
    out.write_text(json.dumps(dict(at=now.isoformat(), flags=flags, positions=len(rows)), default=str, indent=1))
    print(f"saved {out}")
    import cs2_dash
    page = cs2_dash.render(dict(at=now.timestamp(), window=[a.past, a.ahead], matches=matches,
                                validation=validation(led), ledger=list(led.values()),
                                baseline_days=(baseline or {}).get("days")))
    print(f"dashboard: {page}")


if __name__ == "__main__":
    main()
