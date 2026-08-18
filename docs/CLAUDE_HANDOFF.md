# RiverMind Poker — Claude 工程交接文稿

> 交接日期：2026-08-18
> 最近更新：2026-08-18（Strategy Artifact、Solve Quality Gate、TexasSolver 接入、Catalog 索引、CI、Board Isomorphism 完成）
> GitHub：https://github.com/runshu-W/rivermind-poker
> 默认分支：`main`
> 功能基线：`5919e00 feat: add verifiable strategy artifacts and an independent quality gate` 之后的 TexasSolver 接入
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

- 276 项自动化测试全部通过（Python 3.11 / 3.12 / 3.13 均已验证）；
- 10,000 手牌导入约 3.8 秒，机器差异允许结果浮动；
- 1,000 个解法元数据：无索引单次匹配约 20–30 ms，预建索引后约 0.03 ms；
- 单个 test_only 策略制品验证约 0.3 ms，单组合查询约 0.01 ms，完整质量门约 0.5 ms；
- 工作树应为空；
- GitHub `main` 应与本地 HEAD 一致。

如果基线不一致，先查清本地改动、Python 版本和远端 HEAD，不要直接 reset 或覆盖用户文件。

建议依次阅读：

1. `README.md`
2. `PROJECT_PLAN.md`
3. `docs/ARCHITECTURE.md`
4. `docs/GTO_MATCHER.md`
5. `docs/STRATEGY_ARTIFACTS.md`
6. `docs/SOLVE_QUALITY_GATE.md`
7. `docs/AI_COACH.md`
8. 本交接文稿

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
- 当前节点指纹（rake model `pokerstars.cash.example`、5%、cap 3 BB）：
  `85b7db3215c80f307bf4c745ab4a001b68f14ab3f0c938577b99e1c6bb469f55`；
- 不提供 rake 时同一节点的指纹是
  `4e53d4c6009715435202549e990eb2c9753027af4320b1be0400f6187abb1e37`（现金匹配仍会失败关闭）。

> 上一版交接文稿在此处写的 `0f0d9b6e…` 与代码实际输出不一致，已更正。
> `tests/test_strategy_artifacts.py::test_demo_node_fingerprint_is_pinned` 现在固定了这个值，
> 规范化逻辑再变时会直接测试失败，而不是让文档静默过期。

这个案例只证明牌谱到解法元数据的映射链路，不证明任何下注频率或 EV。

### 3.7 Strategy Artifact v0.1

主要文件：

- `src/rivermind_core/strategy_artifacts.py`
- `src/rivermind_core/strategy_query.py`
- `solutions/catalog.test_only.json`
- `solutions/fixtures/btn_flop_cbet.test_only.json`
- `docs/STRATEGY_ARTIFACTS.md`
- `tests/test_strategy_artifacts.py`

已冻结的版本：

- `strategy-artifact/1.0.0`
- `strategy-evidence/1.0.0`
- `strategy-aggregation/1.0.0`

冻结的设计决策：

- `artifact_id` 是 catalog 文件所在目录下的相对 POSIX 路径，解析在该目录沙箱内完成；
- 序列化格式是严格 JSON，所有数值是十进制字符串，不是 JSON number；
- 一个 artifact 表达**单个节点**，不是一棵动作树；
- 私牌按 **1,326 个具体组合**表达，可以正确处理公共牌冲突；
- action 由节点显式声明（`action_id` + `kind` + `size_bb`），组合层只能引用已声明的 ID；
- 概率、EV、权重和尺度最多 6 位小数、总计最多 18 位数字，单组合概率和与 1 的偏差不超过 `0.00001`；
- 结构上限：每节点最多 64 个 action、最多 1,326 个组合；
- `ev_unit = "bb"`、`ev_semantics = "action_ev_from_node"` 必填；
- provenance 必填求解器、版本、配置 ID、生成时间、质量标签、质量报告 ID 和许可证。

