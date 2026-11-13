"""
Mirror account (Pedro, Nov 13 2026): paper-copy one Polymarket wallet, every market it trades, for one week.

Each tick reads the wallet's new activity (Data API v2 /activity since the last tick):
  BUY    we buy the same outcome with his dollars x SCALE at the real order book. SCALE = our $100 / his median
         daily buying of the 7 days before we opened, so we turn over our money about as fast as he does his.
         Tiny copies are pooled per outcome until they reach $1 (Polymarket's minimum order).
  SELL   we sell the same share of our position as the share of his (tracked since we opened) that he sold, at the
         real bids. MERGE (his yes + no turned back into cash) counts as selling both sides.
  end    a resolved market pays our shares out at its final price (1 / 0 / 0.5 if voided).
New buys stop after DAYS; open positions still settle. No phone alerts (Pedro). Positions he opened before we
started are not copied, so their sells are ignored.
"""

import json
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone

import bankroll as B
import cs2_watch as C
import polydata as P

WALLET = "0xf68a281980f8c13828e84e147e3822381d6e5b1b"
NAME = "cutjalenhurts"
BUDGET = 100.0
DAYS = 7
MIN_ORDER = 1.0
DUST = 0.01                    # shares below this are treated as gone


def scale_from_history(now):
    """our budget / his median daily buying over the 7 days before now."""
    acts = P.walk("activity", dict(user=WALLET, start=int(now - 7 * 86400), end=int(now)), 20000)
    day = defaultdict(float)
    for a in acts:
        if a["type"] == "TRADE" and a.get("side") == "BUY":
            day[datetime.fromtimestamp(a["timestamp"], timezone.utc).date()] += a.get("usdc_size") or 0
    med = statistics.median(day.values()) if day else 250000.0
    return BUDGET / med, med


def open_account(st, now, log=print):
    if st.get("opened_at"):
        return
    k, med = scale_from_history(now)
    st.update(opened_at=now, until=now + DAYS * 86400, scale=k, his_daily=round(med), last_ts=int(now), seen=[],
              cash=BUDGET, pos={}, pending={}, his={}, trades=[], closed=[])
    log(f"mirror: opened, his median daily buying ${med:,.0f}, scale {k:.6f}")


def _pos(st, a):
    tok = a["token_id"]
    return st["pos"].setdefault(tok, dict(token=tok, cid=a["condition_id"], outcome=a.get("outcome"),
                                          title=a.get("title"), slug=a.get("event_slug") or a.get("slug"),
                                          shares=0.0, cost=0.0, opened=a["timestamp"]))


def _trade(st, kind, a, usd, shares, px, note=""):
    st["trades"].append(dict(t=int(time.time()), his_t=a["timestamp"], kind=kind, title=a.get("title"),
                             outcome=a.get("outcome"), his_usd=round(a.get("usdc_size") or 0, 2),
                             his_px=round(a.get("price") or 0, 3), usd=round(usd, 2), shares=round(shares, 4),
                             px=round(px, 4) if px else None, note=note,
                             url=f"https://polymarket.com/event/{a.get('event_slug') or a.get('slug') or ''}"))


def buy(st, a, book_asks, fill, now):
    tok = a["token_id"]
    his = st["his"].setdefault(tok, 0.0)
    st["his"][tok] = his + (a.get("size") or 0)
    if now > st["until"]:
        return
    want = st["pending"].get(tok, 0.0) + (a.get("usdc_size") or 0) * st["scale"]
    if want < MIN_ORDER:
        st["pending"][tok] = want
        return
    st["pending"].pop(tok, None)
    amount = min(want, st["cash"])
    if amount < MIN_ORDER:
        _trade(st, "skip", a, 0, 0, None, "no cash left")
        return
    asks = book_asks(tok)
    sh, spent, avg, _ = fill(amount, asks) if asks else (0, 0, None, 0)
    if not sh:
        _trade(st, "skip", a, 0, 0, None, "nobody selling at the book")
        return
    p = _pos(st, a)
    p["shares"] += sh
    p["cost"] += spent
    st["cash"] -= spent
    _trade(st, "buy", a, spent, sh, avg)


def sell_fraction(st, tok, frac, a, book_bids, sell, kind="sell"):
    p = st["pos"].get(tok)
    if not p or p["shares"] < DUST or frac <= 0:
        return
    n = p["shares"] * min(1.0, frac)
    bids = book_bids(tok)
    sold, got, avg, _ = sell(n, bids) if bids else (0, 0, None, 0)
    if not sold:
        _trade(st, "skip", a, 0, 0, None, "nobody buying at the book (sell not copied)")
        return
    part = sold / p["shares"]
    cost = p["cost"] * part
    p["shares"] -= sold
    p["cost"] -= cost
    st["cash"] += got
    _trade(st, kind, a, got, sold, avg, f"P&L {got - cost:+.2f}")
    st.setdefault("realized", 0.0)
    st["realized"] += got - cost
    if p["shares"] < DUST:
        _close(st, tok, "sold")


def _close(st, tok, how, payout=0.0):
    p = st["pos"].pop(tok)
    st["closed"].append(dict(p, how=how, payout=round(payout, 2), pnl=round(payout - p["cost"], 2) if how != "sold" else None,
                             closed_at=int(time.time())))


