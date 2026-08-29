"""
Phase 1 (SSL-fixed): News Fetcher & Categorizer
"""

import calendar
import feedparser
import hashlib
import html
import json
import os
import re
import ssl
import time
import certifi
import requests
import urllib.request
from google import genai
from google.genai import types
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from dotenv import load_dotenv

# Fixes a common Mac issue where Python can't find trusted security
# certificates, by explicitly using certifi's up-to-date certificate list.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
_HTTPS_HANDLER = urllib.request.HTTPSHandler(context=_SSL_CONTEXT)

# Load Telegram/Gemini settings from the .env file that sits next to this
# script, regardless of which folder the script is run from.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DIGEST_MODEL = "gemini-3.5-flash-lite"

CEMENT_INDIA_KEYWORDS = [
    "cement India", "cement price hike India", "cement demand India",
    "UltraTech Cement", "Ambuja Cement", "Shree Cement", "ACC Limited cement",
    "Dalmia Bharat cement", "JK Cement", "India Cements", "Nuvoco Vistas",
    # Mid/small-cap Indian cement makers
    "Ramco Cements", "Birla Corporation cement", "Heidelberg Cement India",
    "Orient Cement", "Star Cement India", "Sagar Cements", "Prism Johnson cement",
    "JK Lakshmi Cement", "Mangalam Cement", "NCL Industries cement",
]

METALS_COMMODITIES_KEYWORDS = [
    "aluminium price", "LME aluminium", "steel price global", "copper price",
    "iron ore price", "coking coal price", "zinc price", "nickel price",
    "China aluminium demand", "China steel production", "China copper demand",
    "China metals stimulus", "India aluminium demand", "India steel demand",
    "India metals import duty", "Hindalco", "NALCO", "Vedanta Aluminium",
    "Tata Steel", "JSW Steel",
    # Mid/small-cap Indian metals companies
    "Jindal Steel Power", "SAIL Steel Authority India", "Jindal Stainless",
    "Hindustan Zinc", "APL Apollo Tubes", "Welspun Corp", "Ratnamani Metals",
    "Hindustan Copper", "Shyam Metalics", "Sarda Energy Minerals",
    "Godawari Power", "Usha Martin", "Jindal Saw",
    # Global large-cap metals & mining companies
    "Rio Tinto", "BHP Group", "Glencore", "Anglo American mining",
    "Freeport-McMoRan", "ArcelorMittal", "Alcoa", "Nucor steel", "Vale mining",
    "Southern Copper", "Norsk Hydro", "POSCO steel", "Nippon Steel",
    "China Baowu Steel",
]

MACRO_KEYWORDS = [
    "RBI monetary policy", "Federal Reserve interest rate", "crude oil price",
    "China PMI manufacturing", "China GDP growth", "China property crisis",
    "China credit data", "India GDP growth", "India inflation CPI",
    "India IIP industrial production", "India infrastructure spending budget",
]

EXTRA_FIXED_FEEDS = {
    "METALS & COMMODITIES (Global, incl. China/India)": [
        ("Mining.com", "https://www.mining.com/feed/"),
    ],
}

SEEN_FILE = "seen_articles.json"
PENDING_FILE = "pending_articles.json"
RECENT_TOPICS_FILE = "recent_topics.json"


def google_news_feed(query, region="US"):
    locales = {"IN": ("en-IN", "IN"), "US": ("en-US", "US")}
    hl, gl = locales.get(region, ("en-US", "US"))
    q = quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:en"


def build_feeds():
    feeds = {
        "CEMENT (India)": [(kw, google_news_feed(kw, region="IN")) for kw in CEMENT_INDIA_KEYWORDS],
        "METALS & COMMODITIES (Global, incl. China/India)": [(kw, google_news_feed(kw, region="US")) for kw in METALS_COMMODITIES_KEYWORDS],
        "MACRO (incl. China/India)": [(kw, google_news_feed(kw, region="US")) for kw in MACRO_KEYWORDS],
    }
    for category, extra_sources in EXTRA_FIXED_FEEDS.items():
        feeds.setdefault(category, []).extend(extra_sources)
    return feeds


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return {}
    with open(SEEN_FILE, "r") as f:
        data = json.load(f)
    if isinstance(data, list):
        # Old format (plain list of links) - migrate to timestamped dict.
        now = datetime.now(timezone.utc).isoformat()
        return {link: now for link in data}
    return data