loader 会拒绝：哈希不符、`solution_id`/`GameSpec` 指纹/动作树/求解器身份/质量标签不一致、未知 schema、未知字段、重复 JSON 键、过深嵌套、孤立代理码点、非法与重复组合、公共牌冲突、未声明/重复/遗漏 action、概率越界或为负、概率不合计、`NaN`/`Infinity`/浮点、精度或位数越界、EV 部分声明、provenance 缺失、`fixtures/` 下的制品声称 `verified`、路径穿越与符号链接、文件缺失或超过 8 MiB。

已知边界：硬链接不会被拒绝，符号链接检查与读取之间有 TOCTOU 窗口。两者都要求攻击者已能写入 catalog 目录，且内容仍被 SHA-256 钉死。不要把不受信任的目录当作 catalog 根。

`build_strategy_evidence` 返回只读 `StrategyEvidence`。`usable_for_teaching` 需要**标签是 `verified`**且**提供了通过质量门的签署**，见 3.8。

重要事实：

- 仓库唯一的策略切片是**手写的** 4 组合 × 3 动作 fixture，质量标签 `test_only`，求解器记为 `rivermind.handwritten`；
- 它只证明"牌谱 → 节点 → 目录 → 制品 → 只读事实"链路可用，不代表任何下注频率或 EV；
- `solutions/catalog.json` 仍然故意为空；
- 有一条测试会扫描 `solutions/` 下所有 JSON，确认没有文件声称 `verified`；
- `.gitattributes` 把 `solutions/**/*.json` 和 `tests/fixtures/**` 标记为 `-text`，防止 checkout 改写行尾破坏 SHA-256。

不要把"制品验证通过"写成"策略已经可信"。验证只证明身份、完整性和内部一致性，不会升级质量标签。

### 3.8 Solve Quality Gate v0.1

主要文件：

- `src/rivermind_core/_contracts.py`（三个协议共用的严格读取原语与路径沙箱）
- `src/rivermind_core/solve_quality.py`
- `src/rivermind_core/quality_gate.py`
- `docs/SOLVE_QUALITY_GATE.md`
- `tests/test_quality_gate.py`

已冻结的版本：

- `solve-quality-report/1.0.0`
- `quality-attestation/1.0.0`
- `quality-gate-policy/1.0.0`

核心结构：loader 检查"字节是不是我们预期的字节"，质量门检查"这份策略是否好到可以教人"。两条路径完全分离，loader 永远不授予标签。

门要求：

- 签署按字节钉死制品与报告的 SHA-256；
- 报告、制品、目录条目三方身份一致（solution、指纹、动作树、求解器、配置 ID、`quality_report_id`）；
- 收敛达到报告自己的门槛，且不超过 `quality-gate-policy/1.0.0` 的绝对上限（`bb_per_100` ≤ 1、`bb` ≤ 0.01、`percent_of_pot` ≤ 1）；
- `source.display_allowed = true`；
- `evaluation.independent_recheck = true`，且复核工具名称不等于求解器名称；
- `solve.rake_model_id` 与节点自己的 `RakeSpec.model_id` 一致（无抽水节点必须为 `null`）；
- `claim_class = equilibrium_approximation` 只允许两人 chip_ev 节点，且收敛指标必须是均衡距离类（`exploitability`/`nash_distance`/`best_response_gap`）；多人局与 ICM/PKO 只能用 `empirical_quality`；
- 至少 2 名签署人、至少 1 名 `independent_reviewer`、`reviewer_id` 大小写不敏感去重；
- 完整时间顺序：许可获取 ≤ 求解完成、求解开始 ≤ 制品生成 ≤ 授予、求解完成 ≤ 签署 ≤ 授予、授予不落在未来。

重要事实：

- 仓库里**没有任何**签署文件、求解质量报告或 `verified` 制品；测试里的完整授予都在临时目录中构造；
- 一条测试会扫描整个仓库，确认没有 `.json` 含有 `quality-attestation`、`solve-quality-report` 或 `"quality": "verified"`；
- 因此 `usable_for_teaching` 在这个仓库里恒为 `false`。门建好了，还没有任何东西通过它。

**需要你确认的一件事：** 绝对收敛上限那三个数字（1 bb/100、0.01 bb、1% pot）是我给的保守起始值，没有经过真实解法校准。接入首批解法时应当重新评估，调整需要发新的 `quality-gate-policy` 版本并重新签署所有既有授予。

### 3.9 TexasSolver 接入链路

