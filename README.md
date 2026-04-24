# okx-monitor-free

一个更贴近盯盘场景的 OKX 合约监控项目：

- GitHub Actions 定时拉取 OKX / CoinGecko 数据
- Python 脚本生成 TOP 市值币盘面摘要
- Cloudflare Worker 转发飞书告警
- 消息面支持多源聚合、去重、打分、确认层加权和自动降级

## 当前能力

- `GET /healthz` 健康检查
- `POST /alert` 飞书告警转发
- TOP 市值 OKX 永续合约监控
- 市场情绪摘要
  - Fear & Greed
  - BTC / 主流币短线趋势
- 消息面主驱动筛选
  - 中文快讯发现层
  - 官方确认层
  - 跨源去重
  - 宏观 / 政策 / 资金面优先级打分
  - 单源失败自动降级

## 已接入消息源

发现层：

- `Wu Blockchain` RSS
- `CoinDesk` RSS
- `Odaily Newsflash`
- `ChainCatcher`

确认层：

- `Fed` press release RSS
- `SEC` press release RSS
- `Treasury` official press release page
- `SoSoValue ETF Flows`

## 配置说明

默认新闻源在 [config/symbols.json](/E:/Codex%20Projects/okx-monitor-free/config/symbols.json)。

重点字段：

- `news_fetch_limit_per_source`
  每个源最多抓多少条候选内容
- `news_sources`
  新闻源列表
- `kind`
  当前支持：
  - `rss`
  - `json`
  - `odaily_newsflash_html`
  - `chaincatcher_nuxt`
  - `treasury_press_html`
  - `sosovalue_etf_summary`
- `role`
  - `headline` 用于快讯发现层
  - `confirmation` 用于确认层，权重更高
- `required_env`
  某些源必须依赖环境变量，缺失时会自动跳过而不是报错

## ETF Flows

`SoSoValue ETF Flows` 使用官方 API 文档里的：

- Base URL: `https://openapi.sosovalue.com/openapi/v1`
- Endpoint: `GET /etfs/summary-history`
- Query: `symbol=BTC&country_code=US`
- Header: `x-soso-api-key`

启用前请设置环境变量：

```bash
$env:SOSOVALUE_API_KEY="your-api-key"
```

未设置时，该源会被自动跳过，不影响其他源运行。

## 飞书消息结构

飞书里会优先展示：

1. `消息面主驱动`
2. `消息源状态`
3. `TOP 市值观察`
4. `风险提示`

这样更贴近盯盘流程，先看会不会驱动 BTC 和主流山寨联动，再看盘面细项。

## 本地调试 Worker

```bash
npm install
npx wrangler login
npx wrangler secret put FEISHU_WEBHOOK_URL
npx wrangler secret put ALERT_AUTH_TOKEN
npm run dev
```

如果你已经开着 `wrangler dev`，改完 [worker/index.js](/E:/Codex%20Projects/okx-monitor-free/worker/index.js) 后建议重启一次本地 dev 进程。

## 测试 Worker

```bash
curl -X POST http://127.0.0.1:8787/alert \
  -H "content-type: application/json" \
  -H "x-alert-token: your-token" \
  -d "{\"title\":\"测试告警\",\"headline\":\"今日趋势分析：先看 BTC 是否确认方向。\"}"
```

## 发布 Worker

```bash
npm run deploy
```

部署后记录地址，例如：

```text
https://okx-monitor-free.<subdomain>.workers.dev/alert
```

## GitHub Secrets

在仓库 `Settings -> Secrets and variables -> Actions` 中添加：

- `ALERT_ENDPOINT`
  Worker 的 `/alert` 地址
- `ALERT_TOKEN`
  与 Cloudflare Secret 中的 `ALERT_AUTH_TOKEN` 保持一致
- `SOSOVALUE_API_KEY`
  可选，仅用于启用 ETF flows 确认层

## 本地运行分析脚本

```bash
python scripts/run_monitor.py --config config/symbols.json --summary-file monitor-summary.md
```

## GitHub Actions

工作流文件：

- `.github/workflows/monitor.yml`

触发方式：

- 每 5 分钟一次
- 手动 `Run workflow`