SEEN_RETENTION = timedelta(days=3)


def save_seen(seen):
    cutoff = datetime.now(timezone.utc) - SEEN_RETENTION
    pruned = {
        link: seen_at for link, seen_at in seen.items()
        if datetime.fromisoformat(seen_at) >= cutoff
    }
    with open(SEEN_FILE, "w") as f:
        json.dump(pruned, f)


def short_id_for(link):
    """A compact, stable id for a link, short enough for a Telegram button's callback_data."""
    return hashlib.md5(link.encode("utf-8")).hexdigest()[:12]


def load_pending():
    if not os.path.exists(PENDING_FILE):
        return {}
    with open(PENDING_FILE, "r") as f:
        return json.load(f)


PENDING_RETENTION = timedelta(days=3)


def save_pending(pending):
    cutoff = datetime.now(timezone.utc) - PENDING_RETENTION
    pruned = {
        sid: info for sid, info in pending.items()
        if datetime.fromisoformat(info["added_at"]) >= cutoff
    }
    with open(PENDING_FILE, "w") as f:
        json.dump(pruned, f)


RECENT_TOPICS_RETENTION = timedelta(hours=48)


def load_recent_topics():
    """Topics already sent in a past digest, so Gemini can avoid re-reporting the
    same story when a different publisher covers it a few hours later."""
    if not os.path.exists(RECENT_TOPICS_FILE):
        return []
    with open(RECENT_TOPICS_FILE, "r") as f:
        return json.load(f)


def save_recent_topics(topics):
    cutoff = datetime.now(timezone.utc) - RECENT_TOPICS_RETENTION
    pruned = [t for t in topics if datetime.fromisoformat(t["added_at"]) >= cutoff]
    with open(RECENT_TOPICS_FILE, "w") as f:
        json.dump(pruned, f)


MAX_ARTICLE_AGE = timedelta(hours=24)


def is_recent(entry):
    """Only keep articles actually published within the last 24 hours - Google
    News search results aren't sorted by recency and often surface old
    evergreen pages that happen to match a keyword."""
    parsed_time = entry.get("published_parsed")
    if not parsed_time:
        return True  # can't tell the age, so don't filter it out
    entry_dt = datetime.fromtimestamp(calendar.timegm(parsed_time), tz=timezone.utc)
    return (datetime.now(timezone.utc) - entry_dt) <= MAX_ARTICLE_AGE


