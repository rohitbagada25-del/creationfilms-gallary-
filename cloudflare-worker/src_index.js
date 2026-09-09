const API_BASE = "https://api.telegram.org";
const FILE_BASE = "https://api.telegram.org/file";

function base64UrlDecode(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + pad);
  let out = "";
  for (let i = 0; i < bin.length; i++) out += String.fromCharCode(bin.charCodeAt(i));
  return out;
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function hmacHex(secret, message) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const digest = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(message));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function validSignature(secret, kind, fileId, exp, sig) {
  const now = Math.floor(Date.now() / 1000);
  const expNum = Number(exp);
  if (!Number.isSafeInteger(expNum) || expNum < now || expNum > now + 7 * 86400) return false;
  const expected = await hmacHex(secret, `${kind}|${fileId}|${expNum}`);
  return timingSafeEqual(expected, sig || "");
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS"
  };
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: corsHeaders() });
    if (request.method !== "GET" && request.method !== "HEAD") return new Response("Method not allowed", { status: 405 });

    const url = new URL(request.url);
    const match = url.pathname.match(/^\/media\/(thumb|preview|raw)\/([^/]+)$/);
    if (!match) return new Response("Not found", { status: 404 });

    const kind = match[1];
    let fileId;
    try {
      fileId = base64UrlDecode(match[2]);
    } catch {
      return new Response("Bad file id", { status: 400 });
    }

    if (!env.TELEGRAM_BOT_TOKEN || !env.MEDIA_MEDIA_SECRET) {
      return new Response("Worker is not configured", { status: 500 });
    }

    if (!await validSignature(env.MEDIA_MEDIA_SECRET, kind, fileId, url.searchParams.get("exp") || "", url.searchParams.get("sig") || "")) {
      return new Response("Forbidden", { status: 403 });
    }

    const cacheKey = new Request(`${url.origin}/cached/${kind}/${match[2]}`, { method: "GET" });
    const cache = caches.default;
    const cached = await cache.match(cacheKey);
    if (cached) {
      const headers = new Headers(cached.headers);
      Object.entries(corsHeaders()).forEach(([k, v]) => headers.set(k, v));
      return new Response(cached.body, { status: cached.status, headers });
    }

    const lookup = await fetch(`${API_BASE}/bot${env.TELEGRAM_BOT_TOKEN}/getFile?file_id=${encodeURIComponent(fileId)}`);
    if (!lookup.ok) return new Response("Telegram lookup failed", { status: 502 });
    const info = await lookup.json();
    if (!info.ok || !info.result?.file_path) return new Response("Telegram file unavailable", { status: 502 });

    const tg = await fetch(`${FILE_BASE}/bot${env.TELEGRAM_BOT_TOKEN}/${info.result.file_path}`);
    if (!tg.ok) return new Response("Media unavailable", { status: tg.status });

    const headers = new Headers(tg.headers);
    headers.set("Cache-Control", "public, max-age=31536000, immutable");
    Object.entries(corsHeaders()).forEach(([k, v]) => headers.set(k, v));

    if (kind === "raw") {
      const filename = url.searchParams.get("filename");
      if (filename) headers.set("Content-Disposition", `attachment; filename="${filename.replace(/["\r\n]/g, "")}"`);
    }

    const response = new Response(tg.body, { status: 200, headers });
    ctx.waitUntil(cache.put(cacheKey, response.clone()));
    return response;
  }
};
