from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
OKX_BASE_URL = "https://www.okx.com"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1&format=json"


@dataclass
class Thresholds:
    change_15m_pct: float
    hourly_change_pct: float
    daily_change_pct: float
    weekly_change_pct: float
    funding_rate_pct: float
    mark_basis_pct: float


@dataclass
class ScanConfig:
    market_cap_top_n: int
    candidate_pool: int
    quote: str
    exclude_symbols: list[str]
    thresholds: Thresholds


def http_get_json(url: str) -> dict | list:
    last_error = None

    for attempt in range(5):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "okx-monitor-free/0.3",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as error:
            last_error = error
            if attempt < 4:
                time.sleep(1.5)

    raise last_error


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
        exclude_symbols=[symbol.upper() for symbol in raw["exclude_symbols"]],
        thresholds=Thresholds(**raw["thresholds"]),
    )


def get_fear_greed_score() -> tuple[int, str]:
    payload = http_get_json(FEAR_GREED_URL)
    score = int(payload["data"][0]["value"])
    return score, payload["data"][0]["value_classification"]


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
                "market_cap_rank": row["market_cap_rank"],
                "name": row["name"],
                "symbol": symbol,
                "swap_symbol": swap_symbol,
                "market_cap": float(row["market_cap"] or 0),
                "current_price_usd": float(row["current_price"] or 0),
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
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
            }
        )
    rows.sort(key=lambda row: row["ts"])
    return rows


def get_mark_price(symbol: str) -> float:
    return float(
        okx_data(
            "/api/v5/public/mark-price",
            {"instType": "SWAP", "instId": symbol},
        )[0]["markPx"]
    )


def get_funding_rate(symbol: str) -> float:
    return float(okx_data("/api/v5/public/funding-rate", {"instId": symbol})[0]["fundingRate"])


def get_open_interest(symbol: str) -> float:
    return float(
        okx_data(
            "/api/v5/public/open-interest",
            {"instType": "SWAP", "instId": symbol},
        )[0]["oi"]
    )


def pct_change(current: float, reference: float) -> float:
    if reference == 0:
        return 0.0
    return ((current - reference) / reference) * 100


def summarize_oi(oi: float) -> str:
    if oi >= 1_000_000_000:
        return f"{oi / 1_000_000_000:.2f}B"
    if oi >= 1_000_000:
        return f"{oi / 1_000_000:.2f}M"
    if oi >= 1_000:
        return f"{oi / 1_000:.2f}K"
    return f"{oi:.2f}"


def summarize_market_cap(market_cap: float) -> str:
    if market_cap >= 1_000_000_000_000:
        return f"${market_cap / 1_000_000_000_000:.2f}T"
    if market_cap >= 1_000_000_000:
        return f"${market_cap / 1_000_000_000:.2f}B"
    if market_cap >= 1_000_000:
        return f"${market_cap / 1_000_000:.2f}M"
    return f"${market_cap:.2f}"


def arrow(value: float) -> str:
    return "▲" if value >= 0 else "▼"


def signed_pct(value: float) -> str:
    return f"{arrow(value)}{abs(value):.2f}%"


def classify_contract_setup(
    change_15m_pct: float,
    change_1h_pct: float,
    change_24h_pct: float,
    funding_rate_pct: float,
    mark_basis_pct: float,
) -> str:
    if abs(funding_rate_pct) >= 0.05 or abs(mark_basis_pct) >= 0.30:
        return "高位拥挤观望"
    if change_15m_pct > 0 and change_1h_pct > 0 and change_24h_pct > 0:
        return "趋势延续偏多"
    if change_15m_pct < 0 and change_1h_pct < 0 and change_24h_pct < 0:
        return "趋势延续偏空"
    if abs(change_15m_pct) <= 0.3 and abs(change_1h_pct) <= 0.8:
        return "震荡等待"
    return "方向待确认"


def contract_bias(change_1h_pct: float, funding_rate_pct: float, mark_basis_pct: float) -> str:
    if change_1h_pct >= 1.2 and funding_rate_pct <= 0.03:
        return "多头偏强"
    if change_1h_pct <= -1.2 and funding_rate_pct >= -0.03:
        return "空头偏强"
    if abs(mark_basis_pct) >= 0.25:
        return "基差偏离"
    return "多空均衡"


def confidence(change_15m_pct: float, change_1h_pct: float, change_24h_pct: float) -> int:
    score = min(abs(change_15m_pct) * 12 + abs(change_1h_pct) * 6 + abs(change_24h_pct) * 2, 100)
    return max(int(score), 15)


