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
    """Per wallet and outcome: money bought before kickoff / in-play, and shares sold back (the exit)."""
    pos = {}
    for t in trades:
        if t["conditionId"] not in mk:
            continue
        k = (t["proxyWallet"].lower(), t["conditionId"], t["outcome"])
        p = pos.setdefault(k, dict(w=k[0], cid=k[1], o=k[2], name=t["name"] or t["pseudonym"] or "", pre=0.0,
                                   pre_sh=0.0, live=0.0, live_sh=0.0, first=t["timestamp"], last=t["timestamp"],
                                   sold_sh=0.0, sold_usd=0.0, sold_at=None))
        usd = t["size"] * t["price"]
        if t["side"] == "SELL":
            p["sold_sh"] += t["size"]
            p["sold_usd"] += usd
            p["sold_at"] = max(p["sold_at"] or 0, t["timestamp"])
            continue
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

TOP_EVENTS = {   # tier 1/2 (Pedro bets tier 3 or worse only). Qualifiers of these events count as low tier.
    "dota": ("the international", "riyadh", "esports world cup", "dreamleague season", "pgl wallachia", "blast slam",
             "fissure", "esl one", "betboom dacha", "clavision", "pari universe"),
    "lol": ("worlds", "world championship", "msi", "mid-season", "first stand", "esports world cup", "lck", "lpl",
            "lec", "lcs", "lta", "lcp", "cblol", "demacia cup", "asian games", "world star challengers")}
LOW_WORDS = ("qualifier", "promotion", "academy", "challengers league", "division 2")
BIG_MATCH = 300000             # a match with more money than this is top tier whatever its name


def tier_of(game, title, vol=0):
    """'T1'..'T6' for CS2 (Valve ranking of the stronger team), 'T1-2' / 'T3+' for Dota 2 and LoL (event name)."""
    if game == "cs2":
        t = C.vtier_fix(title)
        if C.tier(title) == "top" and t not in ("T1", "T2"):
            t = "T2"
    else:
        low = title.lower()
        top = any(k in low for k in TOP_EVENTS.get(game, ())) and not any(k in low for k in LOW_WORDS)
        t = "T1-2" if top else "T3+"
    if vol >= BIG_MATCH and t not in ("T1", "T2", "T1-2"):
        t = "T1-2" if game != "cs2" else "T2"
    return t


TOP_TIERS = ("T1", "T2", "T2*", "T1-2")     # never shown or bet (Pedro: "tier 1 or tier 2 is just noise")
EXIT = 0.5                     # a wallet that sold this share of what it bought has left: we leave too


def top_tier(game, title, vol=0):
    return tier_of(game, title, vol) in TOP_TIERS


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
RULE_TEXT = {"A": "dinheiro ao vivo, ≥10% do mercado", "B": "dinheiro ao vivo em Dota",
             "C": "dinheiro pré-jogo em Dota, jogo < $100k", "D": "conta CS2 fresca pré-jogo: apostar contra",
             "E": "dinheiro pré-jogo em LoL, ≥10% do mercado"}


def classify(u):
    """(rule, why not). rule is None when no rule fires; `why not` (Portuguese, for the page) explains it."""
    if u.get("top"):
        return None, "tier 1/2"
    if u["px"] < MIN_PX:
        return None, f"underdog a {u['px']:.2f} (perdem muito)"
    age, live, share = u.get("age"), u["phase"] == "live", u["share"]
    if u["game"] == "cs2" and age is not None and age < 3 and live:
        return None, "conta CS2 com menos de 3 dias (perdem)"
    if u.get("kind", "match") != "match":                       # maps / games: only rule A held up
        if live and share >= 0.10:
            return "A", None
        return None, "mapa: só conta ao vivo com ≥10% do mercado"
    if not live:
        if u["game"] == "cs2" and age is not None and age < 14:
            return "D", None
        if u["game"] == "dota" and u["vol"] < 100000:
            return "C", None
        if u["game"] == "lol" and share >= 0.10:
            return "E", None
        return None, "pré-jogo sem vantagem no teste"
    if u["game"] == "lol":
        return None, "LoL ao vivo sem vantagem no teste"
    if share >= 0.10:
        return "A", None
    if u["game"] == "cs2":
        return None, "CS2 ao vivo com fatia pequena do mercado"
    return "B", None


