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

function paragraph(text) {
  return [{ tag: "text", text }];
}

function signedPct(value) {
  const arrow = value >= 0 ? "▲" : "▼";
  return `${arrow}${Math.abs(value).toFixed(2)}%`;
}

function formatPrice(value) {
  if (value >= 1000) {
    return value.toFixed(2);
  }
  if (value >= 1) {
    return value.toFixed(4);
  }
  return value.toFixed(6);
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
  const title = body.title || "📊 OKX 合约市值榜观察";
  const content = [];

  content.push(paragraph(body.headline || "今日趋势分析：🧭 主流币分化明显，先看方向确认。"));
  content.push(paragraph(body.summary || ""));
  content.push(paragraph(`🕒 观察周期：${body.interval_label || "15 分钟"}`));
  content.push(paragraph("🏁 TOP 10 市值榜（OKX 永续）"));

  const rankings = Array.isArray(body.rankings) ? body.rankings : [];
  for (const item of rankings) {
    content.push(paragraph(`🔹 ${item.position}. ${item.symbol}`));
    content.push(paragraph(`最新价🔥：${formatPrice(item.latest_price)}`));
    content.push(
      paragraph(`15分钟：${signedPct(item.change_15m_pct)}  /  1小时：${signedPct(item.change_1h_pct)}`)
    );
    content.push(
      paragraph(`今日涨跌：${signedPct(item.change_24h_pct)}  /  本周涨跌：${signedPct(item.change_7d_pct)}`)
    );
    content.push(paragraph(`本月涨跌：${signedPct(item.change_30d_pct)}`));
    content.push(paragraph(`策略：${item.strategy}`));
  }

  const news = Array.isArray(body.news) ? body.news : [];
  if (news.length) {
    content.push(paragraph("📰 消息面快照"));
    for (const headline of news) {
      content.push(paragraph(`• ${headline}`));
    }
  }

  const flags = Array.isArray(body.flags) ? body.flags : [];
  if (flags.length) {
    content.push(paragraph("⚠️ 风险提示"));
    for (const flag of flags) {
      content.push(paragraph(`• ${flag}`));
    }
  }

  content.push(
    paragraph(
      `⏰ ${new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}`
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

      try {
        const result = await sendFeishuAlert(
          env.FEISHU_WEBHOOK_URL,
          buildFeishuPayload(body)
        );
        return json({ ok: true, forwarded: true, feishu: result });
      } catch (error) {
        return json({ ok: false, error: error.message }, 502);
      }
    }

    return json({ ok: false, error: "Not found" }, 404);
  }
};
