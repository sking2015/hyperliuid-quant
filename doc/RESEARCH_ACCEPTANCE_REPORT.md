# Hyperliquid Quant Research V2 第一阶段验收报告

> R3.1 复验已经补充。原报告保留修复前证据；后置标签失败用例原期望现已通过。隔离库测试从 55 通过、1 失败变为 58 通过、0 失败；最终浏览器回归 3 项通过。完整两小时默认后置窗口、24 小时稳定性及长期 20 资产负载仍待验收。见 [R3.1 修复报告](R3_1_FIX_REPORT.md)。

**2026-11-27，Asia/Shanghai。结论：R0–R3 尚未达到完整交付标准。** 已验证 10 资产持续入库、20 资产短时主网负载、实时与多资产历史任务、S01 独立消融及页面展示。一项新增集成测试明确复现 REPLAY 后置标签截尾。24 小时稳定性、完整两小时默认回放、生产归档清理等仍有门槛。本轮只增补验收测试和报告，保留失败用例及现有运行逻辑。

## 1. 覆盖矩阵

路径缩写：`R=backend/app/research`，`W=backend/app/workers`，`A=backend/app/market_data/archive.py`，`UI=frontend/src/views/ResearchLab.vue`，`FC=frontend/src/components/ForwardCurve.vue`。测试缩写：`U=backend/tests/test_research.py`，`M=backend/tests/test_research_acceptance.py`，`I=backend/tests/test_integration.py`，`E=frontend/e2e/research.spec.ts`。下表的主网、数学及浏览器证据见后文。空缺项注明缺失位置和验证边界。

