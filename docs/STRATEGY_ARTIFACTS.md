# Strategy Artifact v0.1

## 目标

GTO Matcher v0.1 只做元数据匹配：命中一个 `SolutionSpec`，说明"这个节点在目录里有登记"，不代表策略内容存在、可读或可信。

Strategy Artifact v0.1 补上后半段。它把 `SolutionSpec.artifact_id` 和 `artifact_sha256` 变成一个**可定位、可校验、可拒绝**的工程制品：

```text
SolutionSpec
  → 在沙箱内定位 artifact 文件
  → 校验文件 SHA-256
  → 校验 solution / GameSpec 指纹 / 动作树 / 求解器 / 质量标签一致
  → 校验动作、私牌组合、概率、EV 单位与语义
  → 校验 provenance 与质量门
  → 产生只读 StrategyEvidence
```

只有整条链全部通过，`gto-query` 才会返回频率和 EV。任何一步失败都返回"没有策略"，不返回部分内容，也不猜测缺失数值。

## 版本化契约

| 版本 | 含义 |
|---|---|
| `strategy-artifact/1.0.0` | 单节点策略切片的文件格式、数值精度与质量字段 |
| `strategy-evidence/1.0.0` | 交给 Study / Practice / AI Coach 的只读事实结构 |
| `strategy-aggregation/1.0.0` | 在制品自带组合上做加权汇总的派生视图 |

三个版本互相独立。改动任何一个的语义都必须发新版本，不能静默修改旧版本行为。

## 冻结的设计决策

v0.1 明确冻结了交接文稿要求先回答的问题：

| 问题 | v0.1 结论 |
|---|---|
| `artifact_id` 如何映射文件 | catalog 文件所在目录为根，`artifact_id` 是该目录下的相对 POSIX 路径；不支持远程 URI 和独立 registry |
| 序列化格式 | 严格 JSON、UTF-8；Parquet/二进制矩阵留给后续版本评估 |
| 一个 artifact 的范围 | **单节点纵向切片**，不是一棵动作树；降低验证面 |
| 私牌粒度 | **1,326 个具体组合**（如 `AhKd`）；翻后可正确处理公共牌冲突 |
| action 绑定 | 节点显式声明 action 列表，每个 action 有唯一 `action_id`、`kind` 和 `size_bb`；组合层只能引用已声明的 `action_id`，不接受自由文本 |
| 数值精度与容差 | 概率、EV、权重和下注尺度最多 6 位小数、总计最多 18 位数字；单个组合概率之和与 1 的偏差不超过 `0.00001` |
| EV 语义与单位 | `ev_semantics = "action_ev_from_node"`、`ev_unit = "bb"`，两者必填；chips 和奖金价值需要新 schema 版本 |
| provenance | 求解器名称、版本、配置 ID、生成时间、质量标签、质量报告 ID 和许可证全部必填（质量报告 ID 可为 `null`，但 `verified` 时不可） |
| 谁能授予 `verified` | 只能由外部质量门写入 artifact 与目录，且两侧必须一致；loader **永远不会**升级质量标签。授予本身还需要一份签署的 `quality-attestation/1.0.0`，见 [SOLVE_QUALITY_GATE.md](SOLVE_QUALITY_GATE.md) |

## 文件结构

```json
{
  "schema_version": "strategy-artifact/1.0.0",
  "solution_id": "test-only.pokerstars-cash.btn-flop-cbet",
  "game_spec_fingerprint": "85b7db32...",
  "action_tree_version": "rivermind.test-only.flop-cbet/0.0.1",
  "node_id": "cash.hu.100bb.flop.btn-cbet-vs-bb-check",
  "ev_unit": "bb",
  "ev_semantics": "action_ev_from_node",
  "actions": [
    {"action_id": "bet_2bb", "kind": "bet", "size_bb": "2"},
    {"action_id": "check", "kind": "check", "size_bb": null}
  ],
  "entries": [
    {
      "combo": "AhKh",
      "weight": "1",
      "policies": [
        {"action_id": "bet_2bb", "probability": "0.5", "ev": "2.5"},
        {"action_id": "check", "probability": "0.5", "ev": "2.2"}
      ]
    }
  ],
  "provenance": {
    "solver_name": "...",
    "solver_version": "...",
    "solver_config_id": "...",
    "generated_at": "2026-08-18T00:00:00Z",
    "quality": "test_only",
    "quality_report_id": null,
    "license": "..."
  }
}
```

约定：

