# HL Quant Lab

Hyperliquid 主网只读行情、历史数据研究和观察池。当前交付 M1，控制台显示 PAPER MODE。

## 启动

安装并启动 Docker 及 Compose v2。Windows PowerShell 和 Linux shell 均在项目根目录执行：

```sh
docker compose up --build -d
```

打开 http://localhost:5173。API 地址为 http://localhost:8000/docs。空数据库通过 SQL 迁移初始化。端口绑定到环回地址。首次全市场补齐由 worker 限速轮转，系统页面会显示积压。Docker 数据卷在普通停止和重启时保留。

```sh
docker compose ps
docker compose logs -f market
docker compose restart market api
```

## 原生开发

依赖 Python 3.12、PostgreSQL 16、Node.js 24。用 PostgreSQL 管理工具创建开发库及所有者，将 `DATABASE_URL` 指向开发库。以下两个后端命令分别在不同终端执行。

### Windows PowerShell

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.lock -r backend/requirements-dev.txt
$env:DATABASE_URL = 'postgresql://quant:quant@127.0.0.1:5432/quant'
$env:PYTHONPATH = "$PWD/backend"
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
# 另一个终端设定相同变量，再启动采集：
.\.venv\Scripts\python.exe -m app.workers.market
```

### Linux

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock -r backend/requirements-dev.txt
export DATABASE_URL=postgresql://quant:quant@127.0.0.1:5432/quant
export PYTHONPATH="$PWD/backend"
.venv/bin/python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
# 另一个终端设定相同变量，再启动采集：
.venv/bin/python -m app.workers.market
```

第三个终端执行 `npm --prefix frontend ci`，随后运行 `npm --prefix frontend run dev -- --host 127.0.0.1`。Windows 可用 `npm.cmd` 调用同样的命令。

## 验收

1. 打开“系统”，检查采集心跳、快照更新时间和四类市场计数。行情源不可达时查看错误和缓存时间。
2. 在“市场”搜索 BTC，打开 K 线。首次启动等待下载，或把资产加入启用的观察池，促进 1m 补齐。图表标记最新一根是否闭合。
3. 在“观察池”创建手动池并加入 BTC。创建动态池，预览原生永续的成交额 Top 10，保存并刷新，检查进出池记录。切换流通市值筛选可检验缺失提示。
4. 同一资产加入两池，在“系统”查看池引用和实际订阅数量。关闭页面或重启后重新打开，确认池、历史及后台采集仍然存在。

运行测试时将 `TEST_DATABASE_URL` 指向具备创建测试 schema 权限的 PostgreSQL 库，并延续上述 `PYTHONPATH` 配置。测试仅创建并清理专用临时 schema。

```sh
python -m pytest -p no:cacheprovider backend/tests -q
npm --prefix frontend test
npm --prefix frontend run build
docker compose config --quiet
```

在已有行情及 BTC K 线的本机服务上，执行 `npm --prefix frontend run test:e2e` 运行浏览器验收。可安装 Playwright Chromium，或在现有 Edge 环境设置 `M1_BROWSER_CHANNEL=msedge`。用 `M1_BASE_URL` 覆盖本地默认地址。

## 数据与恢复

- PostgreSQL 是目录、观察池、行情快照、事件、资金费率和在线 K 线的权威存储。闭合 K 线使用 REST 确认，历史轮转覆盖 1m/3m/5m/15m/1h/4h/1d。Outcomes 当前仅目录。永续 OI 以基础资产计。
- 24h 成交额筛选仅对 USDC 计价市场做可比排序。30 分钟波动率使用 30 个连续的 1m 收益率。成交量异动使用最近 5 根闭合 1m K 线的 `close × volume` 之和，与前五个等长窗口的平均值相比。该数值是成交额估算。
- 外部流通市值可由部署者配置 `MARKET_CAP_FILE` 指向只读 JSON 文件，按稳定资产 ID 显式映射。例如 `{"PERP::0":{"market_cap_usd":"1200000000000","as_of":"2026-11-24T03:00:00Z","source":"vendor:bitcoin"}}`。超过 24 小时或缺失的市值显示 N/A。
- 使用 `python -m app.market_data.archive --date YYYY-MM-DD` 导出已过去的 UTC 日期内已采集的闭合 K 线。追加 `--dataset bbo` 或 `--dataset trades` 可按接收日期归档对应事件。分区按 UTC 日期及市场分类，写入 ZSTD Parquet，回读校验、计算 SHA-256 并登记 manifest。Parquet 可作为冻结的研究输入，金额字段按精确的十进制字符串保存。自动热数据清理待完成。
- 使用 `python -m app.market_data.import_candles data/input.csv --instrument PERP::0 --interval 1m --source vendor-dataset-version` 导入明确授权的本地 CSV。表头为 `open_ts,open,high,low,close,volume,trades`，要求显式时区、周期对齐、时间递增、闭合 OHLCV。重复文件按 SHA-256 识别，官方确认 K 线优先。也可将 CSV 放在 `DATA_DIR/imports`，以文件名、资产 ID、周期、来源调用 `POST /api/v1/data/import-jobs`，再按返回的 ID 查询。
- 将 PostgreSQL 客户端放入 PATH，设置 `DATABASE_URL` 后执行 `python scripts/database.py backup quant.dump`。恢复时先建立独立空库，设置 `RESTORE_DATABASE_URL`，再执行 `python scripts/database.py restore quant.dump`。备份 Parquet 时同时保存 `DATA_DIR/parquet`，通过 manifest 核验文件。Windows PowerShell 使用 `$env:DATABASE_URL` 和 `$env:RESTORE_DATABASE_URL`。

设计、实施分期和验收记录见 [设计基线](doc/Hyperliquid_Quant_Design_v1.md)、[实施计划](docs/IMPLEMENTATION_PLAN.md) 和 [开发进度](docs/PROGRESS.md)。