| V2 条款 | 验收项 | 结果 | 代码、测试及证据 |
|---|---|---|---|
| §3.1 | Tier 1 市场、K 线、状态复用 | PASS | `W/market.py`、`scripts/m1_load.py::main`、`I::test_candle_dedup_and_coverage`、M1 Edge 实测。 |
| §3.1 | Tier 2 30–50 资产、Tier 3 10–20 资产 | PARTIAL | `W/depth.py::universe/collect` 上限 20，生产为 10；主网独立 20 资产接收通过，30–50 研究特征与持续入库无验收。 |
| §3.1 | 多流动性覆盖、池进入退出、规则记录与分用途引用 | PARTIAL | `universe` 固定代币列表，V1 观察池和引用测试通过；研究池历史、持仓/待标签引用及突发退池合同无实现。 |
| §3.1–2 | 主网 Trades、BBO、L2 解析、精度、有限档、预先持续采集 | PASS | `R/feed.py::normalize/valid_levels`，`U::test_mainnet_l2_fixture_preserves_precision_and_limits`，30 个实际资产×流组均有数据库记录。 |
| §3.2 | 原始字段、稳定资产/源键与时间戳 | PARTIAL | `feed::normalize/persist`，迁移 008；SQL 零 null 交易所时间、零负接收延时、零 available 早于 receive。写入语句时间可能早于批事务提交；真实接收时间取自应用出队，负载下含队列等待。成交额由原始价格×数量计算，原始行未显式保存 notional。 |
| §3.2 | 上下文、资金结算、缺口、manifest | PARTIAL | `W/depth.py::contexts/finish_gap`、`A::archive_partition`、`I::test_funding_settlement_idempotency`；实际 context 缺交易所时间显式为 null；缺口表无连接 ID；V1 结算未进入研究输入。 |
| §3.2–3 | 重复、迟到、乱序、断线、报价过期、回放排序 | PARTIAL | `feed::persist` 主键冲突忽略，两次跨提交重复同一真实成交保留原始行。`R/store.py::feed_rows` 三键排序，`R/features.py::on_event/seal`。`M::test_disconnect_out_of_order_and_stale_quote`、`U::test_feature_boundaries_late_and_unknown` 通过；主网实收一处断线，593 次迟到、275 次乱序。跨批累计计数不完整，全局时钟误差及仅有 exchange time 的独立导入模式未验证。 |
| §3.3 | 1 秒 as-of、窗口开闭、坏报价及无成交/缺口 | PASS | `FeatureEngine::advance/on_event/seal/snapshot`，`M::test_disconnect_out_of_order_and_stale_quote`、`U::test_feature_boundaries_late_and_unknown`；手工值见 §5。S03 5 秒规则留待 R5。 |
| §3.4 | PostgreSQL、按日/资产/流 Parquet 回读、引用保护 | PARTIAL | `A::archive_day/archive_partition`，`R/archive.py::ArchivedFeed.merge`、`routes::retention_preview`；`I::test_research_fixture_to_event_label_api_and_archive` 隔离库验证 132 行、热/冷 hash、pin 与 dry-run。隔离库另以真实主网 1,484 行验证三流全字段与顺序相等。生产研究 manifest 为 0，未运行真实热删除。 |
| §3.4 | 磁盘安全、限流、积压、吞吐及恢复 | PARTIAL | `depth::space_available/collect`，`U::test_research_disk_hysteresis_preserves_m1_headroom`；绝对剩余容量、配额、恢复滞回通过，生产告警为 ok；缺少完整 20/10/5% 分级研究保护、丢弃/积压/时钟偏差全量指标与压力注入。 |
| §3.4 | 20 观察资产实际负载 | PASS | `scripts/m1_load.py` 60 秒以及独立公开 WS 三流 20 资产测试均执行，见 §4。 |
| §3.4 | 24 小时连续稳定性 | BLOCKED | `docker compose ps` 与真实最早入库记录显示本轮约 2 小时；无 24 小时完成证据，待运行。 |
| §4.1 | Mid/Spread、窗口成交额、TFI/CVD/RVOL、Persistence、Return/MAD/ShockZ、Depth/OBI | PASS | `features::snapshot/depth`，`M::test_manual_feature_fixture`、`U::test_volume_baseline_excludes_current_and_book_truncation`、`U::test_feature_boundaries_late_and_unknown`。已执行的手算见 §5。 |
| §4.1 | Impact、OI、市场因子、价差分布、全部元数据 | PARTIAL | `features::snapshot` 有 60s Impact、OI、只接受事件前系数、价差中位/P90。无本轮独立逐项手算，空报价部分指标默认 coverage=1，与无有效源不符；回调特征缺失。 |
| §4.2 | 公共接口、参数校验、版本及重启 | PARTIAL | `R/config.py::ResearchParameters.bounds/fingerprint`、`features::reset`、`detectors::S01.checkpoint/restore`、`W/research.py::Runner`；S01 内存 checkpoint 单测通过，任务通过重扫预热恢复。无持久 Feature checkpoint；标签重启用例只验证到 PENDING 后重扫，边界成熟失败。 |
| §5.1 | LIVE/REPLAY 共用代码、预热禁止发正式事件 | PASS | `Runner.advance` 统一调用 `R/engine.py::ResearchEngine.on_tick`，只在 `start <= tick < end` 检测；主网 LIVE/REPLAY 完成，`I::test_replay_matures_labels_after_detection_end` 的前置断言核实四个事件都在开始 tick。 |
| §5.1 | 可扩展多检测器同时管理与状态隔离 | PARTIAL | `detectors::Detector` 有协议，`ResearchEngine.__init__` 硬编码 S01；未提供动态注册、按 detector 启停，也无第二检测器测试。 |
| §5.1 | 边沿、去重、冷却、episode | PASS | `S01.on_tick`、`store::persist_results`；`U::test_detector_cooldown_tracks_edges_and_checkpoints` 与隔离库重跑同一 ID 测试通过；4 组共用 episode。 |
| §5.1 | 候选/拒绝/确认及事件字段、不可变性 | PARTIAL | `engine::on_tick`、`store::persist_results`；主网确认/拒绝及 transition 持久化；价格不满足时没有候选边沿，无法保留所有未通过样本。市场状态固定 UNKNOWN，缺 universe snapshot、引用资产等完整字段。 |
| §5.2 | 默认 8 周期、方向收益、MFE/MAE、路径覆盖和独立上下文 | PASS | `config::ResearchParameters`、`labels::ForwardReturnAnalyzer.label`；`M::test_forward_path_costs_and_visible_quantity` 手算；真实默认任务 4 组×8 周期；检测器只收 features。 |
| §5.2 | PENDING/COMPLETE/MISSING/CENSORED、期后标签及重启 | FAIL | `Runner.advance/run` 与 `store::finalize`；`I::test_replay_matures_labels_after_detection_end` 重启后期外实际两项 CENSORED，两项目标报价确实存在；同用例观测 PENDING，单测验证 MISSING，真实任务验证 COMPLETE。 |
| §5.2 | BBO taker + 手续费、滑点、可见数量与失败率 | PASS | `labels::execution` 支持规范允许的 `insufficient_depth` 分支；`M::test_forward_path_costs_and_visible_quantity` 手算，真实多资产有深度不足记录。 |
| §5.2 | 部分成交输出、目标阈值首次到达 | NOT_IMPLEMENTED | `labels::execution/label` 只做全量或失败，当前首次时间为 MFE/MAE 极值首次时间；无任意目标触及标签。部分成交单列是本次检查项，失败返回满足 V2 的二选一。 |
| §6.1 | 默认阈值、多空镜像、A–D 逐阈值不同 | PASS | `config::ResearchParameters` 默认 .5/.3/2/.75、r60 [.0005,.008]、8bps、20 笔、60s；`M::test_s01_reasons_and_distinct_ablation` 7 组，`U::test_s01_thresholds_mirror_short_and_reject_unknown_notional`。 |
| §6.1 | 拒绝原因、共用质量保护、Impact/OBI/OI/回调 | PARTIAL | `S01.on_tick`，7 组测试及主网拒绝；返回过大/过小时可能没有候选可记录，未预热/坏报价亦直接返回；无回调值，L2 容量未实现。 |
| §6.2 | 同区间同资产同质量 A–D、分组收益/缺失及匹配对照 | PARTIAL | `S01.on_tick`、`routes::aggregates`、`R/ablation.py::paired_summary`；实测各组和各周期，合成区分 AB/ABC/ABCD，bootstrap 可重复。缺分箱报告、各组绝对收益 CI/胜率与直接汇总手续费；匹配差值和净收益存在。 |
| §6.2 | E=增加 OBI/Impact | NOT_IMPLEMENTED | `config::variants` 只接受 A–D；本次用户优先检验 A–D，但 V2 列有 E，单列缺项。 |
| §6.2 | 固定持有/跟踪退出及正式策略 | OUT_OF_SCOPE | R7；当前研究只作行情标签。 |
| §11 | 配置、任务、事件、状态、标签、pin、增量迁移 | PASS | `backend/migrations/008_research_feed.sql`、`009_research_runs.sql`，`store::persist_results`、`routes::config/start`；隔离库迁移、集成与主网 SQL 验证。 |
| §11 | 执行标签、实验可追溯字段、episode 实体、checkpoint | PARTIAL | 执行位于 `research_labels.detail`，实验别名复用 run，episode ID 内嵌事件。独立执行版本/量/Q、代码 hash、冻结宇宙、时间切分、episode 实体与 checkpoint 未完整实现。 |
| §11 | 模型实体 | OUT_OF_SCOPE | §12 明确为 R5；`routes::deferred`。 |
| §11–12 | 限界/分页、事件详情及有界行情回放、按 group 聚合 | PARTIAL | `routes::events/detail/replay/aggregates`；E 与隔离集成测试通过。无 detector 筛选、特征逐秒 replay 或明确 SQL 查询超时；有最大 2,000 条响应及有界查询。 |
| §12 | 检测器 schema、不可变配置、任务进度与幂等停止 | PASS | `routes::detectors/config/start/run_detail/stop`；重复 STOP 两次 200、最终 STOPPED；配置 schema 与参数 hash 随任务保存。 |
| §12 | 启动前覆盖预检、成组异步实验 | PARTIAL | `routes::start` 检查资产池和连接，历史缺数据仍接受进入队列，由 `Runner.advance` 末尾判定。POST experiments 只接单一配置，本轮三组通过 3 个任务提交。 |
| §12 | WS 事件摘要、进度、质量告警与重连补发 | PARTIAL | `routes::updates/latest_update` 仅推 revision；UI 用 10 秒 REST 轮询，无事件摘要推送和 WS 重连恢复测试。 |
| §12 | Research Lab 质量、配置、任务、事件及未来收益页面 | PARTIAL | UI `refresh/start/stop/openEvent` 和 FC `draw`；Edge 实测生产任务/详情/原始行情；真值、净收益分列，特征细节与拒绝原因展示有限、详情未显示价差，历史 L2 质量仅近一小时、组别重合难辨、收益图仅预测收益。 |
| §12 | models/capacity 路由 | OUT_OF_SCOPE | `routes::deferred` 显式 unavailable；R5/R6。 |
| §13 | T-R01 至 T-R12 / T-R20 | PARTIAL | 下节逐例对应；未触发或未运行场景保持原验收状态。 |
| §14 | 设计/代码/迁移/样例/README、运行记录与 R4 数据准备 | PARTIAL | 上一轮计划及进度有在 repo，本轮本报告重新核实；未满足完整实验合同和长时测试。 |
| §14 | 保留 V1、公开研究、禁止下单和凭据 | PASS | `backend/tests/test_market.py::test_refuse_live_execution` 与 M1/M2 浏览器回归；`depth.collect` 仅公开订阅/心跳，研究引擎路径无 /exchange、签名或下单调用，本轮未调用真实订单。 |

