from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"
COINDESK_RSS_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"
OKX_BASE_URL = "https://www.okx.com"
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1&format=json"
FED_PRESS_RSS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
SEC_PRESS_RSS_URL = "https://www.sec.gov/news/pressreleases.rss"
TREASURY_PRESS_URL = "https://home.treasury.gov/news/press-releases"
SOSOVALUE_OPENAPI_BASE_URL = "https://openapi.sosovalue.com/openapi/v1"

HTML_TAG_RE = re.compile(r"<[^>]+>")
ODAILY_PATTERN = re.compile(
    r'\\"(?:publishedTime|publishTimestamp)\\":(?:\\"(?P<published_text>[^"]+)\\"|(?P<published_ts>\d+)).*?'
    r'\\"isImportant\\":(?P<important>true|false|null).*?'
    r'\\"newsUrl\\":\\"(?P<link>[^"]*)\\".*?'
    r'\\"(?:summary|description)\\":\\"(?P<summary>.*?)\\".*?'
    r'\\"title\\":\\"(?P<title>.*?)\\"',
    re.DOTALL,
)
CHAINCATCHER_SECTION_RE = re.compile(r"newsFlashList:\[(?P<body>.*?)\],hotAdvertising", re.DOTALL)
CHAINCATCHER_ITEM_RE = re.compile(
    r'description:"(?P<summary>(?:\\.|[^"])*)".*?title:"(?P<title>(?:\\.|[^"])*)"',
    re.DOTALL,
)
TREASURY_ITEM_RE = re.compile(
    r'<time[^>]+datetime="(?P<datetime>[^"]+)"[^>]*>.*?</time>\s*'
    r'<div class="news-title"><a href="(?P<link>[^"]+)"[^>]*>(?P<title>.*?)</a></div>',
    re.DOTALL,
)


@dataclass
class Thresholds:
    change_15m_pct: float
    hourly_change_pct: float
    daily_change_pct: float
    weekly_change_pct: float


@dataclass
class NewsSourceConfig:
    name: str
    kind: str
    url: str
    language: str = "zh"
    weight: int = 0
    enabled: bool = True
    timeout_sec: int = 20
    items_path: str | None = None
    title_field: str | None = None
    link_field: str | None = None
    role: str = "headline"
    required_env: str | None = None


@dataclass
class NewsItem:
    source_name: str
    source_language: str
    source_weight: int
    source_role: str
    title: str
    link: str | None
    normalized_title: str
    summary: str = ""
    published_at: str | None = None
    is_important: bool = False


@dataclass
class ScanConfig:
    market_cap_top_n: int
    candidate_pool: int
    quote: str
    news_headlines: int
    news_fetch_limit_per_source: int
    news_sources: list[NewsSourceConfig]
    exclude_symbols: list[str]
    thresholds: Thresholds


