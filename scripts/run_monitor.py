from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
COINDESK_RSS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"
OKX_BASE_URL = "https://www.okx.com"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1&format=json"


@dataclass
class Thresholds:
    change_15m_pct: float
    hourly_change_pct: float
    daily_change_pct: float
    weekly_change_pct: float


@dataclass
class ScanConfig:
    market_cap_top_n: int
    candidate_pool: int
    quote: str
    news_headlines: int
    exclude_symbols: list[str]
    thresholds: Thresholds


def http_get_text(url: str) -> str:
    last_error = None

    for attempt in range(5):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "okx-monitor-free/0.4",
                "Accept": "application/json, application/xml, text/xml, */*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read().decode("utf-8", errors="ignore")
        except urllib.error.URLError as error:
            last_error = error
            if attempt < 4:
                time.sleep(1.5)

    raise last_error


def http_get_json(url: str) -> dict | list:
    return json.loads(http_get_text(url))


def coingecko_get(path: str, params: dict[str, str]) -> dict | list:
    query = urllib.parse.urlencode(params)
    return http_get_json(f"{COINGECKO_BASE_URL}{path}?{query}")


def okx_get(path: str, params: dict[str, str]) -> dict:
    query = urllib.parse.urlencode(params)
    payload = http_get_json(f"{OKX_BASE_URL}{path}?{query}")
    if not isinstance(payload, dict) or payload.get("code") != "0":
        raise RuntimeError(f"OKX request failed for {path}: {payload}")
    return payload


def okx_data(path: str, params: dict[str, str]) -> list[dict]:
    return okx_get(path, params)["data"]


def load_config(path: Path) -> ScanConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return ScanConfig(
        market_cap_top_n=raw["market_cap_top_n"],
        candidate_pool=raw["candidate_pool"],
        quote=raw["quote"],
        news_headlines=raw["news_headlines"],
        exclude_symbols=[symbol.upper() for symbol in raw["exclude_symbols"]],
        thresholds=Thresholds(**raw["thresholds"]),
    )


def get_fear_greed_score() -> tuple[int, str]:
    payload = http_get_json(FEAR_GREED_URL)
    score = int(payload["data"][0]["value"])
    return score, payload["data"][0]["value_classification"]


def get_news_headlines(limit: int) -> list[str]:
    root = ET.fromstring(http_get_text(COINDESK_RSS_URL))
    headlines = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if title:
            headlines.append(title)
        if len(headlines) >= limit:
            break
    return headlines


def get_okx_swap_universe(quote: str) -> set[str]:
    rows = okx_data("/api/v5/public/instruments", {"instType": "SWAP"})
    suffix = f"-{quote}-SWAP"
    return {
        row["instId"]
        for row in rows
        if row.get("instId", "").endswith(suffix) and row.get("state") == "live"
    }


def get_top_market_cap_coins(config: ScanConfig, okx_swaps: set[str]) -> list[dict]:
    rows = coingecko_get(
        "/coins/markets",
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": str(config.candidate_pool),
            "page": "1",
            "sparkline": "false",
            "price_change_percentage": "1h,24h,7d,30d",
        },
    )

    coins = []
    for row in rows:
        symbol = row["symbol"].upper()
        if symbol in config.exclude_symbols:
            continue

        swap_symbol = f"{symbol}-{config.quote}-SWAP"
        if swap_symbol not in okx_swaps:
            continue

        coins.append(
            {
                "name": row["name"],
                "symbol": symbol,
                "swap_symbol": swap_symbol,
                "market_cap": float(row["market_cap"] or 0),
                "change_1h_pct": float(row.get("price_change_percentage_1h_in_currency") or 0),
                "change_24h_pct": float(row.get("price_change_percentage_24h_in_currency") or 0),
                "change_7d_pct": float(row.get("price_change_percentage_7d_in_currency") or 0),
                "change_30d_pct": float(row.get("price_change_percentage_30d_in_currency") or 0),
            }
        )

        if len(coins) >= config.market_cap_top_n:
            break

    for index, coin in enumerate(coins, start=1):
        coin["position"] = index

    return coins


def get_candles(symbol: str, bar: str, limit: int) -> list[dict]:
    rows = []
    for item in okx_data(
        "/api/v5/market/candles",
        {"instId": symbol, "bar": bar, "limit": str(limit)},
    ):
        rows.append(
            {
                "ts": int(item[0]),
                "close": float(item[4]),
            }
        )
    rows.sort(key=lambda row: row["ts"])
    return rows


def pct_change(current: float, reference: float) -> float:
    if reference == 0:
        return 0.0
    return ((current - reference) / reference) * 100


def arrow(value: float) -> str:
    return "▲" if value >= 0 else "▼"


def signed_pct(value: float) -> str:
    return f"{arrow(value)}{abs(value):.2f}%"


