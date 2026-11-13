"""
The hunter (Pedro, Nov 13 2026): one rule set, one paper account, CS2 and Dota 2.

Every tick it reads every CS2 and Dota 2 match on Polymarket from 5 hours ago to 36 hours ahead, every buy of $200+ in
its team markets (series and map/game winner), and adds them up per wallet and side, before kickoff and in-play
apart. A wallet's money is a signal when it passes RULES (tested on 60 days of finished matches, hunt_research.py).
The account starts at START ($63.47, Pedro's real balance), bets once per match and team at the real order book, and
settles from Polymarket's result. The page shows the bets; the phone gets a push for each new bet.
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import bankroll as B
import cs2_watch as C
import polydata as P

GAMES = {"cs2": 10310, "dota": 10309, "lol": 10311}
START = 63.47
MIN_TRADE = 200
WALLET_MIN = 1000              # a wallet needs this much on one side to be looked at
DAY = 86400
MAX_PRICE = 0.90               # never buy above this (nothing left to win)
MIN_PX = 0.35                  # big bets on long shots lost 40%+ in the test
PREFIXES = ("Counter-Strike: ", "Dota 2: ", "LoL: ", "League of Legends: ")


# ---------------------------------------------------------------------------------------------------- scan

def events(now):
    lo = now - timedelta(hours=8)
    out = []
    for game, series in GAMES.items():
        off = 0
        while True:
            page = C.get(f"{C.GAMMA}/events?series_id={series}&end_date_min={lo:%Y-%m-%dT%H:%M:%SZ}&closed=false"
                         f"&limit=100&offset={off}")
            for e in page:
                st = C.iso(e.get("startTime"))
                if st and now - timedelta(hours=5) <= st <= now + timedelta(hours=36) and e.get("markets"):
                    out.append((game, e))
            if len(page) < 100 or off >= 900:
                break
            off += 100
    return out


def team_markets(e):
    """Series winner and map/game winner markets (2 team outcomes; no totals or handicaps)."""
    out = []
    for m in e.get("markets") or []:
        try:
            outs = json.loads(m.get("outcomes") or "[]")
            toks = json.loads(m.get("clobTokenIds") or "[]")
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        except Exception:
            continue
        q = m.get("question") or ""
        if len(outs) != 2 or {o.lower() for o in outs} & {"yes", "no", "over", "under"} or \
                "handicap" in q.lower() or "o/u" in q.lower():
            continue
        kind = "map" if any(k in q for k in ("Map ", "Game ")) else "match"
        label = "Match Winner" if kind == "match" else (q.split(" - ")[-1] if " - " in q else q)
        out.append(dict(cid=m["conditionId"], kind=kind, label=label, outcomes=outs, tokens=dict(zip(outs, toks)),
                        prices=dict(zip(outs, prices)), vol=float(m.get("volume") or 0),
                        winner=C.winner_of(m, e.get("ended")), closed=bool(m.get("closed"))))
    return out


def positions(trades, mk, start):
    pos = {}
    for t in trades:
        if t["side"] != "BUY" or t["conditionId"] not in mk:
            continue
        k = (t["proxyWallet"].lower(), t["conditionId"], t["outcome"])
        p = pos.setdefault(k, dict(w=k[0], cid=k[1], o=k[2], name=t["name"] or t["pseudonym"] or "", pre=0.0,
                                   pre_sh=0.0, live=0.0, live_sh=0.0, first=t["timestamp"], last=t["timestamp"]))
        usd = t["size"] * t["price"]
        ph = "pre" if t["timestamp"] < start else "live"
        p[ph] += usd
        p[ph + "_sh"] += t["size"]
        p["first"], p["last"] = min(p["first"], t["timestamp"]), max(p["last"], t["timestamp"])
    return list(pos.values())


def created_at(w, cache, now):
    c = cache.get(w)
    if c and (c.get("t") is not None or now - c.get("checked", 0) < 6 * 3600):
        return c.get("t")
    t = None
    try:
        prof = C.get(f"{C.GAMMA}/public-profile?address={w}", tries=2)
        t = C.iso(prof["createdAt"]).timestamp() if prof.get("createdAt") else None
    except Exception:
        pass
    cache[w] = dict(t=t, checked=now)
    return t


# ----------------------------------------------------------------------------------------------- the rules

TOP_EVENTS = {   # tier 1/2 events, never bet (Pedro: "shit tiers only"); CS2 also uses the Valve ranking
    "dota": ("the international", "riyadh", "esports world cup", "dreamleague season", "pgl wallachia", "blast slam",
             "fissure", "esl one", "betboom dacha", "clavision"),
    "lol": ("worlds", "world championship", "lck", "lpl", "lec", "lcs", "msi", "mid-season", "lta", "first stand",
            "esports world cup")}


def top_tier(game, title):
    if game == "cs2":
        return C.tier(title) == "top" or C.vtier(title) in ("T1", "T2")
    t = title.lower()
    return any(k in t for k in TOP_EVENTS.get(game, ()))


# Tested on 60 days of finished matches (hunt_research.py; one bet per match, filled 3 cents worse than the wallet):
#   A  in-play, match < $200k, wallet >= 10% of the market, price >= 0.35   n=103  +13%  (halves +8% / +18%)
#   B  in-play, match < $200k, price >= 0.35                               n=760   +6%  (halves +4% / +8%)
#      (CS2 accounts under 3 days left out: they lost 35% in-play)
#   C  Dota 2 before kickoff, match < $100k, price >= 0.35                  n=52   +14%  (halves +11% / +18%)
#   D  CS2 before kickoff, account under 14 days, price >= 0.35: bet the OTHER team
#                                                                          n=74   +22%  (halves +17% / +27%)
#   E  LoL before kickoff, match < $200k, wallet >= 10% of the market      n=34   +10%  (halves +4% / +17%)
#   LoL in-play money did not work (-3%): A is CS2 and Dota 2 only. B on low-tier CS2 was flat (+1% / -2%): Dota 2 only.
#   Tier 1/2 events are never bet (Pedro, "shit tiers only"), though rule D's 74 cases were mostly tier 1/2.
#   Low tiers only, 60 days, $63.47 at these stakes: -> $191 (3 cents worse fills) / $136 (5 cents). In-sample.
# What did NOT work: big bets in big markets (tier 1/2), CS2 big bets before kickoff, long shots under 0.35
# (-13% to -40%), following fresh CS2 accounts (-24% to -36%).
RULES = {"A": (0.05, 80), "B": (0.02, 50), "C": (0.04, 70), "D": (0.03, 65), "E": (0.02, 60)}
RULE_TEXT = {"A": "in-play money, low tier, big share of the market", "B": "in-play money, low tier",
             "C": "Dota 2 pre-match money, low tier", "D": "fresh CS2 account before kickoff: bet against it",
             "E": "LoL pre-match money, low tier, big share of the market"}


def classify(u):
    """(rule, why not). rule is None when no rule fires; `why not` explains it for the page."""
    if u.get("kind", "match") != "match":
        return None, "map/game market (rules tested on series only)"
    if u.get("top"):
        return None, "tier 1/2 event"
    if u["px"] < MIN_PX:
        return None, f"long shot at {u['px']:.2f}"
    age = u.get("age")
    if u["phase"] == "pre":
        if u["game"] == "cs2" and age is not None and age < 14:
            return "D", None
        if u["game"] == "dota" and u["vol"] < 100000:
            return "C", None
        if u["game"] == "lol" and u["vol"] < 200000 and u["share"] >= 0.10:
            return "E", None
        return None, "pre-match money: no edge here in the test" if u["vol"] < 200000 else "big market (tier 1/2)"
    if u["game"] == "lol":
        return None, "LoL in-play money: no edge in the test"
    if u["vol"] >= 200000:
        return None, "big market (tier 1/2)"
    if u["game"] == "cs2" and age is not None and age < 3:
        return None, "CS2 account under 3 days (they lose)"
    if u["share"] >= 0.10:
        return "A", None
    if u["game"] == "cs2":
        return None, "CS2 in-play, small share of the market: no edge in low tiers"
    return "B", None


def score(u, rule):
    """0-100: the rule's strength in the test, plus a little for size and share of the market."""
    base = RULES[rule][1] if rule else 20
    return round(min(100, base + min(10, 10 * u["usd"] / 20000) + min(10, 40 * u["share"])))