def title_fingerprint(title):
    """A normalized version of a headline, used as a second dedup key alongside
    the link. Google News gives the same real article a different tracking link
    depending on which keyword search surfaced it, so link-only dedup lets the
    identical headline slip through again under a new link - this catches that."""
    normalized = re.sub(r"[^a-z0-9\s]", "", title.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return f"title::{normalized}"


def fetch_new_articles(feeds, seen):
    new_by_category = {}
    for category, sources in feeds.items():
        new_items = []
        for source_name, url in sources:
            try:
                parsed = feedparser.parse(url, handlers=[_HTTPS_HANDLER])
            except Exception as e:
                print(f"  [ERROR] '{source_name}': crashed - {e}")
                continue

            raw_count = len(parsed.entries)
            print(f"  [debug] '{source_name}': entries found={raw_count}")

            for entry in parsed.entries:
                link = entry.get("link")
                title = entry.get("title", "(no title)")
                published = entry.get("published", "")
                if not link or link in seen:
                    continue
                if not is_recent(entry):
                    continue
                fp = title_fingerprint(title)
                now = datetime.now(timezone.utc).isoformat()
                if fp in seen:
                    seen[link] = now  # remember this link too, so we don't re-evaluate it every run
                    continue
                seen[link] = now
                seen[fp] = now
                new_items.append({"source": source_name, "title": title, "link": link, "published": published})

        if new_items:
            new_by_category[category] = new_items
    return new_by_category


def print_results(new_by_category):
    if not new_by_category:
        print("\nNo new articles found this run.")
        return
    for category, items in new_by_category.items():
        print(f"\n=== {category} ({len(items)} new) ===")
        for item in items:
            print(f"- [{item['source']}] {item['title']}")
            print(f"  {item['link']}")


def send_telegram_message(text, parse_mode=None, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("  [warn] Telegram not configured (check .env) - skipping alert send.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(url, data=data, timeout=10)
        if not resp.ok:
            print(f"  [ERROR] Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  [ERROR] Telegram send crashed: {e}")


def send_telegram_alerts(new_by_category):
    """Raw fallback: one message per article. Used when no ANTHROPIC_API_KEY is set."""
    for category, items in new_by_category.items():
        for item in items:
            text = f"\U0001F4CC {category}\n{item['title']}\n({item['source']})\n{item['link']}"
            send_telegram_message(text)
            time.sleep(0.3)


DIGEST_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Short label for the underlying company/commodity/event, e.g. 'UltraTech Cement Q1 results' or 'Fed holds rates steady'",
                    },
                    "category": {
                        "type": "string",
                        "description": "Which of the original categories this belongs to",
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "How likely this is to be price-moving / decision-relevant for an equity analyst covering metals, cement, and commodities",
                    },
                    "summary": {
                        "type": "string",
                        "description": "1-2 plain-English sentences: what happened and why it matters",
                    },
                    "article_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Indices (from the numbered input list) of the headlines that belong to this topic",
                    },
                },
                "required": ["topic", "category", "importance", "summary", "article_indices"],
            },
        },
    },
    "required": ["groups"],
}


def synthesize_digest(new_by_category, recent_topics):
    """Uses Gemini (free tier) to group scattered headlines into deduplicated, prioritized topics.

    recent_topics: topics already sent in a previous digest (within the last 48h),
    so a different outlet re-covering the same story hours later doesn't get
    reported again as if it were new."""
    flat_items = []
    for category, items in new_by_category.items():
        for item in items:
            flat_items.append({**item, "category": category})

    numbered_list = "\n".join(
        f"{i}. [{item['category']}] ({item['source']}) {item['title']}"
        for i, item in enumerate(flat_items)
    )

    if recent_topics:
        recent_block = "\n".join(f"- {t['topic']}: {t['summary']}" for t in recent_topics)
        recent_instructions = (
            "\n\nThe analyst was ALREADY sent a digest covering these topics in the last 48 hours "
            "(listed below as 'topic: summary'). If a headline below is just re-reporting one of these "
            "same stories with no genuinely new information (e.g. a different outlet covering the same "
            "results/event/price move), do NOT create a new group for it and do NOT include its index "
            "anywhere - silently drop it. Only make a new group for it if there's a real update (new "
            "numbers, a follow-up development, etc.) beyond what was already reported.\n\n"
            f"Already covered recently:\n{recent_block}"
        )
    else:
        recent_instructions = ""

    client = genai.Client(api_key=GEMINI_API_KEY)
    contents = (
        "You are triaging news headlines for an equity research analyst who covers "
        "cement (India), metals & commodities (global, with extra focus on China and India), "
        "and macro news (RBI, US Fed, China PMI/GDP, India GDP/inflation).\n\n"
        "Below is a numbered list of headlines gathered from many overlapping keyword searches, "
        "so the same real-world story often appears multiple times under different entries. "
        "Group them by the actual underlying company/commodity/event (not by which keyword "
        "matched), merging duplicate coverage of the same story into one group. For each group, "
        "write a short, plain-English 1-2 sentence summary of what happened and why it matters, "
        "and rate how likely it is to be price-moving or decision-relevant as high/medium/low."
        f"{recent_instructions}\n\n"
        f"{numbered_list}"
    )

    # Gemini's free-tier flash models occasionally return 503 "high demand" -
    # retry a couple of times with backoff before giving up, since this is
    # usually a brief spike rather than a real outage.
    last_error = None
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=DIGEST_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=DIGEST_RESPONSE_SCHEMA,
                ),
            )
            data = json.loads(response.text)
            return data.get("groups", []), flat_items
        except Exception as e:
            last_error = e
            if attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"  [warn] Digest synthesis attempt {attempt + 1} failed ({e}) - retrying in {wait}s...")
                time.sleep(wait)

    raise last_error


