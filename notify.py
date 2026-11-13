"""
Phone alerts through ntfy.sh (free, no account): one push per tick, listing the paper bets that tick opened or exited.

The topic name is the only secret: anyone who knows it can read and send messages, so it lives in the NTFY_TOPIC
environment variable (a GitHub secret), not in this public repo. Without it, nothing is sent.
An alert must never stop the tick: every failure is logged and ignored.
"""

import json
import os
import urllib.request

URL = "https://ntfy.sh"
PREFIXES = {"Counter-Strike: ": "CS2", "Dota 2: ": "Dota", "LoL: ": "LoL", "League of Legends: ": "LoL"}
MAX_CHARS = 3500                # ntfy turns longer messages into attachments


def _match(m):
    """'Counter-Strike: A vs B (BO3) - Event' -> 'CS2 · A vs B (BO3) - Event'."""
    for p, g in PREFIXES.items():
        if m.startswith(p):
            return f"{g} · {m[len(p):]}"
    return m


def _line(tag, verb, b):
    return (f"{tag}: {verb} ${b['stake']:.2f} on {b['side']} at {b['price']:.2f} (odd {1 / b['price']:.2f})\n"
            f"{_match(b['match'])} ({b['market']})")


def events(accounts, now):
    """What this tick did: (line, url) for each paper bet opened or sold at `now`, across [(tag, state), ...]."""
    out = []
    for tag, state in accounts:
        for b in (state.get("account") or {}).get("bets", {}).values():
            if b["status"] == "open" and b.get("placed_at") == now:
                out.append((_line(tag, "bet", b), b.get("url")))
            elif b["status"] == "sold" and b.get("settled_at") == now:
                line = f"{tag}: sold {b['side']} for ${b['proceeds']:.2f} (paid ${b['stake']:.2f})\n" \
                       f"{_match(b['match'])} ({b['market']})"
                out.append((line, b.get("url")))
    return out


def payload(topic, evs, balance=None):
    """One message for the whole tick. Tapping it opens the market when there is only one."""
    n = len(evs)
    text = "\n\n".join(line for line, _ in evs) + (f"\n\nBanca: ${balance:.2f}" if balance is not None else "")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit("\n\n", 1)[0] + "\n\n(more on the dashboard)"
    out = dict(topic=topic, title="1 new paper bet" if n == 1 else f"{n} new paper bets", message=text)
    if n == 1 and evs[0][1]:
        out["click"] = evs[0][1]
    return out


def push(title, message, log=print, click=None):
    """One plain push (e.g. 'the scan is failing'). Silent without NTFY_TOPIC; never raises."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    try:
        body = dict(topic=topic, title=title, message=message[:MAX_CHARS])
        if click:
            body["click"] = click
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "cs2-watch"})
        with urllib.request.urlopen(req, timeout=20):
            pass
        return True
    except Exception as e:
        log(f"push failed: {type(e).__name__}")
        return False


def send(accounts, now, log=print, balance=None):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        evs = events(accounts, now)
        if not evs:
            return
        req = urllib.request.Request(URL, data=json.dumps(payload(topic, evs, balance)).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "cs2-watch"})
        with urllib.request.urlopen(req, timeout=20):
            pass
        log(f"alert sent: {len(evs)} events")
    except Exception as e:
        log(f"alert failed: {type(e).__name__}")    # not the error text: it could carry the secret topic into public logs