## 2. 验收用例及实际执行

| 用例 | 状态 | 已执行证据与缺口 |
|---|---|---|
| T-R01 | PASS | U 的真实 L2 fixture 原始数值相等、稳定资产 ID、双边 20、finite_depth；主网双边 20。 |
| T-R02 | PARTIAL | M 报价逆序、2000/3000ms、断线，U 迟到和未来保护；SQL 观察到一处真实重连缺口。研究管线尚无跨批重复→既有信号不变的完整用例。 |
| T-R03 | PARTIAL | I 的 fixture 归档 132 行、真实 DuckDB 与热/冷 hash 相等，pin dry-run 不列为清理候选；另有 1,484 行真实数据冷读逐行比对。生产分区发布和实际清理保护未执行。 |
| T-R04 | PARTIAL | U 滞回测试、M1 及独立主网 20 资产负载；未在本轮强压实际磁盘、限流、队列积压或掉线后恢复写入。 |
| T-R05 | PASS | U 75/25→0.5、健康无成交 TFI=null、RVOL=3；M 补 CVD、Persistence。 |
| T-R06 | PASS | M 手算 mid/价差/深度/OBI/log return/MAD/ShockZ 与边界；U 当前窗口不入 RVOL、零成交和 L2 截断。 |
| T-R07 | PARTIAL | U 未来数据拒绝、迟到隔离，S01 checkpoint 恢复结果一致；无完整 Feature checkpoint 路径。 |
| T-R08 | PASS | U 边沿与冷却；I 同 run 重试不重复、同资产方向 episode，真实独立 run 正规化事件重复性一致。 |
| T-R09 | PASS | M/U 双向、路径覆盖、MFE/MAE、报价缺失 null，I 观察到 PENDING 后按周期计算或截尾。 |
| T-R10 | FAIL | I 探针明确复现期后标签有数据却 CENSORED；真实 LIVE、REPLAY 共用路径，检测器上下文无标签。 |
| T-R11 | PARTIAL | U 和 M 核对多空/成交数/价差/未知方向/TFI/RVOL/持续性/冷却；超幅追涨及质量拒绝样本落库不完整。 |
| T-R12 | PASS | M 分组确有独立门槛；真实 BTC/ETH 默认扫描与报告通过，逐组逐周期见 §6。 |
| T-R20 | PASS | 隔离 PostgreSQL、V1 API/实际观察池负载、前端 build/E2E、禁止实盘回归；另外保留本轮 T-R10 的失败。 |
| T-R13–T-R19 | OUT_OF_SCOPE | R4–R6，用例未执行，不计入通过。 |

