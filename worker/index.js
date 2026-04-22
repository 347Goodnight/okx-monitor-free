function json(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8"
    }
  });
}

function unauthorized() {
  return json({ ok: false, error: "Unauthorized" }, 401);
}

async function sendFeishuAlert(webhook, payload) {
  const response = await fetch(webhook, {
    method: "POST",
    headers: {
      "content-type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  const text = await response.text();

  if (!response.ok) {
    throw new Error(`Feishu webhook failed: ${response.status} ${text}`);
  }

  return text;
}

function buildFeishuPayload(body) {
  const title = body.title || "OKX 监控测试";
  const lines = [
    "OKX",
    `标题：${title}`,
    `级别：${body.level || "info"}`,
    `内容：${body.message || "测试消息"}`,
    `来源：${body.source || "cloudflare-worker"}`,
    `时间：${new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}`
  ];

  return {
    msg_type: "text",
    content: {
      text: lines.join("\n")
    }
  };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/healthz") {
      return json({
        ok: true,
        service: "okx-monitor-free-worker",
        now: new Date().toISOString()
      });
    }

    if (request.method === "POST" && url.pathname === "/alert") {
      const expectedToken = env.ALERT_AUTH_TOKEN;
      const token =
        request.headers.get("x-alert-token") ||
        request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");

      if (expectedToken && token !== expectedToken) {
        return unauthorized();
      }

      if (!env.FEISHU_WEBHOOK_URL) {
        return json(
          { ok: false, error: "Missing FEISHU_WEBHOOK_URL secret" },
          500
        );
      }

      let body;
      try {
        body = await request.json();
      } catch {
        return json({ ok: false, error: "Request body must be JSON" }, 400);
      }

      const payload = buildFeishuPayload(body);

      try {
        const result = await sendFeishuAlert(env.FEISHU_WEBHOOK_URL, payload);
        return json({ ok: true, forwarded: true, feishu: result });
      } catch (error) {
        return json({ ok: false, error: error.message }, 502);
      }
    }

    return json({ ok: false, error: "Not found" }, 404);
  }
};
