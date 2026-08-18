# RiverMind 结算、Session 与回放 v0.1

## 确定性筹码账本

每个规范化动作都会生成账本增量：

- blind、ante、Call、Bet：按 `amount` 增加投入；
- Raise：按 `to_amount - 本街已投入` 计算实际新增投入；
- Return：返还玩家并从当前底池扣除；
- Collect：计入玩家收池。

ante 不计入本街当前下注，因此不会扭曲 Raise 的实际投入。每手牌同时校验：

```text
总投入 - 总返还 = Total pot
所有玩家净结果 + Rake = 0
```

Cash 的 `net_result` 使用牌谱货币；MTT 的 `net_result` 只表示筹码变化，不冒充奖金、ROI 或现金盈利。两者都提供 `net_result_bb`。

## Session 口径

- Cash：同一玩家、站点和货币下，相邻手牌间隔不超过 30 分钟归为同一 Session；间隔可通过 CLI 修改；
- MTT：同一站点与 `tournament_id` 归为同一 Session，即使中途换桌；
- 无法解析时间的 Cash 手牌单独成 Session，避免猜测；
- MTT Session 结果仍是已导入手牌的筹码变化，不代表完整比赛盈亏。

## 相关手牌查询

`HandQuery` 支持：

- 玩家或 Hero；
- Cash/MTT、位置、有效筹码；
- 九项指标的机会、发生或未发生；
- ISO 起止时间；
- `limit/offset` 分页。

```powershell
python -m rivermind_core hands --database data/rivermind.db `
  --metric fold_to_three_bet --missed --position UTG HJ --limit 50 --json
```

每条结果携带投入、返还、收池、净结果和各指标证据位，可以直接成为“漏洞 → 相关手牌”接口。

## 回放接口

```powershell
python -m rivermind_core replay pokerstars 100000000001 `
  --database data/rivermind.db --json
```

回放帧包含街道、当时可见牌面、玩家、动作、实际新增投入、返还、收池和行动后底池。已知私牌来自离线牌谱复盘，不用于实时决策。

## 第一个分析页面

```powershell
python -m rivermind_core report --database data/rivermind.db `
  --output data/rivermind-report.html
```

页面使用单文件 HTML，无外部脚本或网络请求，包含核心统计、Session 和最近手牌。所有用户文本经过 HTML 转义；页面只读本地导出结果。

## 当前限制

尚未实现 All-in EV、奖金/买入 ROI、玩家别名合并、标签、收藏和手牌备注。Session 时间目前保留 PokerStars 牌谱中的本地墙上时间；跨时区统一将在多平台导入阶段处理。
