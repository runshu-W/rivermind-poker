# RiverMind Poker

RiverMind 是一个以 **H2N-lite 牌谱分析作为入口、GTO 策略系统作为长期壁垒、AI 教练作为用户体验层** 的德州扑克学习产品。

## 产品路线

```text
牌谱导入 → 统计与漏洞 → AI 解释 → GTO 节点匹配 → 针对性训练
```

- Beta：离线牌谱导入、标准化、Session 报告、固定核心统计、手牌回放、漏洞识别和证据约束的 AI 解释。
- 下一阶段：把高频错误映射到经过验证的 GTO 解法，形成 Study → Practice 闭环。
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
- 41 项自动化测试、黄金集清单和可重复的批量导入基准脚本；
- Beta 范围与架构文档。

## 快速开始

需要 Python 3.11+。

```powershell
$env:PYTHONPATH = "src"
python -m rivermind_core import tests/fixtures --database data/dev.db

# 默认统计牌谱中标记的 Hero；也可使用 --player 指定玩家
python -m rivermind_core stats --database data/dev.db

# Session、Leak Cards、相关手牌和结构化回放
python -m rivermind_core sessions --database data/dev.db
python -m rivermind_core leaks --database data/dev.db --json
python -m rivermind_core hands --database data/dev.db --metric flop_cbet --occurred
python -m rivermind_core replay pokerstars 100000000001 --database data/dev.db --json

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
benchmarks/              可重复的导入性能基准
docs/                    产品、架构与决策文档
PROJECT_PLAN.md          完整项目计划
```

导入管道的状态约定、存储结构和当前限制见 [docs/IMPORT_PIPELINE.md](docs/IMPORT_PIPELINE.md)，统计口径见 [docs/STATS_ENGINE.md](docs/STATS_ENGINE.md)，结算、Session、手牌查询和回放见 [docs/ACCOUNTING_REPORTS.md](docs/ACCOUNTING_REPORTS.md)，漏洞规则和证据协议见 [docs/LEAK_ENGINE.md](docs/LEAK_ENGINE.md)。

## 产品边界

RiverMind 用于训练、复盘和研究，不提供自动点击、牌桌注入、屏幕读取或隐蔽的实时行动建议。导入牌谱默认不用于模型训练，数据授权将与分析授权分开。
