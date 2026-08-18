# Solve Quality Gate v0.1

## 为什么需要它

Strategy Artifact v0.1 让策略内容变成可验证的工程制品：定位、哈希、身份、动作、组合、概率、EV、来源全部检查通过，才返回只读事实。

但"这些字节就是我们预期的字节"和"这份策略好到可以教人"是两个不同的断言。前者由 loader 负责，它**故意没有**升级质量标签的能力。后者需要证据和人签字，由这里的质量门负责。

```text
QualityAttestation（签署的授予）
  → 在沙箱内定位并加载 SolveQualityReport，按字节校验 SHA-256
  → 用普通 loader 完整验证 StrategyArtifact
  → 报告 / 制品 / 目录条目三方身份一致
  → 收敛达到报告自己声明的门槛，且不超过策略层绝对上限
  → 许可允许展示
  → 求解范围与节点相符（rake 模型、claim_class）
  → 至少两名签署人，其中至少一名独立复核
  → 时间顺序自洽，且授予不在未来
  → 才允许 usable_for_teaching = true
```

## 版本化契约

| 版本 | 含义 |
|---|---|
| `solve-quality-report/1.0.0` | 来源与许可、求解配置、收敛证据、评估范围、显式限制 |
| `quality-attestation/1.0.0` | 把 `verified` 授予给一组确切字节的签署文件 |
| `quality-gate-policy/1.0.0` | 门本身执行的规则：签署人数、绝对收敛上限、时间约束 |

三份文件互相独立、分别哈希。制品说"策略是什么"，报告说"它怎么来的、有多好"，签署说"谁为此负责"。一份文件不能既是被评判者又是评判依据，所以报告不放进制品里。

## 冻结的设计决策

| 交接文稿 §9.2 的问题 | v0.1 结论 |
|---|---|
| 来源与许可怎么记录 | `source`：`origin`（自研/授权/公开数据集）、`provider`、`license_id`、`obtained_at`、`display_allowed`、`redistribution_allowed`。`display_allowed=false` 直接拒绝授予 |
| 质量报告写什么、怎么引用 | 求解器身份与配置哈希、迭代数、收敛指标/值/单位/门槛、牌面与下注抽象、是否使用同构、独立复核工具、以及至少一条显式限制。用 `report_id` 引用，制品的 `provenance.quality_report_id` 必须指向它 |
| 谁能授予 `verified`、怎么留痕 | 至少 2 名签署人、至少 1 名 `independent_reviewer`；每人留 `reviewer_id`、角色、签署时间和书面意见的 SHA-256。**loader 永远不授予**，`gto-quality-verify` 是独立命令 |
| 动作树如何冻结 | 报告、制品和目录条目的 `action_tree_version` 必须三方一致；换下注尺度必须换版本 |
| 多人局质量怎么表述 | `claim_class` 二选一，见下 |

## claim_class：不要把多人局说成均衡

这是整个协议里最容易出事的字段。

- `equilibrium_approximation` —— 强断言："本策略在声明的距离内逼近唯一的两人零和均衡"。**只有** `players_dealt == 2` 且 `objective == chip_ev` 时可用，并且收敛指标必须是真正衡量均衡距离的那一类（`exploitability`、`nash_distance`、`best_response_gap`）。`average_regret` 衡量训练进度，不是均衡距离，用它做强断言会被拒绝。
- `empirical_quality` —— 其他所有情况的诚实说法：多人底池和 ICM/PKO 没有等价的唯一性结论。求解器在那里仍然可以被测量得很好，但把它叫作"均衡逼近"是没人能支撑的宣称。

六人桌、三人局、ICM、PKO 一律无法通过 `equilibrium_approximation`。

## rake 范围：结构化字段，不是自由文本

早期设计用"在 `limits` 里搜 rake 这个词"来确认抽水被处理过。这个检查会被一句 `"Rake was ignored completely"` 满足——报告承认没处理抽水，检查反而通过了。

现在 `solve.rake_model_id` 是结构化字段，直接和节点自己的 `RakeSpec.model_id` 比对：

- 节点有抽水 → 必须填，且必须等于节点的 model ID；
- 节点没有抽水 → 必须是 `null`；
- 两者不符 → 拒绝。

## 绝对收敛上限

报告自己声明门槛、自己达标，这套"自证"如果没有天花板，一份写着 `threshold = 999999` 的报告就能"达标"。

`quality-gate-policy/1.0.0` 因此加了按单位的绝对上限：

| 单位 | 上限 |
|---|---:|
| `bb_per_100` | 1 |
| `bb` | 0.01 |
| `percent_of_pot` | 1 |

收敛值和报告自己的门槛**都**不得超过上限。

> 这三个数字是刻意保守的起始值，不是行业标准，也还没有经过真实解法校准。调整它们需要发新的 `quality-gate-policy` 版本并重新签署所有既有授予。首批真实解法接入时应当重新评估。

