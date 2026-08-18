# GGPoker 解析器 v0.1

> ⚠️ **这个解析器还没有被真实导出验证过。**
> 它是照 GGPoker 公开的行格式，以及各家追踪软件支持论坛里贴出的真实牌谱片段写的，
> 仓库里的 fixture 是**据此重建**的，不是从真实账号导出的文件。
> 上线前请务必拿一份真实的 PokerCraft 导出跑一遍 `rivermind import`，
> 确认没有东西因为错误的原因落进 `failed` 或 `unsupported`。

## 和 PokerStars 的差异

GGPoker 的导出几乎照抄了 PokerStars 的行格式，所以座位、盲注、行动、返还、摊牌这些语法是**逐字共用**的（见 `parsers/_common.py`）。一份定义意味着两个站点不会各自漂移。真正不同的地方在这里：

| | PokerStars | GGPoker |
|---|---|---|
| 手牌头 | `PokerStars Hand #12345` | `Poker Hand #RC837124540`（带字母前缀） |
| 盲注 | `($0.05/$0.10 USD)` | `($0.1/$0.25)`——不补零，也没有货币代码 |
| 摊牌标记 | `*** SHOW DOWN ***` | `*** SHOWDOWN ***`——**没有空格** |
| 摘要行 | `\| Rake $0.75` | `\| Rake $0.75 \| Jackpot $0.37 \| Bingo $0` |
| 玩家名 | 真实昵称 | 匿名 ID，Hero 固定叫 `Hero` |

手牌 ID 的字母前缀是有含义的：`RC` = Rush & Cash、`HD` = 常规桌、`OM` = 奥马哈、`TM` = 锦标赛。前缀是 ID 的一部分，原样保留。

## 明确拒绝，而不是勉强解析

GGPoker 有几个功能在规范化模型里没有对应物。**每一个都会被检测并拒绝**，不做近似：

| 功能 | 拒绝码 | 为什么不能勉强 |
|---|---|---|
| 跑两次 / 三次<br>`*** FIRST FLOP ***`、`Hand was run two times` | `ggpoker_run_it_multiple_times` | 模型只有一个牌面和一份结算。悄悄取第一个牌面，等于报出一个玩家从没拿到过的结果 |
| EV Cashout<br>`Hero: Chooses to EV Cashout` | `ggpoker_ev_cashout` | 钱在底池之外结算，筹码账本不守恒 |
| Cash Drop<br>`Cash Drop to Pot : total $5` | `ggpoker_cash_drop` | 凭空多出一笔没人投入的钱 |
| 赏金 | `ggpoker_bounty` | 赏金在底池之外结算 |
| 非德州<br>`Omaha Pot Limit`、四张底牌 | `ggpoker_unsupported_variant` | 模型只覆盖德州 |
| 锦标赛 | `ggpoker_tournament_unsupported` | 锦标赛头部的买入、级别和赏金字段**没有经过真实导出验证**，宁可拒绝也不猜 |

这些都是**逐手**隔离的：一个文件里有一手跑了两次，只有那一手被标成 `unsupported`，其余照常导入。

## 一个必须知道的限制：rake 不等于抽成

GGPoker 在 Rake 之外还扣 Jackpot 和 Bingo。规范化模型只有一个 `rake` 字段，存的是 **Rake 那一列**。

所以在 fixture 那一手里：

```text
Total pot $3.6 | Rake $0.15 | Jackpot $0.05 | Bingo $0
hand.rake                    = 0.15
玩家净额之和（房间实际拿走）  = 0.20
差额（jackpot）              = 0.05
```

**筹码账本本身是对的**——净额由实际的投入/返还/收池算出，和房间怎么给自己那份钱贴标签无关。有问题的是「用 `hand.rake` 当作现金局成本」这个用法：它会低估。

对 GTO 层尤其重要：`RakeSpec` 描述的是牌室的抽水规则，接入 GGPoker 数据时必须单独考虑 jackpot drop，否则 EV 会偏高。

`tests/test_ggpoker_parser.py::test_the_rake_field_understates_what_the_house_took` 把这条限制钉成了可执行的形式。

## 匿名对手意味着什么

GGPoker 的对手名是匿名 ID（Rush & Cash 里每手都换）。后果：

- **Hero 的统计正常**——默认统计口径就是 Hero，`Dealt to Hero` 永远在；
- **对手统计没有意义**——同一个人在不同手里是不同 ID，`player_hand_stats` 里的非 Hero 行无法跨手聚合；
- 未来的对手建模在 GGPoker 数据上不可用，除非只在单桌会话内做。

## 多手文件的切分

切分手牌边界的前缀现在**由解析器自己声明**：

```python
class GGPokerCashParser(HandHistoryParser):
    header_prefix = "Poker Hand #"
```

`ParserRegistry.header_prefixes()` 把注册过的站点前缀收集起来（长的优先，避免短前缀吞掉长的），导入器用它切分。注册一个新解析器就够了，不用再改导入器——这条以前是写死 `PokerStars Hand #` 的，GGPoker 的多手文件会被当成一整块。

## 下一步

1. **拿真实导出验证。** 这是唯一能把上面那句警告去掉的办法。
2. 补锦标赛解析器——需要一份真实的 GG 锦标赛牌谱样例。
3. 决定 jackpot 要不要进模型（新增字段会影响 schema、存储和序列化）。
4. 跑两次的支持需要模型层支持多牌面与分池结算，是独立的一块工作。
