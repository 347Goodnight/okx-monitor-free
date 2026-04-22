from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

OKX_BASE_URL = "https://www.okx.com"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1&format=json"


@dataclass
class Thresholds:
    five_min_change_pct: float
    one_hour_change_pct: float
    volume_ratio: float
    atr_ratio_pct: float


def http_get_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "okx-monitor-free/0.1",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def okx_get(path: str, params: dict[str, str]) -> dict:
    query = urllib.parse.urlencode(params)
    return http_get_json(f"{OKX_BASE_URL}{path}?{query}")


def get_candles(symbol: str, bar: str, limit: int) -> list[dict]:
    payload = okx_get(
        "/api/v5/market/candles",
        {"instId": symbol, "bar": bar, "limit": str(limit)},
    )

    if payload.get("code") != "0":
        raise RuntimeError(f"OKX candles error for {symbol}: {payload}")

    rows = []
    for item in payload["data"]:
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


def get_fear_greed_score() -> tuple[int, str]:
    payload = http_get_json(FEAR_GREED_URL)
    score = int(payload["data"][0]["value"])
    return score, payload["data"][0]["value_classification"]


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0

    multiplier = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = (value - result) * multiplier + result
    return result


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0

    gains = []
    losses = []
    for left, right in zip(values[:-1], values[1:]):
        change = right - left
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))

    avg_gain = mean(gains[:period])
    avg_loss = mean(losses[:period])

    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0

    true_ranges = []
    for previous, current in zip(candles[:-1], candles[1:]):
        tr = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )
        true_ranges.append(tr)

    return mean(true_ranges[-period:])


def pct_change(current: float, reference: float) -> float:
    if reference == 0:
        return 0.0
    return ((current - reference) / reference) * 100


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


def confidence_from_metrics(
    price: float, ema20: float, ema60: float, current_rsi: float
) -> int:
    trend_score = min(abs(pct_change(ema20, ema60)), 5.0) * 12
    price_score = min(abs(pct_change(price, ema20)), 5.0) * 8
    rsi_score = abs(current_rsi - 50) * 0.8
    score = int(min(100, trend_score + price_score + rsi_score))
    return max(score, 15)


def infer_strategy(
    price: float, ema20: float, ema60: float, current_rsi: float, atr_ratio_pct: float
) -> str:
    if atr_ratio_pct >= 2.2:
        return "高风险观望"
    if price > ema20 > ema60 and current_rsi >= 55:
        return "趋势多"
    if price < ema20 < ema60 and current_rsi <= 45:
        return "趋势空"
    return "震荡"


def compute_symbol_report(symbol: str, thresholds: Thresholds) -> tuple[dict, list[dict]]:
    candles_5m = get_candles(symbol, "5m", 120)
    candles_1h = get_candles(symbol, "1H", 48)

    closes_5m = [row["close"] for row in candles_5m]
    last = candles_5m[-1]
    previous = candles_5m[-2]
    latest_price = last["close"]
    five_min_change = pct_change(last["close"], previous["close"])

    one_hour_reference = (
        candles_5m[-13]["close"] if len(candles_5m) >= 13 else candles_1h[-2]["close"]
    )
    one_hour_change = pct_change(latest_price, one_hour_reference)

    latest_volume = last["volume"]
    baseline_volume = mean([row["volume"] for row in candles_5m[-21:-1]]) or latest_volume
    volume_ratio = latest_volume / baseline_volume if baseline_volume else 1.0

    ema20 = ema(closes_5m[-60:], 20)
    ema60 = ema(closes_5m[-60:], 60)
    current_rsi = rsi(closes_5m[-30:], 14)
    current_atr = atr(candles_5m[-30:], 14)
    atr_ratio_pct = (current_atr / latest_price) * 100 if latest_price else 0.0

    recent_high = max(row["high"] for row in candles_5m[-21:-1])
    recent_low = min(row["low"] for row in candles_5m[-21:-1])
    breakout = latest_price > recent_high
    breakdown = latest_price < recent_low

    strategy = infer_strategy(latest_price, ema20, ema60, current_rsi, atr_ratio_pct)
    confidence = confidence_from_metrics(latest_price, ema20, ema60, current_rsi)

    alerts = []
    if abs(five_min_change) >= thresholds.five_min_change_pct:
        alerts.append(
            {
                "title": f"{symbol} 5m 异动",
                "level": "warning",
                "message": f"5 分钟涨跌幅 {five_min_change:.2f}%，最新价 {latest_price:.4f}",
                "source": "okx-monitor",
            }
        )

    if abs(one_hour_change) >= thresholds.one_hour_change_pct:
        alerts.append(
            {
                "title": f"{symbol} 1h 趋势拉伸",
                "level": "warning",
                "message": f"1 小时涨跌幅 {one_hour_change:.2f}%，最新价 {latest_price:.4f}",
                "source": "okx-monitor",
            }
        )

    if volume_ratio >= thresholds.volume_ratio:
        alerts.append(
            {
                "title": f"{symbol} 成交量放大",
                "level": "info",
                "message": f"5m 成交量放大 {volume_ratio:.2f}x，最新价 {latest_price:.4f}",
                "source": "okx-monitor",
            }
        )

    if atr_ratio_pct >= thresholds.atr_ratio_pct:
        alerts.append(
            {
                "title": f"{symbol} 波动风险升高",
                "level": "warning",
                "message": f"ATR/Price={atr_ratio_pct:.2f}%，请注意波动放大",
                "source": "okx-monitor",
            }
        )

    if breakout:
        alerts.append(
            {
                "title": f"{symbol} 向上突破",
                "level": "info",
                "message": f"价格 {latest_price:.4f} 突破近 20 根高点 {recent_high:.4f}",
                "source": "okx-monitor",
            }
        )

    if breakdown:
        alerts.append(
            {
                "title": f"{symbol} 向下跌破",
                "level": "warning",
                "message": f"价格 {latest_price:.4f} 跌破近 20 根低点 {recent_low:.4f}",
                "source": "okx-monitor",
            }
        )

    return (
        {
            "symbol": symbol,
            "price": latest_price,
            "five_min_change_pct": round(five_min_change, 2),
            "one_hour_change_pct": round(one_hour_change, 2),
            "volume_ratio": round(volume_ratio, 2),
            "rsi14": round(current_rsi, 2),
            "atr_ratio_pct": round(atr_ratio_pct, 2),
            "strategy": strategy,
            "confidence": confidence,
        },
        alerts,
    )