def format_price(value: float) -> str:
    if value >= 1000:
        return f"{value:.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


def classify_contract_setup(change_15m_pct: float, change_1h_pct: float, change_24h_pct: float) -> str:
    if change_15m_pct > 0 and change_1h_pct > 0 and change_24h_pct > 0:
        return "🚀 趋势偏多"
    if change_15m_pct < 0 and change_1h_pct < 0 and change_24h_pct < 0:
        return "🔻 趋势偏空"
    if abs(change_15m_pct) <= 0.3 and abs(change_1h_pct) <= 0.8:
        return "🧊 震荡等待"
    return "👀 方向待确认"


def translate_fear_greed_label(label: str) -> str:
    mapping = {
        "Extreme Fear": "😱 极度恐慌",
        "Fear": "😟 恐慌",
        "Neutral": "😐 中性",
        "Greed": "🙂 贪婪",
        "Extreme Greed": "🤯 极度贪婪",
    }
    return mapping.get(label, label)


def explain_fear_greed(score: int) -> str:
    if score <= 25:
        return "说明场外资金明显偏谨慎，遇到利空时更容易放大波动。"
    if score <= 45:
        return "说明市场仍偏谨慎，追涨意愿不强。"
    if score <= 55:
        return "说明市场整体偏观望，需要消息面和盘面一起确认方向。"
    if score <= 75:
        return "说明市场风险偏好正在回升，顺势行情更容易延续。"
    return "说明市场情绪已经偏热，注意冲高后的回撤风险。"


def score_news_headline(headline: str) -> tuple[int, str]:
    lower = headline.lower()

    macro_keywords = [
        "trump",
        "fed",
        "federal reserve",
        "powell",
        "cpi",
        "inflation",
        "tariff",
        "war",
        "ceasefire",
        "truce",
        "iran",
        "israel",
        "geopolitical",
        "treasury",
        "rates",
        "rate cut",
    ]
    policy_keywords = [
        "sec",
        "regulation",
        "regulatory",
        "lawmakers",
        "bill",
        "senate",
        "house",
        "ban",
        "approval",
        "approve",
        "approved",
    ]
    fund_flow_keywords = [
        "etf",
        "inflow",
        "outflow",
        "treasury reserve",
        "buyback",
        "capital",
        "institutional",
    ]
    risk_keywords = [
        "hack",
        "exploit",
        "liquidation",
        "sell-off",
        "selloff",
        "lawsuit",
        "crash",
        "tension",
        "attack",
        "conflict",
        "default",
    ]
    positive_keywords = [
        "ceasefire",
        "truce",
        "approval",
        "approved",
        "inflow",
        "surge",
        "rally",
        "gain",
        "record high",
        "optimism",
        "easing",
        "support",
        "bullish",
    ]
    negative_keywords = [
        "war",
        "tariff",
        "hack",
        "exploit",
        "outflow",
        "selloff",
        "sell-off",
        "lawsuit",
        "liquidation",
        "attack",
        "conflict",
        "ban",
        "crackdown",
        "delay",
        "fear",
        "slump",
        "drop",
        "fall",
    ]

    score = 0
    theme = "行业动态"
    if any(keyword in lower for keyword in macro_keywords):
        score += 6
        theme = "宏观/地缘"
    if any(keyword in lower for keyword in policy_keywords):
        score += 4
        theme = "政策/监管" if theme == "行业动态" else theme
    if any(keyword in lower for keyword in fund_flow_keywords):
        score += 4
        theme = "资金/ETF" if theme == "行业动态" else theme
    if any(keyword in lower for keyword in risk_keywords):
        score += 4
        theme = "风险事件" if theme == "行业动态" else theme
    if "bitcoin" in lower or "crypto" in lower or "market" in lower:
        score += 1

    positive_hits = sum(keyword in lower for keyword in positive_keywords)
    negative_hits = sum(keyword in lower for keyword in negative_keywords)
    score += positive_hits + negative_hits

    return score, theme


