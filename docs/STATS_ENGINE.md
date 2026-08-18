# RiverMind 确定性统计引擎 v0.1

## 原则

统计只读取经过校验的 `HandHistory`，由确定性代码计算。每个指标保留：

- `occurrences`：指标发生次数；
- `opportunities`：指标机会次数；
- `percentage`：前两者相除，分母为零时返回 `null`。

LLM 后续只能读取这些证据字段用于解释，不能重算或改写统计结果。

## 当前指标口径

| 指标 | 发生条件 | 机会条件 |
|---|---|---|
| VPIP | 玩家翻牌前至少一次主动 Call、Bet 或 Raise | 玩家在该手有座位 |
| PFR | 玩家翻牌前至少一次 Raise | 玩家在该手有座位 |
| RFI | 无人主动入池时，玩家首次决策为 Raise | 玩家首次翻牌前决策时尚无人主动入池 |
| 3Bet | 玩家首次决策面对恰好一次 Raise，并选择 Raise | 玩家首次翻牌前决策面对恰好一次 Raise |

盲注和 ante 不计入 VPIP。RFI 与 3Bet 使用机会分母，不能直接除以总手数。

## 使用

```powershell
$env:PYTHONPATH = "src"

# 统计数据库中被标记为 Hero 的玩家
python -m rivermind_core stats --database data/rivermind.db

# 指定玩家，输出证据 JSON
python -m rivermind_core stats --database data/rivermind.db --player Hero --json

# 只看 MTT，或输出全部观察到的玩家
python -m rivermind_core stats --database data/rivermind.db --game-type tournament
python -m rivermind_core stats --database data/rivermind.db --all-players
```

## 当前限制

这是口径优先的首版，不包含位置拆分、有效筹码、玩家池过滤、Fold to 3Bet、CBet 和派生宽表。CLI 当前流式读取 SQLite 中的规范化 JSON；在 100k/1M 手牌基准完成前，不把它视为最终分析物理模型。

当前开发环境的合成 10,000 手牌冒烟基准为 0.641 秒，约 15,608 手/秒。它只用于发现性能回退，不代表真实玩家池查询延迟。
