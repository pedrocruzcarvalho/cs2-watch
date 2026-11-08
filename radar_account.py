"""
Radar account: bet on what the pre-match radar shows (CS2 and tennis each have their own).

Rules (fixed on 2026-10-21 at Pedro's request, before any result):
  signal   a market on the pre-match radar, before kickoff, on Polymarket: the side with the most radar money, with
           less than half of that on the other side. Since Oct 21 2026 the radar only keeps hand-picked wallets,
           snipers ($300+), informed wallets ($200+), suspect/sharp repeat bettors and big bets from wallets that
           are not known whales with normal luck (cloud_run.radar); small bets and "rising" wallets are left out.
  bet      $20 on that side at the next 10-minute scan, filled against the real order book (fee included), once per
           market; skipped at 0.95+ or 0.05- (decided). $300 paper bankroll.
  exit     (Nov 5 2026) a bet copied from hand-picked wallets is sold at the real bids once they all leave its side
           (sell out, or go bigger on the other side); the market can then be bet again on their new side (follow()).
Expectation, said up front: in 60 days of CS2 data, following big pre-match bets was about break-even (the same
as backing the favourite). This account shows that live; it is not a tested edge.
State: "account" (bets). KV bundle parts state:radar (CS2) and state:tradar (tennis).
"""

from collections import defaultdict

import informed as I


CLEAN_SINCE = 1792590499    # the noise-free radar went live (Oct 21 2026, ~13:48 UTC)


def _counts_now(card, snipers):
    """Would this copied wallet count on today's radar (cloud_run.radar)?"""
    tags, usd, w = " ".join(card.get("tags") or []), card.get("usd", 0), card.get("wallet")
    if "your list" in tags and usd >= 900:
        return True
    if "repeat big bettor" in tags:
        return True
    return "big bet" in tags and "normal luck" not in (card.get("who") or "")


def prune_old(state, snipers):
    """Once (Pedro, Oct 21 2026): bets placed before CLEAN_SINCE that today's radar would not have made are refunded
    and hidden; the others keep their real result. A first, blanket refund of all of them is undone here as well."""
    if state.get("pruned"):
        return 0
    n = 0
    for b in state.get("account", {}).get("bets", {}).values():
        if b.get("placed_at", 0) >= CLEAN_SINCE:
            continue
        if b["status"] == "void" and "old, noisy radar" in (b.get("reason") or ""):   # undo the blanket refund
            b.pop("reason", None)
            w = b.get("winner")
            if w is None:
                b["status"] = "open"
                b.pop("pnl", None)
            else:
                b.update(status="won" if w == b["side"] else "lost",
                         pnl=round(b["shares"] - b["stake"], 2) if w == b["side"] else -b["stake"])
        if b["status"] in ("open", "won", "lost") and not any(_counts_now(c, snipers) for c in b.get("wallets") or []):
            b.update(status="void", pnl=0.0, reason="would not count on today's radar (small, rising or whale money)")
            n += 1
    state["pruned"] = True
    return n


def merge_backfill(state, bets):
    """Once: add the bets today's rules would have made since the account opened (radar_replay.py), with their real
    results. A market that already has a live (non-refunded) bet keeps it."""
    if state.get("backfilled"):
        return 0
    acct = state.setdefault("account", dict(opened_at=min((b["placed_at"] for b in bets), default=0), bets={}))
    n = 0
    for b in bets:
        cur = acct["bets"].get(b["cid"])
        if cur and cur["status"] != "void":
            continue
        acct["bets"][b["cid"]] = b
        n += 1
    state["backfilled"] = True
    return n


def restore_big(state):
    """Once (Pedro, Oct 21 2026): the early bets refunded as 'would not count on today's radar' come back with their
    real result when they copied $1,000+ (the big-money ones); the tiny ones ($33-$250) stay refunded."""
    if state.get("restored_big"):
        return 0
    n = 0
    for b in state.get("account", {}).get("bets", {}).values():
        if b["status"] != "void" or not (b.get("reason") or "").startswith("would not count on today's radar"):
            continue
        if max((c.get("usd", 0) for c in b.get("wallets") or []), default=0) < 1000:
            continue
        b.pop("reason", None)
        w = b.get("winner")
        if w is None:
            b["status"] = "open"
            b.pop("pnl", None)
        else:
            b.update(status="won" if w == b["side"] else "lost",
                     pnl=round(b["shares"] - b["stake"], 2) if w == b["side"] else -b["stake"])
        n += 1
    state["restored_big"] = True
    return n


