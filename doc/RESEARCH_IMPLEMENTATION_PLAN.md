# Research Lab 实施计划

V2 是 V1 的增量。V1 已交付 M1 公共采集、行情、观察池、监控、Parquet、M2 策略及 L1 回测；M3 模拟账户和 L2 执行尚未交付。当前 PostgreSQL 已应用 001–007，五个 Compose 服务运行。研究层复用 instruments、market_events、data_gaps、worker_leases、market_data_manifest、PostgreSQL/DuckDB、公开 WS 适配和现有 Vue 应用。

1. **R0** 独立 depth 进程先启用公开主网 l2Book。研究通道使用现有 market_events 的 research.* 类型与独立接收序列，使原 M1 订阅、写入和回测语义保持。研究 BBO 与成交采样同源，独立连接上的同一市场消息仅在研究通道内去重。保留双时间戳、连接区间和质量；复用原子 Parquet 归档。
2. **R1** 公共事件驱动特征。以可用时间推进 1 秒 tick，先读输入再取快照；成交使用交易所时间的左开右闭窗口。健康空窗与缺口分开处理。配置参数 Pydantic 校验并哈希，禁止从 OHLC 推断盘口。
3. **R2** 独立 research worker 与持久化任务，存储确定性事件、状态和标签。回放按可用时间及入库序号排序，在线同样消费已持久化输入。固定 horizon 分离方向预测、覆盖、MFE/MAE 与 BBO 深度约束执行，缺失显式记录。
4. **R3** S01 阈值、双向边沿、冷却及 A–D 消融。按资产、方向、时间块和事前波动及价差匹配 A 组对照。使用固定种子的时间块 bootstrap，匹配不足时标记原因并省略置信区间。Research Lab 接入质量、配置、任务、事件详情、原始行情、有界回放及标签。
5. R4–R7 复用市场事件、特征、Detector 及标签接口。由隔离 PostgreSQL、真实主网扫描和浏览器验收各阶段。长时测试与磁盘治理未验证前继续列待办。

官方 l2Book 是有限档快照；trades 的稳定键使用交易所时间、资产和 tid；账户数据和签名路径与本项目无关。官方每 IP 10 WS 连接、30 次新连接/分钟、1,000 订阅、2,000 发送消息/分钟。本阶段从 10 个研究资产、每资产三条流开始，配置可减小或扩容。 [官方订阅](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions) · [官方限额](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)