主要文件：

- `src/rivermind_core/texassolver_import.py`
- `docs/SOLVER_INGEST_TEXASSOLVER.md`
- `tests/test_texassolver_import.py`

已确认的 TexasSolver 事实（读源码得来，不是猜的）：

- 树根是 OOP 的第一个翻后决策；**player 0 = IP，player 1 = OOP**（见 `src/tools/Rule.cpp`）；
- `set_accuracy` 的单位是**底池百分比**：`total_exploitability = exploitable / player_number / initial_pot * 100`（见 `src/solver/BestResponse.cpp`）；
- 范围记法只支持 `XY` / `XYs` / `XYo` 加可选 `:weight`，**不支持 `+`**；权重 ≤ 0.005 会被直接丢弃（见 `src/tools/PrivateRangeConverter.cpp`）；
- `dump_result` **只导策略，不导 EV**；EV 只能从 GUI/API 拿。所以首批制品的 `ev` 全是 `null`；
- 全下被写成 `BET <全部筹码>`，没有独立的 ALLIN 标签；
- 组合拼写是 `AsAh` 这种，和协议要求的 `AhAs` 不同，必须重新规范化。

转换器的边界：

- 概率取整到 6 位小数后，把残差加到最大的那个动作上，确定性规则，结果精确等于 1；
- 权重必须显式提供（`--range`）或显式声明按 1 处理（`--assume-uniform-weights`），不允许静默填 1；
- 提供的范围与 dump 里的组合对不上就报错——说明范围贴错了；
- **转换器永远不写 `verified`**。升级标签是一次刻意的手工编辑，然后才是质量门。

已知协议缺口：**`GameSpec` 指纹不包含输入范围。** 同一节点用两套范围求解会得到指纹相同、内容不同的制品。兜底是 Matcher 对重复指纹返回 `ambiguous` 并失败关闭；真正钉死范围的是质量报告的 `solver.config_sha256`，所以**求解配置文件必须存档**。

### 3.10 Catalog 索引 v0.1

主要文件：

- `src/rivermind_core/gto_index.py`
- `tests/test_gto_index.py`

问题不在「扫描」，在 `GameSpec.fingerprint`——它是 property，每次访问都重算一遍 SHA-256。1,000 节点的目录每次匹配要算 1,000 次哈希。

`SolutionCatalogIndex` 构建时把指纹和硬维度键各算一次，之后匹配是两次字典查找：

| 目录规模 | 无索引 | 有索引 | 构建 |
|---:|---:|---:|---:|
| 1,000 | 21.6 ms | 0.058 ms | 20 ms |
| 20,000 | 455.2 ms | 0.126 ms | 422 ms |

构建是一次性 O(n)，所以单次匹配不吃亏；价值在**重复匹配同一目录**（Study、Practice、整段 Session 复盘）。同一节点的不同筹码深度共享一个硬维度桶——筹码*数量*是数值维度，筹码*位置*才是硬维度。

**索引只改变开销，不改变结论。** `tests/test_gto_index.py` 保留了索引化之前的线性实现作为可执行规格，用随机目录逐字节差分比对；对抗性审查另跑了约 48 万次比对，覆盖全部分支，无分歧。

传入的索引必须来自同一个 catalog 对象，否则 `match_game_spec` 直接报错。

顺带修掉的一个真问题：`GameSpec.to_dict` 里的 `_decimal_text` 原本用 `Decimal.normalize()`，它会按 **decimal 全局上下文**取整。这意味着节点指纹依赖解释器全局状态，而且长小数会被静默截断（`123456789012345678901234567890.5` 在默认 prec=28 下变成 `…567900`）。现在改成不依赖上下文的手工去尾零，指纹成为节点数据的纯函数。演示节点的指纹 `85b7db32…` 不变，有测试守住。

### 3.11 Board Isomorphism v0.1

主要文件：

- `src/rivermind_core/board_isomorphism.py`
- `docs/BOARD_ISOMORPHISM.md`
- `tests/test_board_isomorphism.py`

```text
所有三张翻牌组合   22,100
按牌面等价折叠后    1,755
冗余倍数            12.6x
```

