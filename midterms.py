#!/usr/bin/env python3
"""
Midterms terminal viewer (Kalshi public API, no auth needed).

    watch -n 10 -c python3 midterms.py       # -c keeps colors
    python3 midterms.py --discover           # list candidate event tickers per row
    python3 midterms.py --raw [TICKER ...]   # dump markets (no args = every pinned margin event)
    python3 midterms.py --reset              # force ticker re-resolution (rarely needed)

Prices are fetched live on every run. Only the label -> event-ticker mapping is
cached (~/.cache/midterms_tickers.json). Pin exact tickers in PIN to skip resolution.
"""
import hashlib, json, os, re, sys, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = "https://api.elections.kalshi.com/trade-api/v2"
CACHE = os.path.expanduser("~/.cache/midterms_tickers.json")
CACHE_TTL = 6 * 3600

# Pin exact event tickers. Signed (margin) rows take a list.
PIN = {
    "House Vote Margin": ["KXHOUSEPOPVOTEMARGIN-27NOV03"],
    "NC Senate Margin":  ["KXMIDTERMMOV-NCSEND", "KXMIDTERMMOV-NCSENR"],
    "GA Senate Margin":  ["KXMIDTERMMOV-GASEND", "KXMIDTERMMOV-GASENR"],
}

STATES = {"NC": "north carolina", "GA": "georgia", "TX": "texas", "OH": "ohio", "MI": "michigan",
          "KS": "kansas", "NE": "nebraska", "IA": "iowa", "SC": "south carolina", "FL": "florida", "ME": "maine"}
ALL_STATES = ["alabama","alaska","arizona","arkansas","california","colorado","connecticut","delaware",
 "florida","georgia","hawaii","idaho","illinois","indiana","iowa","kansas","kentucky","louisiana","maine",
 "maryland","massachusetts","michigan","minnesota","mississippi","missouri","montana","nebraska","nevada",
 "new hampshire","new jersey","new mexico","new york","north carolina","north dakota","ohio","oklahoma",
 "oregon","pennsylvania","rhode island","south carolina","south dakota","tennessee","texas","utah",
 "vermont","virginia","washington","west virginia","wisconsin","wyoming"]

def row(label, kind, kw, ex=(), **kw2):
    return dict(label=label, kind=kind, kw=kw, ex=list(ex), **kw2)

NAT_EX = ALL_STATES
ODDS_EX = ["margin", "vote share", "seats", "popular vote"]
MARGIN_EX = ["seats", "turnout", "generic ballot"]
MAX_MARGIN = 100
CALL = 0.98   # an odds row at or above this for one side is treated as called

D_HOLDOVER = 34   # Dem Senate seats not up in 2026 (market counts seats won this cycle only)

def senate_margin(st):
    return row(f"{st} Senate Margin", "margin", [["senate"], [STATES[st]], ["margin", "vote"]], MARGIN_EX, signed=True)
def senate_odds(st, **kw):
    return row(f"{st} Senate", "odds", [["senate"], [STATES[st]]], ODDS_EX, **kw)
def gov_odds(st):
    return row(f"{st} Governor", "odds", [["governor", "gubernatorial"], [STATES[st]]], ODDS_EX)

SECTIONS = [
    ("NATIONAL", [
        row("House Vote Margin", "margin", [["house"], ["margin", "popular vote"]], NAT_EX + MARGIN_EX, signed=True),
        row("House Seats (D)", "seats", [["house"], ["seats"]], NAT_EX, majority=218, total=435),
        row("Senate Control", "odds", [["senate"], ["control", "win the senate", "which party"]], NAT_EX + ODDS_EX),
        row("Senate Seats (D)", "seats", [["senate"], ["seats"]], NAT_EX, majority=51, total=100, offset=D_HOLDOVER),
    ]),
    ("SENATE", [
        senate_margin("NC"), senate_margin("GA"),
        senate_odds("TX"), senate_odds("OH"), senate_odds("MI"), senate_odds("KS"),
        senate_odds("NE", ind="D"),
        senate_odds("IA"), senate_odds("SC"), senate_odds("FL"), senate_odds("ME"),
    ]),
    ("GOVERNOR", [gov_odds("TX"), gov_odds("OH"), gov_odds("FL")]),
]

