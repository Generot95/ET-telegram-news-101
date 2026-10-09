"""Telegram news bot that posts categorised news into a forum group's topics. Runs on GitHub Actions.

Each run: answers pending Telegram commands, checks every category's RSS feeds every --interval
seconds for --minutes, posts new items matching the category's keywords into that category's
topic (with watchlist prices attached), sends earnings reminders and the daily brief when due,
then saves config.json/state.json (the workflow commits them back for the next run).

Env:
    TELEGRAM_BOT_TOKEN  - from @BotFather
    TELEGRAM_CHAT_ID    - the group's chat ID (starts with -100); the group must have Topics enabled
                          and the bot must be an admin with "Manage Topics"
"""

import argparse
import calendar
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
SEEN_LIMIT = 5000
USER_AGENT = "Mozilla/5.0 (TelegramNewsBot)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
TOPIC_COLORS = [0xFF93B2, 0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFB6F5F]
SGT = timezone(timedelta(hours=8))
BRIEF_TOPIC = "Daily Brief"
WATCH_TOPIC = "Watchlist"
WATCH_EXTRA_FEEDS = {
    "Business Times Companies": "https://www.businesstimes.com.sg/rss/companies-markets",
    "MarketWatch Real-time": "https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
    "Investing.com All News": "https://www.investing.com/rss/news.rss",
}

try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GROUP_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"

TOPIC_ERRORS = {}
SOURCE_STATUS = {}
QUOTE_CACHE = {}
YAHOO = {}


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_config():
    cfg = load_json(CONFIG_PATH, {})
    cfg.setdefault("categories", {})
    cfg.setdefault("watchlist", {})
    brief = cfg.setdefault("brief", {})
    brief.setdefault("topic_id", None)
    brief.setdefault("hour_sgt", 8)
    brief.setdefault("earnings_alert_days", 2)
    sync_watchlist(cfg)
    for cat in cfg["categories"].values():
        cat.setdefault("topic_id", None)
        cat.setdefault("feeds", {})
        cat.setdefault("keywords", [])
        cat.setdefault("paused", False)
    return cfg


def load_state():
    state = load_json(STATE_PATH, {})
    state.setdefault("offset", 0)
    state.setdefault("seen", [])
    state.setdefault("last_digest", "")
    state.setdefault("earnings", {})
    state.setdefault("earnings_notified", [])
    return state


def yahoo_feed_url(tickers):
    return f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={','.join(tickers)}&region=US&lang=en-US"


def sync_watchlist(cfg):
    """The Watchlist category's feeds and keywords are generated from cfg['watchlist']."""
    wl = cfg["watchlist"]
    if not wl:
        return
    cat = cfg["categories"].setdefault(WATCH_TOPIC, {"topic_id": None})
    tickers = list(wl)
    feeds = {}
    for i in range(0, len(tickers), 6):
        chunk = tickers[i:i + 6]
        feeds[f"Yahoo Finance ({', '.join(chunk)})"] = yahoo_feed_url(chunk)
    feeds.update(WATCH_EXTRA_FEEDS)
    cat["feeds"] = feeds
    cat["keywords"] = sorted({a for aliases in wl.values() for a in aliases}, key=str.lower)


def sgt_now():
    return datetime.now(SGT)


def fmt_sgt(ts, pattern="%a %d %b %H:%M"):
    return datetime.fromtimestamp(ts, SGT).strftime(pattern) if ts else "never"


# ---------- Telegram ----------

def tg(method, **params):
    for _ in range(3):
        try:
            data = requests.post(f"{API}/{method}", json=params, timeout=40).json()
        except (requests.RequestException, ValueError) as e:
            print(f"[telegram] {method} error: {e}", file=sys.stderr)
            return {}
        retry = data.get("parameters", {}).get("retry_after")
        if data.get("ok") or not retry:
            break
        time.sleep(retry + 1)
    if not data.get("ok"):
        print(f"[telegram] {method} failed: {data.get('description')}", file=sys.stderr)
    return data


