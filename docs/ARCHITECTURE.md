# RiverMind 架构 v0.6

## 设计原则

1. 牌谱原文不可直接进入 LLM；先解析、验证和脱敏。
2. 统计、GTO 和解释共享同一套规范化手牌与节点标识。
3. 确定性计算负责金额、行动、统计和 EV；LLM 只负责表达与课程组织。
4. 解析失败时显式报错，不用启发式猜测污染用户数据库。
5. Beta 优先离线复盘，不构建实时牌桌辅助链路。

## 数据流

```mermaid
flowchart LR
    HH["牌谱文件"] --> DET["格式检测"]
    DET --> PAR["站点解析器"]
    PAR --> VAL["领域校验"]
    VAL --> NORM["规范化 HandHistory"]
    NORM --> STORE["分析存储"]
    STORE --> STAT["确定性统计引擎"]
    STAT --> LEAK["漏洞规则/模型"]
    LEAK --> EVID["解释证据包"]
    EVID --> CRT["教练运行时/资源门"]
    CRT --> LLM["显式授权的外部模型适配器"]
    LLM --> CVAL["候选忠实性校验"]
    CVAL --> COACH["解释或模板回退"]
    NORM --> NODE["决策前 GameSpec"]
    NODE --> MATCH["GTO Matcher"]
    CAT["严格版本化 Solution Catalog"] --> MATCH
    MATCH -->|"仅 exact 命中"| ART["Strategy Artifact 严格 loader"]
    ART --> SEV["只读 StrategyEvidence"]
    RPT["Solve Quality Report"] --> GATE["独立质量门（签署）"]
    ART --> GATE
    GATE -->|"授予 verified"| SEV
    SEV -->|"仅 usable_for_teaching；后续"| EVID
```

## 第一阶段模块

| 模块 | 责任 | 当前状态 |
|---|---|---|
| Canonical Model | 玩家、筹码、行动、街道、牌面、现金/赛事元数据与结算 | 已建立 v0.3 |
| Parser Registry | 识别来源并路由到站点解析器 | 已建立 v0.1 |
| PokerStars Parser | 英文现金桌、付费 MTT、Freeroll 文本解析 | Cash v4 / MTT v2 |
| Import Pipeline | 文件扫描、拆手、去重、失败隔离 | v0.1 已实现 |
| Import Store | 导入审计、规范化 JSON、原文回放 | SQLite v0.1 已实现 |
| Analytics Store | 玩家–手牌统计宽表与维度索引 | SQLite v0.2，列式方案待百万手基准 |
| Stats Engine | 固定核心指标和机会分母 | 9 项翻前/Flop 指标 v0.2 |
| Accounting | 逐动作投入、返还、收池、净结果与守恒校验 | v0.1 已实现 |
| Reports | Session、相关手牌分页、结构化回放 | API/CLI v0.1 |
| Analysis Page | Leak Cards、AI 教练、核心统计、Session、最近手牌 | 本地 HTML v0.3 |
| Leak Engine | 版本化规则、样本门槛、Wilson 区间与证据手牌 | v0.1 已实现 |
| AI Coach | 脱敏证据、中文模板、候选输出校验与回退 | v0.3 已实现；默认使用模板 |
| Coach Runtime | 异步 Provider、超时重试、资源预算与无正文审计 | v0.2 已实现 |
| OpenAI Adapter | Responses API、严格 JSON Schema、拒绝/不完整处理 | v0.1 默认关闭；尚未真实调用 |
| Coach Eval Gate | 候选 schema、证据、数值、隐私和行动攻击面回归 | 50 例全部通过；真实专家质量集待收集 |
| Expert Review Gate | 盲化解释、双专家评分、不同证据数和 fatal error 门槛 | 工作流已实现；真实评分待收集 |
| GTO Matcher | 真实决策节点提取；精确/阈值近似/不支持；差异说明 | 元数据 v0.1 已实现 |
| Catalog Index | 指纹与硬维度预计算；匹配降为字典查找，结论逐字节不变 | v0.1 已实现 |
| Board Isomorphism | 花色重标号 + 翻牌顺序等价；默认关闭，命中携带置换 | v0.1 已实现 |
| Strategy Artifact Loader | 目录沙箱定位、SHA-256、身份、动作/组合/概率/EV/来源校验 | v0.1 已实现；仓库仅有 test_only 切片 |
| Strategy Query | 已验证制品的组合事实与版本化加权汇总 | v0.1 已实现；`usable_for_teaching` 恒为 false |
| Solve Quality Report | 来源、许可、求解配置、收敛证据、评估范围与显式限制 | v0.1 已实现；仓库无报告 |
| Quality Gate | 双人签署、字节钉死、收敛上限、claim_class 与 rake 范围 | v0.1 已实现；仓库无签署 |
| TexasSolver Import | 节点定位、组合重规范化、概率重归一、范围权重 | v0.1 已实现；等待首份真实 dump |

