const OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses";
const DEFAULT_TIMEOUT_MS = 180_000;
const DEFAULT_MAX_BODY_BYTES = 2 * 1024 * 1024;

function json(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...extraHeaders,
    },
  });
}

function normalizePath(pathname) {
  if (pathname.startsWith("/v1/")) return pathname.slice(3);
  return pathname;
}

function positiveInt(value, fallback) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function readBearer(request) {
  const authorization = request.headers.get("Authorization") || "";
  const match = authorization.match(/^Bearer\s+(.+)$/i);
  if (match) return match[1];
  return request.headers.get("X-Proxy-Key");
}

function allowedModels(env) {
  const raw = (env.ALLOWED_MODELS || "").trim();
  if (!raw) return null;
  return new Set(raw.split(",").map((value) => value.trim()).filter(Boolean));
}

async function proxyResponses(request, env, requestId) {
  const maxBytes = positiveInt(env.MAX_BODY_BYTES, DEFAULT_MAX_BODY_BYTES);
  const contentLength = Number(request.headers.get("Content-Length") || "0");
  if (contentLength > maxBytes) {
    return json({ error: { message: "Request body is too large.", request_id: requestId } }, 413);
  }

  const bodyText = await request.text();
  if (new TextEncoder().encode(bodyText).byteLength > maxBytes) {
    return json({ error: { message: "Request body is too large.", request_id: requestId } }, 413);
  }

  let payload;
  try {
    payload = JSON.parse(bodyText);
  } catch {
    return json({ error: { message: "Invalid JSON body.", request_id: requestId } }, 400);
  }

  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return json({ error: { message: "JSON body must be an object.", request_id: requestId } }, 400);
  }

  const whitelist = allowedModels(env);
  if (whitelist && !whitelist.has(payload.model)) {
    return json({ error: { message: `Model '${payload.model ?? ""}' is not allowed.`, request_id: requestId } }, 403);
  }

  const controller = new AbortController();
  const timeoutMs = positiveInt(env.OPENAI_TIMEOUT_MS, DEFAULT_TIMEOUT_MS);
  const timeout = setTimeout(() => controller.abort("timeout"), timeoutMs);

  try {
    const upstream = await fetch(OPENAI_RESPONSES_URL, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.OPENAI_API_KEY}`,
        "Content-Type": "application/json",
        "Accept": payload.stream === true ? "text/event-stream" : "application/json",
      },
      body: bodyText,
      signal: controller.signal,
    });

    const headers = new Headers();
    headers.set("Cache-Control", "no-store");
    headers.set("Content-Type", upstream.headers.get("Content-Type") || "application/json; charset=utf-8");
    headers.set("X-Factorio-AI-Request-Id", requestId);
    const openaiRequestId = upstream.headers.get("x-request-id");
    if (openaiRequestId) headers.set("X-OpenAI-Request-Id", openaiRequestId);

    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers,
    });
  } catch (error) {
    const timedOut = controller.signal.aborted;
    return json(
      {
        error: {
          message: timedOut ? "OpenAI request timed out." : "OpenAI request failed before a response was received.",
          detail: String(error),
          request_id: requestId,
        },
      },
      timedOut ? 504 : 502,
    );
  } finally {
    clearTimeout(timeout);
  }
}

export default {
  async fetch(request, env) {
    const requestId = crypto.randomUUID();
    const url = new URL(request.url);
    const path = normalizePath(url.pathname);

    if (request.method === "GET" && path === "/health") {
      const cf = request.cf || {};
      return json({
        ok: true,
        service: "factorio-ai-openai-proxy",
        endpoint: "/v1/responses",
        openai_key_configured: Boolean(env.OPENAI_API_KEY),
        shared_secret_configured: Boolean(env.WORKER_SHARED_SECRET),
        edge: {
          country: cf.country ?? null,
          colo: cf.colo ?? null,
          region: cf.region ?? null,
          region_code: cf.regionCode ?? null,
          timezone: cf.timezone ?? null,
          placement: request.headers.get("cf-placement"),
          ray: request.headers.get("cf-ray"),
        },
        request_id: requestId,
      });
    }

    if (request.method !== "POST" || path !== "/responses") {
      return json({ error: { message: "Only POST /v1/responses is allowed.", request_id: requestId } }, 404);
    }

    if (!env.OPENAI_API_KEY || !env.WORKER_SHARED_SECRET) {
      return json({ error: { message: "Worker secrets are not configured.", request_id: requestId } }, 500);
    }

    const suppliedSecret = readBearer(request);
    if (!suppliedSecret || suppliedSecret !== env.WORKER_SHARED_SECRET) {
      return json({ error: { message: "Unauthorized.", request_id: requestId } }, 401);
    }

    return proxyResponses(request, env, requestId);
  },
};