- 先执行 `$env:PYTHONPATH="$pwd/backend"` 和 `$env:TEST_DATABASE_URL='postgresql://quant:quant@127.0.0.1:55434/quant_test'`。隔离 PostgreSQL 使用 `docker run --rm -d --name hl-quant-research-acceptance -e POSTGRES_USER=quant -e POSTGRES_PASSWORD=quant -e POSTGRES_DB=quant_test -p 127.0.0.1:55434:5432 postgres:16`，不会写生产库。
- 原套件 `.venv/Scripts/python.exe -m pytest -p no:cacheprovider backend/tests -v -ra` 为 **45 PASS、0 FAIL、0 SKIP**。增补后执行 `.venv/Scripts/python.exe -m pytest -p no:cacheprovider backend/tests -ra --junitxml=data/research-acceptance-junit.xml` 为 **55 PASS、1 FAIL、0 SKIP**。通过分布：backtest 15、integration 10、market 11、research 9、research_acceptance 10。逐用例记录在本机 `data/research-acceptance-junit.xml`。唯一失败为 `test_replay_matures_labels_after_detection_end`。业务逻辑与规范期望保留；手算探针初次未像生产路径那样逐秒调用 snapshot，修正测试驱动后期望原样通过。
- `cd frontend; npm run build; npm test`，`vue-tsc --noEmit` 及 Vite 生产构建通过，前端 2 PASS、0 FAIL、0 SKIP；主入口体积告警。后端有 Starlette 别名弃用告警。
- `$env:M1_BROWSER_CHANNEL='msedge'; $env:M2_REPORT_ID='fd441c82-570d-4373-9909-970a55bb7fce'; npm run test:e2e -- --reporter=line`，**3 PASS、0 FAIL、0 SKIP**。逐项为 M1 市场及观察池、M2 策略与成交图、Research 事件/收益/原始回放。浏览器使用本次真实 API 和已持久化运行。
- 合成全链路额外实测 1 PASS、0 FAIL、0 SKIP。使用隔离容器内独立库 `quant_browser` 以原始 fixture 生成事件和标签并启动 8001 FastAPI；前端执行 `$env:RESEARCH_TEST_API='http://127.0.0.1:8001'; npm run test:e2e -- research.spec.ts --reporter=line`，Playwright 访问实际 FastAPI/PostgreSQL，打开同一 fixture 的事件、图和原始归档。该项单列，不重复计为生产浏览器回归通过数。

## 3. 主要缺陷与修复优先级

| 优先级 | 缺陷 | 建议修复及复验 |
|---|---|---|
| P0 | REPLAY 使用 `border-1000` 同时限制检测和取价。就算数据库存有后置 BBO，仍被标 CENSORED。失败用例：开始 tick 触发时 5s、15s 均 PENDING，重启后实际均 CENSORED、规范验收期望均 COMPLETE。 | 分开检测边界和标签水位，冻结数据 as-of；锁定前置预热及后置标签 manifest，在已有报价、缺报、数据尾端、主动取消四种场景区分完整/缺失/截尾。 |
| P1 | 入库 SQL 的 available_ts 早于事务对研究读取者可见的提交时刻。隔离双连接复现实例：按生产 SQL 写入后保留事务，读方在标记时间过去 253.6ms 仍查不到该行，之后回滚。这证明现有字段定义存在误差窗口；尚未测得生产信号影响幅度。 | 持久化发布批次边界及提交后水位，增加并发提交和时钟偏移实验、日志和断点比对。 |
| P1 | 硬编码 S01、历史覆盖预检不足、实验溯源和重启合同不完整。 | 流程实际引入检测器注册，持久化宇宙/代码/数据清单和游标，重启复验后置标签；配置版本升级明确迁移或作废。 |
| P1 | L2 约 12 秒更新而默认 freshness 为 3 秒，连续原始样本不足以支撑每秒可用深度；归档及回收在真实数据上缺证据。 | 先做 24 小时真实归档和固定 UTC 分区读回，按真实更新率审查 S02 特征有效率；保持 freshness 语义，不能用未来插值。 |
| P1 | S01 价格过大、质量缺失等候选遗漏；收益报告缺成本汇总、按条件/市场分箱、独立组置信区间。 | 提前定义可追踪候选抽样、分箱、明确成本分解及无可执行/零样本状态，按预注册规则验收。 |
| P2 | C/D 曲线重合时无法一眼区分，收益图只画预测均值；UI 未实现历史 L2 区间查询和 WS 重连补齐。 | 增加开关、差值/样本和重合提示，以及可对照净收益和数据覆盖；补任务详情错误及进度。 |

