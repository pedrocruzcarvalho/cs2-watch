"""
Tennis watch: the pre-match radar and the radar account for tennis (ITF, ATP incl. Challengers, WTA on Polymarket).

Separate from CS2 at Pedro's request (Oct 21 2026). Same idea as the CS2 radar: every upcoming match where someone
put unusual money before kickoff, plus wallets on the hand-picked list, and a $300 paper account that bets the side
with the most radar money (radar_account.py).

What "big" means here (tennis_research.py, 30 days, 6,132 matches): an ITF match's pre-match money is $17 at the
median and only 1 in 20 sees a single position over ~$550; Challenger-level matches ~$4k at that mark; tour matches
~$20-25k. Floors by level (ITF $1,000, small ATP/WTA $6k, tour $30k: about 1-3 matches in 100), then:
  HIGH    10x the floor, or 4x with half of the match's pre-match money
  MEDIUM  4x the floor, or 2x with 40% of the match's pre-match money
  LOW     the floor
Level from the event name: tour events are named "... Open / Masters / Championships / Cup ...", Challengers and
WTA 125s just by city. Research found no edge in following big tennis bets (pre-match -2..-12%); this is the
radar Pedro asked for, shown live.
"""

import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import cs2_watch as C

SERIES = {"itf": 11634, "atp": 10365, "wta": 10366}
BIG = re.compile(r"open|masters|championship|classic|cup|tennis|slam|finals|wimbledon|roland|international|grand prix", re.I)
FLOOR = {"ITF": 1000, "Challenger": 6000, "Tour": 30000}     # raised Oct 21 2026 (Pedro): bigger money only
MIN_VOLUME = 100                                      # skip matches with almost nothing traded (most ITF)


def level_of(tour, title):
    if tour == "itf":
        return "ITF"
    return "Tour" if BIG.search(title.split(":")[0]) else "Challenger"


def flag(usd, share, lvl):
    f = FLOOR[lvl]
    if usd >= 10 * f or (usd >= 4 * f and share >= 0.5):
        return "HIGH"
    if usd >= 4 * f or (usd >= 2 * f and share >= 0.4):
        return "MEDIUM"
    return "LOW" if usd >= f else ""


def events(now):
    """Matches starting from 6 h ago to 30 h ahead. Tennis events end 7 days after they start (ITF at least), so
    both end-date windows are read and the start time decides."""
    lo, hi = now - timedelta(hours=6), now + timedelta(hours=30)
    out, seen = [], set()
    for tour, sid in SERIES.items():
        for shift in (timedelta(0), timedelta(days=7)):
            off = 0
            while off <= 900:
                page = C.get(f"{C.GAMMA}/events?series_id={sid}&end_date_min={lo + shift:%Y-%m-%dT%H:%M:%SZ}"
                             f"&end_date_max={hi + shift:%Y-%m-%dT%H:%M:%SZ}&limit=100&offset={off}")
                for e in page:
                    st = C.iso(e.get("startTime") or e.get("startDate"))
                    if e["id"] not in seen and e.get("markets") and st and lo <= st <= hi:
                        seen.add(e["id"])
                        out.append((e, tour))
                if len(page) < 100:
                    break
                off += 100
    return out


def winner_market(e):
    for m in e["markets"]:
        outs = json.loads(m.get("outcomes") or "[]")
        if len(outs) == 2 and not set(outs) & {"Yes", "No", "Over", "Under"}:
            return m, outs
    return None, None