def split_message(text, limit=4000):
    """Split on line boundaries so HTML tags (which never span lines here) aren't cut."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    chunks.append(current)
    return [c for c in chunks if c.strip()] or [" "]


def send(chat_id, text, thread_id=None):
    data = {}
    for chunk in split_message(text):
        params = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
        if thread_id:
            params["message_thread_id"] = thread_id
        data = tg("sendMessage", **params)
    return data


def topic_holders(cfg):
    """(name, dict-with-topic_id) for every topic the bot manages."""
    holders = list(cfg["categories"].items())
    holders.append((BRIEF_TOPIC, cfg["brief"]))
    return holders


def ensure_topic(name, holder, index):
    if holder["topic_id"] or not GROUP_ID:
        return holder["topic_id"]
    res = tg("createForumTopic", chat_id=GROUP_ID, name=name,
             icon_color=TOPIC_COLORS[index % len(TOPIC_COLORS)])
    if res.get("ok"):
        holder["topic_id"] = res["result"]["message_thread_id"]
        print(f"[topic] created '{name}' -> {holder['topic_id']}")
    else:
        TOPIC_ERRORS[name] = res.get("description", "no response from Telegram")
    return holder["topic_id"]


def ensure_all_topics(cfg, state):
    """Create missing topics; tell the group (once per distinct error) if Telegram refuses."""
    TOPIC_ERRORS.clear()
    for i, (name, holder) in enumerate(topic_holders(cfg)):
        ensure_topic(name, holder, i)
    error = "; ".join(sorted(set(TOPIC_ERRORS.values())))
    if error and error != state.get("topic_error"):
        send(GROUP_ID, "<b>Couldn't create category topics</b>, so alerts will be posted here for now.\n"
                       f"Telegram said: <code>{html.escape(error)}</code>\n\n"
                       "Fix: make sure Topics is on, and the bot is an admin with the "
                       "<b>Manage Topics</b> permission. Then send /setup.")
    state["topic_error"] = error


def post_to_topic(cfg, name, text):
    holders = topic_holders(cfg)
    index = [n for n, _ in holders].index(name)
    holder = holders[index][1]
    thread = ensure_topic(name, holder, index)
    if not thread:
        text = f"[{html.escape(name)}]\n{text}"
    res = send(GROUP_ID, text, thread)
    if not res.get("ok") and "thread not found" in str(res.get("description", "")).lower():
        holder["topic_id"] = None
        send(GROUP_ID, text, ensure_topic(name, holder, index))


# ---------- Feeds ----------

def fetch_items(source, url):
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[feed] {source}: {e}", file=sys.stderr)
        SOURCE_STATUS[source] = {"ok": False, "detail": str(e)[:120], "at": time.time()}
        return []
    items = []
    for e in feedparser.parse(resp.content).entries:
        summary = re.sub(r"<[^>]+>", "", e.get("summary", "")).strip()
        parsed_time = e.get("published_parsed") or e.get("updated_parsed")
        items.append({
            "ts": calendar.timegm(parsed_time) if parsed_time else None,
            "id": e.get("id") or e.get("link"),
            "source": source,
            "title": html.unescape(e.get("title", "")).strip(),
            "summary": html.unescape(summary),
            "link": e.get("link", ""),
            "published": e.get("published", ""),
            "tags": [t.get("term", "") for t in e.get("tags", [])],
        })
    SOURCE_STATUS[source] = {"ok": bool(items), "detail": f"{len(items)} items", "at": time.time()}
    return items


def fetch_all(cfg):
    """{url: items} for every distinct feed across categories."""
    cache = {}
    for cat in cfg["categories"].values():
        for source, url in cat["feeds"].items():
            if url not in cache:
                cache[url] = fetch_items(source, url)
    return cache


def keyword_pattern(k):
    # "hack*" matches hack, hacker, hacked; plain words match whole words only.
    if k.endswith("*"):
        return rf"\b{re.escape(k[:-1])}\w*"
    return rf"\b{re.escape(k)}\b"


def item_text(item):
    return " ".join([item["title"], item["summary"], " ".join(item["tags"])])


def matched_keywords(item, keywords):
    if not keywords:
        return ["*"]
    haystack = item_text(item)
    return [k for k in keywords if re.search(keyword_pattern(k), haystack, re.IGNORECASE)]


# ---------- Prices & earnings (Yahoo Finance) ----------

def get_quote(ticker):
    cached = QUOTE_CACHE.get(ticker)
    if cached and time.time() - cached["at"] < 600:
        return cached
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                         params={"range": "1d", "interval": "1d"},
                         headers={"User-Agent": BROWSER_UA}, timeout=20)
        meta = r.json()["chart"]["result"][0]["meta"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as e:
        print(f"[quote] {ticker}: {e}", file=sys.stderr)
        return None
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None:
        return None
    quote = {
        "at": time.time(),
        "price": price,
        "change": (price / prev - 1) * 100 if prev else None,
        "currency": meta.get("currency", ""),
        "name": meta.get("shortName") or ticker,
    }
    QUOTE_CACHE[ticker] = quote
    return quote


def fmt_quote(cfg, ticker, quote):
    label = cfg["watchlist"].get(ticker, [ticker])[0]
    change = f" ({quote['change']:+.1f}%)" if quote["change"] is not None else ""
    return f"{html.escape(label)} {quote['price']:,.2f} {quote['currency']}{change}"


def watch_hits(cfg, item):
    haystack = item_text(item)
    return [t for t, aliases in cfg["watchlist"].items()
            if any(re.search(keyword_pattern(a), haystack, re.IGNORECASE) for a in aliases)]


def yahoo_session():
    """Yahoo's quoteSummary endpoint needs a cookie + crumb pair."""
    if "session" not in YAHOO:
        s = requests.Session()
        s.headers["User-Agent"] = BROWSER_UA
        try:
            s.get("https://fc.yahoo.com", timeout=20)
            crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=20).text.strip()
        except requests.RequestException as e:
            print(f"[earnings] crumb: {e}", file=sys.stderr)
            crumb = ""
        YAHOO["session"], YAHOO["crumb"] = s, crumb
    return YAHOO["session"], YAHOO["crumb"]