def signals(groups, infos, now, skip_tiers=(), pre_match_only_list=False):
    """pre_match_only_list: before kickoff, only hand-picked wallets count (CS2 radar since Oct 24 2026: in 30 days
    its pre-match big bets lost 8% over 111 bets while its in-play ones made +157% over 32)."""
    by_key = {(i["match"], i.get("market", "Match Winner")): i for i in infos if i.get("venue") == "poly"}
    out = []
    for g in groups:
        i = by_key.get((g["match"], g["market"]))
        if not i or (g["start"] <= now) != bool(g.get("live")) or i.get("winner"):
            continue
        money, items = defaultdict(float), defaultdict(list)
        for x in g["items"]:
            kalshi_pre = any(t["kind"] == "kalshi_pre" for t in x["tags"])
            if not x.get("wallet") and not kalshi_pre:                 # other Kalshi blocks: not a trigger
                continue
            if not g.get("live") and pre_match_only_list and not any(t["kind"] in ("watched", "kalshi_pre") for t in x["tags"]):
                continue                    # before kickoff: hand-picked wallets and big Kalshi orders only (Pedro)
            money[x["outcome"]] += x["usd"]
            items[x["outcome"]].append(x)
        if not money:
            continue
        side = max(money, key=money.get)
        other = sum(v for o, v in money.items() if o != side)
        reasons = []
        if g.get("vtier") in skip_tiers:
            reasons.append(f"tier {g['vtier']} is not bet")
        if other >= 0.5 * money[side]:
            reasons.append(f"radar money is split (${money[side]:,.0f} vs ${other:,.0f})")
        card = lambda x: dict(wallet=x["wallet"], name=x.get("name"), usd=x["usd"], avg=x["avg"],
                              tags=[t["label"] for t in x["tags"]], who=x.get("who"))
        out.append(dict(match=g["match"], start=g["start"], market=g["market"], cid=i.get("cid"), slug=i.get("ref"),
                        url=i.get("url"), teams=i.get("teams"), prices=i.get("prices") or {}, tokens=i.get("tokens") or {},
                        side=side, money=round(money[side]), other_money=round(other),
                        wallets=[card(x) for x in sorted(items[side], key=lambda x: -x["usd"])][:6],
                        against=[card(x) for o, xs in items.items() if o != side for x in xs][:6],
                        vtier=g.get("vtier"), live=bool(g.get("live")), active=not reasons, why_not=reasons))
    return sorted(out, key=lambda s: (not s["active"], s["start"]))


def follow(state, sigs, infos, listed, now, book_bids, sell, log=print, keep=0.25, flip_min=300, max_exits=3):
    """Follow the hand-picked wallets out (Pedro, Nov 5 2026: TopStreamer bought megoshort map 2, we copied at 0.75,
    he sold out two minutes later and went big on Butterfly, which won).
    An open bet copied from hand-picked wallets (`listed`) is sold at the real bids when every one of them has left
    its side: sold since our bet and holding under `keep` of what we copied, or now holding more on the other side
    (at least `flip_min`). The market is then free again, so the next signal there (say the wallet's new side) is
    bet as usual; the side just left is not re-entered, and a market is exited at most `max_exits` times."""
    acct = state.get("account") or {}
    bets = acct.get("bets", {})
    exited = acct.setdefault("exited", {})
    by_cid = {i.get("cid"): i for i in infos if i.get("venue") == "poly"}
    for key, b in list(bets.items()):
        if b["status"] != "open" or key != b["cid"]:
            continue
        i = by_cid.get(b["cid"])
        trig = [c for c in b.get("wallets") or [] if (c.get("wallet") or "").lower() in listed]
        if not i or i.get("winner") or not trig:
            continue
        held = {(w, o): (sh, avg, last) for w, _, o, sh, avg, last in i.get("heldpos") or []}
        why = []
        for c in trig:
            w, nm = c["wallet"].lower(), c.get("name") or c["wallet"][:8]
            sh, avg, last = held.get((w, b["side"]), (0.0, 0.0, 0))
            mine = sh * avg
            other_o, other = max(((o, s * a) for (x, o), (s, a, _) in held.items() if x == w and o != b["side"]),
                                 key=lambda t: t[1], default=(None, 0.0))
            if other >= flip_min and other > mine:
                why.append(f"{nm} switched to {other_o} (${other:,.0f} there, ${mine:,.0f} left on {b['side']})")
            elif last >= b["placed_at"] and mine < keep * c["usd"]:
                why.append(f"{nm} sold out of {b['side']} (${mine:,.0f} left of ${c['usd']:,.0f})")
            else:
                why = []
                break
        if not why:
            continue
        bids = book_bids((i.get("tokens") or {}).get(b["side"]))
        n, got, px, _ = sell(b["shares"], bids or [])
        if not bids or n < b["shares"] - 0.01:
            log(f"radar exit waits: not enough buyers for {b['side']} ({b['match'][16:60]}, {b['market']})")
            continue
        bets[f"{key}~{now}"] = dict(b, status="sold", proceeds=round(got, 2), sold_price=round(px, 4), settled_at=now,
                                    pnl=round(got - b["stake"], 2), exit_reason="; ".join(why))
        del bets[key]
        exited.setdefault(b["cid"], []).append(b["side"])
        log(f"radar exit: sold {b['side']} ({b['match'][16:60]}, {b['market']}) at {px:.3f} for ${got:.2f} "
            f"(stake ${b['stake']:.2f}): {'; '.join(why)}")
    for s in sigs:
        left = exited.get(s["cid"]) or []
        if not s["active"] or not left:
            continue
        if s["side"] == left[-1]:
            s["active"] = False
            s["why_not"] = list(s.get("why_not") or []) + [f"the radar wallets just left {s['side']}"]
        elif len(left) >= max_exits:
            s["active"] = False
            s["why_not"] = list(s.get("why_not") or []) + [f"already exited this market {len(left)} times"]