def scan(now, watch, log=print):
    """Returns (infos, rows): one info per match (winner market), and every pre-match position of $100+ that is
    flagged or belongs to a hand-picked wallet."""
    ts = now.timestamp()
    evs = [(e, t) for e, t in events(now) if float(e.get("volume") or 0) >= MIN_VOLUME]

    def one(et):
        e, tour = et
        m, outs = winner_market(e)
        if not m:
            return None
        start = C.iso(e.get("startTime") or e.get("startDate"))
        if not start:
            return None
        start = start.timestamp()
        trades = [t for t in C.poly_trades(e["id"]) if t["conditionId"] == m["conditionId"]
                  and (1 - C.SWEEP) < t["price"] < C.SWEEP]
        px = {}
        for t in sorted(trades, key=lambda t: t["timestamp"]):
            px[t["outcome"]] = t["price"]
            px[next(o for o in outs if o != t["outcome"])] = 1 - t["price"]
        if not px:
            try:
                px = dict(zip(outs, (float(x) for x in json.loads(m.get("outcomePrices") or "[]"))))
            except Exception:
                px = {}
        lvl = level_of(tour, e["title"])
        info = dict(venue="poly", sport="tennis", match=e["title"], start=start, market="Match Winner", teams=outs,
                    prices=px, winner=C.winner_of(m, e.get("ended")), cid=m["conditionId"], ref=e["slug"],
                    url=f"https://polymarket.com/event/{e['slug']}", tokens=dict(zip(outs, json.loads(m.get("clobTokenIds") or "[]"))),
                    level=lvl, tour=tour, volume=float(e.get("volume") or 0))
        live = defaultdict(lambda: [0.0, 0.0, 0.0, 0])                 # in-play: shares, usd bought, usd sold, last
        for t in sorted(trades, key=lambda t: t["timestamp"]):
            if t["timestamp"] < start:
                continue
            q = live[(t["proxyWallet"].lower(), t["outcome"])]
            if t["side"] == "BUY":
                q[0] += t["size"]
                q[1] += t["size"] * t["price"]
            else:
                q[2] += t["size"] * t["price"]
            q[3] = t["timestamp"]
        info["livepos"] = [[w, m["conditionId"], o, round(ub - us, 2), round(ub / sb, 4) if sb else 0,
                            next((t.get("name") or t.get("pseudonym") or "" for t in trades if t["proxyWallet"].lower() == w), ""), last]
                           for (w, o), (sb, ub, us, last) in live.items() if ub - us >= 1000]
        pre = [t for t in trades if t["timestamp"] < start]
        flow = sum(t["size"] * t["price"] for t in pre)
        pos, names = defaultdict(lambda: [0.0, 0.0, 0.0]), {}
        for t in pre:
            k = (t["proxyWallet"].lower(), t["outcome"])
            p = pos[k]
            if t["side"] == "BUY":
                p[0] += t["size"]
                p[1] += t["size"] * t["price"]
            else:
                p[2] += t["size"] * t["price"]
            names[k[0]] = t.get("name") or t.get("pseudonym") or ""
        rows = []
        for (w, o), (sb, ub, us) in pos.items():
            net = ub - us
            if net < 100 or not sb:
                continue
            share = net / flow if flow else 0
            lv = flag(net, share, lvl)
            if not lv and w not in watch:
                continue
            why = [f"${net:,.0f} position", f"{share:.0%} of the ${flow:,.0f} traded on this match before kickoff",
                   f"{lvl} match: a big bet here starts at ${FLOOR[lvl]:,}"]
            rows.append(dict(venue="poly", match=e["title"], market="Match Winner", outcome=o, wallet=w, name=names[w],
                             usd=net, avg_price=ub / sb, level=lv, start=start, url=info["url"], tier=lvl, why=why,
                             share=share))
        return info, rows

    infos, rows = [], []
    with ThreadPoolExecutor(8) as ex:
        for r in ex.map(lambda et: _safe(one, et, log), evs):
            if r:
                infos.append(r[0])
                rows += r[1]
    log(f"tennis: {len(evs)} matches with ${MIN_VOLUME}+ traded, {len(rows)} radar positions")
    return infos, rows


def _safe(fn, arg, log):
    try:
        return fn(arg)
    except Exception as e:
        log(f"  tennis {arg[0].get('id')}: {e}")
        return None


def radar(rows, watch, now, limit=40):
    groups = {}
    for r in rows:
        if r["start"] <= now:
            continue
        tags = []
        if r.get("level"):
            tags.append(dict(kind="big", label=f"big bet ({r['level']})"))
        v = watch.get(r["wallet"])
        if v and v.get("source") == "manual" and r["usd"] >= 900:
            tags.append(dict(kind="watched", label="your list: " + (v.get("reason") or "")[:70]))
        if not tags:
            continue
        g = groups.setdefault(r["match"], dict(match=r["match"], market="Match Winner", start=r["start"], url=r["url"],
                                               vtier=r["tier"], items=[], score=0))
        g["items"].append(dict(wallet=r["wallet"], name=r.get("name"), outcome=r["outcome"], usd=round(r["usd"]),
                               avg=round(r["avg_price"], 3), tags=tags, why=r["why"],
                               who=f"{r['share']:.0%} of this match's pre-match money"))
        g["score"] += sum({"big": 1 + {"HIGH": 3, "MEDIUM": 2, "LOW": 1}.get(r.get("level"), 0), "watched": 8}[t["kind"]]
                          for t in tags)
    out = sorted(groups.values(), key=lambda g: (-g["score"], g["start"]))[:limit]
    for g in out:
        g["items"] = sorted(g["items"], key=lambda x: -x["usd"])[:8]
    return out