协议只承认两条互相独立的等价：**花色重标号**，以及**翻牌顺序**（三张同时亮出，没有行动能区分）。转牌和河牌不可重排。规范形式 = 24 种重标号里「翻牌排序后 + 转牌 + 河牌」的字典序最小值，排序必须在重标号之后做。翻前不折叠花色（`AhKh` 和 `AhKs` 不是同一手牌）。

**默认关闭**，必须 `board_isomorphism=True` 或 CLI `--board-isomorphism` 显式打开。关闭时输出逐字节等于 `gto-match-policy/1.0.0`，有差分测试守着。

新增状态 `isomorphic`，**不复用 `exact`**——任何检查 `status == "exact"` 的既有消费方在被刻意更新之前，对重标号牌面继续失败关闭。`exact` 优先；两个目录节点互为等价时返回 `ambiguous`。

命中携带「观察牌面 → 解法牌面」的花色置换。查询时你问的组合在观察坐标系，返回的也在观察坐标系，另附 `solution_frame_combo` 供审计。索引的等价映射首次使用时才构建。

**这条等价的前提**：花色重标号只在求解的**输入范围**也花色对称时成立。这是关于求解的假设，不是关于牌面的定理，代码无法检查（制品里没有输入范围），所以由报告承担，`StrategyEvidence` 会把这句话带出来。

验证：全部 22,100 个翻牌穷举到 1,755 类并逐个验幂等；4,000 次随机重标号必须同类；2,000 次置换往返与 500 次逆元/复合律。

### 3.12 CI

`.github/workflows/ci.yml`：

- `test`：**Linux + Windows × Python 3.11/3.12/3.13** 六个组合跑测试与 compileall。Windows 那一路是专门用来守住哈希寻址文件行尾的；
- `benchmark`：小规模跑一遍基准，验证它还能执行、内部断言还成立（不做性能门，共享 runner 太吵）；
- `guards`：仓库守卫。

`tools/check_repository.py` 与 `tools/check_docs.py` 本地和 CI 共用。守卫内容：禁止提交数据库/私有牌谱/密钥/`verified` 制品与签署，`.gitattributes` 的 `-text` 规则必须还在，哈希寻址文件不得含 CRLF，默认目录必须为空，文档链接必须可解析。

建 CI 时立刻抓到一个真问题：`test_rejects_json_nested_too_deeply` 在 3.12/3.13 上失败——CPython 3.12 提高了 json 的嵌套上限，3000 层不再抛 `RecursionError`。行为本身仍然失败关闭（文档被当成非对象拒绝），是测试写死了错误文案。已改成用 50,000 层（三个版本都稳定触发）并补一条浅层用例。

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
- `gto-artifact-verify`
- `gto-quality-verify`
- `gto-artifact-package`
- `gto-catalog-add`
- `gto-import-texassolver`
- `gto-query`
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

python -m rivermind_core gto-artifact-verify `
  solutions/catalog.test_only.json `
  test-only.pokerstars-cash.btn-flop-cbet --json

python -m rivermind_core gto-query pokerstars 100000000001 `
  --before-action 5 `
  --database data/dev.db `
  --catalog solutions/catalog.test_only.json `
  --rake-model pokerstars.cash.example `
  --rake-percent 5 `
  --rake-cap-bb 3 `
  --combo AhKh `
  --json

# 独立质量门；仓库当前没有任何签署可以喂给它
python -m rivermind_core gto-quality-verify grants/attestation.json `
  --catalog solutions/catalog.json --json

# 草稿制品规范化；--update-catalog 必须搭配 --write
python -m rivermind_core gto-artifact-package draft.json `
  --catalog solutions/catalog.json --write --update-catalog --json