def build_news_snapshot(headlines: list[str], limit: int) -> tuple[str, list[str]]:
    if not headlines:
        return (
            "📰 消息面判断：暂未捕捉到足以带动全市场联动的明确事件，先以盘面走势为主。",
            [],
        )

    ranked = []
    for index, headline in enumerate(headlines):
        score, theme = score_news_headline(headline)
        ranked.append((score, index, theme, headline))

    ranked.sort(key=lambda item: (-item[0], item[1]))
    meaningful = [item for item in ranked if item[0] >= 6 and item[2] != "行业动态"]
    if not meaningful:
        return (
            "📰 消息面判断：当前暂无特别明确的宏观、监管或资金面事件在驱动全市场，先重点看盘面是否自行走趋势。",
            [],
        )

    selected_group = meaningful[: min(limit, len(meaningful))]
    selected = [item[3] for item in selected_group]

    theme_counts: dict[str, int] = {}
    lower_joined = " ".join(selected).lower()
    for _, _, theme, _ in selected_group:
        theme_counts[theme] = theme_counts.get(theme, 0) + 1
    dominant_theme = max(theme_counts, key=theme_counts.get)

    positive_keywords = ["ceasefire", "truce", "approval", "approved", "inflow", "surge", "rally", "optimism", "easing", "bullish"]
    negative_keywords = ["war", "tariff", "hack", "exploit", "outflow", "selloff", "sell-off", "lawsuit", "liquidation", "attack", "conflict", "ban", "crackdown", "fear"]
    positive_hits = sum(keyword in lower_joined for keyword in positive_keywords)
    negative_hits = sum(keyword in lower_joined for keyword in negative_keywords)

    if positive_hits > negative_hits:
        direction = "偏利多"
        impact = "更容易带动主流币同步走强"
    elif negative_hits > positive_hits:
        direction = "偏利空"
        impact = "更容易放大全市场回撤"
    else:
        direction = "影响中性"
        impact = "是否形成联动还要看盘面是否继续共振"

    return (
        f"📰 消息面判断：当前更像{dominant_theme}驱动，整体{direction}，{impact}。",
        selected,
    )


def build_symbol_report(coin: dict, thresholds: Thresholds) -> tuple[dict, list[str]]:
    swap_symbol = coin["swap_symbol"]
    candles_15m = get_candles(swap_symbol, "15m", 2)
    latest_price = candles_15m[-1]["close"]
    change_15m_pct = pct_change(candles_15m[-1]["close"], candles_15m[-2]["close"])

    strategy = classify_contract_setup(
        change_15m_pct,
        coin["change_1h_pct"],
        coin["change_24h_pct"],
    )

    report = {
        "position": coin["position"],
        "symbol": coin["symbol"],
        "latest_price": latest_price,
        "change_15m_pct": change_15m_pct,
        "change_1h_pct": coin["change_1h_pct"],
        "change_24h_pct": coin["change_24h_pct"],
        "change_7d_pct": coin["change_7d_pct"],
        "change_30d_pct": coin["change_30d_pct"],
        "strategy": strategy,
    }

    flags = []
    if abs(change_15m_pct) >= thresholds.change_15m_pct:
        flags.append(f"⚡ {coin['symbol']} 15分钟异动 {signed_pct(change_15m_pct)}")
    if abs(coin["change_1h_pct"]) >= thresholds.hourly_change_pct:
        flags.append(f"📈 {coin['symbol']} 1小时趋势拉伸 {signed_pct(coin['change_1h_pct'])}")
    if abs(coin["change_24h_pct"]) >= thresholds.daily_change_pct:
        flags.append(f"🌋 {coin['symbol']} 日内波动较大 {signed_pct(coin['change_24h_pct'])}")
    if abs(coin["change_7d_pct"]) >= thresholds.weekly_change_pct:
        flags.append(f"📅 {coin['symbol']} 周线趋势显著 {signed_pct(coin['change_7d_pct'])}")

    return report, flags


def classify_sentiment(score: int) -> str:
    if score <= 20:
        return "😱 极度恐慌"
    if score <= 40:
        return "😟 恐慌"
    if score <= 60:
        return "😐 中性"
    if score <= 80:
        return "🙂 贪婪"
    return "🤯 极度贪婪"


def compute_market_sentiment(reports: list[dict], fear_greed_score: int) -> dict:
    avg_1h = sum(report["change_1h_pct"] for report in reports) / len(reports)
    avg_24h = sum(report["change_24h_pct"] for report in reports) / len(reports)

    score = int(
        round(
            max(min(50 + avg_1h * 6 + avg_24h * 1.5, 100), 0) * 0.6
            + fear_greed_score * 0.4
        )
    )
    return {"score": score, "label": classify_sentiment(score)}


def build_market_digest(
    reports: list[dict],
    sentiment: dict,
    fear_greed_score: int,
    fear_greed_label: str,
    flags: list[str],
    headlines: list[str],
) -> dict:
    bullish = [report["symbol"] for report in reports if "偏多" in report["strategy"]]
    bearish = [report["symbol"] for report in reports if "偏空" in report["strategy"]]

    if len(bullish) >= 4:
        trend = "🚀 主流币整体偏强，短线多头更占优。"
    elif len(bearish) >= 4:
        trend = "🔻 主流币整体承压，短线空头更占优。"
    else:
        trend = "🧭 主流币分化明显，先看方向确认。"

    news_summary, selected_headlines = build_news_snapshot(headlines, 3)
    external_sentiment_label = translate_fear_greed_label(fear_greed_label)

    return {
        "title": "📊 OKX 合约市值榜观察",
        "headline": f"今日趋势分析：{trend}",
        "summary": f"综合市场情绪：{sentiment['label']}（{sentiment['score']}/100）",
        "external_sentiment": (
            f"外部情绪温度：{external_sentiment_label}（{fear_greed_score}/100），"
            f"{explain_fear_greed(fear_greed_score)}"
        ),
        "news_summary": news_summary,
        "rankings": reports,
        "flags": flags[:8],
        "news": selected_headlines,
        "interval_label": "15 分钟",
    }


