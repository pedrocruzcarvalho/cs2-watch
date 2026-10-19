// Serves the latest dashboard snapshot written by cloud_run.py. The whole site sits behind Cloudflare Access.
export async function onRequestGet({ env }) {
  const body = await env.CS2.get("dashboard");
  return new Response(body || "{}", {
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}