def rule(u):
    return classify(u)[1]


LIVE_FRESH = 15 * 60           # an in-play signal is only copyable right after the buy
CHASE = 0.08                   # don't buy when the price has already run this far past what the wallet paid


def late(u, now):
    """Why the signal can no longer be copied (None if it still can)."""
    px = u["prices"].get(u["side"])
    if px is None or px >= 0.97 or px <= 0.03:
        return "decided"
    if u["phase"] == "pre" and u["start"] <= now:
        return "started (pre-match signal)"
    if u["phase"] == "live" and now - u["last"] > LIVE_FRESH:
        return f"in-play buy {(now - u['last']) / 60:.0f} min ago"
    paid = u["px"] if u["side"] == u["o"] else 1 - u["px"]
    if px > paid + CHASE:
        return f"price ran to {px:.2f} (was {paid:.2f})"
    return None


def why(u):
    out = [f"${u['usd']:,.0f} on {u['o']} at {u['px']:.2f} ({'in-play' if u['phase'] == 'live' else 'before kickoff'})",
           f"{u['share']:.0%} of the money on this market (match total ${u['vol']:,.0f})"]
    if u["age"] is not None:
        out.append(f"account {u['age']:.0f} days old when it bet" if u["age"] >= 1 else
                   f"account created {u['age'] * 24:.0f} h before it bet")
    return out