# ---------- ANSI ----------
B, R_, D_, X = "\033[34;1m", "\033[31;1m", "\033[2m", "\033[0m"
BOLD, YEL, GRN = "\033[1m", "\033[33m", "\033[32m"

# ---------- HTTP ----------
def get(path, **params):
    url = f"{BASE}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "midterms-viewer/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.load(r)
        except Exception:
            if attempt == 2: raise
            time.sleep(0.5 * (attempt + 1))

def list_open_events():
    out, cursor = [], None
    while True:
        p = dict(status="open", limit=200)
        if cursor: p["cursor"] = cursor
        d = get("/events", **p)
        out += d.get("events", [])
        cursor = d.get("cursor")
        if not cursor: break
    return out

def fetch_event(ticker):
    d = get(f"/events/{ticker}", with_nested_markets="true")
    ms = d.get("markets") or d.get("event", {}).get("markets") or []
    return d.get("event", {}), ms

# ---------- ticker cache (prices are never cached) ----------
def sig(r):
    s = json.dumps([r["kw"], r["ex"], bool(r.get("signed"))], sort_keys=True)
    return hashlib.md5(s.encode()).hexdigest()[:10]

def load_cache():
    try: return json.load(open(CACHE))
    except Exception: return {}

def save_cache(c):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(c, open(CACHE, "w"), indent=1)

def matches(ev, r):
    t = (ev.get("title", "") + " " + ev.get("sub_title", "")).lower()
    return all(any(k in t for k in grp) for grp in r["kw"]) and not any(x in t for x in r["ex"])

def resolve(rows, discover=False):
    cache, now = load_cache(), time.time()

    def fresh(r):
        e = cache.get(r["label"])
        if not isinstance(e, dict) or e.get("sig") != sig(r) or now - e.get("ts", 0) > CACHE_TTL:
            return False
        return isinstance(e.get("v"), list) if r.get("signed") else isinstance(e.get("v"), str)

    need = rows if discover else [r for r in rows if r["label"] not in PIN and not fresh(r)]
    if need:
        evs = list_open_events()
        for r in need:
            cands = sorted((e for e in evs if matches(e, r)), key=lambda e: len(e.get("title", "")))
            if discover:
                print(f"\n{r['label']}")
                for e in cands[:8]: print(f"   {e['event_ticker']:<34} {e.get('title','')}")
                if not cands: print("   (no match)")
            elif cands:
                v = [e["event_ticker"] for e in cands[:4]] if r.get("signed") else cands[0]["event_ticker"]
                cache[r["label"]] = {"sig": sig(r), "ts": now, "v": v}
        if not discover: save_cache(cache)
    out = {l: e["v"] for l, e in cache.items() if isinstance(e, dict) and "v" in e}
    return {**out, **PIN}

