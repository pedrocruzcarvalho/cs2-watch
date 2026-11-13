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
LOW_SHARE = 0.70               # a fixer lives on tier 3+: at least this share of its matches there (Pedro, Nov 13 2026)
# walk-forward, sharp wallets in-play CS2/Dota: no share rule +9% (352 bets); 70%+ tier 3+ +15.5% (118, halves +15/+16);
# 90%+ +25% (52). Wallets that also bet a lot on tier 1/2 (e.g. DarkzaoGod, ry077) are good bettors, not insiders.
MIN_USD = 200
KEEP = 75 * DAY


def low_share(book, w):
    """Share of the wallet's matches (in the book) that were tier 3+; entries carry e[7] = 1 for tier 1/2."""
    es = book.get(w, [])
    return sum(1 for e in es if not (len(e) > 7 and e[7])) / len(es) if es else 0.0


def record(book, w, before=None, skip_event=None):
    """(n, wins, edge, roi) over the wallet's resolved tier 3+ matches (settled before `before`)."""
    es = [e for e in book.get(w, []) if e[5] is not None and e[0] != skip_event and not (len(e) > 7 and e[7])
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
    return n >= SHARP_N and edge >= SHARP_EDGE and low_share(book, w) >= LOW_SHARE


def note(book, w, event, cid, side, px, usd, start, top=False):
    """Remember the wallet's biggest bet on this match (entry: [event, cid, px, usd, start, won, side, top])."""
    if usd < MIN_USD or not 0.2 <= px <= 0.97:
        return
    es = book.setdefault(w, [])
    if top:                                             # tier 1/2: only counted (low_share), never scored
        if not any(e[0] == event for e in es):
            es.append([event, "", 0, 0, start, 0, "", 1])
        return
    for e in es:
        if e[0] == event:
            if usd > e[3] and e[5] is None:
                e[1:5], e[6] = [cid, round(px, 3), round(usd), start], side
            return
    es.append([event, cid, round(px, 3), round(usd), start, None, side, 1 if top else 0])


def prune(book):
    """Wallets that never bet a tier 3+ match carry no information here."""
    for w in [w for w, es in book.items() if all(len(e) > 7 and e[7] for e in es)]:
        del book[w]


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


ACTIVE = 14 * DAY              # the page lists only wallets that bet in this window (Pedro: no dead wallets)


def board(book, now, names, listed, limit=60):
    """The page's wallet list: sharp and hand-picked wallets that bet on a tier 3+ match in the last ACTIVE days,
    best record first."""
    out = []
    for w in set(book) | set(listed):
        n, wins, edge, roi = record(book, w, now)
        ls = low_share(book, w)
        is_sharp = n >= SHARP_N and edge >= SHARP_EDGE and ls >= LOW_SHARE
        if not is_sharp and w not in listed:
            continue
        last = max((e[4] for e in book.get(w, [])), default=None)
        if not last or now - last > ACTIVE:
            continue
        out.append(dict(wallet=w, name=names.get(w) or (listed.get(w) or {}).get("name"), n=n, wins=wins,
                        edge=round(edge, 3), roi=round(roi, 3), sharp=is_sharp, listed=w in listed,
                        note=(listed.get(w) or {}).get("note"), last=last, low=round(ls, 2),
                        open=sum(1 for e in book.get(w, []) if e[5] is None)))
    return sorted(out, key=lambda x: (not x["sharp"], -x["edge"] * min(x["n"], 30)))[:limit]
