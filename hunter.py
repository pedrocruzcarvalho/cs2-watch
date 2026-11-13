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
import book as BK
import cs2_watch as C
import polydata as P

GAMES = {"cs2": 10310, "dota": 10309, "lol": 10311}
START = 63.47
MIN_TRADE = 50                 # read every buy from $50, so a bet split into small pieces still adds up
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
#   F  a sharp wallet (book.py: 10+ resolved tier 3+ matches, +20 pp over its prices) betting in-play on CS2 / Dota 2,
#      from $200, series or maps                                            n=339  +11%  (halves +14% / +9%)
#      (re-tested Nov 13 with REAL P&L per match, the way Pedro counts a win: only +3.9%, so it bets 1%)
#   G  one wallet's in-play money on one team, all of the match's markets added up, $2k+, CS2 / Dota 2
#                                                    CS2 n=303 +7.2% (halves +4% / +10%); Dota n=305 +3.9% (+4% / +4%)
RULES = {"A": (0.05, 80), "B": (0.02, 50), "C": (0.04, 70), "D": (0.03, 65), "E": (0.02, 60), "F": (0.01, 60),
         "G": (0.02, 60), "H": (0.0, 55)}   # H: flag + push only, no paper bet (untested, Pedro Nov 13)
H_MIN = {"T3": 3300, "T4": 2200, "T5": 1500, "T6": 1000}   # rule H: one wallet's pre-match money on a CS2 team
REL_MIN, REL_STRONG = 0.20, 0.50   # one wallet's in-play money on a team vs the tier's median match money
# (Nov 13 2026, Pedro: "big" depends on the tier) 20%+: +4.9% over 488 (halves +4/+6); 50%+: +11.9% over 163 (+7/+17);
# the fixed $2k it replaced: +3.1% over 608. In dollars, 20% = CS2 T3 ~$4k, T4 ~$2k, T5 ~$1.1k, T6 ~$570, Dota ~$4.7k.


def team_rel(u):
    m = TIER_MEDIAN.get(f"{u['game']}|{u.get('tier')}") if "TIER_MEDIAN" in globals() else None
    return u.get("team_live", 0) / m if m else None
RULE_TEXT = {"A": "wallet with a huge live bet for the tier (≥50% of a normal match)", "B": "live money in Dota",
             "C": "pre-match money in Dota, match < $100k", "D": "fresh CS2 account pre-match: bet against it",
             "E": "pre-match money in LoL, ≥10% of the market",
             "F": "wallet with a winning record, betting live",
             "G": "wallet with a big live bet for the tier (≥20% of a normal match)",
             "H": "CS2 pre-match: one wallet put big money for the tier (T3 $3.3k, T4 $2.2k, T5 $1.5k, T6 $1k) on one team (flag only, no bet)"}


def classify(u):
    """(rule, why not). rule is None when no rule fires; `why not` (Portuguese, for the page) explains it."""
    if u.get("top"):
        return None, "tier 1/2"
    if u["px"] < MIN_PX:
        return None, f"underdog a {u['px']:.2f} (perdem muito)"
    age, live, share = u.get("age"), u["phase"] == "live", u["share"]
    if u["game"] == "cs2" and age is not None and age < 3 and live:
        return None, "CS2 account under 3 days old (they lose)"
    if u.get("sharp") and live and u["game"] != "lol":
        return "F", None
    big = team_rel(u)                                           # this wallet's in-play money on this team, all markets,
    if live and u["game"] != "lol" and big is not None:         # against the tier's normal match money
        if big >= REL_STRONG:
            return "A", None
        if big >= REL_MIN:
            return "G", None
    need = H_MIN.get(u.get("tier"))                      # Pedro's thresholds per CS2 tier (Nov 13 2026), all markets summed
    if not live and u["game"] == "cs2" and need and u.get("team_pre", 0) >= need and not (age is not None and age < 14):
        return "H", None
    if u["usd"] < WALLET_MIN:                                   # small bets: only sharp / listed / big-for-tier count
        return None, ("sharp wallet, but only counts live in CS2/Dota" if u.get("sharp") else
                      "listed wallet, but its low-tier record does not win" if u.get("listed") else
                      "small bet")
    if u.get("kind", "match") != "match":
        return None, "map market: only counts live with a big bet for the tier"
    if not live:
        if u["game"] == "cs2" and age is not None and age < 14:
            return "D", None
        if u["game"] == "dota" and u["vol"] < 100000:
            return "C", None
        if u["game"] == "lol" and share >= 0.10:
            return "E", None
        return None, "pre-match: no edge in the backtest"
    if u["game"] == "lol":
        return None, "LoL live: no edge in the backtest"
    if u["game"] == "cs2":
        return None, "CS2 live: small bet for the tier"
    return "B", None