## 4. 主网 R0 运行证据

以下是 2026-11-27 约 19:33:58 的 PostgreSQL `market_events` 检查。区间是**行实际可用**时间，三字段检查针对 30 个组合全量样本。表内延时为 exchange→receive 的 P50/P95/P99（毫秒），末列为 receive→available P95，时间均 UTC+8。成交长尾包含订阅时先到的较早成交记录，不能当作实时传输全部变慢。另用 `lag(available_ts)` 测相邻消息最大间距，成交静默与断线分别解释。

| 资产 | 流 | 条数 | 首次–末次可用 | 延迟 p50/p95/p99 | 入库 p95 | 最大间距秒 |
|---|---|---:|---|---|---:|---:|
| BTC | BBO | 49062 | 17:38:10–19:33:58 | 493.5/667.4/822.3 | 354.5 | 4.14 |
| BTC | L2 | 567 | 17:38:10–19:33:50 | 583.7/765.2/1139.5 | 447.1 | 13.17 |
| BTC | Trades | 20652 | 17:38:10–19:33:57 | 525.6/776.8/929.7 | 402.1 | 6.37 |
| ETH | BBO | 53119 | 17:38:10–19:33:58 | 490.9/669.0/829.0 | 365.6 | 4.14 |
| ETH | L2 | 567 | 17:38:10–19:33:50 | 584.0/765.4/1115.5 | 447.7 | 13.17 |
| ETH | Trades | 5910 | 17:38:10–19:33:56 | 519.4/740.8/1104.4 | 395.5 | 14.28 |
| DOGE | BBO | 15052 | 17:38:10–19:33:58 | 501.2/700.7/879.7 | 401.1 | 29.09 |
| DOGE | L2 | 567 | 17:38:10–19:33:50 | 583.8/765.3/1115.3 | 447.7 | 13.17 |
| DOGE | Trades | 874 | 17:38:10–19:33:53 | 518.1/922.8/75004.3 | 385.0 | 88.06 |
| SUI | BBO | 17881 | 17:38:11–19:33:55 | 502.5/695.8/874.1 | 401.2 | 12.47 |
| SUI | L2 | 566 | 17:38:10–19:33:50 | 631.5/787.8/1032.5 | 429.1 | 13.17 |
| SUI | Trades | 920 | 17:38:10–19:33:03 | 507.3/837.4/205596.1 | 408.2 | 86.62 |
| HYPE | BBO | 42601 | 17:38:10–19:33:58 | 497.1/685.6/853.0 | 407.6 | 4.40 |
| HYPE | L2 | 567 | 17:38:10–19:33:50 | 584.0/775.3/1115.8 | 447.9 | 13.17 |
| HYPE | Trades | 22021 | 17:38:10–19:33:57 | 539.8/733.8/1005.7 | 406.9 | 5.34 |
| LINK | BBO | 14176 | 17:38:11–19:33:56 | 504.3/694.6/868.6 | 401.5 | 17.53 |
| LINK | L2 | 567 | 17:38:10–19:33:50 | 584.1/775.3/1032.1 | 448.0 | 13.17 |
| LINK | Trades | 1413 | 17:38:10–19:33:14 | 515.3/777.9/241257.5 | 383.8 | 118.41 |
| XRP | BBO | 26557 | 17:38:14–19:33:56 | 497.6/687.1/853.7 | 402.8 | 10.24 |
| XRP | L2 | 566 | 17:38:10–19:33:50 | 639.9/795.9/1032.6 | 409.7 | 13.17 |
| XRP | Trades | 2005 | 17:38:10–19:33:52 | 492.0/795.2/154565.8 | 446.0 | 85.69 |
| SOL | BBO | 36990 | 17:38:10–19:33:58 | 497.0/681.8/849.2 | 395.7 | 4.40 |
| SOL | L2 | 566 | 17:38:10–19:33:50 | 584.8/776.1/1003.4 | 444.0 | 13.17 |
| SOL | Trades | 5722 | 17:38:10–19:33:56 | 514.6/815.3/1069.7 | 430.6 | 17.21 |
| AVAX | BBO | 15966 | 17:38:13–19:33:57 | 493.7/690.1/869.6 | 403.7 | 15.85 |
| AVAX | L2 | 567 | 17:38:10–19:33:50 | 582.3/765.2/1139.4 | 431.5 | 13.17 |
| AVAX | Trades | 638 | 17:38:10–19:33:56 | 501.9/1001.6/368014.4 | 396.1 | 98.82 |
| ADA | BBO | 7720 | 17:38:10–19:33:58 | 508.1/709.4/887.9 | 395.3 | 26.09 |
| ADA | L2 | 567 | 17:38:10–19:33:50 | 581.2/765.0/1133.6 | 418.0 | 13.17 |
| ADA | Trades | 815 | 17:38:10–19:33:52 | 517.7/905.1/234229.2 | 394.6 | 90.08 |