def fetch_earnings_date(ticker):
    """Next earnings timestamp, None if Yahoo has no date, False on error."""
    session, crumb = yahoo_session()
    if not crumb:
        return False
    try:
        data = session.get(f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                           params={"modules": "calendarEvents", "crumb": crumb}, timeout=20).json()
        dates = data["quoteSummary"]["result"][0]["calendarEvents"]["earnings"]["earningsDate"]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as e:
        print(f"[earnings] {ticker}: {e}", file=sys.stderr)
        return False
    return dates[0]["raw"] if dates else None


def refresh_earnings(cfg, state, force=False):
    now = time.time()
    for ticker in cfg["watchlist"]:
        entry = state["earnings"].get(ticker)
        if force or not entry or now - entry["fetched"] > 86400:
            ts = fetch_earnings_date(ticker)
            if ts is not False:
                state["earnings"][ticker] = {"ts": ts, "fetched": now}
    for ticker in list(state["earnings"]):
        if ticker not in cfg["watchlist"]:
            del state["earnings"][ticker]


def upcoming_earnings(cfg, state, days):
    today = sgt_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = [(e["ts"], t) for t, e in state["earnings"].items()
            if t in cfg["watchlist"] and e.get("ts") and today <= e["ts"] <= today + (days + 1) * 86400]
    return sorted(rows)


def earnings_line(cfg, ticker, ts):
    days = (datetime.fromtimestamp(ts, SGT).date() - sgt_now().date()).days
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    label = cfg["watchlist"][ticker][0]
    return f"{html.escape(label)} ({ticker}): {fmt_sgt(ts, '%a %d %b')} ({when})"