def compute_market_sentiment(reports: list[dict], fear_greed_score: int) -> dict:
    if not reports:
        return {"score": fear_greed_score, "label": classify_sentiment(fear_greed_score)}

    trend_component = mean(
        [
            max(min(report["one_hour_change_pct"] * 4 + 50, 100), 0)
            for report in reports[:2]
        ]
    )
    momentum_component = mean(
        [max(min((report["rsi14"] - 50) * 2 + 50, 100), 0) for report in reports[:2]]
    )
    volatility_penalty = mean(
        [max(0, min(100, 100 - report["atr_ratio_pct"] * 20)) for report in reports[:2]]
    )

    score = int(
        round(
            trend_component * 0.3
            + momentum_component * 0.2
            + volatility_penalty * 0.2
            + fear_greed_score * 0.3
        )
    )

    return {"score": score, "label": classify_sentiment(score)}


def load_config(path: Path) -> tuple[list[str], Thresholds]:
    config = json.loads(path.read_text(encoding="utf-8"))
    thresholds = Thresholds(**config["thresholds"])
    return config["symbols"], thresholds


def post_alert(endpoint: str, token: str | None, alert: dict) -> None:
    data = json.dumps(alert).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
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
        if body:
            body = body.replace("\n", " ")[:240]
            return f"{location} -> HTTP {error.code}: {body}"
        return f"{location} -> HTTP {error.code}"

    return f"{location} -> {error}"


def write_summary(
    summary_path: Path,
    sentiment: dict,
    reports: list[dict],
    alerts: list[dict],
    delivery_errors: list[str],
) -> None:
    lines = [
        "# OKX Monitor Summary",
        "",
        f"- 市场情绪：**{sentiment['label']}** ({sentiment['score']}/100)",
        f"- 告警数量：**{len(alerts)}**",
        "",
        "## 策略摘要",
        "",
        "| 交易对 | 最新价 | 5m涨跌 | 1h涨跌 | 成交量倍数 | RSI14 | 波动率 | 结论 | 置信度 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
    ]

    for report in reports:
        lines.append(
            "| {symbol} | {price:.4f} | {five_min_change_pct:.2f}% | {one_hour_change_pct:.2f}% | "
            "{volume_ratio:.2f}x | {rsi14:.2f} | {atr_ratio_pct:.2f}% | {strategy} | {confidence} |".format(
                **report
            )
        )

    if alerts:
        lines.extend(["", "## 当前触发的告警", ""])
        for alert in alerts:
            lines.append(f"- `{alert['title']}`：{alert['message']}")

    if delivery_errors:
        lines.extend(["", "## 告警发送异常", ""])
        for error in delivery_errors:
            lines.append(f"- {error}")

    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    github_step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if github_step_summary:
        Path(github_step_summary).write_text(
            summary_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--summary-file", type=Path, default=Path("monitor-summary.md"))
    args = parser.parse_args()

    symbols, thresholds = load_config(args.config)

    reports = []
    alerts = []
    for symbol in symbols:
        report, symbol_alerts = compute_symbol_report(symbol, thresholds)
        reports.append(report)
        alerts.extend(symbol_alerts)

    fear_greed_score, fear_greed_label = get_fear_greed_score()
    sentiment = compute_market_sentiment(reports, fear_greed_score)

    alerts.insert(
        0,
        {
            "title": "市场情绪摘要",
            "level": "info",
            "message": (
                f"情绪分 {sentiment['score']}/100，当前为 {sentiment['label']}；"
                f"Fear & Greed {fear_greed_score}（{fear_greed_label}）"
            ),
            "source": "okx-monitor",
        },
    )

    endpoint = os.getenv("ALERT_ENDPOINT")
    token = os.getenv("ALERT_TOKEN")
    delivery_errors = []

    if endpoint:
        for alert in alerts:
            try:
                post_alert(endpoint, token, alert)
            except Exception as error:
                delivery_errors.append(
                    f"{alert['title']}: {format_delivery_error(endpoint, error)}"
                )

    write_summary(args.summary_file, sentiment, reports, alerts, delivery_errors)

    if delivery_errors:
        print("Alert delivery errors detected:", file=sys.stderr)
        for error in delivery_errors:
            print(f"- {error}", file=sys.stderr)

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
