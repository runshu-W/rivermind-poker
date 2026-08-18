# RiverMind Poker — Claude 工程交接文稿

> 交接日期：2026-08-18
> GitHub：https://github.com/runshu-W/rivermind-poker
> 默认分支：`main`
> 功能基线：`577f6f2 feat: add versioned GTO node matcher`
> 运行环境：Windows PowerShell、Python 3.11+

## 1. 接手时先做什么

不要从零重构。先执行：

```powershell
git status --short
git log -5 --oneline
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python benchmarks/import_benchmark.py --hands 10000
```

交接时的预期基线：

- 81 项自动化测试全部通过；
- 10,000 手牌导入约 3.8 秒，机器差异允许结果浮动；
- 1,000 个解法元数据线性匹配约 29 ms；
- 工作树应为空；
- GitHub `main` 应与本地 HEAD 一致。

如果基线不一致，先查清本地改动、Python 版本和远端 HEAD，不要直接 reset 或覆盖用户文件。

建议依次阅读：

1. `README.md`
2. `PROJECT_PLAN.md`
3. `docs/ARCHITECTURE.md`
4. `docs/GTO_MATCHER.md`
5. `docs/AI_COACH.md`
6. 本交接文稿

## 2. 产品定位，不要改变

RiverMind 的定位是：

> 以 H2N-lite 牌谱分析作为最快入口，以 GTO 策略系统作为长期壁垒，以 AI 教练作为解释与学习体验层。

它不是纯 H2N，也不是只做单挑求解器。长期覆盖方向包括 6-max Cash、MTT、ICM/PKO、Spin/HU SNG 和常见多人场景，产品闭环参考 GTO Wizard 的 `Analyze → Study → Practice → 复测`。

核心架构原则：

- 真正的扑克策略、频率和 EV 必须来自专用求解器、策略制品或策略/价值网络；
- LLM 只负责解释、教学表达、课程组织和受约束的对手建模；
- LLM 不计算确定性统计，不直接决定 `fold/call/raise`，也不能补齐缺失 GTO 数值；
- 没有可靠解法时显示“近似”“不支持”或“待求解”，不能伪造答案；
- 产品用于离线训练、复盘和研究，不开发自动点击、读屏、牌桌注入或隐蔽实时辅助。

## 3. 当前已经完成的能力

### 3.1 手牌领域模型与解析

主要文件：

- `src/rivermind_core/models.py`
- `src/rivermind_core/parsers/`
- `src/rivermind_core/serialization.py`

现状：

- 规范化 `HandHistory`、玩家、位置、行动、街道、公共牌和结算字段；
- 支持 PokerStars 英文现金桌与 MTT 文本；
- MTT 支持付费赛、Freeroll、ante 和赛事级别；
- 行动序号连续，金额使用 `Decimal`；
- 支持 2–10 人位置规范化，GTO `GameSpec` 当前明确限制为 2–9 人；
- 黄金 fixture 位于 `tests/fixtures/`。

不要宣称已经兼容第二个平台或所有 PokerStars 变体。当前“现金 + MTT”是两类赛制，不是两个站点格式。

### 3.2 导入与本地存储

主要文件：

- `src/rivermind_core/importer.py`
- `src/rivermind_core/storage.py`

现状：

- 文件/目录导入、拆手、来源检测、逐手错误隔离；
- 稳定指纹去重；
- 成功、重复、失败、不支持四类导入结果；
- SQLite schema v3；
- 保存导入批次、逐手状态、规范化 JSON、原文、统计宽表和玩家结算；
- v1/v2 数据库打开时回填派生表。

尚未完成：用户数据导出、删除、备份恢复演练、云端账户和百万手性能门。

### 3.3 确定性统计、结算与报告

主要文件：

- `src/rivermind_core/stats.py`
- `src/rivermind_core/accounting.py`
- `src/rivermind_core/sessions.py`
- `src/rivermind_core/reports.py`
- `src/rivermind_core/replay.py`
- `src/rivermind_core/html_report.py`

现状：

- 9 项指标：VPIP、PFR、RFI、3Bet、Call Open、Cold Call、Fold to 3Bet、Flop CBet、Fold to Flop CBet；
- 每项机会分母由确定性代码计算；
- 支持赛制、位置和有效筹码过滤；
- 逐动作投入、返还、收池、运行底池和守恒校验；
- Cash 输出货币盈亏，MTT 明确输出筹码变化；
- Session 聚合、相关手牌分页和结构化回放；
- 可生成本地静态 HTML 分析页。