def score(u, rule):
    """0-100: the rule's strength in the test, plus a little for size and share of the market."""
    base = RULES[rule][1] if rule else 20
    return round(min(100, base + min(10, 10 * u["usd"] / 20000) + min(10, 40 * u["share"])))


def rule(u):
    return classify(u)[1]


# median money on one match, by tier (60 days to Nov 13 2026, hunt_collect data): what "a lot" means in each tier.
# A bet's size against this did NOT predict winners on its own (tested), so it is shown, not used as a trigger.
TIER_MEDIAN = {"cs2|T1": 577731, "cs2|T2": 87624, "cs2|T3": 20305, "cs2|T2*": 36553, "cs2|T4": 10137, "cs2|T5": 5590, "cs2|T6": 2863, "dota|T3+": 23273, "dota|T1-2": 378971, "lol|T3+": 73674, "lol|T1-2": 674372}


def rel_size(u):
    m = TIER_MEDIAN.get(f"{u['game']}|{u.get('tier')}")
    return u["usd"] / m if m else None


LIVE_FRESH = 15 * 60           # an in-play signal is only copyable right after the buy
CHASE = 0.08                   # don't buy when the price has already run this far past what the wallet paid
KEEP_SIGNALS = 30 * DAY


def late(u, now):
    """Why the account can no longer copy the signal (None if it still can)."""
    px = u["prices"].get(u["side"])
    if px is None or px >= 0.97 or px <= 0.03:
        return "market already decided"
    if u["phase"] == "pre" and u["start"] <= now:
        return "the match already started"
    if u["phase"] == "live" and now - u["last"] > LIVE_FRESH:
        return f"live buy from {(now - u['last']) / 60:.0f} min ago"
    paid = u["px"] if u["side"] == u["o"] else 1 - u["px"]
    if px > paid + CHASE:
        return f"price already up to {px:.2f} (was {paid:.2f})"
    return None


def why(u):
    out = [f"${u['usd']:,.0f} on {u['o']} at {u['px']:.2f} ({'live' if u['phase'] == 'live' else 'pre-match'})",
           f"{u['share']:.0%} of this market's money (match total ${u['vol']:,.0f})"]
    r = rel_size(u)
    if r is not None:
        m = TIER_MEDIAN[f"{u['game']}|{u['tier']}"]
        out.append(f"{r:.0%} of a normal {u['tier'].replace('T', 'tier ')} match's money (median ${m:,.0f})")
    if u["age"] is not None:
        out.append(f"account {u['age']:.0f} days old when it bet" if u["age"] >= 1 else
                   f"account created {u['age'] * 24:.0f} h before betting")
    return out


def split_title(title):
    """Title -> (teams, event): the part before ' - ' and the part after, game prefix removed."""
    t = title
    for p in PREFIXES:
        t = t.removeprefix(p)
    match, _, event = t.partition(" - ")
    return match.strip(), event.strip()