def score(u, rule):
    """0-100: the rule's strength in the test, plus a little for size and share of the market."""
    base = RULES[rule][1] if rule else 20
    return round(min(100, base + min(10, 10 * u["usd"] / 20000) + min(10, 40 * u["share"])))


def rule(u):
    return classify(u)[1]


LIVE_FRESH = 15 * 60           # an in-play signal is only copyable right after the buy
CHASE = 0.08                   # don't buy when the price has already run this far past what the wallet paid
KEEP_SIGNALS = 30 * DAY


def late(u, now):
    """Why the account can no longer copy the signal (None if it still can)."""
    px = u["prices"].get(u["side"])
    if px is None or px >= 0.97 or px <= 0.03:
        return "mercado já decidido"
    if u["phase"] == "pre" and u["start"] <= now:
        return "o jogo já começou"
    if u["phase"] == "live" and now - u["last"] > LIVE_FRESH:
        return f"compra ao vivo há {(now - u['last']) / 60:.0f} min"
    paid = u["px"] if u["side"] == u["o"] else 1 - u["px"]
    if px > paid + CHASE:
        return f"preço já subiu para {px:.2f} (era {paid:.2f})"
    return None


def why(u):
    out = [f"${u['usd']:,.0f} em {u['o']} a {u['px']:.2f} ({'ao vivo' if u['phase'] == 'live' else 'antes do jogo'})",
           f"{u['share']:.0%} do dinheiro deste mercado (jogo com ${u['vol']:,.0f} no total)"]
    if u["age"] is not None:
        out.append(f"conta com {u['age']:.0f} dias quando apostou" if u["age"] >= 1 else
                   f"conta criada {u['age'] * 24:.0f} h antes de apostar")
    return out


def split_title(title):
    """Title -> (teams, event): the part before ' - ' and the part after, game prefix removed."""
    t = title
    for p in PREFIXES:
        t = t.removeprefix(p)
    match, _, event = t.partition(" - ")
    return match.strip(), event.strip()


def signals(now_dt, cache, log=print):
    """Every $1k+ wallet bet on the CS2 / Dota 2 / LoL matches from 5 h ago to 36 h ahead, classified."""
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
        evol = max([m["vol"] for m in mk.values() if m["kind"] == "match"] or [0])   # the match's size
        tier = tier_of(game, e["title"], evol)
        for m in mk.values():
            infos.append(dict(m, game=game, event=e["id"], slug=e["slug"], title=e["title"], start=start, url=url,
                              tier=tier))
        pos = positions(trades, mk, start)
        held = {(p["w"], p["cid"], p["o"]): p["pre"] + p["live"] for p in pos}
        for p in pos:
            bought = p["pre_sh"] + p["live_sh"]
            p["sold_frac"] = min(1.0, p["sold_sh"] / bought) if bought else 0.0
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
                                prices=m["prices"], tokens=m["tokens"], tier=tier, top=tier in TOP_TIERS,
                                sold_frac=p["sold_frac"], sold_usd=p["sold_usd"], sold_at=p["sold_at"],
                                hedge=held.get((p["w"], p["cid"], other), 0) >= 0.3 * usd))
    for u in out:
        c = created_at(u["w"], cache, now) if not u["top"] else None
        u["age"] = (u["first"] - c) / DAY if c else None
        r, no = classify(u)
        u["rule"], u["side"] = r, (u["other"] if r == "D" else u["o"])
        u["score"] = score(u, r)
        u["why"] = why(u) + ([f"regra {r}: {RULE_TEXT[r]}"] if r else [])
        u["no"] = "a carteira apostou nas duas equipas" if u.get("hedge") else no
        u["exited"] = u["sold_frac"] >= EXIT
        if u["sold_frac"] > 0.05:
            u["why"].append(f"vendeu {u['sold_frac']:.0%} da posição (${u['sold_usd']:,.0f})")
        if u["exited"] and not u["no"]:
            u["no"] = "a carteira já vendeu (saiu)"
        u["late"] = None if u["no"] else late(u, now)
    out.sort(key=lambda u: -u["score"])
    return out, infos


# ------------------------------------------------------------------------------------------ signal ledger

