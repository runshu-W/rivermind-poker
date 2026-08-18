# RiverMind Poker

RiverMind 是一个以 **H2N-lite 牌谱分析作为入口、GTO 策略系统作为长期壁垒、AI 教练作为用户体验层** 的德州扑克学习产品。

## 产品路线

```text
牌谱导入 → 统计与漏洞 → AI 解释 → GTO 节点匹配 → 针对性训练
```

- Beta：离线牌谱导入、标准化、Session 报告、固定核心统计、手牌回放、漏洞识别和证据约束的 AI 解释。
- 当前 GTO 底座：从真实牌谱提取决策前节点，对版本化解法目录返回精确、阈值内近似或不支持；命中后再对策略制品做哈希、身份、动作、组合、概率、EV 和来源校验，全部通过才返回只读策略事实。
- 质量边界：`verified` 只能由 loader 之外的独立质量门授予，需要求解质量报告、许可、收敛证据和至少两名签署人（其中一名独立复核）。
- 下一阶段：接入一小批来源与许可明确的真实解法，让第一份制品真正通过这道门；随后把高频错误映射到可追溯解法，形成 Study → Practice 闭环。
- 长期：预计算解法库、专用策略/价值网络、定制求解和教学 Bot。LLM 不直接决定扑克行动。

## 当前状态

项目已完成 H2N-lite Beta 的核心分析底座：

- 规范化手牌领域模型；
- 可插拔牌谱解析器接口；
- PokerStars 英文现金桌与 MTT 文本解析器，支持付费赛、免费赛、ante 和赛事级别；
- 多手拆分、来源识别、逐手错误隔离和稳定指纹去重；
- SQLite 本地事务存储、可恢复批次报告和原始牌谱回放；
- 文件/文件夹导入 CLI，以及成功、重复、失败、不支持四类结果；
- 确定性 VPIP、PFR、RFI、3Bet、Call Open、Cold Call、Fold to 3Bet、Flop CBet 与 Fold to Flop CBet；
- 位置、起始/有效筹码 BB 标准化，以及可索引的玩家–手牌统计宽表；
- 按赛制、位置、有效筹码过滤的 Python API 和 CLI；
- 逐动作筹码账本、Cash 盈亏、MTT 筹码变化和 Session 聚合；
- 相关手牌分页查询、结构化回放，以及可直接打开的本地分析页；
- 6 条版本化确定性复盘规则，使用样本门槛和 95% Wilson 区间生成 Leak Cards；
- 每张 Leak Card 包含统计口径、严重度、复盘问题和可下钻的证据手牌；
- 脱敏 `ExplanationEvidence`、确定性中文教练模板、候选模型输出忠实性校验和自动回退；
- 50 例候选合同/对抗性离线评测门，以及带超时、重试、输入/输出上限和费用预算的模型无关异步运行时；
- 版本化中文提示词、严格 JSON Schema，以及默认关闭且必须显式授权的 OpenAI Responses 适配器；
- 50 个不同证据、双专家评分、零 fatal error 的盲审质量门；当前尚未收集真实专家结果；
- 默认报告仍使用确定性模板；所有自动化测试均为本地假传输，没有向第三方发送牌谱数据；
- `GameSpec/SolutionSpec/SolutionCatalog` 严格版本契约、决策前节点指纹、精确/近似/不支持匹配和逐字段差异；
- `strategy-artifact/1.0.0` 策略制品协议：单节点纵向切片、1,326 组合粒度、显式 action 定义、6 位小数版本化精度，以及必填的 EV 单位与语义；
- 目录沙箱内的严格 loader：SHA-256 完整性、solution/GameSpec/动作树/求解器/质量标签一致性、路径穿越与符号链接拒绝、8 MiB 上限；
- 组合、概率与 EV 内容校验：非法牌、重复牌、公共牌冲突、未声明或遗漏 action、概率越界或不合计、NaN/Infinity/浮点、精度与位数越界、重复 JSON 键、孤立代理码点、EV 部分声明一律失败关闭；
- `strategy-evidence/1.0.0` 只读事实与 `strategy-aggregation/1.0.0` 加权汇总；
- `solve-quality-report/1.0.0` 求解质量报告：来源与许可、求解配置、收敛指标与单位、评估范围、结构化 rake 模型和显式限制；
- `quality-attestation/1.0.0` 与 `quality-gate-policy/1.0.0`：按字节钉死制品与报告、双人签署且至少一名独立复核、绝对收敛上限、完整时间顺序约束；
- `usable_for_teaching` 需要同时满足“标签是 verified”和“存在通过质量门的签署”，否则输出 `teaching_block_reason`；
- `claim_class` 强制区分两人零和均衡逼近与多人/ICM 经验质量，多人局无法冒用 exploitability 口径；
- `gto-artifact-verify`、`gto-quality-verify`、`gto-artifact-package` 与 `gto-query` CLI，明确区分“元数据命中”“策略内容已验证”和“质量已授予”；
- 默认解法目录为空，不包含伪造频率、EV 或“已验证”测试解法；仓库唯一的策略切片是手写、标记 `test_only` 的链路 fixture，且没有任何签署文件；
- 206 项自动化测试、黄金集清单，以及含 1,000 节点匹配、制品验证、单节点查询和质量门耗时的可重复性能基准；
- Beta 范围与架构文档。