IMPORTANCE_ORDER = {"high": 0, "medium": 1, "low": 2}
IMPORTANCE_EMOJI = {"high": "\U0001F534", "medium": "\U0001F7E1", "low": "\U000026AA"}


def format_digest_messages(groups, flat_items):
    """Returns (messages, new_pending) where messages is a list of (text, buttons) -
    one message per topic, each with a single "Summarize this topic" button covering
    every article in that topic - and new_pending maps short_id -> topic bundle info."""
    groups_sorted = sorted(groups, key=lambda g: IMPORTANCE_ORDER.get(g.get("importance", "low"), 2))
    now = datetime.now(timezone.utc).isoformat()

    messages = []
    new_pending = {}
    for g in groups_sorted:
        emoji = IMPORTANCE_EMOJI.get(g.get("importance", "low"), "\U000026AA")
        lines = [
            f"{emoji} <b>{html.escape(g.get('topic', ''))}</b> ({html.escape(str(g.get('category', '')))})",
            html.escape(g.get("summary", "").strip()),
        ]
        articles = []
        for n, idx in enumerate(g.get("article_indices", [])[:3], start=1):
            if 0 <= idx < len(flat_items):
                item = flat_items[idx]
                lines.append(f"{n}. {html.escape(item['source'])}: {html.escape(item['link'])}")
                articles.append({"title": item["title"], "source": item["source"], "link": item["link"]})

        buttons = []
        if articles:
            sid = short_id_for("|".join(sorted(a["link"] for a in articles)))
            new_pending[sid] = {
                "topic": g.get("topic", ""), "category": g.get("category", ""),
                "articles": articles, "added_at": now,
            }
            buttons = [[{"text": "\U0001F4DD Summarize this topic", "callback_data": sid}]]

        messages.append(("\n".join(lines), buttons))
    return messages, new_pending


def send_digest_alerts(new_by_category):
    recent_topics = load_recent_topics()

    try:
        groups, flat_items = synthesize_digest(new_by_category, recent_topics)
    except Exception as e:
        print(f"  [ERROR] Digest synthesis crashed: {e} - falling back to raw per-article alerts.")
        send_telegram_alerts(new_by_category)
        return

    if not groups:
        # A genuinely empty result (as opposed to an exception above) means Gemini
        # filtered every headline out as a repeat of something already covered
        # recently - that's a successful outcome, not a failure, so don't fall
        # back to raw alerts (that would just resurface the exact repeats we're
        # trying to suppress).
        print("  [info] Digest synthesis returned no groups - everything was likely a repeat of recently covered topics.")
        return

    messages, new_pending = format_digest_messages(groups, flat_items)

    pending = load_pending()
    pending.update(new_pending)
    save_pending(pending)

    now = datetime.now(timezone.utc).isoformat()
    recent_topics.extend(
        {"topic": g.get("topic", ""), "summary": g.get("summary", ""), "added_at": now}
        for g in groups
    )
    save_recent_topics(recent_topics)

    header = f"\U0001F4F0 <b>News Digest</b> ({len(flat_items)} new articles → {len(groups)} topics)"
    send_telegram_message(header, parse_mode="HTML")
    time.sleep(0.3)

    for text, buttons in messages:
        reply_markup = {"inline_keyboard": buttons} if buttons else None
        send_telegram_message(text, parse_mode="HTML", reply_markup=reply_markup)
        time.sleep(0.5)


def main():
    print(f"Checking feeds at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}...\n")
    feeds = build_feeds()
    seen = load_seen()
    new_by_category = fetch_new_articles(feeds, seen)
    print_results(new_by_category)
    if new_by_category:
        if GEMINI_API_KEY:
            send_digest_alerts(new_by_category)
        else:
            print("  [info] GEMINI_API_KEY not set in .env - sending raw per-article alerts instead of a smart digest.")
            send_telegram_alerts(new_by_category)
    save_seen(seen)


if __name__ == "__main__":
    main()