def track(state, sigs, infos, now, log=print):
    """Every suspicious bet on a tier 3+ match, kept from the moment it is first seen (with the price then) until
    its market resolves, whether the account bets it or not: the page's list and simulation."""
    led = state.setdefault("signals", {})
    for u in sigs:
        if u["top"]:
            continue
        key = f"{u['cid']}|{u['o']}|{u['phase']}"
        x = led.get(key)
        px_now = u["prices"].get(u["side"])
        if x is None:
            match, event = split_title(u["title"])
            led[key] = dict(game=u["game"], tier=u["tier"], match=match, event=event, market=u["market"],
                            kind=u["kind"], cid=u["cid"], slug=u["slug"], url=u["url"], start=u["start"],
                            phase=u["phase"], their_side=u["o"], side=u["side"], seen=now,
                            price=round(px_now, 3) if px_now is not None and 0.02 < px_now < 0.98 else None,
                            wallet=u["w"], name=u["name"], usd=round(u["usd"]), their_px=round(u["px"], 3),
                            age=u["age"], share=round(u["share"], 3), score=u["score"], rule=u["rule"],
                            no=u["no"], why=u["why"], status="open", winner=None)
        else:
            if u["usd"] > x["usd"]:                      # the wallet added more: the signal grows, its price stays
                x.update(usd=round(u["usd"]), their_px=round(u["px"], 3), share=round(u["share"], 3),
                         score=max(x["score"], u["score"]))
            x["why"] = u["why"]
        if u["exited"] and not led[key].get("exited"):
            led[key].update(exited=True, exit_at=u["sold_at"] or now)
    by = {i["cid"]: i for i in infos}
    todo = [x for x in led.values() if x["status"] == "open" and now > x["start"] + 1800]
    need = {x["cid"] for x in todo if not (by.get(x["cid"]) or {}).get("winner")}
    gamma = market_states(need) if need else {}
    for x in todo:
        w = (by.get(x["cid"]) or {}).get("winner")
        if not w and x["cid"] in gamma:
            w = C.winner_of(gamma[x["cid"]], False)
        if w == C.CANCELLED:
            x.update(status="void")
        elif w:
            x.update(status="won" if w == x["side"] else "lost", winner=w, settled_at=now)
    for k in [k for k, x in led.items() if now - x["start"] > KEEP_SIGNALS]:
        del led[k]


def market_states(cids):
    """{condition_id: Gamma market}, 20 per call."""
    out, cids = {}, list(cids)
    for i in range(0, len(cids), 20):
        q = "&".join(f"condition_ids={c}" for c in cids[i:i + 20])
        try:
            for m in C.get(f"{C.GAMMA}/markets?{q}&limit=50"):
                out[m["conditionId"]] = m
        except Exception:
            pass
    return out


# ------------------------------------------------------------------------------------------------- account

def stake_pct(u):
    return RULES[u["rule"]][0] if u.get("rule") else 0.0


def balance(acct):
    bal = acct.get("start", START)
    for b in acct["bets"].values():
        if b["status"] in ("open", "won", "lost", "sold"):
            bal -= b["stake"]
        if b["status"] == "won":
            bal += b["shares"]
        if b["status"] == "sold":                       # left with the wallet before the end
            bal += b["proceeds"]
    return round(bal, 2)


def place(state, sigs, now, log=print, book_asks=None, fill=None):
    """Bet each APOSTAR signal once per match and team at the real order book; never both teams of a match."""
    book_asks, fill = book_asks or C.book_asks, fill or B.fill
    acct = state.setdefault("account", dict(opened_at=now, start=START, bets={}))
    for u in sigs:
        if u["no"] or u.get("late") or stake_pct(u) <= 0:
            continue
        key = f"{u['event']}|{u['side']}"
        if any(k.startswith(f"{u['event']}|") for k in acct["bets"]):
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
        acct["bets"][key] = dict(game=u["game"], tier=u.get("tier"), event=u["event"], slug=u["slug"], match=u["title"],
                                 start=u["start"], url=u["url"], cid=u["cid"], market=u["market"], side=u["side"],
                                 phase=u["phase"], score=u["score"], rule=u["rule"], their_side=u["o"], wallet=u["w"],
                                 name=u["name"], usd=round(u["usd"]), their_px=round(u["px"], 3), age=u["age"],
                                 why=u["why"], placed_at=now, status="open", stake=round(spent, 2),
                                 shares=round(sh, 4), price=round(avg, 4))
        log(f"hunter bet ({u['rule']}): ${spent:.2f} on {u['side']} ({u['title'][:60]}, {u['market']}) at {avg:.3f}")


