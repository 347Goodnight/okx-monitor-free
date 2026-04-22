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

function textParagraph(text) {
  return [{ tag: "text", text }];
}

function levelLabel(level) {
  if (level === "warning") {
    return "预警";
  }
  if (level === "error") {
    return "风险";
  }
  return "摘要";
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
  const title = body.title || "OKX 合约监控";
  const metrics =
    body.metrics && typeof body.metrics === "object"
      ? Object.entries(body.metrics)
      : [];
  const points = Array.isArray(body.points) ? body.points : [];
  const content = [];

  content.push(
    textParagraph(
      `【${levelLabel(body.level || "info")}】${body.source || "okx-monitor"}`
    )
  );

  if (body.summary || body.message) {
    content.push(textParagraph(body.summary || body.message));
  }

  if (body.symbol || body.strategy || body.confidence !== undefined) {
    const parts = [];
    if (body.symbol) {
      parts.push(`合约：${body.symbol}`);
    }
    if (body.strategy) {
      parts.push(`策略：${body.strategy}`);
    }
    if (body.confidence !== undefined) {
      parts.push(`置信度：${body.confidence}`);
    }
    if (parts.length) {
      content.push(textParagraph(parts.join(" | ")));
    }
  }

  for (const [key, value] of metrics) {
    content.push(textParagraph(`- ${key}：${value}`));
  }

  if (points.length) {
    content.push(textParagraph("关注点："));
    for (const point of points) {
      content.push(textParagraph(`• ${point}`));
    }
  }

  content.push(
    textParagraph(
      `时间：${new Date().toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai"
      })}`
    )
  );

  return {
    msg_type: "post",
    content: {
      post: {
        zh_cn: {
          title,
          content
        }
      }
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
