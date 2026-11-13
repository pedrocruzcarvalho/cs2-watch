"""
One-off: the last N days of tier 3+ signals from the research data, in the hunter's signal-ledger format, so the page's
list and simulation start with history (Pedro, Nov 13 2026). Prices are the wallet's own average (we were not
watching then), marked seed=True. Upload: wrangler kv key put seed:signals --path local_state/seed_signals.json
"""

import json
import sys
import time

import hunt_research as R
import hunter as H


def main(days=14):
    now = time.time()
    best = {}
    for u in R.units(1000, "all"):
        if u["top"] or u["start"] < now - days * 86400:
            continue
        key = f"{u['cid']}|{u['o']}|{u['phase']}"
        if key not in best or u["usd"] > best[key]["usd"]:
            best[key] = u
    out = {}
    for key, u in best.items():
        u = dict(u, prices={}, market=None)
        rule, no = H.classify(u)
        if u["hedge"]:
            rule, no = None, "a carteira apostou nas duas equipas"
        other = next(o for o in u["outcomes"] if o != u["o"])
        side = other if rule == "D" else u["o"]
        px = 1 - u["px"] if rule == "D" else u["px"]
        u["vol"] = u["vol"]
        match, event = H.split_title(u["title"])
        q = u["q"]
        label = "Match Winner" if u["kind"] == "match" else (q.split(" - ")[-1] if " - " in q else q)
        out[key] = dict(game=u["game"], tier=u["tier"], match=match, event=event, market=label, kind=u["kind"],
                        cid=u["cid"], slug=u["slug"], url=f"https://polymarket.com/event/{u['slug']}", start=u["start"],
                        phase=u["phase"], their_side=u["o"], side=side, seen=u["first"], price=round(px, 3),
                        wallet=u["w"], name=u["name"], usd=round(u["usd"]), their_px=round(u["px"], 3), age=u["age"],
                        share=round(u["share"], 3), score=H.score(u, rule), rule=rule, no=no, why=H.why(u),
                        status="won" if u["winner"] == side else "lost", winner=u["winner"], seed=True)
    path = "local_state/seed_signals.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    n = len(out)
    a = sum(1 for x in out.values() if x["rule"])
    print(f"{n} signals ({a} APOSTAR) -> {path}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()


def seed_book():
    """The wallet book (book.py) from the research data: each wallet's biggest bet per tier 3+ match, resolved."""
    import book as BK
    best = {}
    for u in R.units(BK.MIN_USD, "all"):
        if u["hedge"] or not 0.2 <= u["px"] <= 0.97:
            continue
        k = (u["w"], u["ev"])
        if k not in best or u["usd"] > best[k]["usd"]:
            best[k] = u
    bk, names = {}, {}
    for (w, ev), u in best.items():
        bk.setdefault(w, []).append([ev, "", 0, 0, u["start"], 0, "", 1] if u["top"] else
                                    [ev, u["cid"], round(u["px"], 3), round(u["usd"]), u["start"], int(u["won"]), u["o"], 0])
        if u["name"] and not u["name"].startswith("0x"):
            names[w] = u["name"]
    bk = {w: es for w, es in bk.items() if any(not e[7] for e in es)}      # only wallets that bet tier 3+ matter
    now = time.time()
    sharp = [w for w in bk if BK.sharp(bk, w, now)]
    names = {w: n for w, n in names.items() if w in sharp}
    with open("local_state/seed_book.json", "w", encoding="utf-8") as f:
        json.dump(dict(book=bk, names=names), f, separators=(",", ":"))
    print(f"book: {len(bk)} wallets, {sum(len(v) for v in bk.values())} matches, {len(sharp)} sharp now")


def seed_book3():
    """Book v3 (real P&L per match) from the recollection with sells (local_state/hunt2)."""
    import book as BK
    bk, now = {}, time.time()
    for x in R.book_matches():
        e = [x["ev"], {}, 0, 0, x["start"], 0, 1] if x["top"] else [x["ev"], {}, round(x["spent"], 2), 0, x["start"], round(x["pnl"], 2), 0]
        bk.setdefault(x["w"], []).append(e)
    bk = {w: es for w, es in bk.items() if any(not e[6] for e in es)}
    sharp = [w for w in bk if BK.sharp(bk, w, now)]
    with open("local_state/seed_book.json", "w", encoding="utf-8") as f:
        json.dump(dict(book=bk, names={}, v=3), f, separators=(",", ":"))
    print(f"book v3: {len(bk)} wallets, {len(sharp)} sharp now")