- 所有数值是**十进制字符串**，不是 JSON number。这样 `NaN`、`Infinity` 和浮点漂移在类型层就不可能出现。
- 数值同时受**位数**限制（总计 ≤ 18 位数字）。没有这条限制，一个 40 位的 EV 会在 Python 默认 28 位十进制上下文里被静默取整，制品报出来的数字就不再是文件里的数字。
- 概率、权重和下注尺度不接受负号，`-0` 也会被拒绝。
- 结构上限：一个节点最多 64 个 action、最多 1,326 个组合（德州扑克起手组合的理论上限）。
- JSON 对象不允许重复键。重复键在 `json.loads` 里会"后者胜出"，等于同一份字节有两种读法。
- artifact **不包含自己的最终哈希**。完整性由 `SolutionSpec.artifact_sha256` 持有。
- `actions` 按 `action_id` 升序，`entries` 按 `combo` 升序，`policies` 按 `action_id` 升序。唯一排列意味着唯一表示。
- 组合的规范拼写是"按点数降序，再按 `c < d < h < s` 花色升序"，例如 `AhKh`、`8c8d`。`KhAh` 会被拒绝。
- 每个组合必须为**每一个**已声明 action 给出恰好一条 policy。
- EV 要么在整个 artifact 的每条 policy 上都有，要么全为 `null`。不允许部分声明。

## 验证器必须拒绝的情况

以下每一条都有对应的失败测试：

**完整性与身份**

- 文件 SHA-256 与 `SolutionSpec.artifact_sha256` 不一致（包括哈希后被编辑）；
- `solution_id`、`game_spec_fingerprint`、`action_tree_version` 与目录条目不一致；
- 求解器名称或版本与目录条目不一致；
- artifact 质量标签与目录条目不一致（loader 不做升级，也不做降级）。

**Schema**

- 未知 `schema_version`；
- 顶层、`action`、`entry`、`policy`、`provenance` 出现缺失键或未知键；
- 同一 JSON 对象出现重复键；
- JSON 嵌套过深（`RecursionError` 也按验证失败处理，不是崩溃）；
- 字符串包含控制字符、C1 控制字符或孤立代理码点（`\ud800` 这类转义在 UTF-8 文件里合法，但无法再编码回 UTF-8）；
- 未知 `ev_unit`、`ev_semantics` 或 action `kind`；
- `generated_at` 不是 RFC3339 UTC 时间戳。

**动作**

- 决策节点少于两个 action，或超过 64 个 action；
- `action_id` 重复或未按升序排列；
- `bet`/`raise`/`all_in` 缺 `size_bb` 或尺度非正；`fold`/`check`/`call` 反而声明了 `size_bb`；
- `fold`/`check`/`call`/`all_in` 重复声明；
- 同一 kind 出现重复尺度。

**私牌组合**

- 非法牌码、重复同一张牌、长度不对；
- 非规范拼写；
- 组合与 `GameSpec` 公共牌冲突（blocker）；
- `entries` 为空、组合重复、未排序或超过 1,326 个；
- 权重不在 `(0, 1]`。

**概率与 EV**

- 引用未声明的 action、重复 action、遗漏已声明 action、policy 未排序；
- 概率不是十进制字符串（含 JSON `NaN` 字面量和 `"NaN"`、`"Infinity"`、`"1e3"`）；
- 概率或权重为负（含 `-0`）；
- 概率超出 `[0, 1]`；
- 单个组合概率之和超出版本化容差；
- 小数位超过 6 位，或总位数超过 18 位；
- EV 只在部分 action 或部分组合上声明。

**来源与质量**

- `provenance` 任一字段缺失；
- `quality = "verified"` 但没有 `quality_report_id`；
- 位于 `fixtures/` 目录下的 artifact 声称 `verified`（测试数据不能冒充已验证解法）。

**路径**

- 绝对路径、盘符、`..` 段、反斜杠、非 `.json` 后缀、非法路径字符；
- 路径经过符号链接；
- 解析结果落在 catalog 目录之外；
- 文件不存在或超过 8 MiB 上限。

已知的沙箱边界：**硬链接不会被拒绝**，符号链接检查与实际读取之间也存在 TOCTOU 窗口。两者都需要攻击者已经能写入 catalog 目录；即便如此，内容仍然被目录里的 SHA-256 钉死，所以拿不到额外能力。真正的防线是不要把不受信任的目录当作 catalog 根。

## StrategyEvidence

验证通过后，`build_strategy_evidence` 产生只读事实。两种 scope：

- `combo`：直接引用制品中该组合的原始概率与 EV，不做任何计算；
- `artifact_entries`：对制品自带组合做 `strategy-aggregation/1.0.0` 加权汇总。

