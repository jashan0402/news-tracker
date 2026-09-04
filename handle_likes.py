"""
Checks Telegram for any "Summarize" button taps since the last check, and for
each one, fetches the full article, asks Gemini for a proper summary, and
sends it back. Meant to run frequently (every few minutes) as its own
lightweight job, separate from the main hourly news_fetcher.py run.
"""

import html
import json
import os

import requests
import trafilatura
from google import genai
from google.genai import types

import news_fetcher as nf

OFFSET_FILE = "telegram_offset.json"
SUMMARY_MODEL = nf.DIGEST_MODEL


def load_offset():
    if not os.path.exists(OFFSET_FILE):
        return 0
    with open(OFFSET_FILE, "r") as f:
        return json.load(f).get("offset", 0)


def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        json.dump({"offset": offset}, f)


def get_updates(offset):
    url = f"https://api.telegram.org/bot{nf.TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "offset": offset,
        "timeout": 0,
        "allowed_updates": json.dumps(["callback_query"]),
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json().get("result", [])


def answer_callback_query(callback_query_id, text):
    url = f"https://api.telegram.org/bot{nf.TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    try:
        requests.post(url, data={"callback_query_id": callback_query_id, "text": text}, timeout=10)
    except Exception as e:
        print(f"  [ERROR] answerCallbackQuery crashed: {e}")


def fetch_article_text(url):
    """Follows the Google News redirect and pulls the main article text.
    Returns None if the site can't be scraped (paywall, JS-only, blocked, etc.)."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded)
        return text.strip() if text and len(text.strip()) > 200 else None
    except Exception as e:
        print(f"  [ERROR] Article fetch/extract crashed: {e}")
        return None


def summarize_topic(topic, category, articles_with_text):
    """articles_with_text: list of {title, source, link, text} - text is None if
    that particular article couldn't be fetched. Combines all sources covering the
    same topic into one gist rather than summarizing each article separately."""
    client = genai.Client(api_key=nf.GEMINI_API_KEY)

    source_blocks = []
    for a in articles_with_text:
        if a["text"]:
            source_blocks.append(f"--- Source: {a['source']} ---\n{a['text'][:6000]}")
        else:
            source_blocks.append(f"--- Source: {a['source']} (full text not accessible) ---\nHeadline only: {a['title']}")
    combined_sources = "\n\n".join(source_blocks)

    content = (
        f"Write a plain-English summary (4-6 sentences) of this news topic - "
        f"\"{topic}\" ({category}) - for an equity research analyst covering metals, cement, "
        "and commodities. The sources below all cover the same underlying story - combine them "
        "into ONE coherent summary. Include the key facts, specific numbers/figures, relevant "
        "context, and why it matters for the sector/market. Don't repeat the same fact separately "
        "per source, and don't add opinions beyond what's in the sources.\n\n"
        f"{combined_sources}"
    )

    response = client.models.generate_content(model=SUMMARY_MODEL, contents=content)
    return response.text.strip()


def process_tap(short_id, callback_query_id, pending, already_handled, reply_chat_id=None):
    """Handles one button tap: looks up its topic bundle, fetches every article in
    it, asks Gemini for one combined summary, and sends it back to whichever chat
    the button was tapped in (reply_chat_id) rather than broadcasting to everyone."""
    bundle = pending.get(short_id)

    if not bundle or "articles" not in bundle:
        answer_callback_query(callback_query_id, "Sorry, this topic has expired and can no longer be summarized.")
        return

    if short_id in already_handled:
        answer_callback_query(callback_query_id, "Already on it - one summary coming up.")
        return
    already_handled.add(short_id)

    articles = bundle["articles"]
    answer_callback_query(callback_query_id, f"Fetching and summarizing {len(articles)} article(s)...")
    print(f"Summarizing topic: {bundle['topic']}")

    try:
        fetched = [{**a, "text": fetch_article_text(a["link"])} for a in articles]
        summary = summarize_topic(bundle["topic"], bundle["category"], fetched)
    except Exception as e:
        print(f"  [ERROR] Summarization crashed: {e}")
        links = "\n".join(a["link"] for a in articles)
        nf.send_telegram_message(
            f"⚠️ Couldn't summarize this topic right now:\n{bundle['topic']}\n{links}",
            chat_id=reply_chat_id,
        )
        return

    any_text = any(a["text"] for a in fetched)
    all_text = all(a["text"] for a in fetched)
    if all_text:
        note = ""
    elif any_text:
        note = "\n\n(Note: full text wasn't accessible for one or more sources; summary combines what could be retrieved.)"
    else:
        note = "\n\n(Note: full article text wasn't accessible for any source - summary is based on headlines alone.)"

    nf.send_telegram_message(
        f"\U0001F4DD <b>Summary</b>: {html.escape(bundle['topic'])} ({html.escape(bundle['category'])})\n\n"
        f"{html.escape(summary)}{note}",
        parse_mode="HTML",
        chat_id=reply_chat_id,
    )


def main():
    pending = nf.load_pending()
    already_handled = set()  # guards against firing multiple summaries if a button was tapped repeatedly

    # Instant path: Cloudflare Worker dispatched this run for one specific tap.
    dispatched_short_id = os.environ.get("SHORT_ID")
    dispatched_cq_id = os.environ.get("CALLBACK_QUERY_ID")
    dispatched_chat_id = os.environ.get("REPLY_CHAT_ID")
    if dispatched_short_id and dispatched_cq_id:
        process_tap(dispatched_short_id, dispatched_cq_id, pending, already_handled, reply_chat_id=dispatched_chat_id)
        return

    # Fallback path (local testing only): poll Telegram directly for taps.
    # Note - this won't see anything once a webhook is registered with Telegram,
    # since a bot can only receive updates one way (webhook OR getUpdates) at a time.
    offset = load_offset()
    updates = get_updates(offset)

    if not updates:
        print("No new button taps.")
        return

    for update in updates:
        offset = max(offset, update["update_id"] + 1)
        cq = update.get("callback_query")
        if not cq:
            continue
        tap_chat_id = str(cq.get("message", {}).get("chat", {}).get("id", "")) or None
        process_tap(cq.get("data", ""), cq["id"], pending, already_handled, reply_chat_id=tap_chat_id)

    save_offset(offset)


if __name__ == "__main__":
    main()