# ---------- price helpers ----------
def f(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def px(m):
    """Market probability 0..1: mid of bid/ask, else last price."""
    def g(k):
        v = f(m.get(k + "_dollars"))
        if v is not None: return v
        v = f(m.get(k))
        return v / 100 if v is not None else None
    bid, ask, last = g("yes_bid"), g("yes_ask"), g("last_price")
    if bid is not None and ask is not None and (bid > 0 or ask > 0):
        return (bid + ask) / 2 if ask > 0 else bid
    if last: return last
    return bid or None

def live(markets):
    a = [m for m in markets if m.get("status") in ("active", "open", None)]
    return a or markets

# ---------- parsing: odds ----------
def party(m):
    t = " ".join(str(m.get(k, "")) for k in ("yes_sub_title", "subtitle", "custom_strike", "title")).lower()
    if "democrat" in t: return "D"
    if "republican" in t: return "R"
    return "O"

def parse_odds(markets):
    ps = {"D": None, "R": None}
    other = 0.0
    ms = live(markets)
    for m in ms:
        p, k = px(m), party(m)
        if p is None: continue
        if k in ps and ps[k] is None: ps[k] = p
        elif k == "O": other += p
    if len(ms) == 1:
        p = px(ms[0]); k = party(ms[0])
        if p is not None:
            ps = {"D": p, "R": 1 - p} if k != "R" else {"D": 1 - p, "R": p}
            other = 0
    return ps["D"], ps["R"], other

# ---------- parsing: seat ladders (unsigned) ----------
def expected(markets):
    pts = []
    for m in live(markets):
        p, st = px(m), m.get("strike_type")
        lo, hi = f(m.get("floor_strike")), f(m.get("cap_strike"))
        if p is None or st is None: continue
        pts.append((st, lo, hi, p))
    if not pts: return None
    between = [(lo, hi, p) for st, lo, hi, p in pts if st == "between" and lo is not None and hi is not None]
    if between:
        w = sorted(hi - lo for lo, hi, _ in between)[len(between) // 2] or 1
        bins = [((lo + hi) / 2, p) for lo, hi, p in between]
        for st, lo, hi, p in pts:
            if st in ("greater", "greater_or_equal") and lo is not None: bins.append((lo + w / 2, p))
            if st in ("less", "less_or_equal") and hi is not None: bins.append((hi - w / 2, p))
    else:
        gt = sorted((lo, p) for st, lo, hi, p in pts if st in ("greater", "greater_or_equal") and lo is not None)
        lt = sorted((hi, p) for st, lo, hi, p in pts if st in ("less", "less_or_equal") and hi is not None)
        if gt:
            ts = [t for t, _ in gt]; S = [p for _, p in gt]
            for i in range(1, len(S)): S[i] = min(S[i], S[i - 1])
            step = (ts[-1] - ts[0]) / (len(ts) - 1) if len(ts) > 1 else 1
            bins = [(ts[0] - step / 2, 1 - S[0])]
            bins += [((ts[i] + ts[i + 1]) / 2, S[i] - S[i + 1]) for i in range(len(ts) - 1)]
            bins.append((ts[-1] + step / 2, S[-1]))
        elif lt:
            ts = [t for t, _ in lt]; C = [p for _, p in lt]
            for i in range(1, len(C)): C[i] = max(C[i], C[i - 1])
            step = (ts[-1] - ts[0]) / (len(ts) - 1) if len(ts) > 1 else 1
            bins = [(ts[0] - step / 2, C[0])]
            bins += [((ts[i] + ts[i + 1]) / 2, C[i + 1] - C[i]) for i in range(len(ts) - 1)]
            bins.append((ts[-1] + step / 2, 1 - C[-1]))
        else:
            return None
    tot = sum(max(p, 0) for _, p in bins)
    if tot <= 0: return None
    return sum(x * max(p, 0) for x, p in bins) / tot

# ---------- parsing: signed (D/R) margin ladders ----------
STRIKE_RE = re.compile(r"^([DR])?([PM])?(\d+(?:\.\d+)?)$", re.I)

def ticker_strike(m):
    """Trailing ticker segment like P18 / D8 / M2.5 / 8 -> (side or None, strike or None)."""
    last = str(m.get("ticker", "")).split("-")[-1]
    mm = STRIKE_RE.match(last)
    if not mm: return None, None
    side = {"D": 1, "R": -1}.get((mm.group(1) or "").upper())
    x = float(mm.group(3))
    if (mm.group(2) or "").upper() == "M": x = -x
    return side, x

def _side_from_text(t):
    t = t.lower()
    has_d, has_r = "democrat" in t, "republican" in t
    if has_r and not has_d: return -1
    if has_d and not has_r: return 1
    return None

def event_side(ev, ticker):
    tk = ticker.upper()
    if tk.endswith("D"): return 1
    if tk.endswith("R"): return -1
    return _side_from_text(ev.get("title", "") + " " + ev.get("sub_title", ""))

def market_side(m, ev_side):
    """Market text > strike-segment letter > market-ticker party segment (...SEND-P18) > event > D."""
    t = " ".join(str(m.get(k, "")) for k in ("yes_sub_title", "subtitle", "title", "custom_strike"))
    s = _side_from_text(t)
    if s is not None: return s
    ts, _ = ticker_strike(m)
    if ts is not None: return ts
    parts = str(m.get("ticker", "")).split("-")
    if len(parts) >= 3 and STRIKE_RE.match(parts[-1]) and parts[-2][-1:].upper() in ("D", "R"):
        return 1 if parts[-2][-1].upper() == "D" else -1
    return ev_side if ev_side is not None else 1

def fetch_signed(tickers):
    tickers = list(dict.fromkeys(tickers))
    for t in list(tickers):                        # flipped D/R sibling for ...D / ...R event tickers
        if t[-1] in "DR":
            sib = t[:-1] + ("R" if t[-1] == "D" else "D")
            if sib not in tickers: tickers.append(sib)
    out, seen = [], set()
    for t in tickers:
        try: ev, ms = fetch_event(t)
        except Exception: continue
        es = event_side(ev, t)
        for m in ms:
            key = m.get("ticker") or id(m)
            if key in seen: continue
            seen.add(key)
            m["_side"] = market_side(m, es)
            out.append(m)
    return ({}, out) if out else None

def ladder_stats(pairs):
    """pairs: [(threshold, P(margin > threshold))] -> (median margin, P(margin > 0))"""
    merged = {}
    for t, p in pairs: merged.setdefault(round(t, 6), []).append(p)
    pairs = sorted((t, sum(v) / len(v)) for t, v in merged.items())
    if not pairs: return None, None
    ts = [t for t, _ in pairs]; S = [p for _, p in pairs]
    for i in range(1, len(S)): S[i] = min(S[i], S[i - 1])      # monotone survival function
    # median: where the survival function crosses 0.5 (linear interpolation)
    med = ts[-1] if S[-1] >= 0.5 else ts[0]
    for i in range(len(ts) - 1):
        if S[i] >= 0.5 >= S[i + 1]:
            w = (S[i] - 0.5) / ((S[i] - S[i + 1]) or 1)
            med = ts[i] + w * (ts[i + 1] - ts[i]); break
    # P(margin > 0)
    if ts[0] > 0:        p0 = 1.0
    elif ts[0] == 0:     p0 = S[0]
    elif ts[-1] < 0:     p0 = 0.0
    elif ts[-1] == 0:    p0 = S[-1]
    else:
        p0 = 0.5
        for i in range(len(ts) - 1):
            if ts[i] <= 0 <= ts[i + 1]:
                w = (0 - ts[i]) / ((ts[i + 1] - ts[i]) or 1)
                p0 = S[i] + w * (S[i + 1] - S[i]); break
    return med, p0

def bin_stats(bins):
    """bins: [(lo, hi, prob)] of the signed margin -> (median, P(margin > 0))"""
    bins = sorted(bins)
    tot = sum(max(p, 0) for _, _, p in bins)
    if tot <= 0: return None, None
    acc, med = 0.0, bins[-1][1]
    for lo, hi, p in bins:
        p = max(p, 0) / tot
        if acc + p >= 0.5 and p > 0:
            med = lo + (0.5 - acc) / p * (hi - lo); break
        acc += p
    p0 = 0.0
    for lo, hi, p in bins:
        p = max(p, 0)
        if lo >= 0: p0 += p
        elif hi > 0: p0 += p * hi / ((hi - lo) or 1)
    return med, p0 / tot

def signed_ladder(markets):
    pairs, bins = [], []
    for m in live(markets):
        p = px(m)
        if p is None: continue
        st = m.get("strike_type")
        lo, hi = f(m.get("floor_strike")), f(m.get("cap_strike"))
        _, tx = ticker_strike(m)
        s = m["_side"]
        if st == "between" and lo is not None and hi is not None:
            a, b = (lo, hi) if s > 0 else (-hi, -lo)
            bins.append((a, b, p)); continue
        if st in ("less", "less_or_equal"):
            x = hi if hi is not None else lo if lo is not None else tx
            if x is None or abs(x) > MAX_MARGIN: continue
            pairs.append((x, 1 - p) if s > 0 else (-x, p))
        else:  # greater / greater_or_equal / unknown: strike from fields, else from the ticker
            x = lo if lo is not None else tx
            if x is None or abs(x) > MAX_MARGIN: continue
            pairs.append((x, p) if s > 0 else (-x, 1 - p))
    if len(pairs) >= 2: return ladder_stats(pairs)
    if bins: return bin_stats(bins)
    return ladder_stats(pairs)

# ---------- render ----------
def bar(d, r, width=28):
    if d is None or r is None: return " " * width
    n = max(0, min(width, round(d / (d + r) * width))) if d + r > 0 else width // 2
    return f"{B}{'█' * n}{R_}{'█' * (width - n)}{X}"

def short_tag(ticker):
    ts = (ticker or "-").split()
    s = " ".join(ts[:2]) + (f" +{len(ts) - 2}" if len(ts) > 2 else "")
    return f"{D_}{s}{X}"

def render_row(r, ticker, data):
    label = f"{r['label']:<19}"
    tag = short_tag(ticker)
    if data is None:
        return f"  {label} {YEL}unresolved / fetch failed{X}  (try --raw){X}  {tag}"
    ev, ms = data
    k = r["kind"]

    if k == "odds":
        d, rr, o = parse_odds(ms)
        if d is None and rr is None: return f"  {label} {YEL}no prices{X}  {tag}"
        d = d if d is not None else max(0, 1 - (rr or 0) - o)
        rr = rr if rr is not None else max(0, 1 - d - o)
        note = ""
        if r.get("ind") == "D":
            d, o, note = d + o, 0, f" {D_}(incl. I){X}"
        tot = d + rr + o
        if tot > 0: d, rr, o = d / tot, rr / tot, o / tot
        oth = f" {D_}other {o:>4.0%}{X}" if o > 0.02 else ""
        called = B if d > CALL else R_ if rr > CALL else None
        if called:
            label = f"{called}{label}{X}"
            note += f" {called}CALLED{X}"
        return f"  {label} {B}D {d:>4.0%}{X} {bar(d, rr)} {R_}R {rr:>4.0%}{X}{note}{oth}  {tag}"

    if r.get("signed"):
        med, p0 = signed_ladder(ms)
        if med is None:
            return f"  {label} {YEL}no prices{X} {D_}({len(ms)} mkts; run --raw){X}  {tag}"
        c = B if med >= 0 else R_
        s = f"{'D' if med >= 0 else 'R'}+{abs(med):.1f}"
        return f"  {label} {c}{s:<8}{X} {D_}D win {p0:.0%}{X}  {tag}"

    mean = expected(ms)
    if mean is None: return f"  {label} {YEL}no prices{X}  {tag}"

    if k == "seats":
        maj, tot = r["majority"], r["total"]
        off = r.get("offset", 0)
        won = mean
        mean += off
        c = B if mean >= maj else R_
        extra = f"  ({won:.1f} won + {off} not up)" if off else ""
        return f"  {label} {c}D {mean:>5.1f}{X} {D_}/ R {tot - mean:>5.1f}   need {maj}{extra}{X}  {tag}"
    return ""

def dump_raw(ticker):
    try: ev, ms = fetch_event(ticker)
    except Exception as e:
        print(f"{ticker}: FETCH FAILED ({e})"); return
    print(f"\nevent: {ev.get('event_ticker')}  title: {ev.get('title')!r}  markets: {len(ms)}")
    for m in ms:
        print(f"  {str(m.get('ticker')):<38} {str(m.get('strike_type')):<16} "
              f"floor={m.get('floor_strike')} cap={m.get('cap_strike')} "
              f"bid={m.get('yes_bid_dollars', m.get('yes_bid'))} ask={m.get('yes_ask_dollars', m.get('yes_ask'))} "
              f"last={m.get('last_price_dollars', m.get('last_price'))} status={m.get('status')} "
              f"sub={m.get('yes_sub_title')!r}")

def main():
    rows = [r for _, rs in SECTIONS for r in rs]
    if "--raw" in sys.argv:
        args = [a for a in sys.argv[sys.argv.index("--raw") + 1:] if not a.startswith("--")]
        if not args:
            args = [t for v in PIN.values() for t in (v if isinstance(v, list) else [v])]
        for t in args: dump_raw(t)
        return
    if "--reset" in sys.argv:
        try: os.remove(CACHE)
        except FileNotFoundError: pass
        print("cache cleared"); return
    if "--discover" in sys.argv:
        resolve(rows, discover=True); return

    try: tickers = resolve(rows)
    except Exception as e:
        print(f"{R_}ticker resolution failed: {e}{X}"); return

    def work(r):
        t = tickers.get(r["label"])
        if not t: return r, None, None
        label = " ".join(t) if isinstance(t, list) else t
        try:
            if r.get("signed"):
                return r, label, fetch_signed([t] if isinstance(t, str) else t)
            return r, label, fetch_event(t)
        except Exception:
            return r, label, None

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = {r["label"]: (t, d) for r, t, d in ex.map(work, rows)}

    print(f"{BOLD}MIDTERMS 2026{X}  {D_}Kalshi · {datetime.now():%Y-%m-%d %H:%M:%S}{X}")
    for name, rs in SECTIONS:
        print(f"\n {BOLD}{name}{X}")
        for r in rs:
            t, d = results[r["label"]]
            print(render_row(r, t, d))
    print()

if __name__ == "__main__":
    main()