def combos(game, e, mk, pos, singles, start, url, evol, tier):
    """One wallet's bets on the same team across this match's markets (series, maps; before and in-play), added up.
    Total >= WALLET_MIN with no single piece that big: one 'several bets' signal on the team's series market.
    Where a single piece is already a signal, its analysis gets the match total instead."""
    groups = {}
    for p in pos:
        if p["pre"] + p["live"] <= 0:
            continue
        groups.setdefault((p["w"], p["o"]), []).append(p)
    total_on = {k: sum(p["pre"] + p["live"] for p in g) for k, g in groups.items()}
    out = []
    for (w, team), g in groups.items():
        total = total_on[(w, team)]
        pieces = sum((p["pre"] > 0) + (p["live"] > 0) for p in g)
        if total < WALLET_MIN or pieces < 2:
            continue
        mine = [u for u in singles if u["event"] == e["id"] and u["w"] == w and u["o"] == team]
        if mine:
            for u in mine:
                u.setdefault("extra", []).append(f"${total:,.0f} on {team} in this match in total, over {pieces} bets")
            continue
        m = next((m for m in mk.values() if m["kind"] == "match" and team in m["outcomes"]), None) or             mk[max(g, key=lambda p: p["pre"] + p["live"])["cid"]]
        if team not in m["outcomes"]:
            continue
        other = next(o for o in m["outcomes"] if o != team)
        sh = sum(p["pre_sh"] + p["live_sh"] for p in g)
        bought = sh or 1
        sold = sum(p["sold_sh"] for p in g)
        live = any(p["live"] > 0 for p in g)
        parts = ", ".join(f"${p['pre'] + p['live']:,.0f} em {mk[p['cid']]['label']}" for p in
                          sorted(g, key=lambda p: -(p["pre"] + p["live"])))
        out.append(dict(game=game, event=e["id"], slug=e["slug"], title=e["title"], start=start, url=url,
                        cid=m["cid"], market=f"Several bets ({pieces})", kind=m["kind"], w=w, name=g[0]["name"],
                        o=team, other=other, phase="live" if live else "pre", usd=total, px=total / sh if sh else 0.5,
                        vol=evol, share=total / max(m["vol"], 1), last=max(p["last"] for p in g),
                        first=min(p["first"] for p in g), prices=m["prices"], tokens=m["tokens"], tier=tier,
                        top=tier in TOP_TIERS, sold_frac=min(1.0, sold / bought),
                        sold_usd=sum(p["sold_usd"] for p in g), sold_at=max((p["sold_at"] or 0) for p in g) or None,
                        hedge=total_on.get((w, other), 0) >= 0.3 * total, combo=True,
                        extra=[f"soma de {pieces} apostas: {parts}"]))
    return out


def signals(now_dt, cache, log=print, book=None, listed=None, names=None, details=None):
    """Every $1k+ wallet bet on the CS2 / Dota 2 / LoL matches from 5 h ago to 36 h ahead (from $200 for sharp or
    hand-picked wallets), classified. Also writes each wallet's biggest bet per tier 3+ match into the book."""
    now = now_dt.timestamp()
    book = {} if book is None else book
    listed = listed or {}
    evs = events(now_dt)

    def one(ge):
        game, e = ge
        mk = {m["cid"]: m for m in team_markets(e)}
        if not mk:
            return game, e, mk, []
        try:
            return game, e, mk, P.event_trades(e["id"], cap=6000, min_cash=MIN_TRADE)
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
                if not sh or usd < BK.MIN_USD:
                    continue
                is_sharp = tier not in TOP_TIERS and BK.sharp(book, p["w"], now, skip_event=e["id"])
                med = TIER_MEDIAN.get(f"{game}|{tier}")
                live_team = sum(q["live"] for q in pos if q["w"] == p["w"] and q["o"] == p["o"])
                big_for_tier = med and live_team >= REL_MIN * med
                if usd < WALLET_MIN and not is_sharp and p["w"] not in listed and not big_for_tier:
                    continue
                out.append(dict(game=game, event=e["id"], slug=e["slug"], title=e["title"], start=start, url=url,
                                cid=m["cid"], market=m["label"], kind=m["kind"], w=p["w"], name=p["name"], o=p["o"],
                                other=other, phase=ph, usd=usd, px=usd / sh, vol=evol,
                                share=usd / m["vol"] if m["vol"] else 0, last=p["last"], first=p["first"],
                                prices=m["prices"], tokens=m["tokens"], tier=tier, top=tier in TOP_TIERS,
                                sold_frac=p["sold_frac"], sold_usd=p["sold_usd"], sold_at=p["sold_at"],
                                hedge=held.get((p["w"], p["cid"], other), 0) >= 0.3 * usd,
                                sharp=is_sharp, rec=BK.record(book, p["w"], now, e["id"]) if (is_sharp or p["w"] in listed) else None,
                                listed=(listed.get(p["w"]) or {}).get("note")))
        agg = {}                                        # the book: each wallet's real money on this match
        for p in pos:
            x = agg.setdefault(p["w"], dict(h={}, spent=0.0, sold=0.0, name=p["name"]))
            x["spent"] += p["pre"] + p["live"]
            x["sold"] += p["sold_usd"]
            left = p["pre_sh"] + p["live_sh"] - p["sold_sh"]
            if left > 0.01:
                x["h"][BK.key(p["cid"], p["o"])] = round(left, 2)
        for w, x in agg.items():
            BK.note(book, w, e["id"], start, tier in TOP_TIERS, x["h"], x["spent"], x["sold"])
            if names is not None and x["name"] and not x["name"].startswith("0x") and w in book:
                names[w] = x["name"]
        out += combos(game, e, mk, pos, out, start, url, evol, tier)
        team_live, team_pre = {}, {}
        for p in pos:
            team_live[(p["w"], p["o"])] = team_live.get((p["w"], p["o"]), 0) + p["live"]
            team_pre[(p["w"], p["o"])] = team_pre.get((p["w"], p["o"]), 0) + p["pre"]
        for u in out:
            if u["event"] == e["id"]:
                u["team_live"] = team_live.get((u["w"], u["o"]), 0)
                u["team_pre"] = team_pre.get((u["w"], u["o"]), 0)
        if details is not None and tier not in TOP_TIERS and trades:
            d = detail(e, mk, trades, start, now, listed, book)
            if d:
                details[e["id"]] = d
    for u in out:
        c = created_at(u["w"], cache, now) if not u["top"] else None
        u["age"] = (u["first"] - c) / DAY if c else None
        r, no = classify(u)
        u["rule"], u["side"] = r, (u["other"] if r == "D" else u["o"])
        u["score"] = score(u, r)
        u["why"] = why(u) + u.get("extra", []) + ([f"rule {r}: {RULE_TEXT[r]}"] if r else [])
        if u.get("rec") and u["rec"][0]:
            n, wins, rate, roi = u["rec"]
            u["why"].insert(1, f"tier 3+ record: made money in {wins} of {n} matches, real profit {roi * 100:+.0f}%")
        if u.get("listed"):
            u["why"].insert(1, f"from your list: {u['listed'][:80]}")
        u["no"] = "the wallet bet on both teams" if u.get("hedge") else no
        u["exited"] = u["sold_frac"] >= EXIT
        if u["sold_frac"] > 0.05:
            u["why"].append(f"sold {u['sold_frac']:.0%} of the position (${u['sold_usd']:,.0f})")
        if u["exited"] and not u["no"]:
            u["no"] = "the wallet already sold (exited)"
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
        key = f"{u['cid']}|{u['o']}|{u['phase']}" + ("|combo" if u.get("combo") else "")
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
        why = "the wallet sold" if u["exited"] else "the wallet switched to the other team"
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


