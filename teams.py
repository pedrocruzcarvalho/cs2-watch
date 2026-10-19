"""
Team strength from Valve's own CS2 ranking (Valve Regional Standings, github.com/ValveSoftware/
counter-strike_regional_standings, updated monthly, ~380 ranked teams). Used for:
  - real tiers instead of "low/mid": T1 #1-20, T2 #21-50, T3 #51-100, T4 #101-200, T5 #201+, T6 unranked;
  - who would have to throw: a flag backing one side means the other side would have to lose on purpose,
    and established teams (big salaries, sponsors, reputation) very rarely do.
Names are matched strictly (exact after normalising), so "Spirit Academy" is never taken for Spirit.
"""

import json
import re
import time
import urllib.request

REPO = "ValveSoftware/counter-strike_regional_standings"
REFRESH = 24 * 3600
TIER_EDGES = [(20, "T1"), (50, "T2"), (100, "T3"), (200, "T4")]
ALIASES = {"natusvincere": "navi", "teamspirit": "spirit", "teamvitality": "vitality", "themongolz": "mongolz",
           "teamliquid": "liquid", "ninjasinpyjamas": "nip", "teamfalcons": "falcons", "virtuspro": "vp",
           "jiejiehao": "jijiehao"}

# Established teams are assumed not to throw (top TRUST_TOP in Valve's ranking), except these, which Pedro's
# knowledge of the scene says could (normalised names).
TRUST_TOP = 40
FIX_PRONE = {"alliance", "hotu", "5star", "tyloo", "lynnvision", "dendele", "eternalfire", "sinners", "jijiehao", "k27",
             "nucleartigeres", "sangal", "wildcard", "magic", "nemiga", "1win", "betboom", "mibr"}


def wont_throw(state, name):
    """(True, rank) if this team is established enough that it is assumed not to throw a match."""
    r = rank(state, name)
    return (r is not None and r <= TRUST_TOP and norm(name) not in FIX_PRONE), r


def norm(name):
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    n = re.sub(r"(esports|esport|gaming|team|club|gg)$", "", n) or n
    n = re.sub(r"^(team)", "", n) or n
    return ALIASES.get(n, n)


def get(url, raw=False):
    req = urllib.request.Request(url, headers={"User-Agent": "cs2-watch", "Accept": "application/vnd.github+json"})
    body = urllib.request.urlopen(req, timeout=30).read()
    return body.decode("utf-8") if raw else json.loads(body)


def fetch():
    """{normalised team name: (rank, points, display name)} from the latest global standings file."""
    years = sorted(x["name"] for x in get(f"https://api.github.com/repos/{REPO}/contents/live") if x["name"].isdigit())
    files = sorted(x["name"] for x in get(f"https://api.github.com/repos/{REPO}/contents/live/{years[-1]}")
                   if x["name"].startswith("standings_global_"))
    md = get(f"https://raw.githubusercontent.com/{REPO}/main/live/{years[-1]}/{files[-1]}", raw=True)
    out = {}
    for line in md.splitlines():
        m = re.match(r"\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|", line)
        if m:
            out.setdefault(norm(m.group(3)), (int(m.group(1)), int(m.group(2)), m.group(3)))
    return dict(at=time.time(), file=files[-1], teams=out)


def refresh(state, now, log=print):
    if now - state.get("at", 0) >= REFRESH or not state.get("teams"):
        try:
            state.update(fetch())
            log(f"valve ranking: {len(state['teams'])} teams from {state['file']}")
        except Exception as err:
            log(f"  valve ranking fetch failed (keeping the old one): {err}")
    return state


def rank(state, name):
    t = (state or {}).get("teams", {}).get(norm(name))
    return t[0] if t else None


def tier_of_rank(r):
    if r is None:
        return "T6"
    return next((t for edge, t in TIER_EDGES if r <= edge), "T5")


def describe(state, name):
    r = rank(state, name)
    return f"{name} is unranked by Valve (tier 6)" if r is None else f"{name} is #{r} in Valve's ranking ({tier_of_rank(r)})"