- 缺口表：上述每个资产/流均有同一断线 `18:21:22.795395–18:21:27.908482`（5.113 秒），共 30 行。最大相邻消息间隔受流动性及上游发送节奏影响。BTC L2 实测约 0.082 Hz，双边每侧 20 档。研究 lease 当时 30 订阅，实收 308,454，593 迟到、275 乱序、1 次重连，状态中的 duplicates=0 **不是跨批无重复的证明**，该字段每次由临时 state 副本写入。
- 磁盘：`pg_database_size` 约 1324 MB；`pg_total_relation_size(market_events)` 约 702 MB，包含 V1；`sum(pg_column_size(m))` 研究行约 154,233,294 B，排除索引/WAL/备份。系统稍后报告数据库 1,396,505,623 B、配额剩余 9,340,912,617 B、容器文件系统空余 1,004,048,986,112 B，宿主机探针缺失。生产研究 Parquet manifest 0。
- 实际负载：`.venv/Scripts/python.exe scripts/m1_load.py --seconds 60`，20 资产、59 样本、66 订阅、24 引用、无重连、WS 指标最大 438ms、状态 API 中位 16ms/最大 140ms；脚本清理临时观察池。独立公开 WS 订阅同类三流，20 资产、60 个确认、接收 BBO 5,052 / 成交 1,416 / L2 120，每资产 6 L2（各 20×20），实测 BBO 延迟 399.20/579.39/1070.58ms，L2 473.89/2420.02/2421.56ms，Trades 508.35/283065.58/576767.58ms（含初始成交批）。后者只计接收，持续 20 资产研究库写入仍待验。
- 取证命令：`docker compose ps`、`docker compose exec -T db psql -X -U quant -d quant`；按 `instrument_id,event_type` 聚合 `market_events` 的 min/max/count、`percentile_cont`、异常时间序计数，对 `data_gaps` 以 `data_type LIKE 'research.%'` 汇总。负载额外使用 `websockets.asyncio.client.connect('wss://api.hyperliquid.xyz/ws')`，逐资产订阅 `bbo,trades,l2Book`。运行查询入口 `/api/v1/research/quality` 与 `/api/v1/system/status`。
- 真实归档探针：只读复制 19:22:30–19:24:30 的 BTC 样本至隔离库。生产 CLI `archive_day` 按设计拒绝归档当天 UTC 分区。用隔离库中的 `archive_partition` 直接测试该分区的底层写入，生成 BBO 875 行/55,550 B、Trades 599 行/45,769 B、L2 10 行/7,717 B，三份自动 pin。经 `ArchivedFeed.read/merge` 与热库比较全部 1,484 行，原始 JSON、质量、连接、三个时间戳和序号完全一致。另把一条真实成交经 `feed.persist` 跨提交重复写入两次，原始行不变，每次批计数均为 1。

## 5. 手算与实际值

执行 `.venv/Scripts/python.exe -m pytest -p no:cacheprovider backend/tests/test_research_acceptance.py -sv`。绝对误差以测试输出为准；合法的 null 不以零参与。以下输入是确定性 fixture，不代表主网信号收益。

| 输入和计算 | 手算期望 | 实际 | 绝对误差 | 状态 |
|---|---:|---:|---:|---|
| 一秒主动买 75、卖 25，TFI | 0.5 | 0.5 | 0 | PASS |
| 最近 12 桶，每桶 100，9 买 3 卖，TFI/CVD | 0.5 / 600 | 0.5 / 600 | 0 / 0 | PASS |
| 同 12 桶，多/空 Persistence | 0.75 / 0.25 | 0.75 / 0.25 | 0 / 0 | PASS |
| 前 2 个非重叠 30s 桶每秒 100、当前每秒 300，RVOL | 3 | 3 | 0 | PASS |
| 过去 60s 成交基准为零，RVOL | null | null | — | PASS |
| 健康空窗、迟到 3000ms、下一 tick 未来消息 | TFI null / 隔离 / 拒绝 | 同期望 | — | PASS |
| 报价 106.39623447±.01，Mid | 106.39623447280337 | 106.39623447280337 | 0 | PASS |
| 同上 Spread bps | 1.8797657735821778 | 1.8797657735831175 | 9.40e−13 | PASS |
| Mid±1bps，数量买2卖1，5bps Depth bid/ask | 212.771189699 / 106.406874096 | 相同 | 0 / 0 | PASS |
| 同上 OBI（外档覆盖指定范围） | .3332444414813827 | .33324444148138277 | 5.55e−17 | PASS |
| 仅 99.99/100.01 单档，5bps 可见下界 | 买 199.98，OBI null 并截断 | 相同 | <1e−10 | PASS |
| 15s 端点相差 log .002 | .002 | .002000000000000036 | 3.60e−17 | PASS |
| 120 个交替 0/.001 的窗，当前 .002 排除，1.4826×MAD | .0007413 | .0007412999999997029 | 2.97e−16 | PASS |
| 同窗 ShockZ=(.002−.0005)/.0007413 | 2.023472278429786 | 2.0234722784309156 | 1.13e−12 | PASS |
| 60s 预热，边界第 59/60 秒，已闭合左端点排除 | false/true；900 买、300 卖 | 相同 | 0 | PASS |
| 较旧 exchange 时间 BBO 后到，age 2000/3000ms，断线 | 保留 old mid=100，边界有效/无效，断线成交额 null | 相同 | 0 | PASS |
| 100→95→105，LONG/SHORT return | ±.04879016416943204 | 相同 | 0 | PASS |
| 同路径，LONG/SHORT MFE | .0487901642 / .0512932944 | 相同 | 0 | PASS |
| 同路径，LONG/SHORT MAE | −.0512932944 / −.0487901642 | 相同 | 0 | PASS |
| 2 秒有 2/1 个路径点 | coverage=1/.5；后一种 MFE null | 相同 | 0 | PASS |
| LONG 101入/104出，1单位，滑点1bps、费5bps | 101.0101/103.9896；手续费 .10249985；净额 2.87700015 | 相同 | 0 | PASS |
| Q=102，目标 bid 可见 1；目标 BBO 缺失 | insufficient_depth；MISSING | 相同 | — | PASS |
| 相同任务重扫，t+5 和 t+15 路径报价在已批准输入内，检测区间只有 5s | COMPLETE / COMPLETE | CENSORED / CENSORED | 状态不符 | FAIL |

