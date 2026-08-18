# TexasSolver 接入指南

> 首批真实解法的操作手册。协议、严格 loader 和质量门都已就绪，这份文档只讲
> 「怎么从 TexasSolver 跑出一个节点，变成仓库里可验证的制品」。

## 为什么是 TexasSolver

[TexasSolver](https://github.com/bupticybee/TexasSolver) 免费、开源、有命令行版本、能把策略导出成 JSON。用它把整条链路跑通不需要任何采购流程。

**许可要注意**：它是 **AGPL v3**。作者的说法是：把发布的**二进制**集成进你的软件可以；如果要集成**源码**，或者**通过互联网提供服务**，需要单独找他谈商业许可。

本地离线跑一次、拿输出文件，属于第一种。等 RiverMind 做成在线产品时这条必须重新评估——这也是为什么求解质量报告里的 `source.redistribution_allowed` 是必填字段。

## 首批节点

用仓库里已经有的那个。`tests/fixtures/pokerstars_cash.txt` 的 `before_action=5`：

> 单挑现金局，双方 100 BB 起。BTN 加注到 3 BB，BB 跟注。
> 翻牌 `2c 7d Ts`，BB 过牌，轮到 BTN。此时底池 6 BB，双方各剩 97 BB。

选它的理由：这个节点的 `GameSpec` 指纹 `85b7db32…` 已经被测试钉死。你在求解器里配一模一样的场景，转换出来的制品会被 Matcher 直接 exact 命中，不用再造新牌谱。

postflop 时 BB 是 OOP、BTN 是 IP。TexasSolver 内部 **player 0 = IP，player 1 = OOP**，树根是 OOP 的第一个决策。所以我们要的节点是「根 → CHECK」之后那个 IP 节点，转换时 `--node-path CHECK`。

## 你必须自己决定的一件事：范围

牌谱只告诉我们「BTN 开到 3 BB，BB 跟注」，**没告诉我们两个人各自拿着什么范围**。求解器必须知道范围才能算。

这是一个建模假设，不是从数据里推出来的，所以它由你决定，并且必须记录下来。三个来源，任选：

1. **你自己的数据库**——用 RiverMind 统计出这两个位置的实际开牌/跟注频率反推。最贴合你的目标人群。
2. **公开的 HU 范围表**——现成，但要写清楚出处。
3. **翻前求解器的输出**——最严谨，但 TexasSolver 只解翻后，需要另找工具。

TexasSolver 接受的范围记法（**不支持 `+`，必须逐个列举**）：

```text
AA            该点数的全部 6 个组合
AKs           同花的 4 个组合
AKo           不同花的 12 个组合
AK            全部 16 个组合
QQ:0.5        权重 0.5；权重 ≤ 0.005 会被求解器直接丢弃
```

下面这行**只是语法示例，不是范围建议**——直接拿去用会得出一份没有依据的解：

```text
AA,KK,AKs,AKo,QQ:0.5,98s:0.75
```

> ⚠️ **一个已知的协议缺口**：`GameSpec` 指纹**不包含输入范围**。同一个节点用两套不同范围求解，会得到两份指纹相同、内容不同的制品。目前的兜底是：如果两份都进了同一个目录，Matcher 会返回 `ambiguous` 并拒绝，失败关闭。真正把范围钉死的是质量报告里的 `solver.config_sha256`——它指向你实际用的那份配置文件。所以**配置文件必须存档**。

## 求解配置

存成 `hu_flop_cbet.txt`。金额单位是 BB，所以导出的下注标签直接就是 BB，正好对上制品的 `size_bb`。

```text
set_pot 6
set_effective_stack 97
set_board 2c,7d,Ts

# ↓↓↓ 换成你自己决定的范围 ↓↓↓
set_range_oop <BB 面对 3BB 开局的跟注范围>
set_range_ip  <BTN 的开局范围>
# ↑↑↑ 换成你自己决定的范围 ↑↑↑

set_bet_sizes oop,flop,bet,33,75
set_bet_sizes oop,flop,raise,60
set_bet_sizes oop,flop,allin
set_bet_sizes ip,flop,bet,33,75
set_bet_sizes ip,flop,raise,60
set_bet_sizes ip,flop,allin
set_bet_sizes oop,turn,bet,50
set_bet_sizes oop,turn,raise,60
set_bet_sizes oop,turn,allin
set_bet_sizes ip,turn,bet,50
set_bet_sizes ip,turn,raise,60
set_bet_sizes ip,turn,allin
set_bet_sizes oop,river,bet,50
set_bet_sizes oop,river,raise,60
set_bet_sizes oop,river,allin
set_bet_sizes ip,river,bet,50
set_bet_sizes ip,river,raise,60
set_bet_sizes ip,river,allin
set_allin_threshold 0.67

build_tree
set_thread_num 8
set_accuracy 0.5
set_max_iteration 200
set_print_interval 10
set_use_isomorphism 1
start_solve

set_dump_rounds 1
dump_result output_result.json
```

几个要点：

- **`set_accuracy 0.5` 的单位是底池百分比。** TexasSolver 源码里 `total_exploitability = exploitable / player_number / initial_pot * 100`，然后和 accuracy 比较。所以质量报告里 `convergence_unit` 填 `percent_of_pot`，`convergence_threshold` 填 `0.5`，`convergence_value` 填求解结束时它实际打印的那个数。策略门的绝对上限是 **1%**，所以 0.5 有余量。
- **`set_dump_rounds 1`** 只导出翻牌，文件小很多。我们只要那一个节点。
- **动作树版本**：改任何一个 `set_bet_sizes`，`action_tree_version` 就必须换新版本号。
- **`set_use_isomorphism 1`** 会启用牌面同构。记得在报告的 `limits` 里说明。

跑：

```powershell
.\console_solver.exe -i hu_flop_cbet.txt
```

## 从导出到制品

四步，全部有 CLI。

```powershell
$env:PYTHONPATH = "src"

# 1. 把节点登记进目录（哈希先占位，此时验证一定失败——这是对的）
python -m rivermind_core gto-catalog-add pokerstars 100000000001 `
  --before-action 5 `
  --database data/dev.db `
  --catalog solutions/catalog.json `
  --rake-model pokerstars.cash.example --rake-percent 5 --rake-cap-bb 3 `
  --solution-id texassolver.hu-cash.btn-flop-cbet `
  --solver-name TexasSolver --solver-version 0.2.0 `
  --action-tree-version rivermind.hu-flop-cbet/0.0.1 `
  --artifact-id strategy/btn_flop_cbet.json `
  --quality experimental

# 2. 把 dump 转成草稿制品
python -m rivermind_core gto-import-texassolver output_result.json `
  --catalog solutions/catalog.json `
  --solution-id texassolver.hu-cash.btn-flop-cbet `
  --node-path CHECK `
  --node-id cash.hu.100bb.flop.btn-cbet-vs-bb-check `
  --solver-version 0.2.0 `
  --solver-config-id hu-flop-cbet-v1 `
  --generated-at 2026-08-18T00:00:00Z `
  --license "TexasSolver AGPL v3 binary output; internal use only." `
  --range "<你在 set_range_ip 里用的那一行，一字不差>" `
  --out draft.json

# 3. 规范化、写入并登记真实哈希
python -m rivermind_core gto-artifact-package draft.json `
  --catalog solutions/catalog.json --write --update-catalog

# 4. 验证
python -m rivermind_core gto-artifact-verify solutions/catalog.json `
  texassolver.hu-cash.btn-flop-cbet
```

第 2 步的 `--range` 必须和 `set_range_ip` **完全一致**：转换器用它给每个组合补权重，只要有一个组合对不上就会报错退出。这是故意的——权重对不上说明你贴错了范围。

不想传范围的话可以用 `--assume-uniform-weights`，全部按权重 1 处理。但那样必须在质量报告的 `limits` 里写明「输入范围的分数权重未被保留」。

## 转换器做了什么

| 问题 | 处理 |
|---|---|
| 组合拼写 | TexasSolver 写 `AsAh`，协议要 `AhAs`（点数降序 + `c<d<h<s`），全部重新规范化 |
| 科学计数法 | `6.5e-11` 这类值取整到 6 位小数 |
| 概率和不为 1 | 取整后把残差加到最大的那个动作上，确定性规则，结果精确等于 1 |
| 全下 | TexasSolver 把全下写成 `BET <全部筹码>`；尺度等于该位置剩余筹码时标记为 `all_in` |
| 权重 | 从你提供的范围字符串解析；不提供就必须显式声明按 1 处理 |
| EV | **没有**。`dump_result` 只导策略，EV 只能从 GUI/API 拿。所以首批制品的 `ev` 全是 `null` |

转换器**永远不会写 `verified`**。把标签升到 `verified` 是一次刻意的手工编辑，然后才是质量门。

## 质量报告里必须写进 limits 的话

这些是这次求解客观上没做到的事，不写进去，报告就不算证据：

- 只有一个翻牌节点，其他牌面和街道什么都没证明；
- 输入范围是假设，不是从牌谱推导的（写清楚出处）；
- 制品不含 EV，因为 TexasSolver 的 `dump_result` 只导策略；
- 启用了牌面同构（`set_use_isomorphism 1`）；
- 下注尺度被抽象成了固定的几档，不是连续尺度；
- 如果用了 `--assume-uniform-weights`：输入范围的分数权重未被保留。

## 走到质量门之前还差什么

制品验证通过只是第一层。要让 `usable_for_teaching` 变成 `true`，还需要：

1. 把制品和目录条目的标签手工改成 `verified`（`gto-artifact-package` 之后再改，然后重新 package 一次登记新哈希）；
2. 写一份 `solve-quality-report/1.0.0`；
3. 找那位独立复核人签字，出一份 `quality-attestation/1.0.0`；
4. `gto-quality-verify` 通过。

细则见 [SOLVE_QUALITY_GATE.md](SOLVE_QUALITY_GATE.md)。在那之前，这份制品的标签是 `experimental`，查询会返回频率但 `teaching_block_reason = "quality_not_verified"`。

**这是对的。** 一次未经复核的求解，本来就不该直接拿去教人。
