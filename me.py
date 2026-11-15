"""
Pedro's own Polymarket account (@offchops2), read every tick so the page knows which flags he actually bet
(Nov 15 2026: "track my account so that i don't need to click manually").

Reads the wallet's Data API activity (buys, sells, redeems) on CS2 / Dota 2 / LoL markets, adds it up per market and
team, and settles each position from Polymarket's result. Real dollars, not the page's flat stake.
"""

import time

import cs2_watch as C
import hunter as H
import polydata as P

WALLET = "0xf1d2ad92306e105e710c309673bc7174790edcfd"     # @offchops2
KEEP = 60 * 86400
GAME_OF = {"Counter-Strike: ": "cs2", "Dota 2: ": "dota", "LoL: ": "lol", "League of Legends: ": "lol"}


def game_of(title):
    return next((g for p, g in GAME_OF.items() if (title or "").startswith(p)), None)


def sync(st, now, log=print, walk=None):
    """Add the wallet's new esports trades to st["trades"] (deduplicated), drop old ones, settle open positions."""
    walk = walk or P.walk
    since = int(st.get("last", now - 30 * 86400) - 3600)
    try:
        rows = walk("activity", dict(user=WALLET, start=since), 3000)
    except Exception as e:
        log(f"me: activity failed: {type(e).__name__}")
        return st
    seen = {t["id"] for t in st.setdefault("trades", [])}
    for a in rows:
        g = game_of(a.get("title"))
        if not g or a.get("type") not in ("TRADE", "REDEEM") or not a.get("condition_id"):
            continue
        tid = f"{a.get('transaction_hash')}|{a['condition_id']}|{a.get('outcome')}|{a.get('type')}|{a.get('side')}"
        if tid in seen:
            continue
        seen.add(tid)
        st["trades"].append(dict(id=tid, t=a["timestamp"], kind=a["type"], side=a.get("side") or "", cid=a["condition_id"],
                                 o=a.get("outcome"), usd=float(a.get("usdc_size") or 0), sh=float(a.get("size") or 0),
                                 title=a.get("title"), slug=a.get("event_slug") or a.get("slug"), game=g))
    st["trades"] = [t for t in st["trades"] if now - t["t"] < KEEP]
    st["last"] = max([now - 30 * 86400] + [t["t"] for t in st["trades"]])
    won = st.setdefault("winners", {})
    todo = {t["cid"] for t in st["trades"]} - set(won)
    if todo:
        for cid, m in H.market_states(todo).items():
            w = C.winner_of(m, False)
            if w:
                won[cid] = w
    return st


def bets(st):
    """One entry per market and team he bought: real stake, average price, status and profit."""
    pos = {}
    for t in sorted(st.get("trades", []), key=lambda t: t["t"]):
        p = pos.setdefault((t["cid"], t["o"]), dict(cid=t["cid"], side=t["o"], slug=t["slug"], game=t["game"], title=t["title"],
                                                     stake=0.0, sh=0.0, sold=0.0, sold_sh=0.0, placed_at=t["t"], last=t["t"]))
        if t["kind"] == "TRADE" and t["side"] == "BUY":
            p["stake"] += t["usd"]; p["sh"] += t["sh"]
        elif t["kind"] == "TRADE" and t["side"] == "SELL":
            p["sold"] += t["usd"]; p["sold_sh"] += t["sh"]
        p["last"] = max(p["last"], t["t"])
    out = []
    winners = st.get("winners", {})
    for p in pos.values():
        if p["stake"] <= 0:
            continue
        match, event = H.split_title(p["title"])
        left = max(0.0, p["sh"] - p["sold_sh"])
        w = winners.get(p["cid"])
        if w == C.CANCELLED:
            status, pnl = "void", 0.0
        elif w:
            pnl = p["sold"] + (left if w == p["side"] else 0.0) - p["stake"]
            status = "won" if w == p["side"] and left > 0.01 else "sold" if left <= 0.01 else "lost"
        elif left <= 0.01:
            status, pnl = "sold", p["sold"] - p["stake"]
        else:
            status, pnl = "open", None
        out.append(dict(cid=p["cid"], slug=p["slug"], game=p["game"], match=match, event=event, side=p["side"],
                        market="Match Winner", stake=round(p["stake"], 2), price=round(p["stake"] / p["sh"], 4) if p["sh"] else None,
                        status=status, pnl=round(pnl, 2) if pnl is not None else None, placed_at=p["placed_at"],
                        settled_at=p["last"] if status != "open" else None, real=True))
    return sorted(out, key=lambda b: -b["placed_at"])


def mark(view, mine):
    """Flag each tracked bet he also bet (same market and team, or same match and team); list the rest as unflagged."""
    by_cid = {(b["cid"], b["side"]): b for b in mine}
    by_slug = {(b["slug"], b["side"]): b for b in mine}
    used = set()
    for b in view.get("bets", []):
        m = by_cid.get((b.get("cid"), b["side"])) or by_slug.get((b.get("slug"), b["side"]))
        b["mine"] = bool(m)
        if m:
            b["my_stake"], b["my_price"] = m["stake"], m["price"]
            used.add((m["cid"], m["side"]))
    for b in mine:
        f = next((x for x in view.get("bets", []) if x.get("cid") == b["cid"] and x["side"] == b["side"]), None) or \
            next((x for x in view.get("bets", []) if x.get("slug") == b["slug"] and x["side"] == b["side"]), None)
        b["flagged"] = bool(f)
        b["rule"], b["alert"] = (f.get("rule"), f.get("alert")) if f else (None, False)
    view["me"] = dict(wallet=WALLET, name="offchops2", bets=mine, at=time.time())
    return view