## 快速开始

需要 Python 3.11+。

```powershell
$env:PYTHONPATH = "src"
python -m rivermind_core import tests/fixtures --database data/dev.db

# 默认统计牌谱中标记的 Hero；也可使用 --player 指定玩家
python -m rivermind_core stats --database data/dev.db

# Session、Leak Cards、AI 教练、相关手牌和结构化回放
python -m rivermind_core sessions --database data/dev.db
python -m rivermind_core leaks --database data/dev.db --json
python -m rivermind_core coach --database data/dev.db --json
python -m rivermind_core coach-eval --json
python -m rivermind_core coach-review-score completed-review.json --json
python -m rivermind_core hands --database data/dev.db --metric flop_cbet --occurred
python -m rivermind_core replay pokerstars 100000000001 --database data/dev.db --json

# 将真实决策前节点与目录比较；默认空目录会诚实返回 unsupported
python -m rivermind_core gto-match pokerstars 100000000001 `
  --before-action 5 --database data/dev.db --catalog solutions/catalog.json `
  --rake-model pokerstars.cash.example --rake-percent 5 --rake-cap-bb 3 --json

# 验证一个解法条目的策略制品（哈希、身份、动作、组合、概率、EV、来源）
python -m rivermind_core gto-artifact-verify solutions/catalog.test_only.json `
  test-only.pokerstars-cash.btn-flop-cbet --json

# 独立质量门：只有它能授予 verified，仓库当前没有任何签署可以喂给它
python -m rivermind_core gto-quality-verify grants/attestation.json `
  --catalog solutions/catalog.json --json

# 把草稿制品规范化成目录将要登记的字节
python -m rivermind_core gto-artifact-package draft.json `
  --catalog solutions/catalog.json --json

# match → verify → query；未 exact 命中或制品未通过验证时不返回任何频率与 EV
python -m rivermind_core gto-query pokerstars 100000000001 `
  --before-action 5 --database data/dev.db `
  --catalog solutions/catalog.test_only.json `
  --rake-model pokerstars.cash.example --rake-percent 5 --rake-cap-bb 3 `
  --combo AhKh --json

# 生成第一个本地分析页面
python -m rivermind_core report --database data/dev.db --output data/report.html

# 示例：只看 40–80bb 的 UTG/HJ MTT
python -m rivermind_core stats --database data/dev.db --game-type tournament `
  --position UTG HJ --min-effective-stack-bb 40 --max-effective-stack-bb 80

# 运行测试
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

机器可读导入报告：

```powershell
$env:PYTHONPATH = "src"
python -m rivermind_core import tests/fixtures --database data/dev.db --json
```

解析一手牌：

```python
from rivermind_core.parsers import default_registry

hand = default_registry().parse(raw_hand_history)
print(hand.hand_id, hand.board, len(hand.actions))
```

## 仓库结构

```text
src/rivermind_core/      牌谱标准化与领域核心
tests/                   黄金牌谱和单元测试
evals/                   AI 教练合同与对抗性评测语料
benchmarks/              可重复的导入性能基准
solutions/               严格版本化解法目录（默认为空）与 test_only 制品切片
docs/                    产品、架构与决策文档
PROJECT_PLAN.md          完整项目计划
```

交接给下一位开发者或 AI 编程代理时，先阅读 [docs/CLAUDE_HANDOFF.md](docs/CLAUDE_HANDOFF.md)。导入管道的状态约定、存储结构和当前限制见 [docs/IMPORT_PIPELINE.md](docs/IMPORT_PIPELINE.md)，统计口径见 [docs/STATS_ENGINE.md](docs/STATS_ENGINE.md)，结算、Session、手牌查询和回放见 [docs/ACCOUNTING_REPORTS.md](docs/ACCOUNTING_REPORTS.md)，漏洞规则见 [docs/LEAK_ENGINE.md](docs/LEAK_ENGINE.md)，AI 教练证据与校验协议见 [docs/AI_COACH.md](docs/AI_COACH.md)，运行时与离线评测门见 [docs/COACH_RUNTIME_EVALS.md](docs/COACH_RUNTIME_EVALS.md)，可选连接器与专家质量门见 [docs/OPENAI_COACH_ADAPTER.md](docs/OPENAI_COACH_ADAPTER.md)，GTO 节点、目录与匹配边界见 [docs/GTO_MATCHER.md](docs/GTO_MATCHER.md)，策略制品协议与验证规则见 [docs/STRATEGY_ARTIFACTS.md](docs/STRATEGY_ARTIFACTS.md)，求解质量报告与 `verified` 授予流程见 [docs/SOLVE_QUALITY_GATE.md](docs/SOLVE_QUALITY_GATE.md)。

## 产品边界

RiverMind 用于训练、复盘和研究，不提供自动点击、牌桌注入、屏幕读取或隐蔽的实时行动建议。导入牌谱默认不用于模型训练，数据授权将与分析授权分开。
