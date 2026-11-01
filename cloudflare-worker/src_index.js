const API_BASE = "https://api.telegram.org";
const FILE_BASE = "https://api.telegram.org/file";

function base64UrlDecodeBytes(s) {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + pad);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function base64UrlDecodeText(s) {
  return new TextDecoder().decode(base64UrlDecodeBytes(s));
}

function base64UrlEncodeBytes(bytes) {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
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

async function verifyUploadTicket(secret, token, expectedKind, fileSize) {
  if (!token || !token.includes(".")) return { ok: false, error: "Missing upload ticket" };
  const dot = token.lastIndexOf(".");
  const encoded = token.slice(0, dot);
  const sig = token.slice(dot + 1);
  const expected = await hmacHex(secret, encoded);
  if (!timingSafeEqual(expected, sig)) return { ok: false, error: "Invalid upload ticket" };

  let payload;
  try {
    payload = JSON.parse(base64UrlDecodeText(encoded));
  } catch {
    return { ok: false, error: "Invalid upload ticket payload" };
  }

  const now = Math.floor(Date.now() / 1000);
  if (!payload || payload.kind !== expectedKind || payload.exp < now || payload.exp > now + 3600) {
    return { ok: false, error: "Expired or invalid upload ticket" };
  }
  if (!payload.slug || !Number.isSafeInteger(payload.max_size) || fileSize <= 0 || fileSize > payload.max_size) {
    return { ok: false, error: "Invalid upload size" };
  }
  return { ok: true, payload };
}

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type"
  };
}