未知成交额占比 50/1000 允许、50.001/1000 给出 `unknown_side`，U/M 已执行。S01 低于阈值 .499/.299、1.999、.749、成交 19、价差 8.001 的期望/实际组与拒绝理由逐一一致。独立探针用 r60=.008001 且其他指标都达到门槛，实际返回空事件列表，证实该追涨样本未留下拒绝记录。尚缺 Impact/OI/市场残差及 60min MAD 的独立数学覆盖、真实恢复中的完整特征 checkpoint、所有无效 feature coverage 语义的验证。

隔离的 `ResearchEngine.on_tick` 差分探针中，两份输入到触发后第 5 秒完全相同，之后 mid 分别为 101 与 99。四个事件 ID 和完整 feature 快照保持相同，15 秒收益从 +0.007952328190495015 变为 −0.01204833851617448；这通过了未来价格扰动测试。

## 6. 主网研究任务、收益和页面取证

回放通过 `POST /api/v1/research/configs` 保存参数，`POST /api/v1/research/runs` 运行，按 `GET /runs/{id}` 轮询，取 `/events` 和 `/forward-returns?run_id=...`，同时用生产库直接核对事件和标签记录。真实输入来自 `market_events`，没有 K 线合成高频行情。全部时间为 UTC+8。

| 任务 | 决策区间 | 结果 | 实际覆盖/事件 |
|---|---|---|---|
| BTC 两小时默认 `e6d070e8-a40d-4fc5-b818-cbeea4f4b480` | 17:32:30–19:32:30 | BLOCKED：`INSUFFICIENT_DATA` | 3,508/7,200 有效（48.72%）。首个可用消息 17:38:10，默认约 61 分钟前置基准。任务仍保存 76 确认、164 拒绝的局部样本，当前 API 允许读取其聚合；这些数据不能作为完整实验结论。 |
| BTC 两小时短预热 `f6956509-3edf-4ef0-ae79-71d3e8db83af` | 17:41:00–19:41:00 | COMPLETE | 60 秒预热、60 秒 RVOL 基准，仍用默认八个 horizon 与默认 S01 检测门槛；6,991/7,200 有效（97.10%），144 确认、354 拒绝。输入 hash `9dbc4322fb4b7b6c9104541cee7b2bd365931d9fadd0eba1e31dabd87466964c`。用于检验两小时链路，默认预热任务仍记 BLOCKED。 |
| BTC+ETH 默认 `badedee7-924c-4320-9193-8d335bd076a0` | 19:22:30–19:32:30 | COMPLETE | BTC 600/600，ETH 596/600，总 1,196/1,200；19 确认、66 拒绝。输入 hash `78b381fda27fe4dc011ed70980e787cedebf1674b05164319f5d4a97ff7f73e9`。 |
| BTC 默认 `6997be18-98d1-42b0-8e51-e98f6938773f` 与重复 `d91d17a0-52f7-4baf-b2ac-bad73271651b` | 19:22:30–19:32:30 | COMPLETE×2 | 600/600，14 确认、24 拒绝；38 条事件的资产/组/方向/tick/episode/状态/原因、全部聚合和统计正好相同。独立 run 的事件 ID 因含 run ID 而不同，输入 hash 均为 `b03891622e0afae28824e277355cd549594b9de7d98d732f7458a9382e688abb`。 |
| BTC 宽松 `35143221-a30f-42eb-9ba0-7f943935ac15` | 同上 | COMPLETE | 600/600，15 确认、20 拒绝，A/B/C/D 为 6/3/3/3、输入 hash 与默认相同。 |
| BTC 严格 `21760cef-c00f-4b7e-9c3c-d6d9fbdb7640` | 同上 | COMPLETE | 600/600，12 确认、9 拒绝，A/B/C/D 均 3、输入 hash 与默认相同。 |
| BTC LIVE `062222f0-fd5b-485f-bfc4-c7a55a1af5f6` | 19:35:42–19:36:12 | COMPLETE | 30/30；确认 2（A/B）、拒绝 7，5 秒及 15 秒 4 项 COMPLETE，均有执行估计。 |
| LIVE 幂等停止 `6388db72-cdc2-4688-89d6-3fc4fbae82b4` | 本轮公共实时源 | STOPPED | 对同一任务两次 POST stop 均 HTTP 200，最终仍可查询历史状态。 |