尚未完成：All-in EV、标签、收藏、更完整街道报告、可交互 Web 前端。

### 3.4 Leak Engine

主要文件：

- `src/rivermind_core/leaks.py`
- `docs/LEAK_ENGINE.md`

现状：

- 6 条版本化、确定性的复盘规则；
- 样本门槛和 95% Wilson 区间；
- 输出 detected、clear、insufficient sample；
- Leak Card 带统计口径、严重度、复盘问题和证据手牌。

这些阈值是“值得复盘的信号”，不是 GTO 频率或 EV 结论。

### 3.5 AI Coach

主要文件：

- `src/rivermind_core/coach.py`
- `src/rivermind_core/coach_prompt.py`
- `src/rivermind_core/coach_runtime.py`
- `src/rivermind_core/coach_evals.py`
- `src/rivermind_core/coach_review.py`
- `src/rivermind_core/openai_provider.py`
- `evals/coach_candidate_cases.json`

现状：

- 脱敏、稳定的 `ExplanationEvidence`；
- 默认使用确定性中文模板；
- 候选模型输出必须使用事实占位符，经过 schema、数值、隐私和未来牌校验；
- 校验失败自动回退模板；
- 50 例合同/对抗语料全部通过；
- Provider-neutral 异步运行时有超时、重试、输入/输出限制和费用预算；
- OpenAI Responses 适配器默认关闭，必须显式授权；
- API key 只从环境变量读取；
- 有双专家盲审工作流和质量门。

重要事实：

- 没有进行真实 OpenAI 网络调用；测试只使用假传输；
- 没有收集真实专家评分；
- 默认产品路径仍是确定性模板；
- 不得把“盲审工作流已实现”写成“专家质量已经验证”。

### 3.6 GTO Matcher v0.1

主要文件：

- `src/rivermind_core/gto_specs.py`
- `src/rivermind_core/gto_matcher.py`
- `solutions/catalog.json`
- `docs/GTO_MATCHER.md`
- `tests/test_gto_matcher.py`

已冻结的版本：

- `game-spec/1.0.0`
- `solution-spec/1.0.0`
- `solution-catalog/1.0.0`
- `gto-match-policy/1.0.0`

`GameSpec` 表达：

- Cash/MTT、2–9 人；
- 盲注、逐位置 ante、剩余筹码；
- ChipEV/ICM/PKO；
- Cash rake model、百分比和 cap；
- ICM/PKO 的赛事上下文 ID；
- 街道、可见牌面、轮到的位置、活跃位置；
- 完整规范动作线和底池 BB。

节点提取由 `--before-action N` 定位“行动执行前”的状态。目标动作被排除，玩家名和牌谱 ID 不进入 `GameSpec` 指纹。

Matcher 返回：

- `exact`：规范化指纹完全相同且唯一；
- `approximate`：所有硬维度相同，数值差异在公开阈值内且最近候选唯一；
- `unsupported`：目录空、rake 缺失、硬维度不同、阈值越界或候选并列。

当前近似阈值：

- 每个位置筹码差不超过 5 BB；
- 底池差不超过 1 BB；
- 小盲差不超过 0.1 BB，大盲必须相同；
- 每个位置 ante 差不超过 0.1 BB；
- rake 百分比差不超过 0.5 个百分点；
- rake cap 差不超过 0.5 BB。

现金牌谱不能从本手实际 rake 推导房间抽水结构，因此调用方不提供 rake model 时失败关闭。ICM/PKO 没有赛事上下文 ID 时也失败。

当前默认 `solutions/catalog.json` 故意为空。仓库没有真实 GTO 频率、EV、范围或已验证策略制品。测试中的 solution 全部标记为 `test_only`。

实际演示节点：

- fixture：`tests/fixtures/pokerstars_cash.txt`
- 决策：`before_action=5`
- 场景：BTN 3 BB open，BB call；翻牌 `2c 7d Ts`，BB check，轮到 BTN；底池 6 BB，双方剩余 97 BB；
- 当前节点指纹：`0f0d9b6eb3eec4999cac9f4bbdde0eab2bb5762b2fa834f8353aba1566a74c74`。

这个案例只证明牌谱到解法元数据的映射链路，不证明任何下注频率或 EV。

