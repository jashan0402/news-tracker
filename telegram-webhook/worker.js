// Receives Telegram's webhook the instant a "Summarize" button is tapped.
// Immediately acknowledges the tap, then asks GitHub Actions to do the real
// work (fetch the article, summarize with Gemini, send the result) - keeping
// all the heavy logic in the existing Python scripts rather than duplicating
// it here.

const GITHUB_REPO = "jashan0402/news-tracker";

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("OK", { status: 200 });
    }

    let update;
    try {
      update = await request.json();
    } catch (e) {
      return new Response("OK", { status: 200 });
    }

    const cq = update.callback_query;
    if (cq) {
      const ackPromise = fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          callback_query_id: cq.id,
          text: "Fetching and summarizing...",
        }),
      });

      const dispatchPromise = fetch(`https://api.github.com/repos/${GITHUB_REPO}/dispatches`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GITHUB_PAT}`,
          "Accept": "application/vnd.github+json",
          "User-Agent": "news-tracker-webhook",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          event_type: "summarize",
          client_payload: {
            short_id: cq.data,
            callback_query_id: cq.id,
          },
        }),
      });

      await Promise.all([ackPromise, dispatchPromise]);
    }

    return new Response("OK", { status: 200 });
  },
};
