"""Telegram news bot that posts categorised news into a forum group's topics. Runs on GitHub Actions.

Each run: answers pending Telegram commands, checks every category's RSS feeds every --interval
seconds for --minutes, posts new items matching the category's keywords into that category's
topic, then saves config.json/state.json (the workflow commits them back for the next run).

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
from pathlib import Path

import feedparser
import requests

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
SEEN_LIMIT = 5000
USER_AGENT = "Mozilla/5.0 (TelegramNewsBot)"
TOPIC_COLORS = [0xFF93B2, 0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFB6F5F]

try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GROUP_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"


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
    for cat in cfg["categories"].values():
        cat.setdefault("topic_id", None)
        cat.setdefault("feeds", {})
        cat.setdefault("keywords", [])
    return cfg


def load_state():
    state = load_json(STATE_PATH, {})
    state.setdefault("offset", 0)
    state.setdefault("seen", [])
    return state


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


def send(chat_id, text, thread_id=None):
    data = {}
    for i in range(0, max(len(text), 1), 4000):
        params = {"chat_id": chat_id, "text": text[i:i + 4000], "parse_mode": "HTML"}
        if thread_id:
            params["message_thread_id"] = thread_id
        data = tg("sendMessage", **params)
    return data


def ensure_topic(name, cat, index):
    if cat["topic_id"] or not GROUP_ID:
        return cat["topic_id"]
    res = tg("createForumTopic", chat_id=GROUP_ID, name=name,
             icon_color=TOPIC_COLORS[index % len(TOPIC_COLORS)])
    if res.get("ok"):
        cat["topic_id"] = res["result"]["message_thread_id"]
        print(f"[topic] created '{name}' -> {cat['topic_id']}")
    else:
        TOPIC_ERRORS[name] = res.get("description", "no response from Telegram")
    return cat["topic_id"]


TOPIC_ERRORS = {}


def ensure_all_topics(cfg, state):
    """Create missing topics; tell the group (once per distinct error) if Telegram refuses."""
    TOPIC_ERRORS.clear()
    for i, (name, cat) in enumerate(cfg["categories"].items()):
        ensure_topic(name, cat, i)
    error = "; ".join(sorted(set(TOPIC_ERRORS.values())))
    if error and error != state.get("topic_error"):
        send(GROUP_ID, "<b>Couldn't create category topics</b>, so alerts will be posted here for now.\n"
                       f"Telegram said: <code>{html.escape(error)}</code>\n\n"
                       "Fix: make sure Topics is on, and the bot is an admin with the "
                       "<b>Manage Topics</b> permission. Then send /setup.")
    state["topic_error"] = error


def post_to_category(cfg, name, text):
    cat = cfg["categories"][name]
    index = list(cfg["categories"]).index(name)
    thread = ensure_topic(name, cat, index)
    if not thread:
        text = f"[{html.escape(name)}]\n{text}"
    res = send(GROUP_ID, text, thread)
    if not res.get("ok") and "thread not found" in str(res.get("description", "")).lower():
        cat["topic_id"] = None
        send(GROUP_ID, text, ensure_topic(name, cat, index))
    elif not thread:
        print(f"[topic] no topic for {name}; posted to General", file=sys.stderr)


# ---------- Feeds ----------

def fetch_items(source, url):
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[feed] {source}: {e}", file=sys.stderr)
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
    return items


def keyword_pattern(k):
    # "hack*" matches hack, hacker, hacked; plain words match whole words only.
    if k.endswith("*"):
        return rf"\b{re.escape(k[:-1])}\w*"
    return rf"\b{re.escape(k)}\b"


def matched_keywords(item, keywords):
    if not keywords:
        return ["*"]
    haystack = " ".join([item["title"], item["summary"], " ".join(item["tags"])])
    return [k for k in keywords if re.search(keyword_pattern(k), haystack, re.IGNORECASE)]


def format_item(item, hits):
    summary = item["summary"]
    if len(summary) > 400:
        summary = summary[:400].rsplit(" ", 1)[0] + "..."
    matched = "" if hits == ["*"] else "\n<i>Matched: " + ", ".join(html.escape(h) for h in hits) + "</i>"
    return (
        f"<b>{html.escape(item['title'])}</b>\n"
        f"{html.escape(summary)}\n\n"
        f"{html.escape(item['source'])} | {html.escape(item['published'])}{matched}\n"
        f"<a href=\"{html.escape(item['link'], quote=True)}\">Read more</a>"
    )


def backfill(cfg, state, hours, only=None, cap=20):
    """Post matching items published in the last `hours` (newest `cap` per category). Returns summary lines."""
    cutoff = time.time() - hours * 3600
    seen_set = set(state["seen"])
    cache = {}
    summary = []
    for name, cat in cfg["categories"].items():
        if only and name != only:
            continue
        found = {}
        for source, url in cat["feeds"].items():
            if url not in cache:
                cache[url] = fetch_items(source, url)
            for item in cache[url]:
                if item["id"] and item["ts"] and item["ts"] >= cutoff and item["id"] not in found:
                    hits = matched_keywords(item, cat["keywords"])
                    if hits:
                        found[item["id"]] = (item, hits)
        picked = sorted(found.values(), key=lambda x: x[0]["ts"])[-cap:]
        for item, hits in picked:
            post_to_category(cfg, name, format_item(item, hits))
        for item_id in found:
            key = f"{name}|{item_id}"
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
                if first_time:
                    continue
                hits = matched_keywords(item, cat["keywords"])
                if hits:
                    post_to_category(cfg, name, format_item(item, hits))
                    sent += 1
    del seen[:-SEEN_LIMIT]
    return sent


# ---------- Commands ----------

HELP = (
    "<b>News bot commands</b>\n"
    "Inside a category topic, [category] can be left out.\n\n"
    "/categories - list categories\n"
    "/topics [category] - list keywords\n"
    "/add [category] &lt;keyword&gt; - watch a keyword (end with * for prefix, e.g. hack*)\n"
    "/remove [category] &lt;keyword&gt;\n"
    "/sources [category] - list feeds\n"
    "/addsource [category] &lt;name&gt; &lt;rss url&gt;\n"
    "/removesource [category] &lt;name&gt;\n"
    "/latest [category] [n] - latest n matching items (default 5)\n"
    "/backfill [category] [hours] - post matching news from the past hours (default 24) into the topics\n"
    "/setup - create any missing category topics\n"
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


def handle_command(chat_id, thread_id, text, cfg, state):
    cmd, _, arg = text.strip().partition(" ")
    cmd = cmd.split("@")[0].lower()
    name, arg = resolve_category(cfg, thread_id, arg.strip())
    cat = cfg["categories"].get(name)
    reply = lambda t: send(chat_id, t, thread_id)
    needs_cat = {"/add", "/remove", "/addsource", "/removesource"}

    if cmd in needs_cat and not cat:
        reply("Which category? Use it inside a category topic, or e.g. "
              f"<code>{cmd} {html.escape(next(iter(cfg['categories']), 'AI'))} ...</code>")
    elif cmd in ("/start", "/help"):
        reply(HELP)
    elif cmd == "/id":
        reply(f"Chat ID: <code>{chat_id}</code>\nTopic ID: <code>{thread_id}</code>")
    elif cmd == "/categories":
        reply("\n".join(f"- {html.escape(n)} ({len(c['feeds'])} feeds, {len(c['keywords']) or 'all'} keywords)"
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
    elif cmd == "/latest":
        n = int(arg) if arg.isdigit() else 5
        cats = {name: cat} if cat else cfg["categories"]
        results = [(i, h) for c in cats.values() for s, u in c["feeds"].items()
                   for i in fetch_items(s, u) if (h := matched_keywords(i, c["keywords"]))]
        if not results:
            reply("No matching items in the current feeds.")
        for item, hits in results[:n]:
            reply(format_item(item, hits))
    elif cmd == "/backfill":
        hours = float(arg) if re.fullmatch(r"\d+(\.\d+)?", arg) else 24
        reply(f"Posting matching news from the past {hours:g} hours into "
              f"{html.escape(name) if name else 'all topics'}...")
        lines = backfill(cfg, state, hours, only=name)
        reply("Backfill done:\n" + "\n".join(html.escape(l) for l in lines))
    elif cmd == "/setup":
        TOPIC_ERRORS.clear()
        for i, (n, c) in enumerate(cfg["categories"].items()):
            ensure_topic(n, c, i)
        missing = [n for n, c in cfg["categories"].items() if not c["topic_id"]]
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
        handle_command(chat_id, thread_id, text, cfg, state)


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
