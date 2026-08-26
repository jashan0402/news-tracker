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


def summarize_article(title, source, article_text):
    client = genai.Client(api_key=nf.GEMINI_API_KEY)
    if article_text:
        content = (
            "Write a tight, plain-English gist (2-4 sentences max) of this news article for an "
            "equity research analyst covering metals, cement, and commodities. Just the core facts "
            "(what happened, key numbers) and why it matters - no padding, no restating the "
            "headline, no opinions beyond what's in the article.\n\n"
            f"Title: {title}\nSource: {source}\n\nArticle text:\n{article_text[:8000]}"
        )
    else:
        content = (
            "The full article text could not be retrieved (paywall or blocked). Based only on "
            "this headline, write 2-3 plain-English sentences on what this is likely about and "
            "why an equity research analyst covering metals, cement, and commodities might care, "
            "making clear this is inferred from the headline alone.\n\n"
            f"Title: {title}\nSource: {source}"
        )

    response = client.models.generate_content(model=SUMMARY_MODEL, contents=content)
    return response.text.strip()


def main():
    offset = load_offset()
    updates = get_updates(offset)

    if not updates:
        print("No new button taps.")
        return

    pending = nf.load_pending()
    already_handled = set()  # guards against firing multiple summaries if a button was tapped repeatedly

    for update in updates:
        offset = max(offset, update["update_id"] + 1)
        cq = update.get("callback_query")
        if not cq:
            continue

        short_id = cq.get("data", "")
        article = pending.get(short_id)

        if not article:
            answer_callback_query(cq["id"], "Sorry, this article has expired and can no longer be summarized.")
            continue

        if short_id in already_handled:
            answer_callback_query(cq["id"], "Already on it - one summary coming up.")
            continue
        already_handled.add(short_id)

        answer_callback_query(cq["id"], "Fetching and summarizing the article...")
        print(f"Summarizing: {article['title']}")

        try:
            article_text = fetch_article_text(article["link"])
            summary = summarize_article(article["title"], article["source"], article_text)
        except Exception as e:
            print(f"  [ERROR] Summarization crashed: {e}")
            nf.send_telegram_message(
                f"⚠️ Couldn't summarize this article right now:\n{article['title']}\n{article['link']}"
            )
            continue

        note = "" if article_text else "\n\n(Note: full article text wasn't accessible - summary is based on the headline alone.)"
        nf.send_telegram_message(
            f"\U0001F4DD <b>Summary</b>: {html.escape(article['title'])}\n"
            f"({html.escape(article['source'])})\n\n{html.escape(summary)}{note}",
            parse_mode="HTML",
        )

    save_offset(offset)


if __name__ == "__main__":
    main()