## 4. 当前 CLI

入口：

```powershell
$env:PYTHONPATH = "src"
python -m rivermind_core --help
```

已有命令：

- `import`
- `stats`
- `leaks`
- `coach`
- `coach-eval`
- `coach-openai`
- `coach-review-score`
- `sessions`
- `hands`
- `replay`
- `gto-match`
- `report`

典型本地流程：

```powershell
python -m rivermind_core import tests/fixtures --database data/dev.db
python -m rivermind_core stats --database data/dev.db
python -m rivermind_core leaks --database data/dev.db --json
python -m rivermind_core coach --database data/dev.db --json
python -m rivermind_core replay pokerstars 100000000001 `
  --database data/dev.db --json
python -m rivermind_core gto-match pokerstars 100000000001 `
  --before-action 5 `
  --database data/dev.db `
  --catalog solutions/catalog.json `
  --rake-model pokerstars.cash.example `
  --rake-percent 5 `
  --rake-cap-bb 3 `
  --json
```

默认空目录返回 `unsupported/catalog_empty` 是正确行为。

## 5. 仓库结构和工程约定

```text
src/rivermind_core/   Python 领域核心、CLI、存储和服务边界
tests/                unittest、黄金牌谱、合同与回归测试
evals/                AI Coach 50 例离线评测语料
benchmarks/           可重复的导入/查询/匹配基准
solutions/            解法目录；当前只有空目录元数据
docs/                 产品、架构、协议和限制
PROJECT_PLAN.md       长期计划；包含未完成的愿景项
README.md             当前可用能力和快速开始
```

工程现状：

- Python 3.11+；
- 运行时无第三方依赖；
- 测试框架是标准库 `unittest`；
- 金额和关键协议数值使用 `Decimal`；
- dataclass 多数使用 `frozen=True, slots=True`；
- schema 版本是协议的一部分，不要静默改变旧版本语义；
- 当前没有正式 CI workflow，只有 `docs/ci.example.yml`；
- `pyproject.toml` 包版本仍为 `0.1.0`。

每次修改至少执行：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall -q src benchmarks tests
git diff --check
```

涉及导入、存储、统计或 Matcher 性能时，再执行：

```powershell
python benchmarks/import_benchmark.py --hands 10000
```

用户要求所有完成的更新提交并推送到 GitHub。提交前确认没有把 `.env`、API key、私有牌谱或 `.db` 文件加入仓库。

## 6. 最重要的真实性与安全边界

以下规则优先级高于“尽快展示效果”：

1. 不得把测试 fixture、随机频率或手写频率标记成 `verified` GTO。
2. 不得让 LLM 生成策略频率、EV、范围或可执行行动，再包装成求解器结果。
3. `SolutionSpec` 命中只表示元数据引用可用，不代表策略制品已经加载或验证。
4. 真实策略制品必须有来源、求解配置、动作树版本、完整性哈希和质量标签。
5. 多人局不要宣称存在与 HU 两人零和相同意义的唯一答案；必须显示方法和经验质量边界。
6. ICM/PKO 不能只用单桌一手牌推导；必须有完整赛事上下文。
7. Cash rake 不能用本手最终抽水倒推规则。
8. 外部 LLM 调用必须显式授权，牌谱原文不能直接发送。
9. 不开发或暗示实时牌桌辅助、自动操作、读屏或规避平台检测。
10. 没有证据时失败关闭，不静默猜测。

## 7. 当前已知缺口

### 产品层

- 没有 Web 前端、账号、登录、订阅或云同步；
- 本地 HTML 只是分析页，不是完整产品 UI；
- 没有标签、收藏、删除、导出和授权管理；
- 没有真实种子用户数据或付费验证。

### H2N-lite 层

- 只有 PokerStars 英文牌谱；
- fixture 覆盖仍小；
- 缺 All-in EV 和大量常见 H2N 报告；
- 百万手性能与迁移策略未验证；
- 没有正式 CI、发布、备份和可观测性。

### GTO 层

- 没有求解器；
- 没有真实策略制品；
- 没有策略 artifact schema/loader/validator；
- 没有概率、EV、范围、动作树内容校验；
- Matcher 当前线性扫描目录；
- 公共牌必须逐张相同，尚未做 suit isomorphism；
- 动作线必须完全相同，尚未做经过验证的 bet-size translation；
- 没有解法浏览器、矩阵、节点导航、训练题或 EV loss；
- Leak Card 尚未自动定位“哪一个决策最值得送入 GTO Matcher”。

### AI 层

- 没有真实外部模型调用结果；
- 没有真实专家盲审评分；
- 当前证据不包含经过验证的 GTO 策略事实；
- 对手建模尚未实现。

## 8. 建议下一阶段：Strategy Artifact v0.1

下一步不要直接做漂亮的策略矩阵 UI。先让 `SolutionSpec.artifact_id` 和 `artifact_sha256` 对应一个真正可加载、可验证、可拒绝的策略制品。

### 8.1 阶段目标

建立 `strategy-artifact/1.0.0`，实现：

```text
SolutionSpec
  → 定位 artifact
  → 校验文件 SHA-256
  → 校验 GameSpec 指纹和动作树版本
  → 校验牌型/组合、动作和概率
  → 校验 EV 单位与有限数值
  → 产生只读 StrategyEvidence
  → 后续才能进入 Study 或 AI Coach
