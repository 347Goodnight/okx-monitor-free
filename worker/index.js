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

function stripPrefix(text, prefixes) {
  const value = typeof text === "string" ? text.trim() : "";
  for (const prefix of prefixes) {
    if (value.startsWith(prefix)) {
      return value.slice(prefix.length).trim();
    }
  }
  return value;
}

function truncateText(text, maxLength = 84) {
  if (typeof text !== "string") {
    return "";
  }
  const value = text.trim();
  if (value.length <= maxLength) {
    return value;
  }
  return `${value.slice(0, maxLength - 1)}…`;
}

function sourceLabel(name) {
  const mapping = {
    "Wu Blockchain": "吴说",
    "Odaily Newsflash": "Odaily",
    ChainCatcher: "ChainCatcher",
    CoinDesk: "CoinDesk",
    Fed: "Fed",
    SEC: "SEC",
    Treasury: "Treasury",
    "SoSoValue ETF Flows": "SoSoValue"
  };
  return mapping[name] || name || "未知源";
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

function compactRankingLine(item) {
  return [
    `${item.position}. ${item.symbol} ${formatPrice(item.latest_price)}`,
    `15m ${signedPct(item.change_15m_pct)}`,
    `1h ${signedPct(item.change_1h_pct)}`,
    `24h ${signedPct(item.change_24h_pct)}`,
    `7d ${signedPct(item.change_7d_pct)}`,
    `策略 ${item.strategy}`
  ].join(" | ");
}

function summarizeBreadth(rankings) {
  const bullish = rankings.filter((item) => item.strategy === "趋势偏多").length;
  const bearish = rankings.filter((item) => item.strategy === "趋势偏空").length;
  const neutral = rankings.length - bullish - bearish;
  return `结构：偏多 ${bullish} | 偏空 ${bearish} | 观察 ${neutral}`;
}

function shouldShowSourceStatus(text) {
  if (typeof text !== "string" || !text.trim()) {
    return false;
  }
  if (text.includes("全部源不可用") || text.includes("降级跳过")) {
    return true;
  }
  const match = text.match(/(\d+)\s*\/\s*(\d+)\s*可用/);
  if (!match) {
    return false;
  }
  return Number(match[1]) < Number(match[2]);
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
  const title = body.title || "OKX 合约市值观察";
  const content = [];
  const headline = stripPrefix(body.headline, ["今日趋势分析："]);
  const summary = stripPrefix(body.summary, ["综合市场情绪："]);
  const externalSentiment = stripPrefix(body.external_sentiment, ["外部情绪温度："]);
  const newsSummary = stripPrefix(body.news_summary, ["消息面判断："]);
  const newsSourceStatus = stripPrefix(body.news_source_status, ["消息源状态：", "源状态："]);

  content.push(paragraph(`趋势判断：${headline || "先看 BTC 是否确认方向。"}`));
  if (summary) {
    content.push(paragraph(`市场情绪：${summary}`));
  }
  if (externalSentiment) {
    content.push(paragraph(`外部情绪：${externalSentiment}`));
  }
  const scanInterval = body.scan_interval_label || body.interval_label || "15 分钟轮询";
  const analysisFramework = body.analysis_framework || "15m 快照 + 1h/24h/7d/30d 对照";
  content.push(paragraph(`更新频率：${scanInterval}`));
  content.push(paragraph(`分析框架：${analysisFramework}`));

  content.push(paragraph("【消息面主驱动】"));
  if (newsSummary) {
    content.push(paragraph(`判断：${newsSummary}`));
  }
  if (shouldShowSourceStatus(newsSourceStatus)) {
    content.push(paragraph(`源状态：${newsSourceStatus}`));
  }

  const marketDrivers = Array.isArray(body.market_drivers) ? body.market_drivers : [];
  if (marketDrivers.length) {
    for (const [index, driver] of marketDrivers.entries()) {
      const tag = `${driver.theme} | ${sourceLabel(driver.source)} | ${driver.direction || "中性"}`;
      content.push(paragraph(`${index + 1}. [${tag}] ${truncateText(driver.headline, 72)}`));
      content.push(paragraph(`影响：${driver.impact}`));
    }
  } else {
    content.push(paragraph("暂无明确的宏观、政策或资金面主驱动，当前以盘面优先。"));
  }

  content.push(paragraph("【TOP 市值观察（OKX 永续）】"));
  const rankings = Array.isArray(body.rankings) ? body.rankings : [];
  if (rankings.length) {
    content.push(paragraph(summarizeBreadth(rankings)));
  }
  for (const item of rankings) {
    content.push(paragraph(compactRankingLine(item)));
  }

  const flags = Array.isArray(body.flags) ? body.flags : [];
  if (flags.length) {
    content.push(paragraph("【风险提示】"));
    for (const flag of flags.slice(0, 5)) {
      content.push(paragraph(`- ${flag}`));
    }
  }

  content.push(
    paragraph(
      `更新时间：${new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}`
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
