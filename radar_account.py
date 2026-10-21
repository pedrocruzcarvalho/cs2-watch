"""
Radar account: bet on what the pre-match radar shows (CS2 and tennis each have their own).

Rules (fixed on 2026-10-21 at Pedro's request, before any result):
  signal   a market on the pre-match radar, before kickoff, on Polymarket: the side with the most radar money, with
           less than half of that on the other side. Since Oct 21 2026 the radar only keeps hand-picked wallets,
           snipers ($300+), informed wallets ($200+), suspect/sharp repeat bettors and big bets from wallets that
           are not known whales with normal luck (cloud_run.radar); small bets and "rising" wallets are left out.
  bet      $20 on that side at the next 10-minute scan, filled against the real order book (fee included), once per
           market; skipped at 0.97+. $300 paper bankroll.
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


def signals(groups, infos, now, skip_tiers=()):
    by_key = {(i["match"], i.get("market", "Match Winner")): i for i in infos if i.get("venue") == "poly"}
    out = []
    for g in groups:
        i = by_key.get((g["match"], g["market"]))
        if not i or (g["start"] <= now) != bool(g.get("live")) or i.get("winner"):
            continue
        money, items = defaultdict(float), defaultdict(list)
        for x in g["items"]:
            if not x.get("wallet"):                                      # Kalshi blocks: nothing to buy on Polymarket
                continue
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
           "void": f"Refunded: {b.get('reason') or 'cancelled'}."}.get(b["status"], b["status"])
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