## 时间约束

| 约束 | 理由 |
|---|---|
| `source.obtained_at <= solve.completed_at` | 不能用还没拿到的数据或许可去求解 |
| `solve.started_at <= solve.completed_at` | 基本自洽 |
| `solve.started_at <= artifact.provenance.generated_at <= granted_at` | 制品不能早于求解，也不能晚于授予 |
| `solve.completed_at <= reviewer.signed_at <= granted_at` | 签的必须是已经完成的求解，且不晚于授予 |
| `granted_at <= now + 1 天` | 授予不能落在未来；容差留给时钟漂移 |

`verify_quality_attestation(..., now=...)` 可注入时钟，测试因此保持确定性。

## 文件结构

`solve-quality-report/1.0.0`：

```json
{
  "schema_version": "solve-quality-report/1.0.0",
  "report_id": "example.report.0001",
  "solution_id": "example.hu-cash.btn-flop-cbet",
  "game_spec_fingerprint": "85b7db32...",
  "action_tree_version": "example.flop-cbet/1.0.0",
  "solver": {
    "name": "example.solver",
    "version": "1.2.3",
    "config_id": "example-config",
    "config_sha256": "..."
  },
  "claim_class": "equilibrium_approximation",
  "source": {
    "origin": "licensed",
    "provider": "...",
    "license_id": "...",
    "obtained_at": "2026-07-01T00:00:00Z",
    "display_allowed": true,
    "redistribution_allowed": false
  },
  "solve": {
    "started_at": "2026-08-01T00:00:00Z",
    "completed_at": "2026-08-02T00:00:00Z",
    "iterations": 500000,
    "convergence_metric": "exploitability",
    "convergence_value": "0.0031",
    "convergence_unit": "bb_per_100",
    "convergence_threshold": "0.005",
    "board_abstraction": "none",
    "bet_size_abstraction": "two sizes, no translation",
    "card_isomorphism_used": false,
    "rake_model_id": "pokerstars.cash.example"
  },
  "evaluation": {
    "scope": "single_node",
    "board_sample_size": 1,
    "independent_recheck": true,
    "recheck_tool_name": "example.rechecker",
    "recheck_tool_version": "0.1"
  },
  "limits": [
    "Single flop node only; nothing is proven about other boards or streets."
  ]
}
```

`quality-attestation/1.0.0`：

```json
{
  "schema_version": "quality-attestation/1.0.0",
  "attestation_id": "example.grant.0001",
  "solution_id": "example.hu-cash.btn-flop-cbet",
  "artifact_sha256": "...",
  "report": {"path": "report.json", "id": "example.report.0001", "sha256": "..."},
  "granted_quality": "verified",
  "policy_version": "quality-gate-policy/1.0.0",
  "granted_at": "2026-08-05T00:00:00Z",
  "reviewers": [
    {
      "reviewer_id": "solver.owner",
      "role": "solver_owner",
      "signed_at": "2026-08-03T00:00:00Z",
      "statement_sha256": "..."
    },
    {
      "reviewer_id": "independent.reviewer",
      "role": "independent_reviewer",
      "signed_at": "2026-08-04T00:00:00Z",
      "statement_sha256": "..."
    }
  ]
}
```

`report.path` 相对**签署文件自己所在的目录**解析，规则与 `artifact_id` 相同：拒绝绝对路径、盘符、`..`、反斜杠、非 `.json`、符号链接和越界解析。所以报告通常和签署文件放在同一目录。

`statement_sha256` 和 `solver.config_sha256` 钉住仓库之外的文件（书面复核意见、求解配置）。门读不到它们；它们的作用是让审计能证明当时签的是哪份文本。

## 验证器必须拒绝的情况

**字节钉死**

- 签署里的 `artifact_sha256` 与实际制品字节不符（包括签署后编辑制品）；
- 签署里的 `report.sha256` 与实际报告字节不符；
- 签署里的 `report.id` 与报告的 `report_id` 不符。

**身份**

- 报告的 `solution_id`、`game_spec_fingerprint`、`action_tree_version` 与制品不符；
- 报告的求解器名称、版本、配置 ID 与制品 `provenance` 不符；
- 制品的 `provenance.quality_report_id` 指向另一份报告；
- 目录条目或制品的质量标签不是 `verified`（门只做授予的**记录**，不做标签升级）；
- 制品位于 `fixtures/` 目录下（loader 先一步拒绝，测试数据永远无法被授予）。

**质量**

- 收敛值超过报告自己的门槛；
- 收敛值或门槛超过策略绝对上限；
- `claim_class = equilibrium_approximation` 用在非两人、非 chip_ev 节点，或搭配非均衡距离指标；
- `solve.rake_model_id` 与节点抽水模型不符；
- `source.display_allowed = false`；
- `evaluation.independent_recheck = false`；
- 复核工具名称与求解器名称相同（自己复核自己不算独立）；
- 报告没有列出任何限制，或限制重复。

