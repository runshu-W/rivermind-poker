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

项目处于 Phase 0。第一条工程纵向切片已经建立：

- 规范化手牌领域模型；
- 可插拔牌谱解析器接口；
- PokerStars 现金桌文本解析器 v0.1；
- 黄金牌谱测试和 GitHub Actions CI；
- Beta 范围与架构文档。

## 快速开始

需要 Python 3.11+。

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
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
docs/                    产品、架构与决策文档
PROJECT_PLAN.md          完整项目计划
```

## 产品边界

RiverMind 用于训练、复盘和研究，不提供自动点击、牌桌注入、屏幕读取或隐蔽的实时行动建议。导入牌谱默认不用于模型训练，数据授权将与分析授权分开。

