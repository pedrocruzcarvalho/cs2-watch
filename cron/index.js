// Every 10 minutes: start the cs2-watch scan workflow on GitHub (workflow_dispatch).
// GH_TOKEN is a fine-grained token limited to the cs2-watch repo with Actions: read & write.
const URL = "https://api.github.com/repos/pedrocruzcarvalho/cs2-watch/actions/workflows/scan.yml/dispatches";

async function dispatch(env) {
  const r = await fetch(URL, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.GH_TOKEN}`,
      accept: "application/vnd.github+json",
      "user-agent": "cs2-watch-cron",
      "content-type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (r.status !== 204) throw new Error(`GitHub said ${r.status}: ${await r.text()}`);
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
};