def http_get_text(url: str, timeout: int = 20, headers: dict[str, str] | None = None) -> str:
    last_error = None
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, application/xml, text/xml, text/html, */*",
    }
    if headers:
        request_headers.update(headers)

    for attempt in range(5):
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code in {403, 404}:
                break
            if attempt < 4:
                time.sleep(1.5)
        except urllib.error.URLError as error:
            last_error = error
            if attempt < 4:
                time.sleep(1.5)

    raise last_error


def http_get_json(url: str, timeout: int = 20, headers: dict[str, str] | None = None) -> dict | list:
    return json.loads(http_get_text(url, timeout=timeout, headers=headers))


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


def default_news_sources() -> list[NewsSourceConfig]:
    return [
        NewsSourceConfig(
            name="Wu Blockchain",
            kind="rss",
            url="https://www.wublock123.com/feed",
            language="zh",
            weight=5,
            role="headline",
        ),
        NewsSourceConfig(
            name="CoinDesk",
            kind="rss",
            url=COINDESK_RSS_URL,
            language="en",
            weight=2,
            role="headline",
        ),
    ]


def parse_news_source(raw: dict) -> NewsSourceConfig:
    return NewsSourceConfig(
        name=raw["name"],
        kind=raw["kind"],
        url=raw["url"],
        language=raw.get("language", "zh"),
        weight=int(raw.get("weight", 0)),
        enabled=bool(raw.get("enabled", True)),
        timeout_sec=int(raw.get("timeout_sec", 20)),
        items_path=raw.get("items_path"),
        title_field=raw.get("title_field"),
        link_field=raw.get("link_field"),
        role=raw.get("role", "headline"),
        required_env=raw.get("required_env"),
    )


def load_config(path: Path) -> ScanConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw_sources = raw.get("news_sources") or [source.__dict__ for source in default_news_sources()]
    news_sources = [parse_news_source(item) for item in raw_sources if item.get("enabled", True)]

    return ScanConfig(
        market_cap_top_n=raw["market_cap_top_n"],
        candidate_pool=raw["candidate_pool"],
        quote=raw["quote"],
        news_headlines=raw["news_headlines"],
        news_fetch_limit_per_source=int(raw.get("news_fetch_limit_per_source", 10)),
        news_sources=news_sources,
        exclude_symbols=[symbol.upper() for symbol in raw["exclude_symbols"]],
        thresholds=Thresholds(**raw["thresholds"]),
    )


def get_fear_greed_score() -> tuple[int, str]:
    payload = http_get_json(FEAR_GREED_URL)
    score = int(payload["data"][0]["value"])
    return score, payload["data"][0]["value_classification"]


def decode_escaped_fragment(text: str) -> str:
    fragment = text or ""
    try:
        return json.loads(f'"{fragment}"')
    except json.JSONDecodeError:
        return html.unescape(fragment)


def clean_headline(text: str) -> str:
    cleaned = decode_escaped_fragment(text).strip()
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(
        r"\s*(?:\||｜|-|—|–|•)\s*(?:coindesk|wu blockchain|wublock|odaily|chaincatcher|panews)\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def clean_summary(text: str) -> str:
    summary = decode_escaped_fragment(text)
    summary = summary.replace("\\n", " ")
    summary = HTML_TAG_RE.sub(" ", summary)
    summary = html.unescape(summary)
    summary = re.sub(r"\s+", " ", summary)
    return summary.strip()


def normalize_headline(text: str) -> str:
    normalized = clean_headline(text).lower()
    replacements = {
        "bitcoin": "btc",
        "比特币": "btc",
        "ethereum": "eth",
        "ether": "eth",
        "以太坊": "eth",
        "crypto": "加密",
        "cryptocurrency": "加密",
        "federal reserve": "fed",
        "美联储": "fed",
        "etf flows": "etf",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized


def read_nested_list(payload: dict | list, path: str | None) -> list | None:
    if not path:
        return payload if isinstance(payload, list) else None

    current = payload
    for segment in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return None
            continue
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
        if current is None:
            return None
    return current if isinstance(current, list) else None


def parse_date_string(value: str | None) -> str | None:
    if not value:
        return None

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            continue

    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return value
    return parsed.astimezone(timezone.utc).isoformat()


def parse_rss_source(source: NewsSourceConfig, limit: int) -> list[NewsItem]:
    root = ET.fromstring(http_get_text(source.url, timeout=source.timeout_sec))
    entries = root.findall("./channel/item")
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    items = []
    for entry in entries:
        title = clean_headline(
            entry.findtext("title")
            or entry.findtext("{http://www.w3.org/2005/Atom}title")
            or ""
        )
        if not title:
            continue

        description = clean_summary(
            entry.findtext("description")
            or entry.findtext("summary")
            or entry.findtext("{http://www.w3.org/2005/Atom}summary")
            or ""
        )
        link = entry.findtext("link") or entry.findtext("{http://www.w3.org/2005/Atom}link")
        if not link:
            atom_link = entry.find("{http://www.w3.org/2005/Atom}link")
            if atom_link is not None:
                link = atom_link.attrib.get("href")

        published_at = parse_date_string(
            entry.findtext("pubDate")
            or entry.findtext("{http://www.w3.org/2005/Atom}updated")
        )

        items.append(
            NewsItem(
                source_name=source.name,
                source_language=source.language,
                source_weight=source.weight,
                source_role=source.role,
                title=title,
                link=link.strip() if isinstance(link, str) and link.strip() else None,
                normalized_title=normalize_headline(title),
                summary=description,
                published_at=published_at,
                is_important=False,
            )
        )
        if len(items) >= limit:
            break

    return items


def parse_json_source(source: NewsSourceConfig, limit: int) -> list[NewsItem]:
    payload = http_get_json(source.url, timeout=source.timeout_sec)
    rows = read_nested_list(payload, source.items_path)
    if rows is None:
        raise RuntimeError(f"JSON source {source.name} missing items_path")

    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = clean_headline(str(row.get(source.title_field or "title", "")))
        if not title:
            continue
        link = row.get(source.link_field or "url") or row.get("link")
        summary = clean_summary(str(row.get("summary", "")))
        items.append(
            NewsItem(
                source_name=source.name,
                source_language=source.language,
                source_weight=source.weight,
                source_role=source.role,
                title=title,
                link=str(link).strip() if link else None,
                normalized_title=normalize_headline(title),
                summary=summary,
            )
        )
        if len(items) >= limit:
            break

    return items


def parse_odaily_newsflash_source(source: NewsSourceConfig, limit: int) -> list[NewsItem]:
    html_text = http_get_text(source.url, timeout=source.timeout_sec)
    items = []
    seen_titles: set[str] = set()

    json_ld_matches = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>',
        html_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    for raw_payload in json_ld_matches:
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue

        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("@type") != "ItemList":
                continue
            for row in candidate.get("itemListElement", []):
                if not isinstance(row, dict):
                    continue
                title = clean_headline(str(row.get("name", "")))
                if not title:
                    continue
                normalized_title = normalize_headline(title)
                if normalized_title in seen_titles:
                    continue
                seen_titles.add(normalized_title)

                items.append(
                    NewsItem(
                        source_name=source.name,
                        source_language=source.language,
                        source_weight=source.weight,
                        source_role=source.role,
                        title=title,
                        link=str(row.get("url", "")).strip() or None,
                        normalized_title=normalized_title,
                        summary="",
                        published_at=None,
                        is_important=False,
                    )
                )
                if len(items) >= limit:
                    return items

    for match in ODAILY_PATTERN.finditer(html_text):
        title = clean_headline(match.group("title"))
        if not title:
            continue
        normalized_title = normalize_headline(title)
        if normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)

        published_at = match.group("published_text")
        if not published_at and match.group("published_ts"):
            published_at = datetime.fromtimestamp(
                int(match.group("published_ts")) / 1000,
                tz=timezone.utc,
            ).isoformat()
        else:
            published_at = parse_date_string(published_at)

        items.append(
            NewsItem(
                source_name=source.name,
                source_language=source.language,
                source_weight=source.weight,
                source_role=source.role,
                title=title,
                link=match.group("link").strip() or None,
                normalized_title=normalized_title,
                summary=clean_summary(match.group("summary")),
                published_at=published_at,
                is_important=match.group("important") == "true",
            )
        )
        if len(items) >= limit:
            break

    return items


def parse_chaincatcher_source(source: NewsSourceConfig, limit: int) -> list[NewsItem]:
    html_text = http_get_text(source.url, timeout=source.timeout_sec)
    match = CHAINCATCHER_SECTION_RE.search(html_text)
    if not match:
        return []

    body = match.group("body")
    items = []
    seen_titles: set[str] = set()

    for item_match in CHAINCATCHER_ITEM_RE.finditer(body):
        title = clean_headline(item_match.group("title"))
        if not title:
            continue
        normalized_title = normalize_headline(title)
        if normalized_title in seen_titles:
            continue
        seen_titles.add(normalized_title)
        items.append(
            NewsItem(
                source_name=source.name,
                source_language=source.language,
                source_weight=source.weight,
                source_role=source.role,
                title=title,
                link=None,
                normalized_title=normalized_title,
                summary=clean_summary(item_match.group("summary")),
                is_important=False,
            )
        )
        if len(items) >= limit:
            break

    return items


def parse_treasury_press_source(source: NewsSourceConfig, limit: int) -> list[NewsItem]:
    html_text = http_get_text(source.url, timeout=source.timeout_sec)
    items = []
    for match in TREASURY_ITEM_RE.finditer(html_text):
        title = clean_headline(match.group("title"))
        if not title:
            continue
        link = match.group("link")
        items.append(
            NewsItem(
                source_name=source.name,
                source_language=source.language,
                source_weight=source.weight,
                source_role=source.role,
                title=title,
                link=f"https://home.treasury.gov{link}" if link.startswith("/") else link,
                normalized_title=normalize_headline(title),
                summary="Treasury press release",
                published_at=parse_date_string(match.group("datetime")),
                is_important=True,
            )
        )
        if len(items) >= limit:
            break
    return items


def format_money(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"${value / 1_000:.2f}K"
    return f"${value:.0f}"


def parse_sosovalue_etf_summary_source(source: NewsSourceConfig, limit: int) -> list[NewsItem]:
    api_key = os.getenv(source.required_env or "")
    if not api_key:
        return []

    query = urllib.parse.urlencode({"symbol": "BTC", "country_code": "US", "limit": "2"})
    payload = http_get_json(
        f"{source.url}?{query}",
        timeout=source.timeout_sec,
        headers={"x-soso-api-key": api_key},
    )
    if not isinstance(payload, list) or not payload:
        return []

    latest = payload[0]
    net_inflow = float(latest.get("total_net_inflow") or 0)
    traded = float(latest.get("total_value_traded") or 0)
    net_assets = float(latest.get("total_net_assets") or 0)
    cum_inflow = float(latest.get("cum_net_inflow") or 0)
    date_value = str(latest.get("date") or "")

    if net_inflow > 0:
        title = f"SoSoValue: US BTC spot ETF daily net inflow {format_money(net_inflow)}"
    elif net_inflow < 0:
        title = f"SoSoValue: US BTC spot ETF daily net outflow {format_money(net_inflow)}"
    else:
        title = "SoSoValue: US BTC spot ETF net flow flat"

    summary = (
        f"Date {date_value}; total ETF value traded {format_money(traded)}, "
        f"net assets {format_money(net_assets)}, cumulative net inflow {format_money(cum_inflow)}."
    )

    return [
        NewsItem(
            source_name=source.name,
            source_language=source.language,
            source_weight=source.weight,
            source_role=source.role,
            title=title,
            link=None,
            normalized_title=normalize_headline(title),
            summary=summary,
            published_at=parse_date_string(date_value),
            is_important=True,
        )
    ][:limit]


def fetch_news_from_source(source: NewsSourceConfig, limit: int) -> list[NewsItem]:
    if source.kind == "rss":
        return parse_rss_source(source, limit)
    if source.kind == "json":
        return parse_json_source(source, limit)
    if source.kind == "odaily_newsflash_html":
        return parse_odaily_newsflash_source(source, limit)
    if source.kind == "chaincatcher_nuxt":
        return parse_chaincatcher_source(source, limit)
    if source.kind == "treasury_press_html":
        return parse_treasury_press_source(source, limit)
    if source.kind == "sosovalue_etf_summary":
        return parse_sosovalue_etf_summary_source(source, limit)
    raise RuntimeError(f"Unsupported news source kind: {source.kind}")


def item_rank(item: NewsItem) -> tuple[int, int, int]:
    role_bonus = 3 if item.source_role == "confirmation" else 0
    important_bonus = 1 if item.is_important else 0
    summary_bonus = 1 if item.summary else 0
    return item.source_weight + role_bonus + important_bonus + summary_bonus, role_bonus, important_bonus


def collect_news_items(config: ScanConfig) -> tuple[list[NewsItem], dict]:
    items: list[NewsItem] = []
    healthy_sources: list[str] = []
    failed_sources: list[str] = []
    skipped_sources: list[str] = []

    for source in config.news_sources:
        if source.required_env and not os.getenv(source.required_env):
            skipped_sources.append(f"{source.name}(missing {source.required_env})")
            continue

        try:
            source_items = fetch_news_from_source(
                source,
                max(config.news_fetch_limit_per_source, config.news_headlines * 4),
            )
        except Exception:
            failed_sources.append(source.name)
            continue

        if source_items:
            items.extend(source_items)
            healthy_sources.append(source.name)
        else:
            failed_sources.append(source.name)

    deduped_map: dict[str, NewsItem] = {}
    for item in items:
        if not item.normalized_title:
            continue
        existing = deduped_map.get(item.normalized_title)
        if existing is None or item_rank(item) > item_rank(existing):
            deduped_map[item.normalized_title] = item

    deduped = list(deduped_map.values())
    deduped.sort(key=lambda item: (-item_rank(item)[0], item.source_name, item.title))

    return deduped, {
        "configured_sources": len(config.news_sources),
        "healthy_sources": healthy_sources,
        "failed_sources": failed_sources,
        "skipped_sources": skipped_sources,
        "raw_items": len(items),
        "deduped_items": len(deduped),
    }


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
        rows.append({"ts": int(item[0]), "close": float(item[4])})
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
        return "趋势偏多"
    if change_15m_pct < 0 and change_1h_pct < 0 and change_24h_pct < 0:
        return "趋势偏空"
    if abs(change_15m_pct) <= 0.3 and abs(change_1h_pct) <= 0.8:
        return "震荡等待"
    return "方向待确认"


def translate_fear_greed_label(label: str) -> str:
    mapping = {
        "Extreme Fear": "极度恐慌",
        "Fear": "恐慌",
        "Neutral": "中性",
        "Greed": "贪婪",
        "Extreme Greed": "极度贪婪",
    }
    return mapping.get(label, label)


def explain_fear_greed(score: int) -> str:
    if score <= 25:
        return "场外资金明显更谨慎，遇到利空时更容易放大波动。"
    if score <= 45:
        return "市场仍偏谨慎，追涨意愿不强。"
    if score <= 55:
        return "市场整体偏观望，需要消息面和盘面一起确认方向。"
    if score <= 75:
        return "风险偏好正在回升，顺势行情更容易延续。"
    return "情绪已经偏热，需要防冲高后的回撤风险。"


def contains_keyword(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if re.search(r"[a-z]", keyword, flags=re.IGNORECASE):
        pattern = rf"(?<![a-z0-9]){re.escape(keyword.lower())}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return keyword in text


def contains_any(text: str, keywords: list[str]) -> bool:
    return any(contains_keyword(text, keyword) for keyword in keywords)


def get_news_age_hours(published_at: str | None) -> float | None:
    if not published_at:
        return None

    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return None

    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)

    age_seconds = (datetime.now(timezone.utc) - published.astimezone(timezone.utc)).total_seconds()
    return max(age_seconds / 3600, 0.0)


def score_news_recency(item: NewsItem) -> int:
    age_hours = get_news_age_hours(item.published_at)
    if age_hours is None:
        return 0
    if age_hours <= 6:
        return 6
    if age_hours <= 24:
        return 4
    if age_hours <= 72:
        return 2
    if age_hours <= 24 * 7:
        return 0
    if age_hours <= 24 * 14:
        return -4
    if age_hours <= 24 * 30:
        return -8
    return -12


def passes_source_relevance_gate(item: NewsItem, text: str, theme: str) -> bool:
    fed_macro_keywords = [
        "fomc",
        "minutes",
        "economic projections",
        "dot plot",
        "rate cut",
        "rate hike",
        "rates",
        "inflation",
        "cpi",
        "ppi",
        "payroll",
        "jobless",
        "liquidity",
        "balance sheet",
        "powell",
        "speech",
        "remarks",
        "meeting",
        "美联储",
        "鲍威尔",
        "降息",
        "加息",
        "通胀",
        "非农",
        "失业率",
        "流动性",
        "议息",
    ]
    crypto_linked_keywords = [
        "crypto",
        "bitcoin",
        "ethereum",
        "ether",
        "digital asset",
        "digital assets",
        "stablecoin",
        "spot bitcoin etf",
        "spot ethereum etf",
        "bitcoin etf",
        "ethereum etf",
        "crypto etf",
        "exchange-traded fund",
        "virtual asset",
        "virtual currency",
        "blockchain",
        "tokenized",
        "tokenization",
        "custody",
        "custodian",
        "wallet",
        "tornado cash",
        "mixer",
        "defi",
        "加密",
        "比特币",
        "以太坊",
        "数字资产",
        "稳定币",
        "区块链",
        "现货比特币etf",
        "现货以太坊etf",
        "现货etf",
        "托管",
        "钱包",
        "混币",
    ]
    generic_market_structure_keywords = [
        "nms",
        "consolidated audit trail",
        "national market system",
        "cross-margining",
        "u.s. treasury market",
        "treasury market",
        "equity market",
        "municipal securities",
        "clearing agency",
        "exchange act",
        "money market fund",
        "interdealer broker",
        "cat plan",
    ]
    title_text = item.title.lower()

    if item.source_name == "Fed":
        return theme == "宏观/流动性" and contains_any(text, fed_macro_keywords)
    if item.source_name in {"SEC", "Treasury"}:
        has_crypto_context = contains_any(title_text, crypto_linked_keywords) or contains_any(
            text,
            crypto_linked_keywords,
        )
        if not has_crypto_context:
            return False
        if contains_any(text, generic_market_structure_keywords) and not contains_any(
            title_text,
            crypto_linked_keywords,
        ):
            return False
        return True
    return True


def score_news_item(item: NewsItem) -> dict:
    full_text = f"{item.title} {item.summary}".lower()
    title_text = item.title.lower()

    theme_keywords = {
        "宏观/流动性": [
            "fed",
            "federal reserve",
            "powell",
            "cpi",
            "ppi",
            "inflation",
            "payroll",
            "jobless",
            "rates",
            "rate cut",
            "rate hike",
            "treasury yield",
            "tariff",
            "war",
            "ceasefire",
            "truce",
            "geopolitical",
            "dxy",
            "dollar index",
            "美元指数",
            "美联储",
            "鲍威尔",
            "非农",
            "失业率",
            "通胀",
            "降息",
            "加息",
            "关税",
            "停火",
            "地缘",
            "中东",
            "美债",
            "流动性",
            "fomc",
            "minutes",
        ],
        "政策/监管": [
            "sec",
            "regulation",
            "regulatory",
            "lawmakers",
            "bill",
            "senate",
            "house",
            "approval",
            "approve",
            "approved",
            "ban",
            "crackdown",
            "license",
            "监管",
            "法案",
            "审批",
            "批准",
            "牌照",
            "禁令",
            "打击",
            "sanction",
            "sanctions",
            "treasury sanctions",
            "白宫",
        ],
        "资金面/ETF": [
            "etf",
            "inflow",
            "outflow",
            "capital",
            "institutional",
            "reserve",
            "buyback",
            "blackrock",
            "ibit",
            "fidelity",
            "microstrategy",
            "strategy",
            "net flow",
            "net inflow",
            "net outflow",
            "净流入",
            "净流出",
            "机构资金",
            "增持",
            "减持",
            "回购",
            "储备",
            "稳定币",
            "铸造",
            "usdt",
            "usdc",
        ],
        "风险事件": [
            "hack",
            "exploit",
            "liquidation",
            "sell-off",
            "selloff",
            "crash",
            "default",
            "attack",
            "conflict",
            "爆仓",
            "清算",
            "黑客",
            "被盗",
            "攻击",
            "踩踏",
            "闪崩",
        ],
    }
    theme_base_score = {
        "宏观/流动性": 12,
        "政策/监管": 11,
        "资金面/ETF": 10,
        "风险事件": 8,
        "行业动态": 3,
    }
    market_wide_keywords = [
        "btc",
        "bitcoin",
        "比特币",
        "eth",
        "ethereum",
        "以太坊",
        "crypto",
        "加密",
        "altcoin",
        "alts",
        "山寨",
        "主流币",
        "market",
        "市场",
    ]
    low_priority_keywords = [
        "airdrop",
        "token unlock",
        "unlock",
        "launch",
        "launches",
        "partnership",
        "funding round",
        "series a",
        "testnet",
        "mainnet",
        "wallet",
        "nft",
        "gaming",
        "meme",
        "memecoin",
        "上币",
        "空投",
        "测试网",
        "主网上线",
        "融资",
    ]
    title_noise_keywords = [
        "tge",
        "发币",
        "空投",
        "上币",
        "测试网",
        "主网上线",
        "nft",
        "gaming",
        "meme",
        "memecoin",
    ]
    positive_keywords = [
        "ceasefire",
        "truce",
        "approval",
        "approved",
        "inflow",
        "easing",
        "cooling",
        "support",
        "bullish",
        "净流入",
        "批准",
        "停火",
        "缓和",
        "降息",
    ]
    negative_keywords = [
        "war",
        "tariff",
        "hack",
        "exploit",
        "outflow",
        "selloff",
        "sell-off",
        "liquidation",
        "attack",
        "conflict",
        "ban",
        "crackdown",
        "delay",
        "爆仓",
        "清算",
        "打击",
        "禁令",
        "关税",
        "被盗",
        "推迟",
        "sanctions",
    ]
    official_sources = {"Fed", "SEC", "Treasury", "SoSoValue ETF Flows"}

    theme = "行业动态"
    matched_keywords: list[str] = []
    best_theme_score = 0
    for candidate_theme, keywords in theme_keywords.items():
        hits = [keyword for keyword in keywords if contains_keyword(full_text, keyword)]
        if not hits:
            continue
        candidate_score = theme_base_score[candidate_theme] + min(len(hits), 3) * 2
        if candidate_score > best_theme_score:
            theme = candidate_theme
            matched_keywords = hits
            best_theme_score = candidate_score

    score = best_theme_score or theme_base_score[theme]
    if theme in {"宏观/流动性", "政策/监管", "资金面/ETF"}:
        score += 5
    if contains_any(full_text, market_wide_keywords):
        score += 4
    if contains_any(full_text, ["btc", "bitcoin", "比特币", "eth", "ethereum", "以太坊"]):
        score += 2
    if item.source_role == "confirmation":
        score += 5
    if item.source_name in official_sources:
        score += 4
    if item.is_important:
        score += 2
    score += min(item.source_weight, 5)
    score += score_news_recency(item)

    positive_hits = sum(contains_keyword(full_text, keyword) for keyword in positive_keywords)
    negative_hits = sum(contains_keyword(full_text, keyword) for keyword in negative_keywords)
    title_positive_hits = sum(contains_keyword(title_text, keyword) for keyword in positive_keywords)
    title_negative_hits = sum(contains_keyword(title_text, keyword) for keyword in negative_keywords)
    if theme == "资金面/ETF" and title_positive_hits != title_negative_hits:
        positive_hits = title_positive_hits
        negative_hits = title_negative_hits
    if positive_hits > negative_hits:
        direction = "偏利多"
        score += min(positive_hits, 2)
    elif negative_hits > positive_hits:
        direction = "偏利空"
        score += min(negative_hits, 2)
    else:
        direction = "中性"

    if contains_any(full_text, low_priority_keywords):
        score -= 8

    is_market_wide = theme in {"宏观/流动性", "政策/监管", "资金面/ETF"} or (
        theme == "风险事件" and contains_any(full_text, market_wide_keywords)
    )
    if not passes_source_relevance_gate(item, full_text, theme):
        is_market_wide = False
        score -= 10
    if contains_any(title_text, title_noise_keywords):
        is_market_wide = False
        score -= 12
    is_priority = is_market_wide and score >= 15

    return {
        "theme": theme,
        "score": score,
        "direction": direction,
        "matched_keywords": matched_keywords[:4],
        "is_market_wide": is_market_wide,
        "is_priority": is_priority,
    }


def summarize_market_impact(item: NewsItem, theme: str, direction: str) -> str:
    full_text = f"{item.title} {item.summary}".lower()
    title_text = item.title.lower()

    if contains_any(full_text, ["fed", "federal reserve", "powell", "美联储", "鲍威尔", "降息", "加息", "rates", "rate cut", "rate hike"]):
        if contains_any(full_text, ["cut", "easing", "cooling", "降息", "缓和"]):
            return "流动性预期改善，BTC 往往先反应，再带动 ETH 和主流山寨扩散。"
        if contains_any(full_text, ["hike", "higher for longer", "加息", "关税", "sticky inflation"]):
            return "风险资产定价容易承压，BTC 与主流山寨更容易同向回撤。"
        return "宏观预期在主导市场风险偏好，先看 BTC 是否放量确认方向。"

    if contains_any(full_text, ["cpi", "ppi", "inflation", "非农", "失业率", "通胀"]):
        if contains_any(full_text, ["cool", "eases", "ease", "lower", "回落", "降温"]):
            return "通胀压力缓和，利于风险偏好回升，主流币更容易联动走强。"
        return "宏观数据会先冲击 BTC 定价，再向主流山寨传导波动。"

    if contains_any(full_text, ["etf", "inflow", "outflow", "净流入", "净流出", "blackrock", "ibit", "strategy", "microstrategy"]):
        if contains_any(title_text, ["outflow", "净流出", "减持"]):
            return "资金面转弱时，BTC 通常先承压，主流山寨会出现放大回撤。"
        if contains_any(title_text, ["inflow", "净流入", "增持"]):
            return "现货 ETF / 机构资金流入更容易先推 BTC，再带动主流山寨补涨。"
        if contains_any(full_text, ["outflow", "净流出", "减持"]):
            return "资金面转弱时，BTC 通常先承压，主流山寨会出现放大回撤。"
        return "现货 ETF / 机构资金流入更容易先推 BTC，再带动主流山寨补涨。"

    if contains_any(full_text, ["sec", "regulation", "regulatory", "监管", "法案", "批准", "牌照", "ban", "crackdown", "禁令", "sanctions"]):
        if contains_any(full_text, ["ban", "crackdown", "delay", "禁令", "打击", "推迟", "sanctions"]):
            return "监管预期转空会压制风险偏好，主流币更容易出现一致性回落。"
        return "政策预期改善有助于资金回流，BTC 与主流山寨的联动性会增强。"

    if contains_any(full_text, ["hack", "exploit", "attack", "liquidation", "selloff", "爆仓", "清算", "黑客", "闪崩"]):
        return "风险事件会抬升避险情绪，主流币短线更容易出现同步杀跌。"

    if theme == "宏观/流动性":
        return "宏观变量正在抢主导权，优先观察 BTC 是否带动全市场共振。"
    if theme == "政策/监管":
        return "政策变量会直接影响风险偏好，通常先从 BTC 扩散到主流山寨。"
    if theme == "资金面/ETF":
        return "资金面变化通常先反馈在 BTC，再决定山寨跟涨还是补跌。"
    if direction == "偏利空":
        return "这类事件更像全市场风险偏好降温信号。"
    return "先把它当作可能触发 BTC 与主流币联动的候选事件。"


def format_news_source_status(status: dict) -> str:
    configured = int(status.get("configured_sources", 0))
    healthy_sources = status.get("healthy_sources", [])
    failed_sources = status.get("failed_sources", [])
    skipped_sources = status.get("skipped_sources", [])
    deduped_items = int(status.get("deduped_items", 0))
    active_configured = max(configured - len(skipped_sources), len(healthy_sources) + len(failed_sources))

    if not healthy_sources:
        return "全部源不可用，已自动降级为纯盘面判断。"

    parts = [
        f"{len(healthy_sources)}/{active_configured} 可用",
        f"候选 {deduped_items} 条",
    ]
    if failed_sources:
        parts.append(f"降级跳过 {'、'.join(failed_sources)}")
    return "，".join(parts) + "。"


def build_news_snapshot(news_items: list[NewsItem], source_status: dict, limit: int) -> tuple[str, list[str], list[dict], str]:
    status_line = format_news_source_status(source_status)
    if not news_items:
        return (
            "消息面判断：当前没有抓到足以驱动 BTC 和主流山寨联动的有效事件，先以盘面为主。",
            [],
            [],
            status_line,
        )

    ranked = []
    for index, item in enumerate(news_items):
        detail = score_news_item(item)
        ranked.append((detail["score"], index, item, detail))

    ranked.sort(key=lambda row: (-row[0], row[1]))
    priority_pool = [row for row in ranked if row[3]["is_priority"]]
    fallback_pool = [row for row in ranked if row[3]["is_market_wide"] and row[3]["score"] >= 10]
    selected_pool = priority_pool or fallback_pool

    if not selected_pool:
        return (
            "消息面判断：多源快讯里暂时没有明确的宏观、政策或资金面主驱动，先观察 BTC 盘口是否自行放量带方向。",
            [],
            [],
            status_line,
        )

    selected_rows = selected_pool[: min(limit, len(selected_pool))]
    dominant_theme_counts: dict[str, int] = {}
    drivers = []
    news_titles = []

    for _, _, item, detail in selected_rows:
        dominant_theme_counts[detail["theme"]] = dominant_theme_counts.get(detail["theme"], 0) + 1
        impact = summarize_market_impact(item, detail["theme"], detail["direction"])
        drivers.append(
            {
                "theme": detail["theme"],
                "source": item.source_name,
                "headline": item.title,
                "impact": impact,
                "score": detail["score"],
                "direction": detail["direction"],
                "matched_keywords": detail["matched_keywords"],
                "link": item.link,
            }
        )
        news_titles.append(f"[{detail['theme']}/{item.source_name}] {item.title}")

    dominant_theme = max(dominant_theme_counts, key=dominant_theme_counts.get)
    positive_count = sum(driver["direction"] == "偏利多" for driver in drivers)
    negative_count = sum(driver["direction"] == "偏利空" for driver in drivers)

    if positive_count > negative_count:
        stance = "整体偏利多"
        action = "更容易先由 BTC 走强，再向主流山寨扩散。"
    elif negative_count > positive_count:
        stance = "整体偏利空"
        action = "更需要提防 BTC 带头回撤引发主流币共振。"
    else:
        stance = "方向中性"
        action = "重点看 BTC 是否先给出放量突破或破位确认。"

    summary = (
        f"消息面判断：当前更像由{dominant_theme}驱动，{stance}，"
        f"优先盯这 {len(drivers)} 条真正可能影响 BTC 与主流山寨联动的事件。{action}"
    )
    return summary, news_titles, drivers, status_line


def build_symbol_report(coin: dict, thresholds: Thresholds) -> tuple[dict, list[str]]:
    candles_15m = get_candles(coin["swap_symbol"], "15m", 2)
    latest_price = candles_15m[-1]["close"]
    change_15m_pct = pct_change(candles_15m[-1]["close"], candles_15m[-2]["close"])
    strategy = classify_contract_setup(change_15m_pct, coin["change_1h_pct"], coin["change_24h_pct"])

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
        flags.append(f"{coin['symbol']} 15分钟异动 {signed_pct(change_15m_pct)}")
    if abs(coin["change_1h_pct"]) >= thresholds.hourly_change_pct:
        flags.append(f"{coin['symbol']} 1小时趋势拉伸 {signed_pct(coin['change_1h_pct'])}")
    if abs(coin["change_24h_pct"]) >= thresholds.daily_change_pct:
        flags.append(f"{coin['symbol']} 日内波动较大 {signed_pct(coin['change_24h_pct'])}")
    if abs(coin["change_7d_pct"]) >= thresholds.weekly_change_pct:
        flags.append(f"{coin['symbol']} 周线趋势显著 {signed_pct(coin['change_7d_pct'])}")
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
    if not reports:
        return {"score": fear_greed_score, "label": classify_sentiment(fear_greed_score)}

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
    news_items: list[NewsItem],
    news_status: dict,
    news_limit: int,
) -> dict:
    bullish = [report["symbol"] for report in reports if report["strategy"] == "趋势偏多"]
    bearish = [report["symbol"] for report in reports if report["strategy"] == "趋势偏空"]

    if len(bullish) >= 4:
        trend = "主流币整体偏强，短线多头更占优。"
    elif len(bearish) >= 4:
        trend = "主流币整体承压，短线空头更占优。"
    else:
        trend = "主流币分化明显，先看 BTC 是否给出方向确认。"

    news_summary, selected_headlines, market_drivers, news_source_status = build_news_snapshot(
        news_items,
        news_status,
        news_limit,
    )
    external_sentiment_label = translate_fear_greed_label(fear_greed_label)

    return {
        "title": "OKX 合约市值观察",
        "headline": trend,
        "summary": f"{sentiment['label']} ({sentiment['score']}/100)",
        "external_sentiment": f"{external_sentiment_label} ({fear_greed_score}/100)，{explain_fear_greed(fear_greed_score)}",
        "news_summary": news_summary,
        "news_source_status": news_source_status,
        "market_drivers": market_drivers,
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
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
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
        f"- 外部情绪温度：{digest['external_sentiment']}",
        f"- 榜单范围：**TOP {len(reports)} 市值币（OKX 永续）**",
        f"- 观察周期：**{digest['interval_label']}**",
        "",
        f"## 今日趋势分析：{digest['headline']}",
        "",
    ]

    if digest["news_summary"] or digest["market_drivers"]:
        lines.extend(["## 消息面主驱动", ""])
        if digest["news_summary"]:
            lines.append(f"- {digest['news_summary']}")
        if digest.get("news_source_status"):
            lines.append(f"- 消息源状态：{digest['news_source_status']}")
        for driver in digest["market_drivers"]:
            lines.append(
                f"- [{driver['theme']}/{driver['source']}] {driver['impact']} 原始快讯：{driver['headline']}"
            )
        lines.extend(["", "## TOP 市值观察（OKX 永续）", ""])

    lines.extend(
        [
            "| 排名 | 币种 | 最新价 | 15分钟 | 1小时 | 今日涨跌 | 本周涨跌 | 本月涨跌 | 策略 |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )

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
    news_items, news_status = collect_news_items(config)
    digest = build_market_digest(
        reports,
        sentiment,
        fear_greed_score,
        fear_greed_label,
        flags,
        news_items,
        news_status,
        config.news_headlines,
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