```

### 8.2 开工前必须冻结的问题

1. `artifact_id` 如何映射文件：相对 catalog 路径、对象存储 URI，还是独立 registry？
2. artifact 的规范序列化格式：首版建议严格 JSON，后续再评估 Parquet/二进制矩阵。
3. 一个 artifact 表达单节点还是一棵动作树？首版建议单节点纵向切片，降低验证面。
4. 私牌策略按 1,326 个具体组合、169 类起手牌，还是带权 range 子集表达？翻后必须处理公共牌冲突。
5. action ID 如何与动作树绑定？不能只用自由文本 `bet 50%`。
6. 概率和 EV 的精度、舍入和容差是多少？必须版本化。
7. EV 是相对当前决策、整手净 EV，还是从节点开始；单位是 BB、chips 或奖金价值？必须显式声明。
8. 策略来源、求解器配置、迭代/收敛证据和许可证如何记录？
9. `verified` 谁能授予、需要什么质量门？Matcher 不能自行升级质量标签。

### 8.3 建议的最小数据边界

建议至少包括：

- `schema_version`；
- `solution_id`；
- `game_spec_fingerprint`；
- `action_tree_version`；
- `node_id`；
- 明确、唯一的 action 定义；
- 私牌组合或 range entry；
- 每个 entry 的动作概率；
- 可选但强类型的 action EV；
- EV 单位和语义；
- provenance：求解器、版本、配置 ID、生成时间、质量报告 ID；
- artifact 内容本身不循环包含自己的最终哈希，哈希由 `SolutionSpec`/manifest 持有。

不要一开始把整个求解器内部状态、训练轨迹和 UI 聚合格式塞进同一个协议。

### 8.4 验证器必须拒绝

- 文件哈希与 `SolutionSpec.artifact_sha256` 不一致；
- `solution_id`、`GameSpec` 指纹或动作树版本不一致；
- 未知 schema 或未知字段；
- 非法/重复私牌组合；
- 私牌与公共牌冲突；
- 未声明、重复或非法 action；
- 概率不是有限数、超出 `[0,1]` 或合计不满足版本化容差；
- EV 为 NaN/Infinity、单位缺失或语义不明；
- provenance/质量标签缺失；
- 测试数据冒充 `verified`；
- artifact 引用越过允许目录或使用未授权远程位置。

### 8.5 建议新增模块

命名可以调整，但职责应保持分离：

```text
src/rivermind_core/strategy_artifacts.py   # schema、严格 loader、hash 校验
src/rivermind_core/strategy_query.py       # 从已验证 artifact 查询节点/组合
tests/test_strategy_artifacts.py           # 合同和攻击性失败用例
solutions/fixtures/                        # 仅 test_only，小而可人工核验
docs/STRATEGY_ARTIFACTS.md                  # 协议、单位、质量和限制
```

建议增加 CLI：

```text
rivermind gto-artifact-verify CATALOG SOLUTION_ID --json
rivermind gto-query SITE HAND_ID --before-action N ... --json
```

`gto-query` 只有在 Matcher 命中且 artifact 完整验证后才返回策略事实。否则维持现有 `unsupported` 边界。

### 8.6 第一阶段测试与验收

至少包括：

- 一个极小、人工可核对的 `test_only` artifact 正向用例；
- hash mismatch；
- solution/game/tree identity mismatch；
- 非法牌、重复牌、board blocker；
- 缺 action、重复 action；
- 概率小于 0、大于 1、不合计、NaN、Infinity；
- EV 单位/语义缺失；
- 路径穿越；
- Matcher exact → artifact verify → query 的端到端测试；
- `unsupported` 时绝不返回策略或 EV；
- 基准记录加载和单节点查询耗时；
- 全量旧测试继续通过。

阶段完成定义：

- artifact 协议和质量边界有文档；
- 严格 loader 与 validator 默认失败关闭；
- 有 test-only 纵向切片；
- 没有任何伪造 `verified` 数据；
- CLI/API 能区分“元数据命中”和“策略内容已验证”；
- 测试、基准、README、ARCHITECTURE、PROJECT_PLAN 同步更新；
- 提交并推送 GitHub `main`。

## 9. Strategy Artifact 之后的推荐顺序

1. **真实小规模解法接入**：选择来源和许可明确的一小批 HU/6-max 高频节点，建立求解质量报告；不要先追求数量。
2. **Catalog 索引**：按 GameSpec 指纹和硬维度建立索引，替代线性扫描，并保持完全相同的匹配结果。
3. **Study v0.1**：策略矩阵、动作频率、EV 和节点元数据；所有展示值来自已验证 artifact。
4. **Leak → Decision Router**：从证据手牌中选出可复盘决策，记录选择理由，不让 LLM 猜节点。
5. **Practice v0.1**：由验证策略生成题目、评分和复测；先做单节点，再做 Street/Full Hand。
6. **牌面同构与动作翻译**：必须用单独版本化协议和回归集，不能混进 Matcher v1 的静默启发式。
7. **真实 AI Coach 试验**：仅把验证后的策略事实加入 `ExplanationEvidence`，先离线评测和专家盲审，再考虑默认开启。
8. **H2N-lite 产品加固**：第二站点、导出/删除、All-in EV、百万手基准、正式 CI 和 Web UI。

## 10. 容易踩的坑

- `PROJECT_PLAN.md` 同时包含愿景和已完成项，不能把所有 P0/P1 描述当现状；
- `SolutionQuality.VERIFIED` 目前只是可表达的枚举，不代表仓库已有 verified 解法；
- `solution_reference_available=true` 不等于策略内容已验证；
- 牌谱中的实际 rake 不是 rake schedule；
- 当前 GameSpec 对动作线和牌面要求精确一致，不能擅自做“看起来差不多”的映射；
- 相同距离候选会返回 ambiguous，不要按 ID 顺序偷偷选择；
- 多人局质量不能复用 HU exploitability 宣传；
- 教练模板可以解释统计漏洞，但没有 GTO 证据时必须继续声明缺失；
- 不要为了演示 UI 而在前端硬编码策略频率；
- 不要提交数据库、私有牌谱、API key 或真实用户身份。

## 11. 可直接交给 Claude 的起始任务

可以把下面这段作为接手后的第一条指令：

```text
请先阅读 README.md、docs/CLAUDE_HANDOFF.md、docs/ARCHITECTURE.md 和
docs/GTO_MATCHER.md，并运行当前 81 项测试和 10,000 手牌基准。

保持产品定位不变：H2N-lite 是入口，GTO + AI 教练是长期差异化；
专用策略系统负责扑克策略，LLM 不得生成或补齐行动频率和 EV。

下一阶段实现 Strategy Artifact v0.1：冻结严格、版本化的策略制品协议，
实现本地安全 loader、SHA-256/身份/动作/牌型/概率/EV/provenance 校验，
增加 test_only 纵向 fixture、gto-artifact-verify CLI，以及
Matcher exact → artifact verify → query 的端到端测试。

不要添加伪造 verified 解法，不要真实调用外部模型，不要做实时牌桌辅助。
完成后运行全量测试和基准，更新 README/架构/项目计划，提交并推送 GitHub main。
```

## 12. 交接结论

当前项目已经完成了从牌谱导入、确定性统计、结算、Session、Leak Card、证据约束的 AI 解释，到 GTO 决策节点元数据匹配的完整工程骨架。

最关键的下一步不是继续增加“看起来像 GTO”的页面，而是把策略内容变成一种可验证的工程制品。只有 Strategy Artifact 的身份、完整性、动作、概率、EV、来源和质量边界全部通过验证，RiverMind 才应把它展示给用户或交给 AI Coach 解释。