async function backupToTelegram(request, env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.MEDIA_MEDIA_SECRET || !env.TELEGRAM_CHAT_ID) {
    return new Response(JSON.stringify({ error: "Worker is not configured" }), {
      status: 500, headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }

  const supplied = request.headers.get("X-Media-Backup-Secret") || "";
  if (!timingSafeEqual(supplied, env.MEDIA_MEDIA_SECRET)) {
    return new Response(JSON.stringify({ error: "Forbidden" }), {
      status: 403, headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }

  let content;
  try {
    content = await request.arrayBuffer();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid backup body" }), {
      status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }
  if (!content.byteLength) {
    return new Response(JSON.stringify({ error: "Empty backup" }), {
      status: 400, headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }

  const blob = new Blob([content], { type: "application/json" });
  const tgForm = new FormData();
  tgForm.append("chat_id", env.TELEGRAM_CHAT_ID);
  tgForm.append("document", blob, "galleries_backup.json");
  tgForm.append("caption", "gallery-db-backup (auto)");

  const tg = await fetch(`${API_BASE}/bot${env.TELEGRAM_BOT_TOKEN}/sendDocument`, {
    method: "POST",
    body: tgForm
  });
  let info = {};
  try { info = await tg.json(); } catch (_) {}
  if (!tg.ok || !info.ok) {
    return new Response(JSON.stringify({ error: info.description || `Telegram backup failed (${tg.status})` }), {
      status: tg.status >= 400 ? tg.status : 502,
      headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }

  // Pinning is optional. If the bot lacks the pin permission, the backup
  // document still exists and the request remains successful.
  try {
    await fetch(`${API_BASE}/bot${env.TELEGRAM_BOT_TOKEN}/pinChatMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        chat_id: env.TELEGRAM_CHAT_ID,
        message_id: String(info.result?.message_id || 0),
        disable_notification: "true"
      })
    });
  } catch (_) {}

  return new Response(JSON.stringify({ ok: true }), {
    status: 200, headers: { ...corsHeaders(), "Content-Type": "application/json" }
  });
}

async function uploadToTelegram(request, env) {
  if (!env.TELEGRAM_BOT_TOKEN || !env.MEDIA_MEDIA_SECRET) {
    return new Response("Worker is not configured", { status: 500, headers: corsHeaders() });
  }

  let form;
  try {
    form = await request.formData();
  } catch {
    return new Response(JSON.stringify({ error: "Invalid multipart upload" }), {
      status: 400,
      headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }

  const file = form.get("file");
  const token = String(form.get("ticket") || "");
  const kind = String(form.get("kind") || "");
  if (!(file instanceof File)) {
    return new Response(JSON.stringify({ error: "Missing file" }), {
      status: 400,
      headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }
  if (!env.TELEGRAM_CHAT_ID) {
    return new Response(JSON.stringify({ error: "Worker is missing TELEGRAM_CHAT_ID secret" }), {
      status: 500,
      headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }

  const check = await verifyUploadTicket(env.MEDIA_MEDIA_SECRET, token, kind, file.size);
  if (!check.ok) {
    return new Response(JSON.stringify({ error: check.error }), {
      status: 403,
      headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }

  const filename = file.name || (kind === "original" ? "photo.jpg" : `${kind}.jpg`);
  const tgForm = new FormData();
  tgForm.append("chat_id", env.TELEGRAM_CHAT_ID);
  tgForm.append("document", file, filename);
  tgForm.append("caption", `gallery-asset ${kind} ${check.payload.slug}`);

  let tg;
  try {
    tg = await fetch(`${API_BASE}/bot${env.TELEGRAM_BOT_TOKEN}/sendDocument`, {
      method: "POST",
      body: tgForm
    });
  } catch (e) {
    return new Response(JSON.stringify({ error: "Telegram connection failed", detail: String(e) }), {
      status: 502,
      headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }

  let info = {};
  try { info = await tg.json(); } catch (_) {}
  if (!tg.ok || !info.ok || !info.result?.document?.file_id) {
    return new Response(JSON.stringify({ error: info.description || `Telegram upload failed (${tg.status})` }), {
      status: tg.status >= 400 ? tg.status : 502,
      headers: { ...corsHeaders(), "Content-Type": "application/json" }
    });
  }

  return new Response(JSON.stringify({
    ok: true,
    kind,
    file_id: info.result.document.file_id,
    message_id: info.result.message_id || 0,
    file_size: file.size
  }), {
    status: 200,
    headers: { ...corsHeaders(), "Content-Type": "application/json" }
  });
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: corsHeaders() });

    const url = new URL(request.url);

    if (url.pathname === "/upload") {
      if (request.method !== "POST") return new Response("Method not allowed", { status: 405, headers: corsHeaders() });
      return uploadToTelegram(request, env);
    }

    if (url.pathname === "/backup") {
      if (request.method !== "POST") return new Response("Method not allowed", { status: 405, headers: corsHeaders() });
      return backupToTelegram(request, env);
    }

    if (request.method !== "GET" && request.method !== "HEAD") return new Response("Method not allowed", { status: 405 });

    const match = url.pathname.match(/^\/media\/(thumb|preview|raw)\/([^/]+)$/);
    if (!match) return new Response("Not found", { status: 404 });

    const kind = match[1];
    let fileId;
    try {
      fileId = new TextDecoder().decode(base64UrlDecodeBytes(match[2]));
    } catch {
      return new Response("Bad file id", { status: 400 });
    }

    if (!env.TELEGRAM_BOT_TOKEN || !env.MEDIA_MEDIA_SECRET) {
      return new Response("Worker is not configured", { status: 500 });
    }

    const exp = url.searchParams.get("exp") || "";
    const sig = url.searchParams.get("sig") || "";
    const now = Math.floor(Date.now() / 1000);
    const expNum = Number(exp);
    if (!Number.isSafeInteger(expNum) || expNum < now || expNum > now + 7 * 86400) return new Response("Forbidden", { status: 403 });
    const expected = await hmacHex(env.MEDIA_MEDIA_SECRET, `${kind}|${fileId}|${expNum}`);
    if (!timingSafeEqual(expected, sig)) return new Response("Forbidden", { status: 403 });

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