def signals(now_dt, cache, log=print):
    """Every wallet bet on today's CS2 / Dota 2 matches that passes the rules, best per market and side first."""
    now = now_dt.timestamp()
    evs = events(now_dt)

    def one(ge):
        game, e = ge
        mk = {m["cid"]: m for m in team_markets(e)}
        if not mk:
            return game, e, mk, []
        try:
            return game, e, mk, P.event_trades(e["id"], cap=3000, min_cash=MIN_TRADE)
        except Exception as x:
            log(f"hunter: trades failed for {e['slug']}: {type(x).__name__}")
            return game, e, mk, []

    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(one, evs))
    out, infos = [], []
    for game, e, mk, trades in got:
        start = C.iso(e["startTime"]).timestamp()
        url = f"https://polymarket.com/event/{e['slug']}"
        for m in mk.values():
            infos.append(dict(m, game=game, event=e["id"], slug=e["slug"], title=e["title"], start=start, url=url))
        evol = max([m["vol"] for m in mk.values() if m["kind"] == "match"] or [0])   # the match's size = its tier
        pos = positions(trades, mk, start)
        held = {(p["w"], p["cid"], p["o"]): p["pre"] + p["live"] for p in pos}
        for p in pos:
            m = mk[p["cid"]]
            other = next(o for o in m["outcomes"] if o != p["o"])
            for ph in ("pre", "live"):
                usd, sh = p[ph], p[ph + "_sh"]
                if usd < WALLET_MIN or not sh:
                    continue
                out.append(dict(game=game, event=e["id"], slug=e["slug"], title=e["title"], start=start, url=url,
                                cid=m["cid"], market=m["label"], kind=m["kind"], w=p["w"], name=p["name"], o=p["o"],
                                other=other, phase=ph, usd=usd, px=usd / sh, vol=evol,
                                share=usd / m["vol"] if m["vol"] else 0, last=p["last"], first=p["first"],
                                prices=m["prices"], tokens=m["tokens"], top=top_tier(game, e["title"]),
                                hedge=held.get((p["w"], p["cid"], other), 0) >= 0.3 * usd))
    for u in out:
        c = created_at(u["w"], cache, now) if u["usd"] >= WALLET_MIN else None
        u["age"] = (u["first"] - c) / DAY if c else None
        r, no = classify(u)
        u["rule"], u["side"] = r, (u["other"] if r == "D" else u["o"])
        u["score"] = score(u, r)
        u["why"] = why(u) + ([f"rule {r}: {RULE_TEXT[r]}"] if r else [])
        u["no"] = "wallet bet both teams" if u.get("hedge") else no or late(u, now)
    out.sort(key=lambda u: -u["score"])
    return out, infos