def check_earnings_reminders(cfg, state):
    if not cfg["watchlist"]:
        return
    refresh_earnings(cfg, state)
    for ts, ticker in upcoming_earnings(cfg, state, cfg["brief"]["earnings_alert_days"]):
        key = f"{ticker}|{fmt_sgt(ts, '%Y-%m-%d')}"
        if key in state["earnings_notified"]:
            continue
        post_to_topic(cfg, WATCH_TOPIC, "<b>Earnings reminder</b>\n" + earnings_line(cfg, ticker, ts))
        state["earnings_notified"].append(key)
    del state["earnings_notified"][:-200]


# ---------- Formatting & posting ----------

def format_item(cfg, item, hits):
    summary = item["summary"]
    if len(summary) > 400:
        summary = summary[:400].rsplit(" ", 1)[0] + "..."
    matched = "" if hits == ["*"] else "\n<i>Matched: " + ", ".join(html.escape(h) for h in hits) + "</i>"
    quotes = [fmt_quote(cfg, t, q) for t in watch_hits(cfg, item)[:3] if (q := get_quote(t))]
    prices = "\n<i>Price: " + " | ".join(quotes) + "</i>" if quotes else ""
    return (
        f"<b>{html.escape(item['title'])}</b>\n"
        f"{html.escape(summary)}\n\n"
        f"{html.escape(item['source'])} | {html.escape(item['published'])}{matched}{prices}\n"
        f"<a href=\"{html.escape(item['link'], quote=True)}\">Read more</a>"
    )


def collect_recent(cfg, hours, only=None, cache=None):
    """{category: [(item, hits)]} for matching items published in the last `hours`, oldest first."""
    cutoff = time.time() - hours * 3600
    cache = cache if cache is not None else fetch_all(cfg)
    result = {}
    for name, cat in cfg["categories"].items():
        if only and name != only:
            continue
        found = {}
        for url in cat["feeds"].values():
            for item in cache.get(url, []):
                if item["id"] and item["ts"] and item["ts"] >= cutoff and item["id"] not in found:
                    hits = matched_keywords(item, cat["keywords"])
                    if hits:
                        found[item["id"]] = (item, hits)
        result[name] = sorted(found.values(), key=lambda x: x[0]["ts"])
    return result


def backfill(cfg, state, hours, only=None, cap=20):
    """Post matching items published in the last `hours` (newest `cap` per category). Returns summary lines."""
    seen_set = set(state["seen"])
    summary = []
    for name, found in collect_recent(cfg, hours, only).items():
        picked = found[-cap:]
        for item, hits in picked:
            post_to_topic(cfg, name, format_item(cfg, item, hits))
        for item, _ in found:
            key = f"{name}|{item['id']}"
            if key not in seen_set:
                state["seen"].append(key)
                seen_set.add(key)
        line = f"{name}: {len(picked)}"
        if len(found) > cap:
            line += f" (newest {cap} of {len(found)})"
        summary.append(line)
    return summary


def check_feeds(cfg, state):
    seen = state["seen"]
    seen_set = set(seen)
    seen_before = set(seen)
    cache = {}
    sent = 0
    for name, cat in cfg["categories"].items():
        for source, url in cat["feeds"].items():
            if url not in cache:
                cache[url] = fetch_items(source, url)
            items = cache[url]
            keys = [f"{name}|{i['id']}" for i in items]
            # A feed new to this category is seeded silently so the topic isn't flooded with backlog.
            first_time = bool(items) and not any(k in seen_before for k in keys)
            for item, key in reversed(list(zip(items, keys))):
                if not item["id"] or key in seen_set:
                    continue
                seen.append(key)
                seen_set.add(key)
                if first_time or cat["paused"]:
                    continue
                hits = matched_keywords(item, cat["keywords"])
                if hits:
                    post_to_topic(cfg, name, format_item(cfg, item, hits))
                    sent += 1
    del seen[:-SEEN_LIMIT]
    state["last_check"] = time.time()
    return sent