**签署**

- 少于 2 名签署人或多于 16 名；
- 没有任何 `independent_reviewer`；
- 同一 `reviewer_id` 重复（大小写不敏感比较）；
- 签署时间早于求解完成，或晚于授予时间；
- 授予时间早于求解完成，或落在未来。

**Schema 与路径**

- 未知 `schema_version` 或未知 `policy_version`；
- 缺失键、未知键、重复 JSON 键、过深嵌套、孤立代理码点；
- `granted_quality` 不是 `verified`（其他标签不需要门）；
- `report.path` 越界、经过符号链接或不是 `.json`；
- 报告超过 1 MiB，签署超过 256 KiB。

## usable_for_teaching 的两个条件

```text
usable_for_teaching = (quality == "verified") AND (存在通过质量门的签署)
```

两个条件都必须满足。手工编辑的目录和制品可以在"我们都是 verified"这件事上互相同意——但没有签署，这种同意教不了任何人。`StrategyEvidence.teaching_block_reason` 会说明是哪一条挡住了：

| 值 | 含义 |
|---|---|
| `quality_not_verified` | 标签本身就不是 `verified` |
| `no_quality_attestation` | 标签是 `verified`，但没有提供通过门的签署 |
| `null` | 可以展示给学习者 |

`build_strategy_evidence` 还会拒绝一份**为别的字节签发**的授予：`solution_id` 和 `artifact_sha256` 都必须与被查询的制品一致。

## CLI

```powershell
$env:PYTHONPATH = "src"

# 独立运行质量门
python -m rivermind_core gto-quality-verify grants/attestation.json `
  --catalog solutions/catalog.json --json

# 把草稿制品规范化，得到目录应当登记的字节与 sha256
python -m rivermind_core gto-artifact-package draft.json `
  --catalog solutions/catalog.json --json

# 确认无误后写入，并同步更新目录里的 sha256
python -m rivermind_core gto-artifact-package draft.json `
  --catalog solutions/catalog.json --write --update-catalog --json

# 带签署查询：只有这条路径才可能返回 usable_for_teaching = true
python -m rivermind_core gto-query pokerstars 100000000001 `
  --before-action 5 --database data/dev.db `
  --catalog solutions/catalog.json `
  --rake-model pokerstars.cash.example --rake-percent 5 --rake-cap-bb 3 `
  --combo AhKh --attestation grants/attestation.json --json
```

`gto-artifact-package` 的安全约束：

- 规范化会改写格式和小数拼写（`"2.0"` → `"2"`），所以草稿和成品可能字节不同。成品是幂等的，这正是 SHA-256 可复现的原因；
- `--update-catalog` 必须搭配 `--write`，不给没写过的字节登记哈希；
- 拒绝写到目录文件本身，也拒绝覆盖任何"不是这个 solution 的制品"的既有 `.json`（例如签署过的报告）；
- 写入走同目录临时文件 + 原子替换，写完后立刻重新验证目录与制品是否自洽；
- 草稿沿用 8 MiB 上限；
- **重新打包会作废既有签署**：制品字节变了，`artifact_sha256` 就不再匹配，必须重新走质量门。

`gto-query` 的新退出码语义：

| 情况 | `strategy_available` | 退出码 |
|---|---|---:|
| 提供了签署且质量门通过 | `true`（可教学） | 0 |
| 未提供签署 | `true`（不可教学） | 0 |
| 签署未通过质量门 | `false`（`attestation_verification_failed`） | 2 |
| 签署是为别的制品签发的 | `false`（`attestation_scope_mismatch`） | 2 |

## 仓库现状

- 仓库里**没有任何**签署文件、求解质量报告或 `verified` 制品；
- `tests/test_quality_gate.py` 里的完整授予全部在临时目录中构造，不会被提交；
- 一条测试会扫描整个仓库，确认没有任何 `.json` 含有 `quality-attestation`、`solve-quality-report` 或 `"quality": "verified"`；
- 因此 `usable_for_teaching` 在这个仓库里恒为 `false`。门已经建好了，还没有任何东西通过它。

## 下一阶段

门建好了，缺的是真实解法。开工前仍需要你决定：

1. 首批解法的来源、许可条款和导出格式；
2. 绝对收敛上限的三个数字是否符合你的质量标准；
3. `independent_reviewer` 具体是谁、书面意见存放在哪里；
4. 首批覆盖哪几个节点（建议先选一个 HU 或 6-max 高频翻牌节点，宁少勿多）。

拿到真实导出格式后，`gto-artifact-package` 前面再加一个来源专用的转换器即可；协议、门和 CLI 都不需要动。
