# GTO Matcher v0.1

## 目标

GTO Matcher 把数据库中的一手真实牌谱收紧为一个可复现的**决策前节点**，再与版本化解法目录比较。v0.1 只负责元数据匹配，不读取策略文件，也不输出或猜测动作频率、EV、范围或“最佳行动”。

三个对外状态：

| 状态 | 含义 |
|---|---|
| `exact` | `GameSpec` 的规范化 SHA-256 指纹完全相同，且唯一命中 |
| `approximate` | 所有硬维度相同，数值差异均在公开阈值内，且最近候选唯一 |
| `unsupported` | 目录为空、关键元数据缺失、硬维度不同、阈值越界或最佳候选并列 |

`solution_reference_available=true` 仅表示命中了一个带制品标识和哈希的 `SolutionSpec`。它不代表 v0.1 已加载或验证制品内容。

## 版本化契约

- `game-spec/1.0.0`：赛制、人数、盲注/ante、目标函数、抽水或赛事上下文，以及决策节点。
- `solution-spec/1.0.0`：`GameSpec`、求解器和动作树版本、质量标签、制品 ID 与 SHA-256。
- `solution-catalog/1.0.0`：唯一目录 ID、版本和解法列表。
- `gto-match-policy/1.0.0`：硬匹配维度、数值阈值和候选选择规则。

目录加载是严格的：缺字段、多字段、未知版本、非法 Decimal、重复解法 ID 或非法哈希都会失败。仓库内 [solutions/catalog.json](../solutions/catalog.json) 故意为空；当前没有把测试 fixture 宣称成真实、已验证的 GTO 解法。

## 决策节点提取

CLI 的 `--before-action N` 指向牌谱中尚未执行的自愿行动。提取器只使用序号 `< N` 的信息：

- 行动者姓名转换为规范位置，输出不包含玩家名或牌谱 ID；
- 筹码按此前投入和返还计算成剩余 BB；
- 公共牌只暴露当前街可见部分；
- 保留仍在手牌中的位置、底池 BB、盲注、ante 和规范化动作线；
- 目标行动本身不进入节点，防止答案泄漏；
- `post blind/ante`、`return`、`collect`、`show` 等非决策动作不能作为目标。

现金牌谱无法证明牌室的完整抽水规则。因此现金匹配必须由调用方同时提供 rake model ID、百分比和 cap，不能用本手最终 rake 倒推。MTT 的 `icm`/`pko` 必须提供独立的 `tournament_context_id`；单手牌的桌上筹码不足以表达奖金、全场筹码和 bounty 状态。

## 匹配规则

以下维度必须完全相同，不做启发式映射：

- 赛制、人数、目标函数、赛事上下文；
- 街道、完整可见牌面、轮到的位置、仍在手牌的位置；
- 每个筹码/ante 对应的位置；
- 完整规范动作线；
- 现金抽水 model ID。

v0.1 的默认数值阈值：

| 字段 | 最大绝对差 |
|---|---:|
| 每个位置剩余筹码 | 5 BB |
| 底池 | 1 BB |
| 小盲 | 0.1 BB |
| 大盲 | 0 BB |
| 每个位置 ante | 0.1 BB |
| 抽水百分比 | 0.5 个百分点 |
| 抽水 cap | 0.5 BB |

近似结果会列出每个非零差异、实际值、解法值、绝对差和阈值。超过任一阈值即拒绝。多个候选具有相同最小归一化距离时也拒绝，避免用 solution ID 顺序悄悄决定策略来源。

## CLI

先导入牌谱，再定位回放中的决策序号：

```powershell
$env:PYTHONPATH = "src"
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

使用仓库默认空目录时会返回 `unsupported/catalog_empty`。这是预期行为，不是求解器结果。

## v0.1 限制与下一阶段

- 公共牌与动作线尚未做经过验证的同构/动作翻译；不同即拒绝。
- 未校验策略制品的内部 schema、概率和 EV 一致性。
- 未生产现金或 MTT 的真实解法包。
- 未将 Leak Card 的证据手牌自动路由到决策节点。
- 未做策略矩阵、训练题或 EV loss。

下一阶段应先定义并验证策略制品协议，导入一小批可追溯解法；随后才能让 Matcher 的制品引用进入 Study/Practice，而不是由 LLM 补齐缺失数值。