# ------------------------------------------------------------------------------------------------ tennis live
# ITF in-play, one wallet $2k+ on the UNDERDOG (price under 0.45): tennis_research.py, 30 days, 56 bets, ROI +46%,
# +45% / +50% in the two halves, while backing the favourite at those moments lost 28%. Small ATP/WTA underdogs
# did the opposite (-40..-75%), so ITF only. Rules fixed on 2026-10-24 before any live result; the stake is 3% of
# the balance (sizing.py). Found after looking at many combinations: promising, not proven.
LIVE_MIN, LIVE_MAX_PRICE, LIVE_OTHER = 2000, 0.45, 1000


def live_signals(infos, now):
    out = []
    for i in infos:
        if i.get("level") != "ITF" or not i["start"] <= now < i["start"] + 5 * 3600 or i.get("winner"):
            continue
        pos = i.get("livepos") or []
        if not pos:
            continue
        top = max(pos, key=lambda x: x[3])
        side = top[2]
        px = (i.get("prices") or {}).get(side)
        against = [x for x in pos if x[2] != side and x[3] >= LIVE_OTHER]
        other = sum(x[3] for x in against)
        reasons = []
        if top[3] < LIVE_MIN:
            reasons.append(f"biggest in-play position is ${top[3]:,.0f} (needs ${LIVE_MIN:,})")
        if px is None or px >= LIVE_MAX_PRICE:
            reasons.append(f"{side} is not the underdog (price {px if px is not None else '?'}; needs under {LIVE_MAX_PRICE})")
        if other >= 0.5 * top[3]:
            reasons.append(f"${other:,.0f} of big money on the other side")
        card = lambda x: dict(wallet=x[0], name=x[5], usd=x[3], avg=x[4], at=x[6])
        out.append(dict(match=i["match"], start=i["start"], market="Match Winner", cid=i["cid"], slug=i["ref"], url=i["url"],
                        teams=i["teams"], prices=i.get("prices") or {}, tokens=i.get("tokens") or {}, side=side,
                        money=round(top[3]), other_money=round(other), wallets=[card(x) for x in sorted(
                            (x for x in pos if x[2] == side), key=lambda x: -x[3])][:6],
                        against=[card(x) for x in against][:6], vtier="ITF", live=True, active=not reasons, why_not=reasons))
    return sorted(out, key=lambda s: (not s["active"], -s["money"]))


def live_explain(b, now):
    import informed as I
    other = next((t for t in b.get("teams") or [] if t != b["side"]), "the other player")
    head = f"{I.money(b['stake'])} on {b['side']} to beat {other}, copying {I.money(b['money'])} bought on the underdog during an ITF match."
    why = ["Rule: ITF match in play, one wallet with $2,000+ on the underdog (priced under 0.45), not much big money on "
           "the other side.",
           "In 30 days of ITF matches this returned +46% over 56 bets (+45% and +50% in the two halves), while backing "
           "the favourite at those moments lost 28%. Promising, not proven: it was found among many combinations."]
    if b["status"] != "skipped":
        why.append(f"Bet {I.money(b['stake'])} (3% of the balance) at {b['price']:.3f}, filled against the real order "
                   f"book, fee included. If {b['side']} wins it pays {I.money(b['shares'])}.")
    res = {"won": f"{b['side']} won: paid {I.money(b.get('shares', 0))}, profit {I.money(b.get('pnl', 0))}.",
           "lost": f"{b.get('winner')} won: the {I.money(b['stake'])} stake was lost.",
           "skipped": "Not placed.", "open": "Not settled yet.",
           "void": f"Refunded: {b.get('reason') or 'cancelled'}."}.get(b["status"], b["status"])
    return dict(headline=head, sections=[
        dict(title="The money", bullets=[f"{x.get('name') or x['wallet'][:10]}: ${x['usd']:,.0f} at {x['avg']:.2f} during the match"
                                         for x in b.get("wallets", [])[:6]] or ["-"]),
        dict(title="Why this bet", bullets=why), dict(title="Result", bullets=[res])])