# ---------- Daily brief ----------

def send_brief(cfg, state):
    now = sgt_now()
    parts = [f"<b>Daily Brief | {now.strftime('%a %d %b %Y')}</b>"]

    if cfg["watchlist"]:
        quotes = [fmt_quote(cfg, t, q) if (q := get_quote(t)) else f"{html.escape(a[0])}: price unavailable"
                  for t, a in cfg["watchlist"].items()]
        parts.append("<b>Watchlist (last price, daily change)</b>\n" + "\n".join(quotes))
        refresh_earnings(cfg, state)
        rows = upcoming_earnings(cfg, state, 14)
        parts.append("<b>Earnings in the next 14 days</b>\n" +
                     ("\n".join(earnings_line(cfg, t, ts) for ts, t in rows) or "None scheduled"))
    post_to_topic(cfg, BRIEF_TOPIC, "\n\n".join(parts))

    sections = []
    for name, found in collect_recent(cfg, 24).items():
        if found:
            top = found[-5:][::-1]
            sections.append(f"<b>{html.escape(name)}</b> ({len(found)} articles)\n" + "\n".join(
                f"- <a href=\"{html.escape(i['link'], quote=True)}\">{html.escape(i['title'])}</a>"
                for i, _ in top))
    post_to_topic(cfg, BRIEF_TOPIC, "<b>Top headlines, past 24 hours</b>\n\n" +
                  ("\n\n".join(sections) or "No matching news."))


def maybe_send_brief(cfg, state):
    now = sgt_now()
    today = now.date().isoformat()
    if now.hour >= cfg["brief"]["hour_sgt"] and state["last_digest"] != today:
        state["last_digest"] = today
        send_brief(cfg, state)


# ---------- Commands ----------

HELP = (
    "<b>News bot commands</b>\n"
    "Inside a category topic, [category] can be left out.\n\n"
    "<b>Topics &amp; keywords</b>\n"
    "/categories - list categories\n"
    "/topics [category] - list keywords\n"
    "/add [category] &lt;keyword&gt; - watch a keyword (end with * for prefix, e.g. hack*)\n"
    "/remove [category] &lt;keyword&gt;\n"
    "/sources [category] - list feeds\n"
    "/addsource [category] &lt;name&gt; &lt;rss url&gt;\n"
    "/removesource [category] &lt;name&gt;\n\n"
    "<b>News</b>\n"
    "/latest [category] [n] - latest n matching items (default 5)\n"
    "/backfill [category] [hours] - post matching news from the past hours (default 24)\n"
    "/digest - send the daily brief now\n\n"
    "<b>Watchlist</b>\n"
    "/watchlist - prices of your stocks\n"
    "/watch &lt;ticker&gt; [name, other name] - add a stock (SGX tickers end in .SI)\n"
    "/unwatch &lt;ticker&gt; - remove a stock\n"
    "/earnings - upcoming earnings dates\n\n"
    "<b>Troubleshooting</b>\n"
    "/status - last check, failing sources, paused topics\n"
    "/test &lt;keyword&gt; - which current headlines a keyword would catch\n"
    "/pause [category] - stop alerts for a topic\n"
    "/resume [category] - restart alerts for a topic\n"
    "/setup - create any missing topics\n"
    "/id - show chat and topic IDs\n\n"
    "<i>Runs on GitHub Actions: replies can take a few minutes between runs.</i>"
)


def resolve_category(cfg, thread_id, arg):
    """Return (category, remaining_arg). Explicit category name in arg wins over the current topic."""
    for name in sorted(cfg["categories"], key=len, reverse=True):
        if arg.lower() == name.lower() or arg.lower().startswith(name.lower() + " "):
            return name, arg[len(name):].strip()
    for name, cat in cfg["categories"].items():
        if thread_id and cat["topic_id"] == thread_id:
            return name, arg
    return None, arg


