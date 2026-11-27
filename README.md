# HL Quant Lab

Hyperliquid 主网只读行情、历史数据研究和观察池。包含 M1 行情采集与观察池、M2 历史回测，控制台显示 PAPER MODE。

## 一键启动

Docker 会运行 PostgreSQL、API、采集 worker、回测 worker、盘口采集、研究计算和前端。使用 Docker 启动时，宿主机无需另外安装 PostgreSQL、Python 或 Node.js。空数据库会自动迁移初始化。

### Windows

1. 首次安装 Docker Desktop，启用 WSL 2 后端并使用 Linux 容器。首次打开 Docker Desktop 时，完成许可提示和 WSL 初始化。
2. 以后双击项目根目录的 `start.bat`。Docker 已就绪时会直接启动项目。Docker 未运行时，脚本会尝试启动 Docker Desktop 并等待。如果 Desktop 使用自定义安装位置且 CLI 无法启动它，请先手动打开 Desktop，再双击脚本。
3. 启动成功后会在浏览器打开控制台。从 PowerShell 运行 `.\start.bat --no-browser` 可跳过打开浏览器和失败时的暂停。

### Linux

安装 Docker Engine 和 Docker Compose v2 插件。首次部署时确认 Docker 服务已启动，并且当前账号可运行 `docker info`。在使用 systemd 的发行版上，可由管理员运行 `sudo systemctl enable --now docker` 启动服务并设为开机启动。

```bash
bash start.sh
```

也可执行 `chmod +x start.sh` 后使用 `./start.sh`。脚本会切换到项目目录，可通过绝对路径从其他目录调用。

### 访问与排查