# ------------------------------------------------------------------------------------------------- account

def stake_pct(u):
    if not u.get("rule"):
        return 0.0
    return RULES[u["rule"]][0]


def balance(acct):
    bal = acct.get("start", START)
    for b in acct["bets"].values():
        if b["status"] in ("open", "won", "lost"):
            bal -= b["stake"]
        if b["status"] == "won":
            bal += b["shares"]
        if b["status"] == "void":
            pass
    return round(bal, 2)


def place(state, sigs, now, log=print, book_asks=None, fill=None):
    book_asks, fill = book_asks or C.book_asks, fill or B.fill
    acct = state.setdefault("account", dict(opened_at=now, start=START, bets={}))
    for u in sigs:
        if u["no"]:
            continue
        key = f"{u['event']}|{u['side']}"                   # one bet per match: never both teams
        if any(k.startswith(f"{u['event']}|") for k in acct["bets"]) or stake_pct(u) <= 0:
            continue
        px = u["prices"].get(u["side"])
        if px is None or not 0.05 < px < MAX_PRICE:
            continue
        bal = balance(acct)
        amount = max(1.0, round(bal * stake_pct(u), 2))
        if bal < amount:
            continue
        asks = book_asks(u["tokens"].get(u["side"]))
        sh, spent, avg, _ = fill(amount, asks) if asks else (0, 0, None, 0)
        if not sh or avg is None or avg >= MAX_PRICE:
            continue
        acct["bets"][key] = dict(game=u["game"], event=u["event"], slug=u["slug"], match=u["title"], start=u["start"],
                                 url=u["url"], cid=u["cid"], market=u["market"], side=u["side"], phase=u["phase"],
                                 score=u["score"], rule=u["rule"], their_side=u["o"], wallet=u["w"], name=u["name"], usd=round(u["usd"]),
                                 their_px=round(u["px"], 3), age=u["age"], why=u["why"], placed_at=now,
                                 status="open", stake=round(spent, 2), shares=round(sh, 4), price=round(avg, 4))
        log(f"hunter bet ({u['rule']}): ${spent:.2f} on {u['side']} ({u['title'][:60]}, {u['market']}) at {avg:.3f}, score {u['score']}")


def settle(state, infos, now, log=print):
    acct = state.get("account") or {}
    by = {i["cid"]: i for i in infos}
    for b in acct.get("bets", {}).values():
        if b["status"] != "open" or now < b["start"] + 1800:
            continue
        w = (by.get(b["cid"]) or {}).get("winner")
        if not w and now > b["start"] + 2 * 3600:
            try:
                e = C.get(f"{C.GAMMA}/events?slug={b['slug']}")[0]
                m = next((m for m in e["markets"] if m["conditionId"] == b["cid"]), None)
                w = C.winner_of(m, e.get("ended")) if m else None
            except Exception as x:
                log(f"hunter settle failed: {type(x).__name__}")
        if not w:
            continue
        if w == C.CANCELLED:
            b.update(status="void", pnl=0.0, settled_at=now, winner=None)
        else:
            b.update(winner=w, settled_at=now, status="won" if w == b["side"] else "lost",
                     pnl=round(b["shares"] - b["stake"], 2) if w == b["side"] else -b["stake"])