```

默认空目录返回 `unsupported/catalog_empty` 是正确行为。`gto-query` 只在 exact 命中且制品验证通过时返回频率与 EV；制品验证失败、签署未通过质量门或签署是为别的字节签发的都用退出码 2 报错，`approximate`、`unsupported` 和"组合未覆盖"则返回退出码 0 加结构化的"没有策略"。不带 `--attestation` 时仍会返回事实，但 `usable_for_teaching = false`。

重新打包制品会作废既有签署：字节变了，`artifact_sha256` 就不再匹配，必须重新走质量门。

## 5. 仓库结构和工程约定

```text
src/rivermind_core/   Python 领域核心、CLI、存储和服务边界
tests/                unittest、黄金牌谱、合同与回归测试
evals/                AI Coach 50 例离线评测语料
benchmarks/           可重复的导入/查询/匹配基准
solutions/            解法目录；默认目录为空，另有 test_only 制品切片；无签署与报告
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
- CI 在 `.github/workflows/ci.yml`：Linux + Windows × Python 3.11/3.12/3.13 跑测试与 compileall，另有基准与仓库守卫两个 job。Windows 那一路是专门用来守住哈希寻址文件行尾的；
- `.gitattributes` 强制 LF，并把哈希寻址的 `solutions/**/*.json` 与 `tests/fixtures/**` 标记为 `-text`；
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
11. `strategy_content_verified=true` 只证明身份、完整性和内部一致性；只有 `usable_for_teaching=true`（标签是 `verified` **且**有通过质量门的签署）才允许把频率或 EV 展示给学习者。
12. 不得为了“先跑通”而放宽或跳过 SHA-256、身份和内容校验；修改被哈希的制品必须同步更新目录里的 `sha256` 并重新签署。
13. `verified` 只能由 loader 之外的质量门授予，且必须有来源、许可、收敛证据和至少两名签署人（其中一名独立复核）。任何代码路径都不得自行升级质量标签。
14. 质量门里不要写"搜关键词"式的检查；用结构化字段与既有协议对象比对。

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
- 没有真实策略制品、求解质量报告或签署；仓库只有手写的 `test_only` 切片，`usable_for_teaching` 恒为 false；
- 绝对收敛上限尚未经过真实解法校准；
- 一个 artifact 只表达一个节点，没有动作树遍历和街道推进；
- 动作线必须完全一致，尚未做经过验证的 bet-size translation；这也是 `gto-query` 能否接受 approximate 命中的前置条件；
- 公共牌必须逐张相同，尚未做 suit isomorphism；
- 动作线必须完全相同，尚未做经过验证的 bet-size translation；因此 `gto-query` 只接受 exact 命中；
- 没有解法浏览器、矩阵、节点导航、训练题或 EV loss；
- Leak Card 尚未自动定位“哪一个决策最值得送入 GTO Matcher”。

### AI 层

- 没有真实外部模型调用结果；
- 没有真实专家盲审评分；
- `ExplanationEvidence` 尚未接入 `StrategyEvidence`；当前教练证据里没有任何 GTO 策略事实；
- 对手建模尚未实现。

## 8. 已完成阶段

两个阶段已经交付，保留在此作为契约说明和验收记录。

### 8.1 Strategy Artifact v0.1

实现见 `docs/STRATEGY_ARTIFACTS.md`。链路：

```text
SolutionSpec
  → 在 catalog 目录沙箱内定位 artifact
  → 校验文件 SHA-256
  → 校验 solution / GameSpec 指纹 / 动作树 / 求解器 / 质量标签
  → 校验 action、私牌组合、概率、EV 单位与语义
  → 校验 provenance
  → 产生只读 StrategyEvidence
```

冻结的答案：

| 开工前的问题 | v0.1 结论 |
|---|---|
| `artifact_id` 如何映射文件 | catalog 目录为根的相对 POSIX 路径 + 目录沙箱 |
| 序列化格式 | 严格 JSON、UTF-8、数值一律十进制字符串、2 空格缩进 + LF 的规范字节 |
| 单节点还是动作树 | 单节点纵向切片 |
| 私牌粒度 | 1,326 个具体组合，翻后校验公共牌冲突 |
| action 绑定 | 节点显式声明 `action_id` + `kind` + `size_bb` |
| 精度与容差 | 最多 6 位小数、18 位数字；单组合概率和容差 `0.00001` |
| EV 语义与单位 | `action_ev_from_node`、`bb`，两者必填 |
| 结构上限 | 每节点最多 64 个 action、1,326 个组合 |

### 8.2 Solve Quality Gate v0.1

实现见 `docs/SOLVE_QUALITY_GATE.md`。链路：