def wallet_line(x, side):
    who = x.get("name") if x.get("name") and not str(x["name"]).startswith("0x") else x["wallet"][:6] + "…" + x["wallet"][-4:]
    tags = ", ".join(x.get("tags") or [])
    return f"{who}: {I.money(x['usd'])} on {side} at {x['avg']:.2f} ({tags}). {x.get('who') or ''}".strip()


def explain(b, now):
    other = next((t for t in b.get("teams") or [] if t != b["side"]), "the other side")
    head = (f"{I.money(b['stake'])} on {b['side']}" + (" to beat " + other if b["market"] == "Match Winner" else f" ({b['market']})")
            + f", following {I.money(b['money'])} of radar money "
            + ("bought during the match." if b.get("placed_at", 0) >= b["start"] else "before kickoff."))
    why = [f"Rule: bet the side with the most pre-match radar money when the other side has less than half of it. "
           f"Here {I.money(b['money'])} vs {I.money(b.get('other_money', 0))}."]
    if b.get("replayed"):
        why.append("Added afterwards: a replay of the day with today's rules found the radar would have made this bet "
                   "(filled at the last traded price then, +1c, fee included).")
    if b["status"] != "skipped":
        why.append(f"Bet {I.money(b['stake'])} at {b['price']:.3f}, filled against the real order book, fee included. "
                   f"If {b['side']} wins it pays {I.money(b['shares'])}.")
    res = {"won": f"{b['side']} won: paid {I.money(b.get('shares', 0))}, profit {I.money(b.get('pnl', 0))}.",
           "lost": f"{b.get('winner')} won: the {I.money(b['stake'])} stake was lost.",
           "skipped": "Not placed: no one was selling on the order book.", "open": "Not settled yet.",
           "void": f"Refunded: {b.get('reason') or 'cancelled'}.",
           "sold": f"Exited early, following the radar wallets out: {b.get('exit_reason')}. Sold the "
                   f"{b.get('shares', 0):.1f} shares at {b.get('sold_price') or 0:.3f} for {I.money(b.get('proceeds', 0))} "
                   f"({'+' if b.get('pnl', 0) >= 0 else '-'}{I.money(abs(b.get('pnl', 0)))})."}.get(b["status"], b["status"])
    return dict(headline=head, sections=[
        dict(title="The radar money", bullets=[wallet_line(x, b["side"]) for x in b.get("wallets", [])] or ["-"]),
        dict(title="Why this bet", bullets=why),
        dict(title="Why it could be wrong", bullets=[
            "Most radar money is whales and ordinary big bettors, not inside information. In 60 days of CS2 data, "
            "following big pre-match bets was about break-even.",
            "Watch the records: 'normal luck' wallets are the least likely to know anything."]),
        dict(title="Result", bullets=[res])])


def dashboard(state, sigs, now, tiers):
    view = I.account_view(state, sigs, now, explain, "RADAR", tiers, ("1 radar wallet", "2+ radar wallets"))
    return dict(view, rules=dict(stake=I.STAKE, start=I.START_BALANCE))
