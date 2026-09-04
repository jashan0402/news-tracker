// Receives Telegram's webhook the instant a "Summarize" button is tapped.
// Immediately acknowledges the tap, then asks GitHub Actions to do the real
// work (fetch the article, summarize with Gemini, send the result) - keeping
// all the heavy logic in the existing Python scripts rather than duplicating
// it here.

const GITHUB_REPO = "jashan0402/news-tracker";

function triggerWorkflow(env) {
  return fetch(`https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/news-tracker.yml/dispatches`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GITHUB_PAT}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "news-tracker-webhook",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Manual unconditional nudge - always triggers a fresh run.
    if (url.pathname === "/nudge") {
      if (url.searchParams.get("token") !== env.TRIGGER_SECRET) {
        return new Response("Forbidden", { status: 403 });
      }
      const resp = await triggerWorkflow(env);
      return new Response(resp.ok ? "Triggered" : `Failed: ${resp.status}`, { status: resp.ok ? 200 : 502 });
    }

    // Safety-net endpoint for the hourly cloud routine: does the staleness
    // check AND the conditional trigger entirely server-side. The routine's
    // cloud sandbox can't reach api.github.com directly (org-level restriction
    // unrelated to our setup), so all GitHub API calls happen here instead -
    // the routine only ever needs to reach this one Worker endpoint.
    if (url.pathname === "/check-and-nudge") {
      if (url.searchParams.get("token") !== env.TRIGGER_SECRET) {
        return new Response("Forbidden", { status: 403 });
      }

      const listResp = await fetch(
        `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/news-tracker.yml/runs?per_page=1`,
        {
          headers: {
            "Authorization": `Bearer ${env.GITHUB_PAT}`,
            "Accept": "application/vnd.github+json",
            "User-Agent": "news-tracker-webhook",
          },
        }
      );
      if (!listResp.ok) {
        return new Response(`Failed to list runs: ${listResp.status}`, { status: 502 });
      }
      const data = await listResp.json();
      const lastRun = (data.workflow_runs || [])[0];
      const staleMs = 70 * 60 * 1000;
      const isStale = !lastRun || (Date.now() - new Date(lastRun.created_at).getTime()) > staleMs;

      if (!isStale) {
        return new Response(`OK - last run at ${lastRun.created_at}, not stale`, { status: 200 });
      }

      const dispatchResp = await triggerWorkflow(env);
      return new Response(
        dispatchResp.ok ? "Triggered - last run was stale or missing" : `Dispatch failed: ${dispatchResp.status}`,
        { status: dispatchResp.ok ? 200 : 502 }
      );
    }

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
      // Guard against duplicate summaries if the same button is tapped several
      // times quickly - remember this short_id for 2 minutes using the Worker's
      // built-in cache (no extra setup needed).
      const cacheKey = new Request(`https://dedup.internal/${cq.data}`);
      const cache = caches.default;
      const alreadySeen = await cache.match(cacheKey);

      if (alreadySeen) {
        await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            callback_query_id: cq.id,
            text: "Already on it - one summary coming up.",
          }),
        });
        return new Response("OK", { status: 200 });
      }

      await cache.put(cacheKey, new Response("1", { headers: { "Cache-Control": "max-age=120" } }));

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
            chat_id: String(cq.message.chat.id),
          },
        }),
      });

      await Promise.all([ackPromise, dispatchPromise]);
    }

    return new Response("OK", { status: 200 });
  },
};