汇总在 96 位精度的显式十进制上下文里计算，避免中间乘积被默认上下文静默取整。公式如下（结果按 6 位小数 ROUND_HALF_EVEN 独立取整，因此各动作概率之和可能与 1 相差最后一位）：

```text
probability(a) = Σ_e w_e · p_ea / Σ_e w_e
ev(a)          = Σ_e w_e · p_ea · ev_ea / Σ_e w_e · p_ea      # 分母为 0 时为 null
```

关键边界：

- 汇总只覆盖**制品里存在的组合**，不是该节点的完整范围。`coverage` 字段会明说这一点。
- `usable_for_teaching` 需要**两个**条件同时成立：标签是 `verified`，并且提供了通过质量门的签署。只满足前者时 `teaching_block_reason = "no_quality_attestation"`。授予流程见 [SOLVE_QUALITY_GATE.md](SOLVE_QUALITY_GATE.md)。当前仓库既没有 `verified` 制品也没有任何签署，所以它恒为 `false`。
- AI Coach 可以引用这些事实，但永远不能生成它们。

## CLI

```powershell
$env:PYTHONPATH = "src"

# 单独验证一个目录条目的策略制品
python -m rivermind_core gto-artifact-verify `
  solutions/catalog.test_only.json `
  test-only.pokerstars-cash.btn-flop-cbet --json

# 从真实牌谱节点走完 match → verify → query
python -m rivermind_core gto-query pokerstars 100000000001 `
  --before-action 5 `
  --database data/dev.db `
  --catalog solutions/catalog.test_only.json `
  --rake-model pokerstars.cash.example `
  --rake-percent 5 `
  --rake-cap-bb 3 `
  --combo AhKh `
  --json
```

`gto-query` 的返回边界：

| 情况 | `strategy_available` | 退出码 |
|---|---|---:|
| exact 命中且制品验证通过 | `true` | 0 |
| approximate 命中 | `false`（`match_not_exact`） | 0 |
| unsupported（目录空、缺 rake、硬维度不同、阈值越界、候选并列） | `false` | 0 |
| 组合合法但制品未覆盖 | `false`（`combo_not_covered`） | 0 |
| 制品验证失败（哈希、身份、内容） | `false`（`artifact_verification_failed`） | 2 |
| 组合拼写非法或参数错误 | `false` | 2 |

v0.1 **只有 exact 命中才返回策略**。approximate 命中意味着节点并不完全相同，把另一个节点的精确频率贴上来是错的；放宽这条需要单独版本化的 bet-size translation 协议。

制品验证失败刻意用退出码 2：哈希不符是完整性事件，不能和"这个节点暂时没有解法"混为一谈。

## 仓库现状

- `solutions/catalog.json` 仍然**故意为空**。仓库没有任何真实 GTO 频率、EV、范围或已验证策略制品。
- `solutions/catalog.test_only.json` + `solutions/fixtures/btn_flop_cbet.test_only.json` 是**手写的** 4 组合 × 3 动作纵向切片，质量标签为 `test_only`，求解器记为 `rivermind.handwritten`。
- 这个切片只证明"牌谱 → 节点 → 目录 → 制品 → 只读事实"这条链路可用。它**不代表任何下注频率或 EV**，也不能用于训练或教学。
- 自动化测试会扫描 `solutions/` 下所有 JSON，确认没有任何文件声称 `verified`。
- `.gitattributes` 把 `solutions/**/*.json` 和 `tests/fixtures/**` 标记为 `-text`，防止 checkout 改写行尾导致 SHA-256 失配。

## v0.1 限制与下一阶段

- 没有求解器，也没有真实策略来源；
- 一个 artifact 只表达一个节点，没有动作树遍历和街道推进；
- 没有牌面同构（suit isomorphism）和 bet-size translation，因此只有 exact 命中可用；
- 目录仍是线性扫描，尚未按 `GameSpec` 指纹和硬维度建索引；
- 没有策略矩阵、节点导航、训练题和 EV loss；
- Leak Card 尚未自动路由到"最值得送入 Matcher 的那个决策"；
- `ExplanationEvidence` 尚未接入 `StrategyEvidence`；接入前需要先离线评测与专家盲审。

`verified` 的授予流程、求解质量报告协议和签署规则已经在 [SOLVE_QUALITY_GATE.md](SOLVE_QUALITY_GATE.md) 中冻结。下一步是**接入一小批来源与许可明确的真实解法**，让第一份制品真正通过那道门，而不是先做漂亮的策略矩阵界面。