def his_sell(st, a, book_bids, sell):
    tok = a["token_id"]
    his = st["his"].get(tok, 0.0)
    if his <= 0:
        return                                          # a position he had before we started: not ours to copy
    n = a.get("size") or 0
    st["his"][tok] = max(0.0, his - n)
    sell_fraction(st, tok, n / his, a, book_bids, sell)


def his_merge(st, a, book_bids, sell):
    for tok, p in list(st["pos"].items()):
        if p["cid"] != a["condition_id"]:
            continue
        his = st["his"].get(tok, 0.0)
        if his > 0:
            n = a.get("size") or 0
            st["his"][tok] = max(0.0, his - n)
            sell_fraction(st, tok, n / his, dict(a, token_id=tok, outcome=p["outcome"], title=p["title"]),
                          book_bids, sell, kind="merge")


def market_states(cids):
    """{condition_id: market} from Gamma, 20 per call."""
    out, cids = {}, list(cids)
    for i in range(0, len(cids), 20):
        q = "&".join(f"condition_ids={c}" for c in cids[i:i + 20])
        try:
            for m in C.get(f"{C.GAMMA}/markets?{q}&limit=50"):
                out[m["conditionId"]] = m
        except Exception:
            pass
    return out


def mark_and_settle(st, log=print):
    """Resolved markets pay out; the rest are valued at their current price."""
    ms = market_states({p["cid"] for p in st["pos"].values()})
    value = 0.0
    for tok, p in list(st["pos"].items()):
        m = ms.get(p["cid"])
        if not m:
            value += p["cost"]
            continue
        try:
            toks = json.loads(m.get("clobTokenIds") or "[]")
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
            px = prices[toks.index(tok)]
        except Exception:
            value += p["cost"]
            continue
        p["price"] = px
        if m.get("closed") and (px >= 0.99 or px <= 0.01 or abs(px - 0.5) < 0.01):
            pay = p["shares"] * (1.0 if px >= 0.99 else 0.0 if px <= 0.01 else 0.5)
            st["cash"] += pay
            st.setdefault("realized", 0.0)
            st["realized"] += pay - p["cost"]
            _close(st, tok, "won" if px >= 0.99 else "lost" if px <= 0.01 else "void", pay)
        else:
            value += p["shares"] * px
    st["value"] = round(value, 2)


def tick(st, now, log=print, book_asks=None, book_bids=None, fill=None, sell=None):
    book_asks, book_bids = book_asks or C.book_asks, book_bids or C.book_bids
    fill, sell = fill or B.fill, sell or B.sell
    open_account(st, now, log=log)
    acts = P.walk("activity", dict(user=WALLET, start=st["last_ts"]), 5000)
    seen = set(st["seen"])
    new = [a for a in sorted(acts, key=lambda a: a["timestamp"])
           if f"{a.get('transaction_hash')}|{a.get('token_id')}|{a.get('size')}|{a['type']}" not in seen]
    for a in new:
        seen.add(f"{a.get('transaction_hash')}|{a.get('token_id')}|{a.get('size')}|{a['type']}")
        if a["type"] == "TRADE" and a.get("side") == "BUY":
            buy(st, a, book_asks, fill, now)
        elif a["type"] == "TRADE" and a.get("side") == "SELL":
            his_sell(st, a, book_bids, sell)
        elif a["type"] == "MERGE":
            his_merge(st, a, book_bids, sell)
    if new:
        st["last_ts"] = max(a["timestamp"] for a in new)
        st["seen"] = [k for k in seen if k.split("|")[0]][-3000:]
    mark_and_settle(st, log=log)
    st["trades"] = st["trades"][-1500:]
    log(f"mirror: {len(new)} new activities, cash ${st['cash']:.2f}, open value ${st['value']:.2f}, "
        f"{len(st['pos'])} open positions")


def view(st, now):
    if not st.get("opened_at"):
        return None
    eq = st["cash"] + st.get("value", 0.0)
    closed = st["closed"]
    res = [c for c in closed if c["how"] in ("won", "lost")]
    pos = sorted(st["pos"].values(), key=lambda p: -p["cost"])
    return dict(name=NAME, wallet=WALLET, budget=BUDGET, opened_at=st["opened_at"], until=st["until"],
                his_daily=st["his_daily"], scale=st["scale"], cash=round(st["cash"], 2), value=st.get("value", 0.0),
                equity=round(eq, 2), pnl=round(eq - BUDGET, 2), realized=round(st.get("realized", 0.0), 2),
                won=sum(c["how"] == "won" for c in res), lost=sum(c["how"] == "lost" for c in res),
                sold=sum(c["how"] == "sold" for c in closed), copying=now <= st["until"],
                open=[dict(title=p["title"], outcome=p["outcome"], shares=round(p["shares"], 2), cost=round(p["cost"], 2),
                           price=p.get("price"), value=round(p["shares"] * (p.get("price") or 0), 2),
                           url=f"https://polymarket.com/event/{p.get('slug') or ''}") for p in pos],
                closed=[dict(title=c["title"], outcome=c["outcome"], how=c["how"], cost=round(c["cost"], 2),
                             payout=c["payout"], pnl=c["pnl"], at=c["closed_at"]) for c in closed[-200:]][::-1],
                trades=st["trades"][-300:][::-1])