def view(state, sigs, now, infos=(), details=None):
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
    return dict(stats=stats, series=series, bets=bet_out, upcoming=upcoming(sigs, bets, now, infos, details, state.get("signals")),
                opened_at=acct.get("opened_at"))


def upcoming(sigs, bets, now, infos=(), details=None, ledger=None):
    """The page's main list: one card per tier 3+ match still to play or being played, with one plain
    recommendation (bet X at these odds, or don't, and why). Matches without suspicious money yet are listed too."""
    games, meta = {}, {}
    for i in infos:
        if i.get("tier") in TOP_TIERS or now > i["start"] + 5 * 3600 or i.get("kind") != "match":
            continue
        px = list((i.get("prices") or {}).values())
        if i.get("winner") or i.get("closed") or (px and max(px) >= 0.97):
            continue                                        # over, or decided
        meta[i["event"]] = i
        games.setdefault(i["event"], [])
    for u in sigs:
        if not u["top"] and u["event"] in games:
            games[u["event"]].append(u)
    out = []
    for ev, us in games.items():
        if not us:
            i = meta[ev]
            match, event = split_title(i["title"])
            out.append(dict(game=i["game"], tier=i["tier"], match=match, event=event, start=i["start"],
                            live=now >= i["start"], url=i["url"], rec=None, no="no suspicious bets yet",
                            bet=None, items=[], money=0, quiet=True, score=0, detail=(details or {}).get(ev), alert=None,
                            event_id=ev))
            continue
        u0 = us[0]
        match, event = split_title(u0["title"])
        mine = next((b for b in bets if b["event"] == ev), None)
        go = sorted((u for u in us if u["rule"] and not u["no"] and not u.get("late")), key=lambda u: -u["score"])
        rec = None
        if go:
            g = go[0]
            px = g["prices"].get(g["side"])
            first = (ledger or {}).get(f"{g['cid']}|{g['o']}|{g['phase']}" + ("|combo" if g.get("combo") else "")) or {}
            a_px = first.get("price") or px
            rec = dict(side=g["side"], other=g["other"] if g["side"] == g["o"] else g["o"], odds=round(1 / a_px, 2) if a_px else None,
                       now=round(1 / px, 2) if px else None, implied=round(a_px, 2) if a_px else None, at=first.get("seen", now),
                       market=g["market"], kind=g["kind"], rule=g["rule"], why=RULE_TEXT[g["rule"]], fade=g["side"] != g["o"],
                       listed=bool(g.get("listed")), sharp=bool(g.get("sharp")),
                       fresh=g["age"] is not None and g["age"] < 14)
            no = None
        else:
            late = next((u for u in us if u["rule"] and not u["no"] and u.get("late")), None)
            no = f"too late to enter ({late['late']})" if late else                 (max(us, key=lambda u: u["usd"])["no"] or "no edge in the backtest")
        flags = []                                          # every signal that fired on this match (Pedro: show everything)
        for u in sorted((u for u in us if u["rule"] and not u["no"]), key=lambda u: -u["score"]):
            f0 = (ledger or {}).get(f"{u['cid']}|{u['o']}|{u['phase']}" + ("|combo" if u.get("combo") else "")) or {}
            fp = f0.get("price") or u["prices"].get(u["side"])
            flags.append(dict(side=u["side"], market=u["market"], kind=u["kind"], rule=u["rule"], why=RULE_TEXT[u["rule"]],
                              odds=round(1 / fp, 2) if fp else None, at=f0.get("seen", u["first"]), live=u["phase"] == "live",
                              late=u.get("late"), score=u["score"], bank=RULES[u["rule"]][0] > 0))
        items = []
        for u in sorted(us, key=lambda u: -u["usd"]):
            items.append(dict(name=u["name"] if u["name"] and not u["name"].startswith("0x") else u["w"][:8],
                              wallet=u["w"], usd=round(u["usd"]), side=u["o"], market=u["market"],
                              live=u["phase"] == "live", px=round(u["px"], 2), age=u["age"],
                              share=round(u["share"], 3), sold=round(u["sold_frac"], 2), good=bool(u["rule"] and not u["no"]),
                              sharp=bool(u.get("sharp")), listed=bool(u.get("listed")), rel=rel_size(u),
                              rec=list(u["rec"][:3]) if u.get("rec") else None))
        out.append(dict(game=u0["game"], tier=u0["tier"], match=match, event=event, start=u0["start"],
                        live=now >= u0["start"], url=u0["url"], rec=rec, no=no,
                        bet=dict(side=mine["side"], stake=mine["stake"], price=mine["price"], status=mine["status"])
                        if mine else None, items=items[:8], money=round(sum(u["usd"] for u in us)), quiet=False,
                        score=max(u["score"] for u in us), detail=(details or {}).get(ev),
                        alert=min(u["first"] if u["phase"] == "pre" else max(u["first"], u["start"]) for u in us),
                        event_id=ev, flags=flags[:8]))
    return sorted(out, key=lambda m: (not m["rec"] and not m["bet"], m["quiet"], m["start"]))