def post_alert(endpoint: str, token: str | None, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36"
            ),
        },
    )
    if token:
        request.add_header("x-alert-token", token)

    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def format_delivery_error(endpoint: str, error: Exception) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    location = f"{parsed.netloc}{parsed.path or '/'}"
    if isinstance(error, urllib.error.HTTPError):
        body = error.read().decode("utf-8", errors="ignore").strip()
        body = body.replace("\n", " ")[:240] if body else ""
        return f"{location} -> HTTP {error.code}: {body}" if body else f"{location} -> HTTP {error.code}"
    return f"{location} -> {error}"


def write_summary(
    summary_path: Path,
    digest: dict,
    sentiment: dict,
    reports: list[dict],
    delivery_errors: list[str],
) -> None:
    lines = [
        "# OKX Futures Market Cap Digest",
        "",
        f"- 综合市场情绪：**{sentiment['label']}** ({sentiment['score']}/100)",
        f"- {digest['external_sentiment']}",
        f"- 榜单范围：**TOP {len(reports)} 市值币（OKX 永续）**",
        f"- 观察周期：**15 分钟**",
        "",
        f"## {digest['headline']}",
        "",
    ]

    if digest["news_summary"] or digest["news"]:
        lines.extend(["## 消息面快照", ""])
        if digest["news_summary"]:
            lines.append(f"- {digest['news_summary']}")
        for headline in digest["news"]:
            lines.append(f"- {headline}")
        lines.extend(["", "## TOP 10 市值榜（OKX 永续）", ""])

    lines.extend([
        "| 排名 | 币种 | 最新价 | 15分钟 | 1小时 | 今日涨跌 | 本周涨跌 | 本月涨跌 | 策略 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ])

    for report in reports:
        lines.append(
            "| {rank} | {symbol} | {latest_price} | {change_15m} | {change_1h} | {change_24h} | {change_7d} | {change_30d} | {strategy} |".format(
                rank=report["position"],
                symbol=report["symbol"],
                latest_price=format_price(report["latest_price"]),
                change_15m=signed_pct(report["change_15m_pct"]),
                change_1h=signed_pct(report["change_1h_pct"]),
                change_24h=signed_pct(report["change_24h_pct"]),
                change_7d=signed_pct(report["change_7d_pct"]),
                change_30d=signed_pct(report["change_30d_pct"]),
                strategy=report["strategy"],
            )
        )

    if digest["flags"]:
        lines.extend(["", "## 风险提示", ""])
        for flag in digest["flags"]:
            lines.append(f"- {flag}")

    if delivery_errors:
        lines.extend(["", "## 告警发送异常", ""])
        for error in delivery_errors:
            lines.append(f"- {error}")

    content = "\n".join(lines) + "\n"
    summary_path.write_text(content, encoding="utf-8")
    github_step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if github_step_summary:
        Path(github_step_summary).write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--summary-file", type=Path, default=Path("monitor-summary.md"))
    args = parser.parse_args()

    config = load_config(args.config)
    okx_swaps = get_okx_swap_universe(config.quote)
    top_coins = get_top_market_cap_coins(config, okx_swaps)
    reports = []
    flags = []

    for coin in top_coins:
        report, report_flags = build_symbol_report(coin, config.thresholds)
        reports.append(report)
        flags.extend(report_flags)

    fear_greed_score, fear_greed_label = get_fear_greed_score()
    sentiment = compute_market_sentiment(reports, fear_greed_score)
    headlines = get_news_headlines(max(config.news_headlines * 6, 18))
    digest = build_market_digest(
        reports,
        sentiment,
        fear_greed_score,
        fear_greed_label,
        flags,
        headlines,
    )

    endpoint = os.getenv("ALERT_ENDPOINT")
    token = os.getenv("ALERT_TOKEN")
    delivery_errors = []

    if endpoint:
        try:
            post_alert(endpoint, token, digest)
        except Exception as error:
            delivery_errors.append(format_delivery_error(endpoint, error))

    write_summary(args.summary_file, digest, sentiment, reports, delivery_errors)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.URLError as error:
        print(f"Network error: {error}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(f"Monitor failed: {error}", file=sys.stderr)
        raise SystemExit(1)