# ----------------------------------------------------------------------------------------------- dashboard

def short(title):
    for p in PREFIXES:
        title = title.removeprefix(p)
    return title


def view(state, sigs, now):
    acct = state.get("account") or dict(opened_at=now, start=START, bets={})
    bets = sorted(acct["bets"].values(), key=lambda b: b["placed_at"], reverse=True)
    done = [b for b in bets if b["status"] in ("won", "lost")]
    open_ = [b for b in bets if b["status"] == "open"]
    start = acct.get("start", START)
    series, bal = [(acct.get("opened_at", now), start)], start
    for b in sorted(done, key=lambda b: b["settled_at"]):
        bal += b["pnl"]
        series.append((b["settled_at"], round(bal, 2)))
    staked = sum(b["stake"] for b in done)
    pnl = sum(b["pnl"] for b in done)
    stats = dict(start=start, balance=balance(acct), equity=round(balance(acct) + sum(b["stake"] for b in open_), 2),
                 profit=round(pnl, 2), staked=round(staked, 2), roi=round(pnl / staked, 4) if staked else None,
                 wins=sum(b["status"] == "won" for b in done), losses=sum(b["status"] == "lost" for b in done),
                 won_usd=round(sum(b["pnl"] for b in done if b["pnl"] > 0), 2),
                 lost_usd=round(-sum(b["pnl"] for b in done if b["pnl"] < 0), 2),
                 open=len(open_), at_risk=round(sum(b["stake"] for b in open_), 2))
    days = {}
    for b in bets:
        d = datetime.fromtimestamp(b["placed_at"], timezone.utc).strftime("%d/%m")
        x = days.setdefault(d, dict(day=d, bets=0, won=0, lost=0, pnl=0.0))
        x["bets"] += 1
        x["won"] += b["status"] == "won"
        x["lost"] += b["status"] == "lost"
        x["pnl"] = round(x["pnl"] + (b.get("pnl") or 0), 2)
    buckets = []
    for lo, hi in ((0, 40), (40, 60), (60, 80), (80, 101)):
        g = [b for b in done if lo <= b["score"] < hi]
        st = sum(b["stake"] for b in g)
        buckets.append(dict(range=f"{lo}-{min(hi, 100)}", n=len(g), won=sum(b["status"] == "won" for b in g),
                            odds=round(sum(1 / b["price"] for b in g) / len(g), 2) if g else None,
                            roi=round(sum(b["pnl"] for b in g) / st, 4) if st else None))
    watch = []
    seen = set()
    for u in sigs:
        if u["start"] + 5 * 3600 < now or (u["event"], u["o"]) in seen or u["no"] == "decided":
            continue
        seen.add((u["event"], u["o"]))
        watch.append(dict(game=u["game"], match=short(u["title"]), market=u["market"], side=u["o"], usd=round(u["usd"]),
                          px=round(u["px"], 3), score=u["score"], phase=u["phase"], start=u["start"], url=u["url"],
                          name=u["name"], wallet=u["w"], age=u["age"], why=u["why"], no=u["no"],
                          rule=u["rule"], bet_side=u["side"],
                          bet=bool(u["rule"]) and any(b["event"] == u["event"] and b["side"] == u["side"] for b in bets)))
    out_bets = [dict({k: b.get(k) for k in ("game", "market", "side", "their_side", "rule", "phase", "score", "name", "wallet", "usd",
                                            "their_px", "age", "why", "placed_at", "start", "status", "stake",
                                            "price", "pnl", "winner", "url")}, match=short(b["match"]))
                for b in bets if b["status"] != "void"]
    return dict(stats=stats, series=series, bets=out_bets, days=sorted(days.values(), key=lambda x: x["day"][3:] + x["day"][:2], reverse=True),
                buckets=buckets, watch=watch[:60], opened_at=acct.get("opened_at"))
