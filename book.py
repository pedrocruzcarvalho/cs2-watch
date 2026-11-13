"""
The wallet book: every wallet's REAL result on each CS2 / Dota 2 / LoL match, kept up to date by the tick.

A match counts as a win only when the wallet's money went up on it (Pedro, Nov 13 2026): everything it spent on the
match's team markets against everything it got back, from sells before the end and from the shares it still held when
the markets resolved. Small bets and sells count; a wallet that buys and sells out at a loss has a loss.

Entry per wallet and match: [event, holdings, spent, sold, start, pnl, top]
  holdings  {"<cid>|<outcome>": shares still held} ({} for tier 1/2, which are resolved as 0 at once)
  pnl       None until every market it holds has resolved
  top       1 for a tier 1/2 match (only counted: a fixer lives on tier 3+)
"""

DAY = 86400
SHARP_N, SHARP_WIN, SHARP_ROI = 5, 0.60, 0.10    # walk-forward, real P&L: +3.9% over 787 in-play copies (halves +3/+5); stricter cuts lost
LOW_SHARE = 0.70
MIN_SPENT = 20
KEEP = 75 * DAY
ACTIVE = 14 * DAY


def key(cid, outcome):
    return f"{cid}|{outcome}"


def low_share(book, w):
    es = book.get(w, [])
    return sum(1 for e in es if not e[6]) / len(es) if es else 0.0


def record(book, w, before=None, skip_event=None):
    """(n, wins, win_rate, roi) over the wallet's resolved tier 3+ matches; a win = money went up."""
    es = [e for e in book.get(w, []) if e[5] is not None and not e[6] and e[0] != skip_event
          and (before is None or e[4] + 3 * 3600 <= before)]
    if not es:
        return 0, 0, 0.0, 0.0
    n = len(es)
    wins = sum(1 for e in es if e[5] > 0)
    spent = sum(e[2] for e in es)
    return n, wins, wins / n, (sum(e[5] for e in es) / spent if spent else 0.0)


def sharp(book, w, now, skip_event=None):
    n, wins, rate, roi = record(book, w, now, skip_event)
    return n >= SHARP_N and rate >= SHARP_WIN and roi >= SHARP_ROI and low_share(book, w) >= LOW_SHARE


def note(book, w, event, start, top, holdings, spent, sold):
    """The wallet's totals on this match so far (replaces the previous ones while the match is in the scan)."""
    if spent < MIN_SPENT:
        return
    es = book.setdefault(w, [])
    for e in es:
        if e[0] == event:
            if e[5] is None:
                e[1], e[2], e[3] = holdings, round(spent, 2), round(sold, 2)
            return
    if top:
        es.append([event, {}, 0, 0, start, 0, 1])
    else:
        es.append([event, holdings, round(spent, 2), round(sold, 2), start, None, 0])


def pending(book, now):
    """Market ids still waiting for a result (match started 2 h+ ago)."""
    return {k.split("|")[0] for es in book.values() for e in es if e[5] is None and now > e[4] + 2 * 3600
            for k in e[1]}


def resolve(book, payouts, now):
    """payouts: {"<cid>|<outcome>": 1.0 / 0.0 / 0.5}. An entry resolves when all its holdings have a payout."""
    for w in list(book):
        keep = []
        for e in book[w]:
            if now - e[4] > KEEP:
                continue
            if e[5] is None and now > e[4] + 2 * 3600:
                if all(k in payouts for k in e[1]):
                    e[5] = round(e[3] + sum(sh * payouts[k] for k, sh in e[1].items()) - e[2], 2)
            keep.append(e)
        if keep and any(not e[6] for e in keep):
            book[w] = keep
        else:
            del book[w]


def board(book, now, names, listed, limit=60):
    """Sharp and hand-picked wallets that bet a tier 3+ match in the last ACTIVE days, best first."""
    out = []
    for w in set(book) | set(listed):
        n, wins, rate, roi = record(book, w, now)
        ls = low_share(book, w)
        is_sharp = n >= SHARP_N and rate >= SHARP_WIN and roi >= SHARP_ROI and ls >= LOW_SHARE
        if not is_sharp and w not in listed:
            continue
        last = max((e[4] for e in book.get(w, [])), default=None)
        if not last or now - last > ACTIVE:
            continue
        pnl = sum(e[5] for e in book.get(w, []) if e[5] is not None and not e[6])
        out.append(dict(wallet=w, name=names.get(w) or (listed.get(w) or {}).get("name"), n=n, wins=wins,
                        rate=round(rate, 3), roi=round(roi, 3), pnl=round(pnl), sharp=is_sharp, listed=w in listed,
                        note=(listed.get(w) or {}).get("note"), last=last, low=round(ls, 2),
                        open=sum(1 for e in book.get(w, []) if e[5] is None)))
    return sorted(out, key=lambda x: (not x["sharp"], -x["roi"] * min(x["n"], 30)))[:limit]