## GTO 元数据边界

`GameSpec` 不包含牌谱 ID 或玩家名，指纹由规范化 JSON 计算。`SolutionSpec` 只登记求解器、动作树、质量标签和制品哈希；Matcher 命中不等于策略内容已被加载。现金局缺 rake 结构、ICM/PKO 缺赛事上下文、硬维度不一致、阈值越界或最近候选并列时均失败关闭。完整契约与阈值见 [GTO_MATCHER.md](GTO_MATCHER.md)。

## 策略制品边界

`SolutionSpec` 命中只是元数据引用。策略内容必须再通过 `strategy-artifact/1.0.0` 的严格 loader：制品在 catalog 目录沙箱内定位（拒绝 `..`、绝对路径、符号链接和越界解析），按文件字节校验 SHA-256，并与目录条目逐项比对 `solution_id`、`GameSpec` 指纹、动作树版本、求解器身份和质量标签。内容层再校验 action 定义、1,326 粒度私牌组合、公共牌冲突、概率区间与合计容差、EV 单位与语义，以及完整 provenance。

loader **不会**升级质量标签：目录说 `test_only`，制品也必须说 `test_only`。`gto-query` 只在 exact 命中且制品完整验证后返回频率与 EV；approximate、unsupported、制品未覆盖该组合或验证失败时一律不返回策略内容。完整契约见 [STRATEGY_ARTIFACTS.md](STRATEGY_ARTIFACTS.md)。

## 质量授予边界

`verified` 由一条与 loader 完全分离的路径授予：一份 `solve-quality-report/1.0.0`（来源、许可、求解配置、收敛证据、结构化 rake 模型、显式限制）加一份 `quality-attestation/1.0.0`（按字节钉死制品与报告，至少两名签署人，至少一名独立复核）。门另外强制绝对收敛上限、完整时间顺序，以及 `claim_class` 与牌局结构相符——多人局和 ICM/PKO 不能冒用两人零和的 exploitability 口径。

`usable_for_teaching` 需要标签与签署同时成立。手工编辑的目录和制品可以互相同意 `verified`，但没有签署就教不了人。完整规则见 [SOLVE_QUALITY_GATE.md](SOLVE_QUALITY_GATE.md)。

## 当前存储边界

SQLite 承担 Beta 的本地事务系统职责：导入批次、逐手状态、规范化载荷、原文、去重索引、`player_hand_stats` 指标宽表和 `player_hand_results` 结算表。派生表与原手牌在同一事务写入；v1/v2 数据库首次打开时分别自动回填指标与结果。后续依据 100k/1M 手牌基准决定是否把同一逻辑宽表迁移到 DuckDB/Parquet。

## 长期边界

策略服务输出 `PolicyDecision`，解释服务读取 `ExplanationEvidence`。解释服务永远不能生成被游戏引擎直接执行的动作字段。这条边界从数据模型和 API 权限两层实施。