```text
QualityAttestation
  → 沙箱内定位并按字节校验 SolveQualityReport
  → 用普通 loader 完整验证 StrategyArtifact
  → 三方身份一致
  → 收敛达到自己的门槛，且不超过策略绝对上限
  → 许可允许展示、有独立复核
  → rake 范围与 claim_class 与节点相符
  → 双人签署、至少一名独立复核
  → 时间顺序自洽
  → usable_for_teaching = true
```

冻结的答案：

| 开工前的问题 | v0.1 结论 |
|---|---|
| 来源与许可怎么记录 | `source`：origin、provider、license_id、obtained_at、display_allowed、redistribution_allowed |
| 质量报告写什么 | 求解器身份与配置哈希、迭代数、收敛指标/值/单位/门槛、抽象、独立复核工具、结构化 rake 模型、至少一条显式限制 |
| 谁能授予 `verified` | ≥2 名签署人、≥1 名 `independent_reviewer`；loader 永不授予 |
| 动作树如何冻结 | 报告 / 制品 / 目录三方 `action_tree_version` 必须一致 |
| 多人局质量怎么表述 | `claim_class`：两人 chip_ev 才可用 `equilibrium_approximation`，其余一律 `empirical_quality` |

### 8.3 新增模块

```text
src/rivermind_core/_contracts.py           三协议共用的严格读取原语与路径沙箱
src/rivermind_core/strategy_artifacts.py   制品 schema、严格 loader、规范序列化
src/rivermind_core/strategy_query.py       只读 StrategyEvidence 与版本化加权汇总
src/rivermind_core/solve_quality.py        求解质量报告协议与节点交叉校验
src/rivermind_core/quality_gate.py         签署协议与独立 verified 授予门
tests/test_strategy_artifacts.py           制品合同与攻击性失败用例
tests/test_quality_gate.py                 质量门合同与攻击性失败用例
solutions/catalog.test_only.json           链路 fixture 目录；正式目录仍为空
solutions/fixtures/                        仅 test_only，小而可人工核验
docs/STRATEGY_ARTIFACTS.md                 制品协议、单位、质量与限制
docs/SOLVE_QUALITY_GATE.md                 报告协议、授予流程与策略上限
.gitattributes                             保护哈希寻址文件的字节
```

### 8.4 验收状态

- 两套协议与质量边界都有文档；
- loader 与质量门默认失败关闭，且互相独立；
- 有 test-only 纵向切片，没有任何伪造 `verified` 数据，也没有任何签署；
- CLI/API 能区分"元数据命中""策略内容已验证"和"质量已授予"三种状态；
- 全部 276 项测试通过；基准新增索引化匹配、制品验证、单节点查询和质量门耗时；
- README、ARCHITECTURE、PROJECT_PLAN、GTO_MATCHER 与本文稿同步更新。

### 8.5 两轮对抗性审查修掉的问题

记录在此，避免以后又被引入：

- 40 位的 EV 会被 Python 默认 28 位十进制上下文静默取整，制品报出的数字不再是文件里的数字 → 加了 18 位数字上限和 96 位精度的汇总上下文；
- 孤立代理码点（`\ud800`）能通过校验却在输出时崩溃 → 文本模式排除代理与 C1 控制字符；
- 深层嵌套 JSON 抛 `RecursionError` 逃逸到 CLI → 三个协议和目录加载都按验证失败处理；
- 重复 JSON 键"后者胜出"，同一份字节有两种读法 → 一律拒绝；
- 汇总对 action 数是二次复杂度 → 改为按 `action_id` 索引的单遍累加，并加结构上限；
- `gto-artifact-package` 能把制品写到 `catalog.json` 或签署过的报告上并抹掉它们 → 拒绝写目录文件本身、拒绝覆盖非本 solution 的既有 JSON、原子替换、写后立即复验；
- rake 处理曾用"在 limits 里搜 rake 这个词"确认，一句"rake was ignored completely"就能满足 → 改成结构化 `solve.rake_model_id` 与节点 `RakeSpec` 比对；
- `equilibrium_approximation` 可以搭配 `average_regret` 绕开 rake 责任 → 强断言必须使用均衡距离类指标；
- 制品 `generated_at`、许可 `obtained_at` 和未来日期的授予都没有被排序检查 → 补齐完整时间约束；
- 报告可以自己声明 `threshold = 999999` 然后"达标" → 加了策略层绝对上限；
- 求解器可以把自己列为"独立复核工具"；两个只差大小写的 `reviewer_id` 算两个人 → 都已拒绝；
- 一个目录里两个 solution 指向同一个 artifact 文件，重打包会静默破坏另一个 → `artifact_id` 在目录内必须唯一。