def follow(state, sigs, now, log=print, book_bids=None, sell=None):
    """The wallet behind an open bet sold out (or now backs the other team too): sell ours at the real bids."""
    book_bids, sell = book_bids or C.book_bids, sell or B.sell
    acct = state.get("account") or {}
    by = {(u["w"], u["cid"], u["o"]): u for u in sigs}
    for b in acct.get("bets", {}).values():
        if b["status"] != "open":
            continue
        u = by.get((b["wallet"], b["cid"], b["their_side"]))
        if not u or not (u["exited"] or u.get("hedge")):
            continue
        why = "a carteira vendeu" if u["exited"] else "a carteira passou a apostar na outra equipa"
        bids = book_bids(u["tokens"].get(b["side"]))
        sold, got, avg, _ = sell(b["shares"], bids) if bids else (0, 0, None, 0)
        if not sold:
            continue
        b.update(status="sold", proceeds=round(got, 2), pnl=round(got - b["stake"], 2), settled_at=now,
                 sell_price=round(avg, 4), exit_reason=why)
        log(f"hunter exit: sold {b['side']} for ${got:.2f} (paid ${b['stake']:.2f}): {why}")


def settle(state, infos, now, log=print):
    acct = state.get("account") or {}
    by = {i["cid"]: i for i in infos}
    todo = [b for b in acct.get("bets", {}).values() if b["status"] == "open" and now >= b["start"] + 1800]
    need = {b["cid"] for b in todo if not (by.get(b["cid"]) or {}).get("winner")}
    gamma = market_states(need) if need else {}
    for b in todo:
        w = (by.get(b["cid"]) or {}).get("winner")
        if not w and b["cid"] in gamma:
            w = C.winner_of(gamma[b["cid"]], False)
        if not w:
            continue
        if w == C.CANCELLED:
            b.update(status="void", pnl=0.0, settled_at=now, winner=None)
        else:
            b.update(winner=w, settled_at=now, status="won" if w == b["side"] else "lost",
                     pnl=round(b["shares"] - b["stake"], 2) if w == b["side"] else -b["stake"])


# ----------------------------------------------------------------------------------------------- dashboard

def short(title):
    return split_title(title)[0]


def view(state, sigs, now):
    acct = state.get("account") or dict(opened_at=now, start=START, bets={})
    bets = sorted(acct["bets"].values(), key=lambda b: b["placed_at"], reverse=True)
    done = [b for b in bets if b["status"] in ("won", "lost", "sold")]
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
                 wins=sum(b["pnl"] > 0 for b in done), losses=sum(b["pnl"] <= 0 for b in done),
                 open=len(open_), at_risk=round(sum(b["stake"] for b in open_), 2))
    keep = ("game", "tier", "market", "kind", "side", "their_side", "rule", "phase", "score", "name", "wallet", "usd",
            "their_px", "age", "why", "start", "status", "price", "winner", "url", "match", "event", "seen", "no",
            "share", "exited", "exit_at", "seed")
    sig_list = sorted(state.get("signals", {}).values(), key=lambda x: x["seen"], reverse=True)
    sig_out = [{k: x.get(k) for k in keep} for x in sig_list if x["status"] != "void" and x.get("tier") not in TOP_TIERS]
    bet_out = []
    for b in bets:
        if b["status"] == "void":
            continue
        match, event = split_title(b["match"])
        bet_out.append(dict({k: b.get(k) for k in ("game", "tier", "market", "side", "their_side", "rule", "phase",
                                                   "score", "name", "wallet", "usd", "why", "placed_at", "start",
                                                   "status", "stake", "price", "pnl", "winner", "url", "exit_reason",
                                                   "proceeds")},
                            match=match, event=event))
    return dict(stats=stats, series=series, bets=bet_out, signals=sig_out, opened_at=acct.get("opened_at"))
