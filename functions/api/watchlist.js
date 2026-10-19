// Hand-picked wallets to follow. Stored in KV under "watchlist"; the scanner reads it every 10 minutes.
// The whole site sits behind Cloudflare Access, so only allowed emails can reach this.
const ADDR = /0x[a-fA-F0-9]{40}/;
const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

async function list(env) {
  try { return JSON.parse((await env.CS2.get("watchlist")) || "{}"); } catch { return {}; }
}

export async function onRequestGet({ env }) {
  return json(await list(env));
}

export async function onRequestPost({ request, env }) {
  const body = await request.json().catch(() => ({}));
  const m = String(body.wallet || "").match(ADDR);
  if (!m) return json({ error: "Paste a Polymarket wallet address (0x followed by 40 characters) or a profile link." }, 400);
  const all = await list(env);
  if (Object.keys(all).length >= 200) return json({ error: "The list is full (200 wallets). Remove one first." }, 400);
  all[m[0].toLowerCase()] = {
    note: String(body.note || "").trim().slice(0, 200),
    added_at: Date.now() / 1000,
    added_by: request.headers.get("cf-access-authenticated-user-email") || "",
  };
  await env.CS2.put("watchlist", JSON.stringify(all));
  return json(all);
}

export async function onRequestDelete({ request, env }) {
  const w = (new URL(request.url).searchParams.get("wallet") || "").toLowerCase();
  const all = await list(env);
  if (!all[w]) return json({ error: "That wallet isn't on the hand-picked list." }, 404);
  delete all[w];
  await env.CS2.put("watchlist", JSON.stringify(all));
  return json(all);
}