- 控制台 [http://127.0.0.1:5173](http://127.0.0.1:5173)
- API 文档 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- 两个脚本会构建镜像、后台启动服务并检查前端能否连到 API。首次需要下载镜像和依赖，行情补齐仍会在启动成功后继续。
- 服务只绑定本机。远程 Linux 服务器可在本地运行 `ssh -N -L 5173:127.0.0.1:5173 -L 8000:127.0.0.1:8000 user@server`，再打开上述地址。
- 查看状态用 `docker compose ps -a`，查看日志用 `docker compose logs --tail 80`。如果提示端口已占用，检查本机 5173 或 8000 上正在运行的服务。
- 停止用 `docker compose stop`。重新运行启动脚本会继续使用原有数据卷。`docker compose down -v` 会删除数据库和行情数据卷。

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

- PostgreSQL 是目录、观察池、行情快照、事件、资金费率和在线 K 线的权威存储。闭合 K 线使用 REST 确认，历史轮转覆盖 1m/3m/5m/15m/30m/1h/4h/1d。Outcomes 当前仅目录。永续 OI 以基础资产计。
- 24h 成交额筛选仅对 USDC 计价市场做可比排序。30 分钟波动率使用 30 个连续的 1m 收益率。成交量异动使用最近 5 根闭合 1m K 线的 `close × volume` 之和，与前五个等长窗口的平均值相比。该数值是成交额估算。
- 外部流通市值可由部署者配置 `MARKET_CAP_FILE` 指向只读 JSON 文件，按稳定资产 ID 显式映射。例如 `{"PERP::0":{"market_cap_usd":"1200000000000","as_of":"2026-11-24T03:00:00Z","source":"vendor:bitcoin"}}`。超过 24 小时或缺失的市值显示 N/A。
- 使用 `python -m app.market_data.archive --date YYYY-MM-DD` 导出已过去的 UTC 日期内已采集的闭合 K 线。追加 `--dataset bbo` 或 `--dataset trades` 可按接收日期归档对应事件。分区按 UTC 日期及市场分类，写入 ZSTD Parquet，回读校验、计算 SHA-256 并登记 manifest。Parquet 可作为冻结的研究输入，金额字段按精确的十进制字符串保存。自动热数据清理待完成。
- 使用 `python -m app.market_data.import_candles data/input.csv --instrument PERP::0 --interval 1m --source vendor-dataset-version` 导入明确授权的本地 CSV。表头为 `open_ts,open,high,low,close,volume,trades`，要求显式时区、周期对齐、时间递增、闭合 OHLCV。重复文件按 SHA-256 识别，官方确认 K 线优先。也可将 CSV 放在 `DATA_DIR/imports`，以文件名、资产 ID、周期、来源调用 `POST /api/v1/data/import-jobs`，再按返回的 ID 查询。
- 将 PostgreSQL 客户端放入 PATH，设置 `DATABASE_URL` 后执行 `python scripts/database.py backup quant.dump`。恢复时先建立独立空库，设置 `RESTORE_DATABASE_URL`，再执行 `python scripts/database.py restore quant.dump`。备份 Parquet 时同时保存 `DATA_DIR/parquet`，通过 manifest 核验文件。Windows PowerShell 使用 `$env:DATABASE_URL` 和 `$env:RESTORE_DATABASE_URL`。

设计、实施分期和验收记录见 [设计基线](doc/Hyperliquid_Quant_Design_v1.md)、[实施计划](docs/IMPLEMENTATION_PLAN.md) 和 [开发进度](docs/PROGRESS.md)。


## M1 运行保障

- 系统页展示首次补齐与周期续采、观察池闭合 1m K 线超时、近 24 小时缺口、WS 行情延迟、上游 REST 错误率及本地 API 5xx 比例。错误率取最近 5 分钟的进程内样本。
- 采集默认在可用空间或项目配额余量低于 2 GiB 时告警，低于 512 MiB 时暂停 REST、WS 与后台行情导入。空间恢复后自动续采。使用 `DISK_WARN_MIB`、`DISK_STOP_MIB`、`DATA_MAX_MIB` 配置，默认项目配额为 10 GiB。项目配额按当前 PostgreSQL 库与行情文件占用计算。Docker 虚拟磁盘空间仍按容器视角显示。可向 market 容器额外挂载只读的宿主磁盘目录，并将 `DISK_PROBE_PATH` 设为该挂载的容器路径，将其报告的可用空间也纳入保护。
- 在行情弹窗选择周期和日期范围后查询闭合 K 线覆盖率。REST 对应 `GET /api/v1/markets/{id}/coverage?interval=15m&start=...&end=...`，区间为左闭右开。
- 已启动 Compose 时可执行 `python scripts/m1_load.py --seconds 60`。脚本创建临时观察池订阅 20 个原生资产，记录延迟和重连，结束后移除测试池。

## M2 回测

启动 Compose 后，在「策略」配置 EMA 或确定性测试策略，在「回测」选择原生 USDC 永续或观察池、日期范围和成本。时间须对齐选定周期。集合、观察池规则版本及策略代码哈希在提交时冻结。每次任务在独立回测 worker 内创建隔离虚拟账户，初始资金、仓位、指标与策略状态按实例分开。报告可查看并导出交易 CSV，点击成交记录可联动 K 线图。

- 回测要求每个资产的闭合 K 线与预热完整。可要求所有历史资金费率时段覆盖，否则报告明确标记缺失时段。资金费率使用实际历史利率，L1 的结算名义价格使用结算时最后已知 K 线价格近似交易所预言机价格。[Hyperliquid 官方资金费率](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)
- 收盘意图默认下一根开盘执行，反向信号先平仓。同根触及止盈和止损时先处理止损，跳空止损按开盘可用价格并计滑点。盘中触发只能从 OHLC 推断，持仓期内不能确定先后的事件按报告模型处理。末尾持仓以最后已知价格估值。
- 基础风控检查可用资金、保证金、持仓数和总敞口。L1 报告用作工程验证，保证金阶梯、强平预测、实时模拟及盘口撮合属于后续工作。
- 信号研究在策略运行结束后单独计算方向收益和 MFE/MAE。仅对当前 K 线粒度能完整覆盖的 1、5、15、60 分钟窗口给出样本。
- `TEST_DATABASE_URL` 指向隔离 PostgreSQL 后运行 `python -m pytest -p no:cacheprovider backend/tests -q`。浏览器验收可设置 `M2_REPORT_ID` 为带已平仓交易的回测 ID，再运行 `npm --prefix frontend run test:e2e`。

## Research Lab（V2 R0–R3）

使用原有一键启动脚本。新增 `depth` 采集和 `research` 计算服务，行情持续采集可通过 `docker compose logs -f depth` 查看。研究默认采集 10 个原生 USDC 永续的 l2Book、BBO 和逐笔成交，资产名单可在 `.env` 配置 `RESEARCH_ASSETS`（交易所代码，逗号分隔，最多 20）。研究数据独立记录实际接收和可用时间。L2 为交易所有限档快照。

在「Research Lab」检查覆盖与延迟，选择资产并创建历史回放或实时研究。S01 默认需要至少约一小时的历史预热与基准，快速验证可将 `warmup_s` 和 `baseline_s` 都设为 60，再用较短标签周期。查看任务覆盖率、A–D 消融、未来收益、BBO 可执行估计与事件详情。研究结果按事件、配置和原始数据指纹追溯。样本不足时显示状态，BBO 估计各腿受可见数量约束并按版本化费率和滑点计算。

- 基线验收使用隔离 PostgreSQL。前端可使用 `npm --prefix frontend run test:e2e` 检查实际 Research Lab 报告。传入 `RESEARCH_TEST_API` 可将研究接口定向到隔离的本地 API。
- 归档例子 `docker compose exec api python -m app.market_data.archive --date 2026-11-26 --dataset research.l2`，使用已经完成的 UTC 日期。原始档位和共享时序元数据按资产/流/日期保存并校验，可按已固定的归档重放。
- `GET /api/v1/research/quality/retention` 打印保留策略预览。已引用的分区标记为 pinned，当前没有自动清理。研究采集先于 M1 的临界空间阈值暂停，默认恢复滞回量为 128 MiB。
- 容量、长期稳定性与当前剩余项目见 [研究进度](doc/RESEARCH_PROGRESS.md)。