def build_symbol_report(coin: dict, thresholds: Thresholds) -> tuple[dict, list[str]]:
    swap_symbol = coin["swap_symbol"]
    candles_15m = get_candles(swap_symbol, "15m", 2)
    latest_price = candles_15m[-1]["close"]
    change_15m_pct = pct_change(candles_15m[-1]["close"], candles_15m[-2]["close"])

    funding_rate_pct = get_funding_rate(swap_symbol) * 100
    mark_price = get_mark_price(swap_symbol)
    mark_basis_pct = pct_change(mark_price, latest_price)
    open_interest = get_open_interest(swap_symbol)

    strategy = classify_contract_setup(
        change_15m_pct,
        coin["change_1h_pct"],
        coin["change_24h_pct"],
        funding_rate_pct,
        mark_basis_pct,
    )
    bias = contract_bias(coin["change_1h_pct"], funding_rate_pct, mark_basis_pct)

    report = {
        "position": coin["position"],
        "market_cap_rank": coin["market_cap_rank"],
        "name": coin["name"],
        "symbol": coin["symbol"],
        "swap_symbol": swap_symbol,
        "market_cap": summarize_market_cap(coin["market_cap"]),
        "latest_price": latest_price,
        "change_15m_pct": change_15m_pct,
        "change_1h_pct": coin["change_1h_pct"],
        "change_24h_pct": coin["change_24h_pct"],
        "change_7d_pct": coin["change_7d_pct"],
        "change_30d_pct": coin["change_30d_pct"],
        "funding_rate_pct": funding_rate_pct,
        "mark_basis_pct": mark_basis_pct,
        "open_interest": summarize_oi(open_interest),
        "strategy": strategy,
        "bias": bias,
        "confidence": confidence(change_15m_pct, coin["change_1h_pct"], coin["change_24h_pct"]),
    }

    flags = []
    if abs(change_15m_pct) >= thresholds.change_15m_pct:
        flags.append(f"{coin['name']} 15分钟异动 {signed_pct(change_15m_pct)}")
    if abs(coin["change_1h_pct"]) >= thresholds.hourly_change_pct:
        flags.append(f"{coin['name']} 1小时趋势拉伸 {signed_pct(coin['change_1h_pct'])}")
    if abs(coin["change_24h_pct"]) >= thresholds.daily_change_pct:
        flags.append(f"{coin['name']} 日内波动较大 {signed_pct(coin['change_24h_pct'])}")
    if abs(coin["change_7d_pct"]) >= thresholds.weekly_change_pct:
        flags.append(f"{coin['name']} 周线趋势显著 {signed_pct(coin['change_7d_pct'])}")
    if abs(funding_rate_pct) >= thresholds.funding_rate_pct:
        flags.append(f"{coin['name']} 资金费率偏热 {funding_rate_pct:.4f}%")
    if abs(mark_basis_pct) >= thresholds.mark_basis_pct:
        flags.append(f"{coin['name']} 标记基差偏离 {mark_basis_pct:.2f}%")

    return report, flags


def classify_sentiment(score: int) -> str:
    if score <= 20:
        return "极度恐慌"
    if score <= 40:
        return "恐慌"
    if score <= 60:
        return "中性"
    if score <= 80:
        return "贪婪"
    return "极度贪婪"


def compute_market_sentiment(reports: list[dict], fear_greed_score: int) -> dict:
    avg_1h = sum(report["change_1h_pct"] for report in reports) / len(reports)
    avg_24h = sum(report["change_24h_pct"] for report in reports) / len(reports)
    avg_funding = sum(report["funding_rate_pct"] for report in reports) / len(reports)

    score = int(
        round(
            max(min(50 + avg_1h * 6 + avg_24h * 1.5, 100), 0) * 0.4
            + max(min(50 + avg_funding * 500, 100), 0) * 0.2
            + fear_greed_score * 0.4
        )
    )
    return {"score": score, "label": classify_sentiment(score)}


def build_market_digest(reports: list[dict], sentiment: dict, fear_greed_score: int, fear_greed_label: str, flags: list[str]) -> dict:
    bullish = [report["name"] for report in reports if report["strategy"] == "趋势延续偏多"]
    bearish = [report["name"] for report in reports if report["strategy"] == "趋势延续偏空"]

    if len(bullish) >= 4:
        trend = "主流币整体偏强，合约顺势多头更占优。"
    elif len(bearish) >= 4:
        trend = "主流币整体承压，合约短线更偏空头。"
    else:
        trend = "主流币分化明显，短线更适合等待方向确认。"

    return {
        "title": "📊 OKX 合约市值榜观察",
        "level": "info",
        "headline": f"今日趋势分析：{trend}",
        "summary": (
            f"市场情绪 {sentiment['label']} {sentiment['score']}/100；"
            f"Fear & Greed {fear_greed_score}（{fear_greed_label}）"
        ),
        "rankings": reports,
        "flags": flags[:8],
        "source": "okx-monitor",
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
        f"- 市场情绪：**{sentiment['label']}** ({sentiment['score']}/100)",
        f"- 榜单范围：**TOP {len(reports)} 市值币（OKX 永续）**",
        f"- 观察周期：**15 分钟**",
        "",
        f"## {digest['headline']}",
        "",
        "| 排名 | 币种 | 最新价 | 15分钟 | 1小时 | 今日涨跌 | 本周涨跌 | 本月涨跌 | 资金费率 | 标记基差 | 策略 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    for report in reports:
        lines.append(
            "| {rank} | {name} ({symbol}) | {latest_price:.4f} | {change_15m} | {change_1h} | {change_24h} | {change_7d} | {change_30d} | {funding_rate:.4f}% | {mark_basis:.2f}% | {strategy} |".format(
                rank=report["position"],
                name=report["name"],
                symbol=report["symbol"],
                latest_price=report["latest_price"],
                change_15m=signed_pct(report["change_15m_pct"]),
                change_1h=signed_pct(report["change_1h_pct"]),
                change_24h=signed_pct(report["change_24h_pct"]),
                change_7d=signed_pct(report["change_7d_pct"]),
                change_30d=signed_pct(report["change_30d_pct"]),
                funding_rate=report["funding_rate_pct"],
                mark_basis=report["mark_basis_pct"],
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

    for index, report in enumerate(reports, start=1):
        report["position"] = index

    fear_greed_score, fear_greed_label = get_fear_greed_score()
    sentiment = compute_market_sentiment(reports, fear_greed_score)
    digest = build_market_digest(reports, sentiment, fear_greed_score, fear_greed_label, flags)

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