## 9. 建议下一阶段：真实小规模解法接入

协议和门都建好了，缺的是数据。这一步**必须由你提供外部输入**：仓库里不能凭空出现真实求解器输出，手写频率也绝不能标成 `verified`。

### 9.1 阶段目标

来源已选定为 TexasSolver（AGPL v3，本地跑二进制），转换器和命令链都已就绪。剩下的全部是**需要人去做**的事：

```text
[人] 决定 IP/OOP 输入范围并存档配置        ← 唯一的建模假设，牌谱推不出来
[人] 跑一次求解，记下它打印的 exploitability
gto-catalog-add          登记节点，哈希占位
gto-import-texassolver   dump → 草稿制品
gto-artifact-package     规范化、写入、登记真实哈希
gto-artifact-verify      确认制品自洽（此时标签是 experimental）
[人] 把标签改成 verified 并重新 package
[人] 写 solve-quality-report/1.0.0
[人] 独立复核人签署 quality-attestation/1.0.0
gto-quality-verify       质量门
gto-query --attestation  首次 usable_for_teaching = true
```

操作细节见 `docs/SOLVER_INGEST_TEXASSOLVER.md`。

### 9.2 开工前必须冻结的问题

1. ~~首批解法的来源与导出格式~~ → 已定：TexasSolver，本地跑二进制，AGPL v3。做成在线服务前必须重新评估许可。
2. ~~首批覆盖哪个节点~~ → 已定：仓库 fixture 的 `before_action=5`，指纹 `85b7db32…`。
3. **IP 与 OOP 的输入范围是什么？** 牌谱推不出来，这是纯建模假设，必须写明出处。
4. 绝对收敛上限的三个数字是否符合你的质量标准？TexasSolver 的 `set_accuracy` 用底池百分比，策略上限是 1%。跑完第一次求解看它实际收敛到多少再定。
5. `independent_reviewer` 具体是谁？书面复核意见存放在哪里，`statement_sha256` 指向什么？
6. 求解配置文件存档在哪里，`solver.config_sha256` 指向什么？（这是唯一钉死输入范围的东西）
7. 制品体积增长后是否需要索引或分片？8 MiB 上限何时提高、依据是什么？

### 9.3 第一阶段测试与验收

- 至少一个真实来源的制品通过完整 loader 与质量门；
- 质量报告 ID 可以从制品追溯到具体求解配置与书面复核意见；
- `gto-query --attestation` 对该节点返回 `usable_for_teaching = true`，对其余节点仍然失败关闭；
- 缺质量报告、缺许可、缺独立复核或签署不足时必须被拒绝；
- 记录制品加载、查询与质量门基准；
- 全量旧测试继续通过。

## 10. 之后的推荐顺序

1. ~~**Catalog 索引**~~ → 已完成，见 3.10。
2. ~~**牌面同构**~~ → 已完成，见 3.11。剩下的是 **bet-size translation**：动作线仍要求完全一致，真实牌谱里 BTN 开 2.5bb 而解法是 3bb 就不匹配。同样需要单独版本化协议与回归集。
3. **Study v0.1**：策略矩阵、动作频率、EV 和节点元数据；只展示 `usable_for_teaching = true` 的内容。
3. **Leak → Decision Router**：从证据手牌中选出可复盘决策，记录选择理由，不让 LLM 猜节点。
4. **Practice v0.1**：由已授予策略生成题目、评分和复测；先做单节点，再做 Street/Full Hand。
5. **牌面同构与动作翻译**：必须用单独版本化协议和回归集，不能混进 Matcher v1 的静默启发式；这也是放开 `gto-query` approximate 命中的前置条件。
6. **真实 AI Coach 试验**：仅把 `usable_for_teaching = true` 的策略事实加入 `ExplanationEvidence`，先离线评测和专家盲审，再考虑默认开启。
7. **H2N-lite 产品加固**：第二站点、导出/删除、All-in EV、百万手基准、正式 CI 和 Web UI。