# --------------------------------------------------------------------------------------------- match detail

BUCKETS = 12


def detail(e, mk, trades, start, now, listed=None, book=None):
    """What the page shows for one match: where the money is going (before kickoff: the last 12 h; in-play: since
    the start), a timeline of money per team with the first team's odds, the last 30 minutes, how concentrated the
    money is, wallets that switched sides, the top wallets and the biggest single bets."""
    listed, book = listed or {}, book or {}
    match_m = next((m for m in mk.values() if m["kind"] == "match"), None) or next(iter(mk.values()))
    teams = match_m["outcomes"]
    if len(teams) != 2:
        return None
    a, b = teams
    live = now >= start
    lo = start if live else max(start - 12 * 3600, min((t["timestamp"] for t in trades), default=now))
    hi = now
    if hi - lo < 600:
        lo = hi - 600
    buys = [t for t in trades if t["side"] == "BUY" and t["conditionId"] in mk and t["outcome"] in teams]
    win = [t for t in buys if lo <= t["timestamp"] <= hi]
    if not win:
        return None
    step = (hi - lo) / BUCKETS
    bars = [[0.0, 0.0, None] for _ in range(BUCKETS)]
    price_a = None
    for t in sorted(win, key=lambda t: t["timestamp"]):
        i = min(BUCKETS - 1, int((t["timestamp"] - lo) / step))
        usd = t["size"] * t["price"]
        bars[i][0 if t["outcome"] == a else 1] += usd
        if t["conditionId"] == match_m["cid"]:
            price_a = t["price"] if t["outcome"] == a else 1 - t["price"]
            bars[i][2] = round(price_a, 3)
    last = None
    for bar in bars:                                    # carry the odds line through buckets without a trade
        if bar[2] is None:
            bar[2] = last
        last = bar[2]
    first_px = next((x[2] for x in bars if x[2] is not None), None)
    now_px = match_m["prices"].get(a)
    tot = {a: 0.0, b: 0.0}
    wallets = {}
    for t in win:
        usd = t["size"] * t["price"]
        tot[t["outcome"]] += usd
        w = t["proxyWallet"].lower()
        x = wallets.setdefault((w, t["outcome"]), dict(wallet=w, name=t["name"] or t["pseudonym"] or w[:8], side=t["outcome"],
                                                        usd=0.0, n=0, first=t["timestamp"]))
        x["usd"] += usd
        x["n"] += 1
        x["first"] = min(x["first"], t["timestamp"])
    recent = [t for t in win if t["timestamp"] >= now - 1800]
    r = {a: sum(t["size"] * t["price"] for t in recent if t["outcome"] == a),
         b: sum(t["size"] * t["price"] for t in recent if t["outcome"] == b)}
    going = a if r[a] >= r[b] else b
    rsum = r[a] + r[b]
    total = tot[a] + tot[b]
    top = sorted(wallets.values(), key=lambda x: -x["usd"])
    per_wallet = {}
    for x in top:
        per_wallet[x["wallet"]] = per_wallet.get(x["wallet"], 0) + x["usd"]
    conc = max(per_wallet.values()) / total if total else 0
    switched = []
    if live:
        pre = {}
        for t in buys:
            if t["timestamp"] < start:
                k = (t["proxyWallet"].lower(), t["outcome"])
                pre[k] = pre.get(k, 0) + t["size"] * t["price"]
        for x in top:
            other = b if x["side"] == a else a
            if x["usd"] >= 100 and pre.get((x["wallet"], other), 0) >= 100:
                switched.append(dict(name=x["name"], wallet=x["wallet"], pre_side=other, pre_usd=round(pre[(x["wallet"], other)]),
                                     side=x["side"], usd=round(x["usd"])))
    pre_side = {}
    for t in buys:
        if t["timestamp"] < start:
            pre_side.setdefault(t["proxyWallet"].lower(), t["outcome"])

    def tag(w):
        return dict(listed=w in listed, sharp=bool(book) and BK.sharp(book, w, now))

    def minute(t):
        return round((t - start) / 60)

    biggest = sorted(win, key=lambda t: -t["size"] * t["price"])[:6]
    return dict(
        live=live, teams=teams, since=lo, mins=minute(now) if live else None,
        totals={a: round(tot[a]), b: round(tot[b])}, trades=len(win), wallets=len(per_wallet),
        bars=[[round(x[0]), round(x[1]), x[2]] for x in bars], odds_from=first_px, odds_now=now_px,
        recent=dict(side=going, usd=round(r[going]), net=round((r[going] - (rsum - r[going])) / rsum, 2) if rsum else 0),
        conc=round(conc, 2), switched=switched[:6],
        top=[dict(name=x["name"], wallet=x["wallet"], side=x["side"], usd=round(x["usd"]), n=x["n"], at=minute(x["first"]),
                  pre=pre_side.get(x["wallet"]) if live and pre_side.get(x["wallet"]) else None, **tag(x["wallet"]))
             for x in top[:6]],
        big=[dict(at=minute(t["timestamp"]), name=t["name"] or t["pseudonym"] or t["proxyWallet"][:8],
                  wallet=t["proxyWallet"].lower(), side=t["outcome"], usd=round(t["size"] * t["price"]),
                  px=round(t["price"], 2), market=mk[t["conditionId"]]["label"], **tag(t["proxyWallet"].lower()))
             for t in biggest])
