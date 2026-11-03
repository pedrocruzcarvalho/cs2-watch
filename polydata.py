"""
Polymarket Data API v2 (v1 was retired on 2026-11-02 and now answers 410 Gone).

v2 differences that matter here: paths under /v2, cursor pagination (no offset), results wrapped in
{"data": [...], "pagination": {"next_cursor": ...}}, snake_case fields. These helpers return the v1-shaped dicts the
rest of the code was written for, so callers don't change.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

V2 = "https://data-api.polymarket.com/v2"
UA = {"User-Agent": "Mozilla/5.0 (cs2-watch)"}


def get(path, params, tries=6):
    url = f"{V2}/{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and k < tries - 1:
                time.sleep(float(e.headers.get("Retry-After") or 2 + 2 * k))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if k == tries - 1:
                raise
            time.sleep(2 + 2 * k)


def walk(path, params, cap):
    """Every row of a cursor-paginated route, up to `cap` rows. Filters are resent on every page (v2 rule)."""
    out, cursor = [], None
    while len(out) < cap:
        q = dict(params, cursor=cursor) if cursor else dict(params, limit=min(1000, cap))
        d = get(path, q)
        out += d.get("data") or []
        cursor = (d.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
    return out[:cap]


def _trade_v1(t):
    return dict(proxyWallet=t["proxy_wallet"], conditionId=t["condition_id"], outcome=t["outcome"],
                outcomeIndex=t.get("outcome_index"), side=t["side"], size=t["size"], price=t["price"],
                timestamp=t["timestamp"], name=t.get("name") or "", pseudonym=t.get("pseudonym") or "",
                asset=t.get("token_id"), title=t.get("title"), slug=t.get("slug"), eventSlug=t.get("event_slug"),
                transactionHash=t.get("transaction_hash"))


def event_trades(event_id, cap=3000, min_cash=None):
    """All trades of an event (newest first), v1 field names."""
    q = dict(event_id=event_id)
    if min_cash:
        q.update(filter_type="CASH", filter_amount=min_cash)
    return [_trade_v1(t) for t in walk("trades", q, cap)]


def user_positions(user, statuses=("CLOSED", "REDEEMABLE", "REDEEMABLE_LOST"), cap=5000):
    """A wallet's positions in the given lifecycle states (v2 fields)."""
    out = []
    for st in statuses:
        q = dict(user=user, status=st)
        if st != "CLOSED":                          # v2 accepts include_archived on every status except CLOSED
            q["include_archived"] = "true"
        out += walk("positions", q, cap)
    return out


def user_stats(user):
    return (get("user-stats", dict(user=user)) or {}).get("data") or {}
