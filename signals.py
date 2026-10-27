"""25-60 signal scan, cash-secured put ideas and a two-stage DCF, all from Yahoo Finance data.

Every threshold lives in config.json under "signals" so it can be tuned without code changes.
"""

import html
import math
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SGT = timezone(timedelta(hours=8))

DEFAULT_RULES = {
    "sma_days": 25,             # 1, 6: moving average length
    "pullback_pct": 3.0,        # 2: minimum drop from the recent high
    "high_lookback_days": 20,   # 2: "recent high" = highest high over this many days
    "vol_short_days": 5,        # 3, 4: short volume average
    "vol_long_days": 60,        # 3, 5: long volume average
    "vol_ratio_max": 0.75,      # 3: short average at most this share of the long average
    "sma_rising_days": 5,       # 6: average must be higher than this many days ago
    "max_stretch_pct": 8.0,     # 6: close at most this far above the average
    "strong_close_min": 0.5,    # 6: close in the top half of the day's range
    "stop_pct": 10,             # trade plan text
    "hold_days": 10,            # trade plan text
}

DEFAULT_PUTS = {
    "target_days": 30,
    "min_days": 21,
    "max_days": 45,
    "delta_min": -0.20,
    "delta_max": -0.10,
    "max_rows": 4,
    "risk_free_rate": 0.04,
}

DEFAULT_DCF = {
    "discount_rate": 0.09,
    "terminal_growth": 0.03,
    "growth_cap": 0.20,
    "growth_floor": 0.0,
    "high_growth_years": 5,
    "fade_years": 5,
}

CONDITION_LABELS = [
    "Price above its 25-day average",
    "Pulled back at least 3% from a recent high",
    "Volume has dried up (5-day at most 75% of 60-day)",
    "5-day volume average turning up",
    "Bullish candle, not a heavy-volume drop",
    "Clean setup: 25-day average rising, price not stretched, strong close",
]

NO_DCF_SECTORS = {"Financial Services", "Real Estate"}


def _get(session, url, **params):
    try:
        return session.get(url, params=params, timeout=25).json()
    except (requests.RequestException, ValueError) as e:
        print(f"[signals] {url}: {e}", file=sys.stderr)
        return None


# ---------- Price history & the 6 conditions ----------