宽松阈值 `tfi15=.3,tfi60=.2,rvol30=1.5,persistence=.6,return_min=.0001,spread_bps=15,trades30=10`；严格为 `.7,.5,3,.9,.001,3,50`，其余使用默认值。BTC 5 秒各组平均方向收益 bps：宽松 A=1.473，B/C/D=2.621；默认 A=.940，B/C/D=1.567；严格 A/B/C=.648，D=.972。真实样本对参数有反应，样本短且相互相关，统计区间不足。

短预热两小时任务在数据库验证 1,152 项标签，其中 1,098 COMPLETE、2 MISSING、52 CENSORED，1,050 有可执行 BBO 估计。A/B/C/D 确认为 58/45/23/18，其中 COMPLETE 分别为 438/345/176/139，MISSING 为 0/0/1/1，CENSORED 为 26/15/7/4。

以下为 BTC+ETH 默认结果，展示各组×8 周期。单元格为 **有效/尝试；毛/净 bps**，毛为方向 log return，净为可执行样本的 BBO 净收益平均，两个均值分母可能不同。各组各周期 MISSING、PENDING 均 0，未完成数为 CENSORED。A/30s 的 7 个有效预测中 1 个因深度不足执行失败，其余表格的有效预测均有执行估计。当前聚合未单独返回平均手续费、滑点、价差分解。

| 秒 | A | B | C | D |
|---:|---|---|---|---|
| 5 | 7/7；1.168/−11.113 | 4/4；1.320/−10.945 | 4/4；1.320/−10.945 | 4/4；1.176/−11.090 |
| 15 | 7/7；2.335/−9.947 | 4/4；4.075/−8.191 | 4/4；4.075/−8.191 | 4/4；4.075/−8.191 |
| 30 | 7/7；4.616/−7.234 | 4/4；8.351/−3.916 | 4/4；8.351/−3.916 | 4/4；8.206/−4.061 |
| 60 | 7/7；4.440/−7.843 | 4/4；8.431/−3.836 | 4/4；8.431/−3.836 | 4/4；7.925/−4.559 |
| 180 | 5/7；−4.707/−16.952 | 2/4；−1.703/−13.863 | 2/4；−1.703/−13.863 | 2/4；−1.703/−13.863 |
| 300 | 5/7；.333/−11.914 | 2/4；4.621/−7.542 | 2/4；4.621/−7.542 | 2/4；4.621/−7.542 |
| 900 | 0/7；— | 0/4；— | 0/4；— | 0/4；— |
| 1800 | 0/7；— | 0/4；— | 0/4；— | 0/4；— |

### 60 秒任务与 C 组

- 通过数据库和 Edge 实际打开 `8743ec55-e9e0-4ba6-91be-a68961069606`，确认原配置只保存 `horizons_s=[5,15]`。页面按已持久化的标签周期画图，60 秒任务时长本身不会把周期配置改成两个。默认八周期任务实际显示八行/组；目前长周期可能被后置标签缺陷标为期外。
- 该短任务 C 的两行均各有一个 COMPLETE 且 BBO 可执行样本。浏览器确实显示两行，预测均为约 −0.037%、净 −0.159%。C 与 D 在两点的原始均值都精确为 `−0.00037290444350543194`，`ForwardCurve.draw` 分别生成系列，曲线坐标完全重合，可被后绘制的 D 遮住。浏览器定向探针初次经 PowerShell 管道传中文选择器变成问号而超时，转义后实际通过。C 独立执行由低 RVOL 合成用例证实，严格真实任务 C 与 D 平均值也不同。
- 页面可见 RESEARCH MODE、参数版本、创建任务、游标、有效资产秒、标签状态、原始行情折叠、K 线区域和匹配不足说明。事件详情价差、完整特征质量、历史 L2 日期区间以及分组净收益曲线仍缺；停止操作的幂等性由 API 实测，通用 Edge 回归覆盖真实详情和曲线画布。

## 7. R4 数据准备及进入条件

当前 BTC、ETH、SOL、HYPE 等已有连续采集过程与有限档样本，可用于整理 S02 数据质量基线。观测到 12 秒左右的 L2 更新间隔、13.17 秒最大相邻间距及一次真实断线。选取 BTC 19:22:31–19:32:29 的 599 个秒点，以 as-of 可用顺序和 3 秒 exchange age 过滤，仅 **106/599（17.70%）** 有新鲜快照，且此为未叠加坏盘口过滤的上界。冲击前深度覆盖、固定价格范围覆盖、时间因子训练 24h 和相邻事件独立性都需要单独测量。生产尚无已发布的研究 Parquet manifest，现有原始数据仍在热库。建议按 §3 优先修复标签跨期与数据可用水位，完成 24 小时连续取证和真实分区冷热校验，再确认 R4 的有效深度覆盖门槛。完整两小时默认实验需要至少约三小时连续输入（再加最长 1,800 秒标签数据），并按实际覆盖做预检。