def status_report(cfg, state):
    lines = ["<b>Bot status</b>",
             f"Last feed check: {fmt_sgt(state.get('last_check'))} SGT",
             f"Daily brief: {cfg['brief']['hour_sgt']:02d}:00 SGT, last sent {state['last_digest'] or 'never'}"]
    paused = [n for n, c in cfg["categories"].items() if c["paused"]]
    lines.append("Paused topics: " + (", ".join(html.escape(p) for p in paused) or "none"))
    missing = [n for n, h in topic_holders(cfg) if not h["topic_id"]]
    if missing:
        lines.append("Missing topics: " + ", ".join(html.escape(m) for m in missing) + " (send /setup)")
    fetch_all(cfg)
    bad = {s: v for s, v in SOURCE_STATUS.items() if not v["ok"]}
    lines.append(f"\n<b>Sources</b>: {len(SOURCE_STATUS) - len(bad)} of {len(SOURCE_STATUS)} working")
    lines += [f"- {html.escape(s)}: {html.escape(v['detail'])}" for s, v in bad.items()]
    earn = [e["fetched"] for e in state["earnings"].values()]
    lines.append(f"Earnings dates updated: {fmt_sgt(min(earn)) if earn else 'never'}")
    return "\n".join(lines)


def handle_command(chat_id, thread_id, text, cfg, state):
    cmd, _, raw_arg = text.strip().partition(" ")
    cmd = cmd.split("@")[0].lower()
    name, arg = resolve_category(cfg, thread_id, raw_arg.strip())
    cat = cfg["categories"].get(name)
    reply = lambda t: send(chat_id, t, thread_id)
    needs_cat = {"/add", "/remove", "/addsource", "/removesource", "/pause", "/resume"}

    if cmd in needs_cat and not cat:
        reply("Which category? Use it inside a category topic, or e.g. "
              f"<code>{cmd} {html.escape(next(iter(cfg['categories']), 'AI'))} ...</code>")
    elif cmd in ("/add", "/remove") and name == WATCH_TOPIC:
        reply("Watchlist keywords come from your stocks. Use /watch or /unwatch instead.")
    elif cmd in ("/start", "/help"):
        reply(HELP)
    elif cmd == "/id":
        reply(f"Chat ID: <code>{chat_id}</code>\nTopic ID: <code>{thread_id}</code>")
    elif cmd == "/categories":
        reply("\n".join(f"- {html.escape(n)} ({len(c['feeds'])} feeds, {len(c['keywords']) or 'all'} keywords)"
                        + (" [paused]" if c["paused"] else "")
                        for n, c in cfg["categories"].items()) or "No categories.")
    elif cmd == "/topics":
        cats = {name: cat} if cat else cfg["categories"]
        reply("\n\n".join(f"<b>{html.escape(n)}</b>: " + (", ".join(html.escape(k) for k in c["keywords"])
                          or "<i>everything</i>") for n, c in cats.items()))
    elif cmd == "/add" and arg:
        if arg.lower() not in (k.lower() for k in cat["keywords"]):
            cat["keywords"].append(arg)
        reply(f"{html.escape(name)}: added {html.escape(arg)}")
    elif cmd == "/remove" and arg:
        before = len(cat["keywords"])
        cat["keywords"] = [k for k in cat["keywords"] if k.lower() != arg.lower()]
        reply(f"{html.escape(name)}: " + ("removed." if len(cat["keywords"]) < before else "not found."))
    elif cmd == "/sources":
        cats = {name: cat} if cat else cfg["categories"]
        reply("\n\n".join(f"<b>{html.escape(n)}</b>\n" + "\n".join(
            f"- {html.escape(s)}: {html.escape(u)}" for s, u in c["feeds"].items()) for n, c in cats.items()))
    elif cmd == "/addsource":
        parts = arg.rsplit(" ", 1)
        if len(parts) != 2 or not parts[1].startswith("http"):
            reply("Usage: /addsource [category] &lt;name&gt; &lt;rss url&gt;")
        elif not fetch_items(*parts):
            reply("Couldn't read any items from that URL. Is it an RSS/Atom feed?")
        else:
            cat["feeds"][parts[0]] = parts[1]
            reply(f"{html.escape(name)}: added source {html.escape(parts[0])}")
    elif cmd == "/removesource" and arg:
        found = cat["feeds"].pop(arg, None)
        reply(f"{html.escape(name)}: " + ("removed." if found else "not found."))
    elif cmd in ("/pause", "/resume"):
        cat["paused"] = cmd == "/pause"
        reply(f"{html.escape(name)}: alerts paused. It still appears in the daily brief." if cat["paused"]
              else f"{html.escape(name)}: alerts resumed.")
    elif cmd == "/latest":
        n = int(arg) if arg.isdigit() else 5
        grouped = collect_recent(cfg, 24 * 7, only=name)
        results = sorted((x for found in grouped.values() for x in found), key=lambda x: x[0]["ts"])[-n:]
        if not results:
            reply("No matching items in the current feeds.")
        for item, hits in reversed(results):
            reply(format_item(cfg, item, hits))
    elif cmd == "/backfill":
        hours = float(arg) if re.fullmatch(r"\d+(\.\d+)?", arg) else 24
        reply(f"Posting matching news from the past {hours:g} hours into "
              f"{html.escape(name) if name else 'all topics'}...")
        lines = backfill(cfg, state, hours, only=name)
        reply("Backfill done:\n" + "\n".join(html.escape(l) for l in lines))
    elif cmd == "/digest":
        send_brief(cfg, state)
        reply("Daily brief posted in the Daily Brief topic.")
    elif cmd == "/status":
        reply(status_report(cfg, state))
    elif cmd == "/test":
        keyword = raw_arg.strip()
        if not keyword:
            reply("Usage: /test &lt;keyword&gt;  (end with * for prefix)")
            return
        pattern = keyword_pattern(keyword)
        hits = {}
        for items in fetch_all(cfg).values():
            for item in items:
                if re.search(pattern, item_text(item), re.IGNORECASE):
                    hits.setdefault(item["id"], item)
        found = sorted(hits.values(), key=lambda i: i["ts"] or 0, reverse=True)
        lines = [f"- {html.escape(i['title'])} <i>({html.escape(i['source'])})</i>" for i in found[:10]]
        reply(f"<b>{html.escape(keyword)}</b> matches {len(found)} current headlines"
              + (":\n" + "\n".join(lines) if lines else "."))
    elif cmd == "/watchlist":
        if not cfg["watchlist"]:
            reply("Watchlist is empty. Add a stock with /watch &lt;ticker&gt;.")
            return
        rows = [fmt_quote(cfg, t, q) if (q := get_quote(t)) else f"{html.escape(a[0])}: price unavailable"
                for t, a in cfg["watchlist"].items()]
        reply("<b>Watchlist (last price, daily change)</b>\n" + "\n".join(rows))
    elif cmd == "/watch":
        ticker, _, rest = raw_arg.strip().partition(" ")
        ticker = ticker.upper()
        if not ticker:
            reply("Usage: /watch &lt;ticker&gt; [name, other name]\ne.g. /watch TSM Taiwan Semiconductor, TSMC")
            return
        quote = get_quote(ticker)
        if not quote:
            reply(f"Couldn't find {html.escape(ticker)} on Yahoo Finance. SGX tickers end in .SI (e.g. D05.SI).")
            return
        aliases = [a.strip() for a in rest.split(",") if a.strip()] or [quote["name"]]
        aliases.append(ticker.split(".")[0])
        cfg["watchlist"][ticker] = list(dict.fromkeys(aliases))
        sync_watchlist(cfg)
        reply(f"Watching {fmt_quote(cfg, ticker, quote)}\nMatches: {html.escape(', '.join(cfg['watchlist'][ticker]))}")
    elif cmd == "/unwatch":
        ticker = raw_arg.strip().upper()
        if cfg["watchlist"].pop(ticker, None) is None:
            reply("Not in watchlist.")
            return
        sync_watchlist(cfg)
        reply(f"Stopped watching {html.escape(ticker)}.")
    elif cmd == "/earnings":
        refresh_earnings(cfg, state)
        rows = upcoming_earnings(cfg, state, 90)
        unknown = [t for t in cfg["watchlist"] if not state["earnings"].get(t, {}).get("ts")]
        reply("<b>Upcoming earnings (next 90 days)</b>\n" +
              ("\n".join(earnings_line(cfg, t, ts) for ts, t in rows) or "None found") +
              (f"\n\n<i>No date from Yahoo: {html.escape(', '.join(unknown))}</i>" if unknown else ""))
    elif cmd == "/setup":
        TOPIC_ERRORS.clear()
        for i, (n, h) in enumerate(topic_holders(cfg)):
            ensure_topic(n, h, i)
        missing = [n for n, h in topic_holders(cfg) if not h["topic_id"]]
        state["topic_error"] = "; ".join(sorted(set(TOPIC_ERRORS.values())))
        reply("All topics ready." if not missing else
              "Couldn't create: " + ", ".join(missing) + f".\nTelegram said: <code>{html.escape(state['topic_error'])}</code>\n"
              "Make sure Topics is on and the bot is an admin with Manage Topics.")
    else:
        reply(HELP)