def daily_bars(session, ticker):
    """Completed daily bars (today's bar is dropped while the market is still open)."""
    data = _get(session, f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                range="1y", interval="1d")
    try:
        res = data["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        ts = res["timestamp"]
    except (TypeError, KeyError, IndexError):
        return None
    rows = [(t, o, h, l, c, v) for t, o, h, l, c, v in
            zip(ts, q["open"], q["high"], q["low"], q["close"], q["volume"])
            if None not in (o, h, l, c, v)]
    period = res["meta"].get("currentTradingPeriod", {}).get("regular", {})
    now = time.time()
    if rows and period and period.get("start", 0) <= now < period.get("end", 0) and rows[-1][0] >= period["start"]:
        rows = rows[:-1]
    if not rows:
        return None
    t, o, h, l, c, v = map(list, zip(*rows))
    return {"ticker": ticker, "ts": t, "open": o, "high": h, "low": l, "close": c, "volume": v,
            "currency": res["meta"].get("currency", ""), "tz_offset": res["meta"].get("gmtoffset", 0)}


def _sma(values, n, end=None):
    end = len(values) if end is None else end
    window = values[end - n:end]
    return sum(window) / n if len(window) == n else None


def evaluate(bars, rules):
    r = {**DEFAULT_RULES, **rules}
    c, h, l, o, v = bars["close"], bars["high"], bars["low"], bars["open"], bars["volume"]
    need = max(r["vol_long_days"] + 1, r["sma_days"] + r["sma_rising_days"], r["high_lookback_days"])
    if len(c) < need:
        return None
    close = c[-1]
    sma = _sma(c, r["sma_days"])
    sma_before = _sma(c, r["sma_days"], len(c) - r["sma_rising_days"])
    recent_high = max(h[-r["high_lookback_days"]:])
    pullback = (1 - close / recent_high) * 100
    vol_s, vol_s_prev = _sma(v, r["vol_short_days"]), _sma(v, r["vol_short_days"], len(v) - 1)
    vol_l = _sma(v, r["vol_long_days"])
    stretch = (close / sma - 1) * 100
    day_range = h[-1] - l[-1]
    close_pos = (close - l[-1]) / day_range if day_range > 0 else 0.5
    heavy_drop = close < c[-2] and v[-1] > vol_l

    checks = [
        (close > sma, f"close {close:,.2f} vs avg {sma:,.2f}"),
        (pullback >= r["pullback_pct"], f"{pullback:.1f}% below {r['high_lookback_days']}-day high {recent_high:,.2f}"),
        (vol_s <= r["vol_ratio_max"] * vol_l, f"5-day vol is {vol_s / vol_l * 100:.0f}% of 60-day"),
        (vol_s > vol_s_prev, f"5-day vol {'up' if vol_s > vol_s_prev else 'down'} {abs(vol_s / vol_s_prev - 1) * 100:.1f}% vs yesterday"),
        (close > o[-1] and not heavy_drop,
         ("green candle" if close > o[-1] else "red candle") + (", heavy-volume drop" if heavy_drop else "")),
        (sma > sma_before and stretch <= r["max_stretch_pct"] and close_pos >= r["strong_close_min"],
         f"avg {'rising' if sma > sma_before else 'falling'}, {stretch:+.1f}% from avg, closed at {close_pos * 100:.0f}% of range"),
    ]
    date = datetime.fromtimestamp(bars["ts"][-1] + bars["tz_offset"], timezone.utc).strftime("%d %b %Y")
    return {"ticker": bars["ticker"], "close": close, "date": date, "currency": bars["currency"],
            "checks": checks, "met": sum(ok for ok, _ in checks)}


# ---------- Cash-secured puts ----------

def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _d1(spot, strike, years, iv, rate):
    return (math.log(spot / strike) + (rate + iv * iv / 2) * years) / (iv * math.sqrt(years))


def put_price(spot, strike, years, iv, rate):
    d1 = _d1(spot, strike, years, iv, rate)
    d2 = d1 - iv * math.sqrt(years)
    return strike * math.exp(-rate * years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(premium, spot, strike, years, rate):
    """Volatility that reproduces the option's price. Yahoo's own IV is a placeholder outside market hours."""
    lo, hi = 0.01, 5.0
    if not put_price(spot, strike, years, lo, rate) < premium < put_price(spot, strike, years, hi, rate):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if put_price(spot, strike, years, mid, rate) < premium else (lo, mid)
    return (lo + hi) / 2


def put_delta(spot, strike, years, iv, rate):
    if not iv or iv <= 0 or years <= 0:
        return None
    return _norm_cdf(_d1(spot, strike, years, iv, rate)) - 1


def put_ideas(session, crumb, ticker, spot, settings):
    p = {**DEFAULT_PUTS, **settings}
    base = f"https://query2.finance.yahoo.com/v7/finance/options/{ticker}"
    data = _get(session, base, crumb=crumb)
    try:
        expiries = data["optionChain"]["result"][0]["expirationDates"]
    except (TypeError, KeyError, IndexError):
        return {"error": "no listed options on Yahoo"}
    now = time.time()
    candidates = [e for e in expiries if p["min_days"] <= (e - now) / 86400 <= p["max_days"]]
    if not candidates:
        return {"error": f"no expiry {p['min_days']}-{p['max_days']} days out"}
    expiry = min(candidates, key=lambda e: abs((e - now) / 86400 - p["target_days"]))
    data = _get(session, base, crumb=crumb, date=expiry)
    try:
        puts = data["optionChain"]["result"][0]["options"][0]["puts"]
    except (TypeError, KeyError, IndexError):
        return {"error": "option chain unavailable"}
    days = max(1, round((expiry - now) / 86400))
    rows = []
    years = (expiry - now) / 86400 / 365
    for o in puts:
        strike = o.get("strike")
        if not strike or strike >= spot:
            continue
        bid, ask = o.get("bid") or 0, o.get("ask") or 0
        live = bid > 0 and ask >= bid
        premium = (bid + ask) / 2 if live else (o.get("lastPrice") or 0)
        if premium <= 0:
            continue
        iv = implied_vol(premium, spot, strike, years, p["risk_free_rate"])
        delta = put_delta(spot, strike, years, iv, p["risk_free_rate"])
        if delta is None or not p["delta_min"] <= delta <= p["delta_max"]:
            continue
        yld = premium / strike
        rows.append({"strike": strike, "delta": delta, "bid": bid, "ask": ask, "premium": premium, "live": live,
                     "yield": yld * 100, "annual": yld * 365 / days * 100, "breakeven": strike - premium,
                     "below": (1 - strike / spot) * 100})
    rows.sort(key=lambda r: -r["strike"])
    return {"expiry": expiry, "days": days, "spot": spot, "rows": rows[:p["max_rows"]]}


# ---------- DCF ----------

def dcf(session, crumb, ticker, settings):
    d = {**DEFAULT_DCF, **settings}
    summary = _get(session, f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                   modules="financialData,earningsTrend,assetProfile,price", crumb=crumb)
    try:
        res = summary["quoteSummary"]["result"][0]
    except (TypeError, KeyError, IndexError):
        return {"error": "no fundamentals on Yahoo"}
    sector = res.get("assetProfile", {}).get("sector", "")
    price = res.get("financialData", {}).get("currentPrice", {}).get("raw") \
        or res.get("price", {}).get("regularMarketPrice", {}).get("raw")
    currency = res.get("price", {}).get("currency", "")
    market_cap = res.get("price", {}).get("marketCap", {}).get("raw")
    fin_currency = res.get("financialData", {}).get("financialCurrency") or currency
    if sector in NO_DCF_SECTORS:
        return {"error": f"DCF not meaningful for {sector.lower()} companies", "price": price, "currency": currency}

    types = "trailingFreeCashFlow,annualFreeCashFlow,annualDilutedAverageShares," \
            "annualCashCashEquivalentsAndShortTermInvestments,annualTotalDebt"
    ts = _get(session, f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{ticker}",
              type=types, period1=int(time.time()) - 4 * 365 * 86400, period2=int(time.time()), crumb=crumb)
    latest = {}
    try:
        for item in ts["timeseries"]["result"]:
            key = item["meta"]["type"][0]
            vals = [x for x in item.get(key, []) if x]
            if vals:
                latest[key] = vals[-1]["reportedValue"]["raw"]
    except (TypeError, KeyError):
        return {"error": "no cash-flow data on Yahoo"}
    fcf = latest.get("trailingFreeCashFlow") or latest.get("annualFreeCashFlow")
    shares = latest.get("annualDilutedAverageShares")
    if not fcf or not shares or not price:
        return {"error": "missing free cash flow, share count or price"}
    if fcf <= 0:
        return {"error": "free cash flow is negative, DCF not meaningful", "price": price, "currency": currency}

    trend = {t["period"]: t.get("growth", {}).get("raw") for t in res.get("earningsTrend", {}).get("trend", [])}
    raw_growth = trend.get("+1y") if trend.get("+1y") is not None else trend.get("0y")
    growth = min(max(raw_growth if raw_growth is not None else d["terminal_growth"], d["growth_floor"]), d["growth_cap"])

    pv, cash_flow = 0.0, fcf
    years = d["high_growth_years"] + d["fade_years"]
    for year in range(1, years + 1):
        if year <= d["high_growth_years"]:
            g = growth
        else:
            step = (year - d["high_growth_years"]) / d["fade_years"]
            g = growth + (d["terminal_growth"] - growth) * step
        cash_flow *= 1 + g
        pv += cash_flow / (1 + d["discount_rate"]) ** year
    terminal = cash_flow * (1 + d["terminal_growth"]) / (d["discount_rate"] - d["terminal_growth"])
    pv += terminal / (1 + d["discount_rate"]) ** years
    equity = pv + latest.get("annualCashCashEquivalentsAndShortTermInvestments", 0) - latest.get("annualTotalDebt", 0)
    fx = 1.0
    if fin_currency and currency and fin_currency != currency:
        fx_data = _get(session, f"https://query1.finance.yahoo.com/v8/finance/chart/{fin_currency}{currency}=X",
                       range="1d", interval="1d")
        try:
            fx = fx_data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        except (TypeError, KeyError, IndexError):
            return {"error": f"couldn't convert {fin_currency} accounts to {currency}"}
    if equity <= 0:
        return {"error": "debt exceeds the value of projected cash flows", "price": price, "currency": currency}
    # Scaling by market cap handles ADRs and share classes without needing share ratios.
    value = price * equity * fx / market_cap if market_cap else equity * fx / shares
    return {"value": value, "price": price, "currency": currency, "safety": (1 - price / value) * 100,
            "pct_of_value": price / value * 100, "growth": growth, "raw_growth": raw_growth,
            "fcf": fcf, "fin_currency": fin_currency, "shares": shares, "settings": d}


# ---------- Formatting ----------

def fmt_check_lines(result):
    lines = []
    for i, ((ok, detail), label) in enumerate(zip(result["checks"], CONDITION_LABELS), 1):
        lines.append(f"{'✓' if ok else '✗'} {i}. {label}\n      <i>{html.escape(detail)}</i>")
    return "\n".join(lines)


def status_tag(result):
    if result["met"] == 6:
        return "TRIGGER"
    if result["met"] == 5:
        return "WATCH (5 of 6)"
    return f"{result['met']} of 6"


def fmt_trade_plan(rules):
    r = {**DEFAULT_RULES, **rules}
    return (f"Strategy rule: buy at next US open. Stop {r['stop_pct']}% below your entry. "
            f"Sell at close of trading day {r['hold_days']}.")


def fmt_puts(ticker, ideas, earnings_ts=None):
    if "error" in ideas:
        return f"<b>Put ideas {html.escape(ticker)}</b>: {html.escape(ideas['error'])}"
    expiry = datetime.fromtimestamp(ideas["expiry"], timezone.utc).strftime("%d %b %Y")
    head = (f"<b>Cash-secured put ideas: {html.escape(ticker)}</b>\n"
            f"Spot {ideas['spot']:,.2f} | expiry {expiry} | {ideas['days']} days")
    if not ideas["rows"]:
        return head + "\nNo strikes in the delta range right now."
    lines = ["<pre>Strike   Delta  Bid/Ask       Yield  /Year  B/even   Below"]
    any_stale = False
    for r in ideas["rows"]:
        quote = f"{r['bid']:.2f}/{r['ask']:.2f}" if r["live"] else f"last {r['premium']:.2f}*"
        any_stale |= not r["live"]
        lines.append(f"{r['strike']:<8.2f} {r['delta']:+.2f}  {quote:<12}  {r['yield']:4.2f}%  "
                     f"{r['annual']:4.1f}%  {r['breakeven']:<7.2f}  {r['below']:.1f}%")
    lines[-1] += "</pre>"
    notes = []
    notes.append("Yield and breakeven use the bid/ask midpoint; delta is calculated from that price.")
    if any_stale:
        notes.append("* market closed or no bid: the last traded price is used instead.")
    if earnings_ts and time.time() < earnings_ts <= ideas["expiry"] + 86400:
        notes.append(f"Earnings on {datetime.fromtimestamp(earnings_ts, SGT).strftime('%d %b')}, before this expiry.")
    notes.append("Quotes from Yahoo (about 15 min delayed). Puts can be assigned early; only sell where "
                 "you are happy to buy the shares at the strike with cash set aside.")
    return head + "\n" + "\n".join(lines) + "\n<i>" + html.escape(" ".join(notes)) + "</i>"


def fmt_dcf(ticker, v, detail=False):
    if "error" in v:
        return f"<b>Valuation {html.escape(ticker)}</b>: {html.escape(v['error'])}"
    label = "Undervalued" if v["safety"] > 0 else "Overvalued"
    text = (f"<b>Valuation {html.escape(ticker)}: {label}</b>\n"
            f"Value {v['currency']} {v['value']:,.2f} | Price {v['currency']} {v['price']:,.2f} | "
            f"Safety {v['safety']:+.0f}%\nPrice is {v['pct_of_value']:.0f}% of intrinsic value")
    if detail:
        s = v["settings"]
        raw = f"{v['raw_growth'] * 100:.1f}%" if v["raw_growth"] is not None else "n/a"
        text += (f"\n<i>Two-stage DCF on trailing free cash flow {v['fin_currency']} {v['fcf'] / 1e9:,.1f}B. "
                 f"Growth {v['growth'] * 100:.1f}% (analyst next-year EPS growth {raw}, capped at "
                 f"{s['growth_cap'] * 100:.0f}%) for {s['high_growth_years']} years, fading to "
                 f"{s['terminal_growth'] * 100:.0f}% over {s['fade_years']}; discount rate "
                 f"{s['discount_rate'] * 100:.0f}%; plus cash, minus debt.</i>")
    return text
