"""
The wallet book: every wallet's record on tier 3+ CS2 / Dota 2 / LoL matches, kept up to date by the tick.

One entry per wallet and match: its biggest bet there (side, average price, dollars, start), resolved once the market
settles. A wallet with SHARP_N+ resolved matches whose win rate beats its prices by SHARP_EDGE+ is "sharp".

Why (walk-forward test, 60 days to Nov 13 2026, each bet judged only on the record the wallet had before it,
copied 3 cents worse, one bet per match): following sharp wallets in-play on CS2 / Dota 2 made +11.4% over 339
bets (halves +14% / +9%); before kickoff -4%, LoL -1%. Picking wallets on a looser record (5+ matches, or +10 pp)
did not hold up: most good records regress to the mean.
Seeded from the research data (hunt_seed.py --book); entries older than KEEP are dropped.
"""

DAY = 86400
SHARP_N, SHARP_EDGE = 10, 0.20
MIN_USD = 200
KEEP = 75 * DAY


def record(book, w, before=None, skip_event=None):
    """(n, wins, edge, roi) over the wallet's resolved matches (settled before `before`)."""
    es = [e for e in book.get(w, []) if e[5] is not None and e[0] != skip_event
          and (before is None or e[4] + 3 * 3600 <= before)]
    if not es:
        return 0, 0, 0.0, 0.0
    n = len(es)
    wins = sum(1 for e in es if e[5])
    edge = wins / n - sum(e[2] for e in es) / n
    roi = sum((1 / e[2] - 1) if e[5] else -1 for e in es) / n
    return n, wins, edge, roi


def sharp(book, w, now, skip_event=None):
    n, wins, edge, roi = record(book, w, now, skip_event)
    return n >= SHARP_N and edge >= SHARP_EDGE


def note(book, w, event, cid, side, px, usd, start):
    """Remember the wallet's biggest bet on this match (entry: [event, cid, px, usd, start, won, side])."""
    if usd < MIN_USD or not 0.2 <= px <= 0.97:
        return
    es = book.setdefault(w, [])
    for e in es:
        if e[0] == event:
            if usd > e[3] and e[5] is None:
                e[1:5], e[6] = [cid, round(px, 3), round(usd), start], side
            return
    es.append([event, cid, round(px, 3), round(usd), start, None, side])


def pending(book, now):
    """Condition ids of entries waiting for a result (match started 2 h+ ago)."""
    return {e[1] for es in book.values() for e in es if e[5] is None and now > e[4] + 2 * 3600}


def resolve(book, winners, now):
    """winners: {cid: winning outcome or '__cancelled__'}. Drops entries older than KEEP and void ones."""
    for w in list(book):
        keep = []
        for e in book[w]:
            if now - e[4] > KEEP:
                continue
            if e[5] is None and e[1] in winners:
                win = winners[e[1]]
                if win == "__cancelled__":
                    continue
                e[5] = 1 if win == e[6] else 0
            keep.append(e)
        if keep:
            book[w] = keep
        else:
            del book[w]


def board(book, now, names, listed, limit=60):
    """The page's wallet list: sharp wallets and hand-picked ones, best record first."""
    out = []
    for w in set(book) | set(listed):
        n, wins, edge, roi = record(book, w, now)
        is_sharp = n >= SHARP_N and edge >= SHARP_EDGE
        if not is_sharp and w not in listed:
            continue
        last = max((e[4] for e in book.get(w, [])), default=None)
        out.append(dict(wallet=w, name=names.get(w) or (listed.get(w) or {}).get("name"), n=n, wins=wins,
                        edge=round(edge, 3), roi=round(roi, 3), sharp=is_sharp, listed=w in listed,
                        note=(listed.get(w) or {}).get("note"), last=last,
                        open=sum(1 for e in book.get(w, []) if e[5] is None)))
    return sorted(out, key=lambda x: (not x["sharp"], -x["edge"] * min(x["n"], 30)))[:limit]