def process_updates(cfg, state, wait):
    params = {"timeout": wait, "allowed_updates": ["message"]}
    if state["offset"]:
        params["offset"] = state["offset"]
    try:
        updates = requests.get(f"{API}/getUpdates", params=params, timeout=wait + 15).json().get("result", [])
    except (requests.RequestException, ValueError) as e:
        print(f"[telegram] poll error: {e}", file=sys.stderr)
        time.sleep(min(wait, 5))
        return
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        text = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))
        thread_id = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        if not text.startswith("/") or not chat_id:
            continue
        if chat_id != GROUP_ID:
            send(chat_id, f"Not authorised. This chat's ID is <code>{chat_id}</code>. "
                          "Set it as the TELEGRAM_CHAT_ID secret.", thread_id)
            continue
        try:
            handle_command(chat_id, thread_id, text, cfg, state)
        except Exception as e:
            print(f"[command] {text!r} failed: {e!r}", file=sys.stderr)
            send(chat_id, f"Command failed: <code>{html.escape(repr(e))[:300]}</code>", thread_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=0, help="keep running this long (0 = single pass)")
    ap.add_argument("--interval", type=int, default=60, help="seconds between feed checks")
    args = ap.parse_args()

    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    if not GROUP_ID:
        print("WARNING: TELEGRAM_CHAT_ID not set; only answering /id until it is.", file=sys.stderr)

    cfg, state = load_config(), load_state()
    if GROUP_ID:
        ensure_all_topics(cfg, state)
    deadline = time.time() + args.minutes * 60
    next_check = 0
    try:
        while True:
            now = time.time()
            if GROUP_ID and now >= next_check:
                n = check_feeds(cfg, state)
                print(f"[{time.strftime('%H:%M:%S')}] feeds checked, {n} sent")
                check_earnings_reminders(cfg, state)
                maybe_send_brief(cfg, state)
                next_check = now + args.interval
            remaining = deadline - time.time()
            wait = int(max(0, min(25, max(0, next_check - time.time()) if GROUP_ID else 25, remaining)))
            process_updates(cfg, state, wait)
            if time.time() >= deadline:
                break
    finally:
        save_json(CONFIG_PATH, cfg)
        save_json(STATE_PATH, state)


if __name__ == "__main__":
    main()
