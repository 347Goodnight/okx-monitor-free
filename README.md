# okx-monitor-free

纯免费、不要信用卡的 MVP：

- GitHub Actions 每 5 分钟拉一次 OKX 市场数据
- Python 脚本做基础情绪分析和策略分析
- Cloudflare Worker 作为飞书告警中转

## 当前功能

- `GET /healthz` 健康检查
- `POST /alert` 飞书告警转发
- GitHub Actions 定时监控
- 基础信号：
  - 5 分钟异动
  - 1 小时趋势拉伸
  - 成交量放大
  - 波动率升高
  - 20 根高低点突破/跌破
- 市场情绪摘要：
  - Fear & Greed
  - BTC / ETH 趋势与动量
- 策略结论：
  - 趋势多
  - 趋势空
  - 震荡
  - 高风险观望

## 本地调试 Worker

```bash
npm install
npx wrangler login
npx wrangler secret put FEISHU_WEBHOOK_URL
npx wrangler secret put ALERT_AUTH_TOKEN
npm run dev
```

## 测试 Worker

```bash
curl -X POST http://127.0.0.1:8787/alert \
  -H "content-type: application/json" \
  -H "x-alert-token: your-token" \
  -d "{\"title\":\"测试告警\",\"level\":\"info\",\"message\":\"Cloudflare Worker 到飞书链路正常\"}"
```

## 发布 Worker

```bash
npm run deploy
```

部署后记下你的地址，例如：

```text
https://okx-monitor-free.<subdomain>.workers.dev/alert
```

## GitHub Secrets

在仓库 `Settings -> Secrets and variables -> Actions` 新增：

- `ALERT_ENDPOINT`
  值为 Worker 的 `/alert` 地址
- `ALERT_TOKEN`
  值为你写入 Cloudflare Secret 的 `ALERT_AUTH_TOKEN`

## 本地跑分析脚本

```bash
python scripts/run_monitor.py --config config/symbols.json --summary-file monitor-summary.md
```

## GitHub Actions

工作流文件：

- `.github/workflows/monitor.yml`

触发方式：

- 每 5 分钟一次
- 手动 `Run workflow`
