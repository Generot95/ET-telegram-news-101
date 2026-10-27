"""Renders the signal board and stock cards as PNG images (HTML drawn by headless Chrome)."""

import html
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

import signals

SGT = timezone(timedelta(hours=8))
PAGE_WIDTH = 1100
CARDS_PER_PAGE = 6
BG = (11, 17, 23)

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0b1117; color: #e6edf3; width: %dpx; padding: 28px; overflow: hidden;
       font-family: "Segoe UI", Roboto, "Helvetica Neue", "DejaVu Sans", Arial, sans-serif; }
.mono { font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; }
.muted { color: #8b98a5; }
h1 { font-size: 30px; font-weight: 700; }
.head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 20px; }
.banner { border: 2px solid #3fb97a; background: #133826; border-radius: 14px; padding: 18px 22px; margin-bottom: 22px; }
.banner.none { border-color: #30363d; background: #161d25; }
.banner b { font-size: 30px; display: block; }
.section { font-size: 14px; letter-spacing: 2px; color: #8b98a5; font-weight: 700; margin: 22px 0 12px; }
.grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.card table { font-size: 12px; } .card th { font-size: 10px; padding: 4px 3px; } .card td { padding: 5px 3px; }
.card { background: #141c24; border: 1px solid #263240; border-radius: 14px; padding: 16px; }
.card.trigger { border: 2px solid #3fb97a; }
.card.watch { border: 2px solid #d29922; }
.row { display: flex; justify-content: space-between; align-items: center; }
.ticker { font-size: 24px; font-weight: 700; }
.name { font-size: 13px; color: #8b98a5; margin: 2px 0 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pill { font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 999px; letter-spacing: 1px; }
.pill.trigger { background: #3fb97a; color: #0b1117; }
.pill.watch { background: #d29922; color: #0b1117; }
.pill.other { background: #263240; color: #c9d1d9; }
.pill.under { background: #133826; color: #56d38a; }
.pill.over { background: #3d1d20; color: #f47067; }
.pill.na { background: #263240; color: #8b98a5; }
.close { font-size: 15px; margin-bottom: 10px; }
.cond { display: flex; gap: 8px; font-size: 13px; line-height: 1.3; margin: 5px 0; }
.cond .mark { width: 18px; font-weight: 700; flex: none; }
.ok { color: #56d38a; } .no { color: #f47067; }
.cond .detail { color: #8b98a5; font-size: 12px; }
.boxes { display: grid; grid-template-columns: 1fr 1fr 0.8fr; gap: 6px; margin: 10px 0 8px; }
.box { background: #0f161d; border: 1px solid #263240; border-radius: 8px; padding: 6px 8px; }
.box small { display: block; font-size: 10px; color: #8b98a5; letter-spacing: 1px; }
.box b { font-size: 14px; }
.bar { position: relative; height: 6px; background: #263240; border-radius: 3px; margin: 6px 0 4px; }
.bar .fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 3px; }
.bar .tick { position: absolute; left: 50%%; top: -3px; bottom: -3px; width: 2px; background: #8b98a5; }
.small { font-size: 12px; color: #8b98a5; }
table { width: 100%%; border-collapse: collapse; font-size: 13px; margin-top: 6px; }
th { text-align: right; color: #8b98a5; font-weight: 600; font-size: 11px; letter-spacing: 1px; padding: 4px 6px; border-bottom: 1px solid #263240; }
td { text-align: right; padding: 6px; border-bottom: 1px solid #1c2630; }
th:first-child, td:first-child { text-align: left; }
.big table { font-size: 18px; } .big th { font-size: 13px; } .big td { padding: 10px 8px; }
.tags { display: flex; gap: 6px; margin-bottom: 4px; }
.tag { font-size: 11px; color: #c9d1d9; background: #1c2630; border: 1px solid #263240; border-radius: 6px; padding: 2px 8px; }
table.matrix { font-size: 15px; }
table.matrix td, table.matrix th { text-align: center; }
table.matrix td:first-child, table.matrix th:first-child { text-align: left; }
table.matrix td:nth-child(2), table.matrix th:nth-child(2) { text-align: right; }
table.matrix td.ok, table.matrix td.no { font-weight: 700; font-size: 17px; }
.legend { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 24px; font-size: 14px; color: #c9d1d9; }
.legend b { color: #e6edf3; margin-right: 8px; }
.footer { margin-top: 18px; font-size: 12px; color: #6e7b88; }
""" % PAGE_WIDTH

SHORT_LABELS = [
    "Above 25-day average",
    "Pulled back 3%+ from high",
    "Volume dried up (5d \u2264 75% of 60d)",
    "5-day volume turning up",
    "Bullish candle, no heavy drop",
    "Clean setup (rising, not stretched, strong close)",
]


def e(text):
    return html.escape(str(text))


def _status(result):
    if not result:
        return "other", "NO DATA"
    if result["met"] == 6:
        return "trigger", "TRIGGER"
    if result["met"] == 5:
        return "watch", "WATCH 5/6"
    return "other", f"{result['met']}/6"


def _money(v, ccy=""):
    return f"{ccy + ' ' if ccy else ''}{v:,.2f}"


def _puts_table(ideas, rows=None, big=False):
    if not ideas or "error" in ideas:
        return f"<div class='small'>Put ideas: {e(ideas.get('error', 'unavailable') if ideas else 'unavailable')}</div>"
    expiry = datetime.fromtimestamp(ideas["expiry"], timezone.utc).strftime("%d %b %Y")
    data = ideas["rows"][:rows] if rows else ideas["rows"]
    if not data:
        return f"<div class='small'>Puts {expiry}: no strikes in the delta range</div>"
    head = f"<div class='small'>Spot {ideas['spot']:,.2f} &middot; expiry {expiry} &middot; {ideas['days']} days</div>"
    if big:
        def quote(r):
            return f"{r['bid']:.2f} / {r['ask']:.2f}" if r["live"] else f"last {r['premium']:.2f}*"
        body = "".join(
            f"<tr class='mono'><td>{r['strike']:.2f}</td><td>{r['delta']:+.2f}</td><td>{quote(r)}</td>"
            f"<td>{r['yield']:.2f}%</td><td>{r['annual']:.1f}%</td><td>{r['breakeven']:.2f}</td><td>{r['below']:.1f}%</td></tr>"
            for r in data)
        return (f"<div class='big'>{head}<table><tr><th>STRIKE</th><th>DELTA</th><th>BID / ASK</th>"
                f"<th>YIELD</th><th>PER YEAR</th><th>BREAKEVEN</th><th>BELOW SPOT</th></tr>{body}</table></div>")
    body = "".join(
        f"<tr class='mono'><td>{r['strike']:.2f}</td><td>{r['delta']:+.2f}</td>"
        f"<td>{r['premium']:.2f}{'' if r['live'] else '*'}</td><td>{r['yield']:.2f}%</td>"
        f"<td>{r['annual']:.1f}%</td><td>{r['breakeven']:.2f}</td><td>{r['below']:.1f}%</td></tr>"
        for r in data)
    return (f"{head}<table><tr><th>STRIKE</th><th>DELTA</th><th>PREM</th><th>YIELD</th><th>/YR</th>"
            f"<th>B/EVEN</th><th>BELOW</th></tr>{body}</table>")


def _valuation_block(val):
    if not val or "error" in val:
        return (f"<div class='row' style='margin-top:10px'><span class='small'>Valuation</span>"
                f"<span class='pill na'>N/A</span></div><div class='small'>{e(val.get('error', '') if val else '')}</div>")
    under = val["safety"] > 0
    pct = val["pct_of_value"]
    fill = min(pct, 200) / 2
    colour = "#3fb97a" if under else "#f0883e"
    return (f"<div class='row' style='margin-top:10px'><span class='small'>DCF valuation</span>"
            f"<span class='pill {'under' if under else 'over'}'>{'Undervalued' if under else 'Overvalued'}</span></div>"
            f"<div class='boxes'><div class='box'><small>VALUE</small><b>{_money(val['value'])}</b></div>"
            f"<div class='box'><small>PRICE</small><b>{_money(val['price'])}</b></div>"
            f"<div class='box'><small>SAFETY</small><b class='{'ok' if under else 'no'}'>{val['safety']:+.0f}%</b></div></div>"
            f"<div class='bar'><div class='fill' style='width:{fill:.0f}%;background:{colour}'></div><div class='tick'></div></div>"
            f"<div class='small'>Price is {pct:.0f}% of intrinsic value</div>")


def stock_card(info):
    result = info.get("result")
    cls, tag = _status(result)
    parts = [f"<div class='card {cls}'><div class='row'><span class='ticker'>{e(info['ticker'])}</span>"
             f"<span class='pill {cls}'>{tag}</span></div><div class='name'>{e(info.get('name', ''))}</div>"]
    if result:
        parts.append(f"<div class='close mono'>Close {_money(result['close'], result['currency'])} "
                     f"<span class='muted'>&middot; {e(result['date'])}</span></div>")
        for (ok, detail), label in zip(result["checks"], SHORT_LABELS):
            parts.append(f"<div class='cond'><span class='mark {'ok' if ok else 'no'}'>{'\u2713' if ok else '\u2717'}</span>"
                         f"<span>{e(label)}<br><span class='detail'>{e(detail)}</span></span></div>")
    parts.append(_valuation_block(info.get("val")))
    ideas = info.get("puts")
    parts.append("<div style='margin-top:10px' class='small'>Cash-secured puts</div>" + _puts_table(ideas, rows=2))
    if info.get("earnings"):
        parts.append(f"<div class='small' style='margin-top:6px'>Next earnings "
                     f"{datetime.fromtimestamp(info['earnings'], SGT).strftime('%d %b %Y')}</div>")
    parts.append("</div>")
    return "".join(parts)


def _page(body):
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}</body></html>"


def board_pages(infos, rules):
    """HTML pages: the board summary, then stock cards CARDS_PER_PAGE at a time."""
    scanned = [i for i in infos if i.get("result")]
    date = max((i["result"]["date"] for i in scanned), key=lambda d: datetime.strptime(d, "%d %b %Y"), default="n/a")
    triggered = [i for i in scanned if i["result"]["met"] == 6]
    watch = [i for i in scanned if i["result"]["met"] == 5]
    plan = signals.fmt_trade_plan(rules)

    body = [f"<div class='head'><h1>25-60 Signal Board</h1><span class='muted mono'>Data through {e(date)} "
            f"&middot; scanned {len(scanned)} stocks</span></div>"]
    if triggered:
        body.append(f"<div class='banner'><b>{len(triggered)} trigger{'s' if len(triggered) > 1 else ''} today</b>"
                    f"Triggered: {e(', '.join(i['ticker'] for i in triggered))}</div>")
        body.append("<div class='section'>TRIGGERED TODAY</div><div class='grid2'>")
        for i in triggered:
            body.append(f"<div class='card trigger'><div class='row'><span class='ticker'>{e(i['ticker'])}</span>"
                        f"<span class='pill trigger'>TRIGGER</span></div><div class='close mono muted' style='margin-top:8px'>"
                        f"Close {i['result']['close']:,.2f}</div><div>{e(plan)}</div></div>")
        body.append("</div>")
    else:
        body.append("<div class='banner none'><b>No triggers today</b>None of the scanned stocks met all 6 conditions.</div>")

    put_names = triggered + watch
    if put_names:
        title = "CASH-SECURED PUT IDEAS FOR TODAY'S TRIGGERS" + (" AND WATCH LIST" if watch else "")
        body.append(f"<div class='section'>{title}</div>")
        sources = set()
        for i in put_names:
            ideas = i.get("puts") or {}
            sources.add(ideas.get("source", "Yahoo"))
            is_trigger = i in triggered
            body.append(f"<div class='card' style='margin-bottom:12px'><div class='row'><span class='ticker' style='font-size:20px'>"
                        f"{e(i['ticker'])}</span><span class='pill {'trigger' if is_trigger else 'watch'}'>"
                        f"{'TRIGGER' if is_trigger else 'WATCH 5/6'}</span></div>{_puts_table(ideas, big=True)}</div>")
        body.append(f"<div class='small'>Ideas come from {e(' / '.join(sorted(sources)))} delayed quotes (about 15 minutes) "
                    "for expiries about 30 days out; yield uses the bid/ask midpoint, * = last trade. Check the earnings date "
                    "before selling. Puts can be assigned early, so only sell where you are happy to buy the shares at the "
                    "strike with cash set aside.</div>")

    if watch:
        body.append("<div class='section'>WATCH FOR TOMORROW (5 OF 6 CONDITIONS)</div><div class='card'>")
        for i in watch:
            missing = next(n for n, (ok, _) in enumerate(i["result"]["checks"], 1) if not ok)
            body.append(f"<div class='cond'><span class='mark' style='width:70px'>{e(i['ticker'])}</span>"
                        f"<span>missing #{missing}: {e(signals.CONDITION_LABELS[missing - 1])}</span></div>")
        body.append("</div>")

    body.append("<div class='section'>ALL STOCKS: CONDITIONS 1-6</div><div class='card'><table class='matrix'>"
                "<tr><th>STOCK</th><th>CLOSE</th>" + "".join(f"<th>{n}</th>" for n in range(1, 7)) + "<th>SCORE</th></tr>")
    for i in sorted(scanned, key=lambda i: -i["result"]["met"]):
        r = i["result"]
        cls, tag = _status(r)
        marks = "".join(f"<td class='{'ok' if ok else 'no'}'>{'&#10003;' if ok else '&#10007;'}</td>" for ok, _ in r["checks"])
        body.append(f"<tr><td><b>{e(i['ticker'])}</b></td><td class='mono'>{r['close']:,.2f}</td>{marks}"
                    f"<td><span class='pill {cls}'>{tag}</span></td></tr>")
    body.append("</table></div>")

    body.append("<div class='section'>THE 6 CONDITIONS</div><div class='card legend'>"
                + "".join(f"<div><b>{n}</b>{e(label)}</div>" for n, label in enumerate(signals.CONDITION_LABELS, 1))
                + "</div>")
    body.append(f"<div class='footer'>Generated {datetime.now(SGT).strftime('%d %b %Y %H:%M')} SGT &middot; "
                "rules-based scan, not investment advice</div>")
    return [_page("".join(body)), valuation_page(infos)]


def valuation_card(info):
    val = info.get("val") or {}
    sector = val.get("sector") or ""
    tags = "<span class='tag'>DCF</span>" + (f"<span class='tag'>{e(sector)}</span>" if sector else "")
    result = info.get("result")
    signal = f"<div class='small'>25-60 signal {result['met']}/6</div>" if result else ""
    if "error" in val or not val:
        price = f"{val['currency']} {val['price']:,.2f}" if val.get("price") else "n/a"
        return (f"<div class='card'><div class='row'><span class='ticker'>{e(info['ticker'])}</span>"
                f"<span class='pill na'>N/A</span></div><div class='name'>{e(info.get('name', ''))}</div>"
                f"<div class='tags'>{tags}</div><div class='boxes'><div class='box'><small>VALUE</small><b>n/a</b></div>"
                f"<div class='box'><small>PRICE</small><b>{e(price)}</b></div><div class='box'><small>SAFETY</small><b>n/a</b></div></div>"
                f"<div class='small'>{e(val.get('error', 'no valuation data'))}</div>{signal}</div>")
    under = val["safety"] > 0
    ccy = val["currency"]
    fill = min(val["pct_of_value"], 200) / 2
    colour = "linear-gradient(90deg,#2ea86b,#56d38a)" if under else "linear-gradient(90deg,#f0883e,#f47067)"
    price_date = result["date"] if result else ""
    as_of = f" &middot; price as of {e(price_date)}" if price_date else ""
    return (f"<div class='card'><div class='row'><span class='ticker'>{e(info['ticker'])}</span>"
            f"<span class='pill {'under' if under else 'over'}'>{'Undervalued' if under else 'Overvalued'}</span></div>"
            f"<div class='name'>{e(info.get('name', ''))}</div><div class='tags'>{tags}</div>"
            f"<div class='boxes'><div class='box'><small>VALUE</small><b>{e(ccy)} {val['value']:,.2f}</b></div>"
            f"<div class='box'><small>PRICE</small><b>{e(ccy)} {val['price']:,.2f}</b></div>"
            f"<div class='box'><small>SAFETY</small><b class='{'ok' if under else 'no'}'>{val['safety']:.0f}%</b></div></div>"
            f"<div class='bar'><div class='fill' style='width:{fill:.0f}%;background:{colour}'></div><div class='tick'></div></div>"
            f"<div class='small' style='margin-top:6px'>Price is {val['pct_of_value']:.0f}% of intrinsic value</div>"
            f"<div class='small'>Growth {val['growth'] * 100:.0f}% &middot; discount "
            f"{val['settings']['discount_rate'] * 100:.0f}%{as_of}</div>{signal}</div>")


def valuation_page(infos):
    def order(i):
        val = i.get("val") or {}
        return 1e9 if "error" in val or not val else -val["safety"]
    cards = "".join(valuation_card(i) for i in sorted(infos, key=order))
    return _page(f"<div class='head'><h1>Valuations</h1><span class='muted mono'>two-stage DCF &middot; "
                 f"{len(infos)} stocks</span></div><div class='grid'>{cards}</div>"
                 "<div class='footer'>DCF: trailing free cash flow grown at analysts' next-year EPS growth (capped at 20%) "
                 "for 5 years, fading to 3% over 5 more, discounted at 9%, plus cash minus debt. Not meaningful for "
                 "banks, insurers and REITs.</div>")


def single_card_page(info):
    return _page(f"<div style='width:{PAGE_WIDTH // 3 + 60}px'>{stock_card(info)}</div>")


def find_chrome():
    for candidate in (os.getenv("CHROME_BIN"), "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                      r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if candidate and (shutil.which(candidate) or Path(candidate).exists()):
            return shutil.which(candidate) or candidate
    return None


def html_to_png(page_html, out_path, width=PAGE_WIDTH, height=4000):
    """Screenshot the page with headless Chrome, then trim the empty background at the bottom."""
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("Chrome not found")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "page.html")
        src.write_text(page_html, encoding="utf-8")
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                        "--force-device-scale-factor=2", f"--window-size={width},{height}",
                        f"--user-data-dir={Path(tmp, 'profile')}", f"--screenshot={out_path}", src.as_uri()],
                       check=True, capture_output=True, timeout=90)
    img = Image.open(out_path).convert("RGB")
    w, h = img.size
    pixels = img.load()
    bottom = h - 1
    while bottom > 0 and all(pixels[x, bottom] == BG for x in range(0, w, 7)):
        bottom -= 1
    right = w - 1
    while right > 0 and all(pixels[right, y] == BG for y in range(0, bottom, 7)):
        right -= 1
    img.crop((0, 0, min(w, right + 50), min(h, bottom + 50))).save(out_path, optimize=True)
    return out_path