## 11. 容易踩的坑

- `PROJECT_PLAN.md` 同时包含愿景和已完成项，不能把所有 P0/P1 描述当现状；
- `SolutionQuality.VERIFIED` 目前只是可表达的枚举，仓库没有任何 verified 解法，也没有任何签署；
- `solution_reference_available=true` 不等于策略内容已验证；
- `strategy_content_verified=true` 也不等于策略可信；它只说明身份、完整性和内部一致性通过；
- `quality = "verified"` 仍然不等于可教学；只有 `usable_for_teaching=true`（标签 + 通过门的签署）才允许展示给学习者；
- 汇总视图只覆盖制品自带的组合，不是节点完整范围；各动作概率独立取整后之和可能与 1 差最后一位；
- 牌谱中的实际 rake 不是 rake schedule；报告的 `solve.rake_model_id` 才是"这次求解建模了哪套抽水"；
- 当前 GameSpec 对动作线和牌面要求精确一致，不能擅自做"看起来差不多"的映射；
- 相同距离候选会返回 ambiguous，不要按 ID 顺序偷偷选择；
- 修改 `solutions/` 下任何被哈希的 JSON 后必须同步更新目录里的 `sha256`，并**重新签署**；
- 不要移除 `.gitattributes` 里的 `-text` 规则，否则 Windows checkout 会改写行尾并破坏哈希；
- 不要在质量门里加"搜关键词"式的检查；上一版的 rake 检查就是这样被一句"rake was ignored"绕过的；
- 多人局质量不能复用 HU exploitability 宣传；协议层已经用 `claim_class` 挡住了，不要绕过；
- 教练模板可以解释统计漏洞，但没有 GTO 证据时必须继续声明缺失；
- 不要为了演示 UI 而在前端硬编码策略频率；
- 不要提交数据库、私有牌谱、API key、签署文件或真实用户身份。

## 12. 可直接交给 Claude 的起始任务

可以把下面这段作为接手后的第一条指令：

```text
请先阅读 README.md、docs/CLAUDE_HANDOFF.md、docs/ARCHITECTURE.md、
docs/GTO_MATCHER.md、docs/STRATEGY_ARTIFACTS.md 和 docs/SOLVE_QUALITY_GATE.md，
并运行当前 276 项测试和 10,000 手牌基准。

保持产品定位不变：H2N-lite 是入口，GTO + AI 教练是长期差异化；
专用策略系统负责扑克策略，LLM 不得生成或补齐行动频率和 EV。

下一阶段做真实小规模解法接入。协议、严格 loader 和独立质量门都已就绪，
缺的是外部数据：先和我确认来源、许可、求解配置、动作树版本、独立复核人
和绝对收敛上限，再写来源专用转换器，把一小批节点走完
package → report → attestation → gto-quality-verify 全链路。

不要添加伪造 verified 解法或签署，不要真实调用外部模型，
不要做实时牌桌辅助。完成后运行全量测试和基准，
更新 README/架构/项目计划，提交并推送 GitHub main。
```

## 13. 交接结论

当前项目已经完成了从牌谱导入、确定性统计、结算、Session、Leak Card、证据约束的 AI 解释，到 GTO 决策节点元数据匹配、可验证策略制品，再到独立质量授予门的完整工程骨架。

三层边界现在是分开的、可分别拒绝的：

1. **元数据命中** —— 目录里有这个节点；
2. **内容已验证** —— 字节、身份和内部一致性都对；
3. **质量已授予** —— 有来源、许可、收敛证据和两个人签字。

只有第三层通过，数字才允许出现在学习者面前。任何一层失败，系统返回"没有策略"，而不是一个看起来合理的数字。

最关键的下一步不是继续加协议或界面，而是让**第一批真实解法**带着来源、许可和求解质量报告走完这三层。在那之前，`usable_for_teaching` 应当始终为 false——而且现在，这不再是一句自律，是代码强制的。
