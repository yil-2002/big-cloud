// ===========================================================================
// hugecloud — Cloudflare Worker: doimiy ommaviy manzil (VPS'siz rejim)
//
// Muammo: bot GitHub Actions runner'ida ishlaydi va har ~6 soatda yangi
// runner'ga ko'chadi. Har safar yangi `*.trycloudflare.com` manzil paydo
// bo'ladi — Mini App tugmasi, ulashish havolalari esa o'zgarmasligi kerak.
//
// Yechim: shu Worker `https://hugecloud.<siz>.workers.dev` da doim turadi va
// barcha so'rovlarni **joriy** tunnel manziliga proksi qiladi. Joriy manzil
// Durable Object'da saqlanadi; GitHub Actions har ishga tushganda uni
// `POST /__hc/origin` (Bearer ORIGIN_SECRET) orqali yangilaydi.
//
// Bepul tarif: Workers 100k so'rov/kun, Durable Objects (SQLite) bepul.
// Durable Object'ga har so'rovda bormaslik uchun manzil izolyatda 30 s
// keshlanadi; tunnel javob bermasa kesh darhol tozalanadi.
// ===========================================================================

const CONTROL_PATH = "/__hc/origin";
const CACHE_MS = 30_000;
// Tunnel yopilgan bo'lsa Cloudflare 530 (Error 1033) qaytaradi. 502 ni bu yerga
// qo'shmaymiz: ilovaning o'zi ham 502 qaytarishi mumkin (Telegram xatosi).
const DEAD_ORIGIN_STATUS = 530;

/** Joriy origin manzilini saqlovchi yagona Durable Object. */
export class OriginStore {
  constructor(ctx) {
    this.ctx = ctx;
  }

  async fetch(request) {
    if (request.method === "PUT") {
      const record = await request.json();
      await this.ctx.storage.put("origin", record);
      return Response.json({ ok: true });
    }
    const record = (await this.ctx.storage.get("origin")) || null;
    return Response.json(record);
  }
}

let cached = { origin: null, expires: 0 };

function store(env) {
  return env.ORIGIN_STORE.get(env.ORIGIN_STORE.idFromName("current"));
}

async function getOrigin(env) {
  const now = Date.now();
  if (cached.origin && cached.expires > now) return cached.origin;
  const res = await store(env).fetch("https://store/origin");
  const record = await res.json();
  cached = { origin: record?.origin || null, expires: now + CACHE_MS };
  return cached.origin;
}

function timingSafeEqual(a, b) {
  const enc = new TextEncoder();
  const x = enc.encode(a);
  const y = enc.encode(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

async function handleControl(request, env) {
  if (request.method === "GET") {
    const res = await store(env).fetch("https://store/origin");
    const record = await res.json();
    // Manzilning o'zini oshkor qilmaymiz — faqat holat.
    return Response.json({ online: Boolean(record?.origin), updated_at: record?.updated_at || null });
  }
  if (request.method !== "POST") return new Response("method not allowed", { status: 405 });

  const secret = env.ORIGIN_SECRET || "";
  const auth = request.headers.get("Authorization") || "";
  if (!secret || !timingSafeEqual(auth, `Bearer ${secret}`)) {
    return new Response("forbidden", { status: 403 });
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return new Response("bad json", { status: 400 });
  }
  // `origin: null` = bot to'xtadi (offline sahifa ko'rsatiladi).
  let origin = null;
  if (body.origin) {
    let parsed;
    try {
      parsed = new URL(body.origin);
    } catch {
      return new Response("bad origin", { status: 400 });
    }
    if (parsed.protocol !== "https:") return new Response("origin must be https", { status: 400 });
    origin = parsed.origin;
  }
  await store(env).fetch("https://store/origin", {
    method: "PUT",
    body: JSON.stringify({ origin, updated_at: new Date().toISOString() }),
  });
  cached = { origin, expires: Date.now() + CACHE_MS };
  return Response.json({ ok: true, online: Boolean(origin) });
}

function offline(request) {
  const url = new URL(request.url);
  const wantsHtml = (request.headers.get("Accept") || "").includes("text/html");
  const headers = { "Retry-After": "30", "Cache-Control": "no-store" };
  if (!wantsHtml || url.pathname.startsWith("/api/")) {
    return Response.json({ detail: "service_restarting" }, { status: 503, headers });
  }
  const html = `<!doctype html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="20"><title>Huge Cloud</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px system-ui,sans-serif;background:#0f1115;color:#e8eaed;text-align:center;padding:16px}
p{color:#9aa0a6}</style></head><body><div><h2>☁️ Server qayta ishga tushmoqda</h2>
<p>Odatda 1–2 daqiqa davom etadi. Sahifa o'zi yangilanadi.</p></div></body></html>`;
  return new Response(html, { status: 503, headers: { ...headers, "Content-Type": "text/html; charset=utf-8" } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === CONTROL_PATH) return handleControl(request, env);

    const origin = await getOrigin(env);
    if (!origin) return offline(request);

    const target = new URL(url.pathname + url.search, origin);
    const headers = new Headers(request.headers);
    headers.delete("Host");
    headers.set("X-Forwarded-Host", url.host);
    headers.set("X-Forwarded-Proto", "https");
    const clientIp = request.headers.get("CF-Connecting-IP");
    if (clientIp) {
      headers.set("X-Forwarded-For", clientIp);
      headers.set("X-Real-IP", clientIp);
    }

    const hasBody = !["GET", "HEAD"].includes(request.method);
    let response;
    try {
      response = await fetch(target, {
        method: request.method,
        headers,
        body: hasBody ? request.body : undefined,
        redirect: "manual",
      });
    } catch {
      cached.expires = 0;
      return offline(request);
    }

    if (response.status === DEAD_ORIGIN_STATUS) {
      // Tunnel yopilgan (runner almashmoqda) — keyingi so'rovda yangi manzilni o'qiymiz.
      cached.expires = 0;
      return offline(request);
    }

    // Origin'ga ishora qiluvchi redirect'larni ommaviy domenga qaytarish.
    const location = response.headers.get("Location");
    if (location && location.startsWith(origin)) {
      const out = new Response(response.body, response);
      out.headers.set("Location", url.origin + location.slice(origin.length));
      return out;
    }
    return response;
  },
};
