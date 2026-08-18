# RiverMind 确定性统计引擎 v0.2

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
| Call Open | 玩家首次决策面对恰好一次 Raise，并选择 Call | 玩家首次翻牌前决策面对恰好一次 Raise |
| Cold Call | 非盲注位玩家首次决策面对一次 Raise，并选择 Call | 非盲注位玩家首次翻牌前决策面对恰好一次 Raise |
| Fold to 3Bet | 首次加注者面对随后 3Bet 并 Fold | 首次加注者被另一位玩家 3Bet，且获得后续决策 |
| Flop CBet | 最后一个翻前加注者在无人领先下注时 Flop Bet | 最后一个翻前加注者在 Flop 首次行动前无人 Bet/Raise |
| Fold to Flop CBet | 防守者面对 Flop CBet 并 Fold | 活跃防守者面对 Flop CBet 并获得 Fold/Call/Raise 决策 |

盲注和 ante 不计入 VPIP。RFI 与 3Bet 使用机会分母，不能直接除以总手数。

## 位置与筹码维度

按有座玩家围绕按钮顺时针分配位置。6 人桌为 `SB / BB / UTG / HJ / CO / BTN`；7–10 人桌逐步加入 `UTG+1`、`UTG+2`、`UTG+3` 和 `LJ`；单挑按钮位记录为 `BTN`，另一位为 `BB`。

- `starting_stack_bb`：起始筹码除以大盲；
- `effective_stack_bb`：该玩家与桌上最大对手可玩的筹码上限除以大盲。

后者适合牌局级粗筛；具体行动节点仍需按仍在底池中的对手重新计算有效筹码。

## 宽表与过滤 API

每次成功导入都会在同一事务内为每位玩家写入一条 `player_hand_stats`。它保存赛制、位置、筹码维度，以及每个指标的发生位和机会位。旧 schema v1 数据库首次打开时自动回填。

```python
from decimal import Decimal

from rivermind_core import PlayerPosition, StatsFilter
from rivermind_core.storage import SQLiteHandStore

with SQLiteHandStore("data/rivermind.db") as store:
    result = store.query_player_stats(
        heroes_only=True,
        stat_filter=StatsFilter(
            positions=frozenset({PlayerPosition.UNDER_THE_GUN}),
            min_effective_stack_bb=Decimal("40"),
            max_effective_stack_bb=Decimal("80"),
        ),
    )
```

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

# 位置与有效筹码过滤
python -m rivermind_core stats --database data/rivermind.db `
  --position UTG HJ --min-effective-stack-bb 40 --max-effective-stack-bb 80
```

## 当前限制

当前不包含按具体对手/底池重新计算的节点有效筹码、Turn/River CBet、Check-Raise、WTSD、W$SD、盈利和 All-in EV。宽表仍是 SQLite 物理实现；在 100k/1M 手牌基准完成前，不把它视为最终列式分析模型。

当前开发环境从宽表聚合合成 10,000 手牌的冒烟基准为 0.183 秒，约 54,717 手/秒。它只用于发现性能回退，不代表真实玩家池查询延迟。
