# 实施计划

设计基线为 [V1.0](../doc/Hyperliquid_Quant_Design_v1.md) 第 1–17 节。交付按 M1、M2、M3、M4 推进，M5 另行授权。

## M1 · 实施顺序

1. 建立 Python 3.12 / FastAPI、PostgreSQL SQL 迁移、独立采集进程和 Vue 3 / TypeScript / Vite 控制台。Compose 负责依赖顺序和本机端口。
2. 实现只读适配器，按市场作用域刷新目录及快照，记录首见和下线，区分展示名与 API 标识。使用 Decimal、UTC 与明确的缺失值。
3. 建立有权重的 REST 限流和短期 K 线轮询补齐，持久化闭合 K 线并检测缺口；观察资产共享 candle、BBO、trades 订阅，恢复时重订阅并补齐 K 线。
4. 实现手动、动态、混合池及按规则版本保存的筛选审计。预览保持只读。缺失市值、过期快照及缺少完整窗口的资产排除并给出原因。
5. 实现市场、观察池、系统页面和增量状态更新。执行单元、数据库集成、真实只读主网、前端构建和启动验收。
6. 提供备份恢复命令和 M2/M3 数据、时钟及执行端口。Parquet/DuckDB 用于长期归档与回放，M1 热数据以 PostgreSQL 为权威，迁移前校验分区和 manifest。

## 主网验证

2026-11-24 主网 POST /info 实测：perpDexs 首项 null，另有 10 个 DEX；原生 universe 和 contexts 各 235 项，xyz 各 148 项，按位置关联；collateralToken 是 token index。spotMetaAndAssetCtxs 的 universe 有 356 项，contexts 有 1195 项，必须用 coin 建索引，不能 zip。outcomeMeta 返回 outcomes/sideSpecs/quoteToken，Outcomes 作为观察目录。验证 allMids、l2Book、fundingHistory 和 candleSnapshot，后者以 t/T/s/i/o/h/l/c/v/n 返回，包含当前未闭合 K 线。

官方限制：REST/IP 汇总权重 1200/分钟，常规 info 权重 20，allMids/l2Book 2，K 线每 60 条另计权重；历史 K 线最多最近 5000 根。WS 最大 10 个连接、30 个连接建立/分钟、1000 个订阅及 2000 条发送消息/分钟。使用保守预算，429 有界退避。全市场规模下无法每分钟逐资产发送 candleSnapshot，轮转补齐闭合区间并优先照顾观察资产。

- [Info](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [永续](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
- [现货](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot)
- [订阅](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)
- [限额](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)

## 后续验收

- M2：在固定 fixture 上验证虚拟时钟、K 线信号时序、L1 撮合、账户账本、风控和可重现回测。
- M3：复用事件端口接入 BBO 模拟，验证订单生命周期、断线停开仓、幂等恢复、账户隔离和 24 小时运行。
- M4：历史动态筛选、订单簿回放、组合实验及滚动验证，要求可追溯且无前视。


## M1 实现细节

- 模块方向为 API → 存储与观察池，观察池 → 筛选器与时点行情，采集 worker → 只读适配器、行情存储与观察池。实时采集与用户 API 分进程。单市场 worker 以 PostgreSQL 会话锁协调共享 REST 预算。
- SQL 管理活动数据。DuckDB 输出不可变 Parquet 分区，支持闭合 K 线及原始 BBO/成交，按日期与市场类型分区。导入任务校验本地文件、来源、时区、排序、闭合状态和缺口。批量研究后续使用 Polars 与 DuckDB，统计页面再接 ECharts。
- REST 验证后在当前主网直接请求 BTC 七天 1m，实际收到最近 5000 根。WebSocket 实测 BTC 的 candle、BBO、成交，并将原始结构纳入测试 fixture，成交参与地址已脱敏。适配器仅连接公开 `/info` 与行情 WS。
- 多筛选器先独立排名，再合并去重，按最佳排名及稳定 ID 排序。动态成员缓冲在已有成员落出出池名次后才释放。波动率要求连续 31 根闭合 1m 数据，市值缺失不以持仓量替代。
- [官方 SDK](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)提供协议参考。M1 用 HTTPX 与 websockets 管理只读网络、权重和重连。
- [DuckDB Parquet 文档](https://duckdb.org/docs/stable/data/parquet/overview)

## M1 运行保障与 M2 回测实施

先补存储保护、覆盖率和运行指标，再接入隔离的回测 worker。策略只接收已闭合事件，执行端口由运行模式注入。回测冻结观察池成员和版本，读取一致的 K 线与结算资金费率，记录来源和数据哈希，按下一可用 K 线开盘成交。L1 同根冲突默认止损优先，未平仓按期末标记价值报告。通过隔离数据库、构造行情及真实主网历史数据验收，前端使用参数 schema 与持久化任务接口。

### M2 回放规则与接口

预检要求全部闭合 K 线和 EMA 预热窗口，时间对齐 UTC 周期，每任务最多 20 个原生 USDC 永续和十万根 K 线。资金费率完整性可设为严格要求。一个回测任务对应一个隔离实例，支持查询与取消，进程中断从固定任务配置重新运行，成功时在同一事务提交幂等账本和报告。用一致读快照计算来源及数据 SHA-256。收盘时策略只能看当前及此前闭合事件，信号收益与 MFE/MAE 在运行结束后计算。独立 backtest worker 与已部署 API、market、db 和 frontend 通过 Compose 启动。报告明确记录 L1 开盘执行、保守冲突处理、资金费率价格近似以及期末持仓估值。
