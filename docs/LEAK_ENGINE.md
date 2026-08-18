# 确定性漏洞引擎与证据协议

## 定位

Leak Engine 负责从已经验证的统计宽表中筛选值得复盘的重复模式。它不调用 LLM，不读取对手未公开底牌，也不声称静态阈值等同于 GTO 策略。

当前规则配置为 `broad-review-signals` v0.1.0，适合做玩家级初筛。用户应优先按 Cash/MTT、位置和有效筹码过滤后再解释结果；跨赛制聚合只能作为线索。

## 保守触发逻辑

每条规则同时定义：

- 指标与方向；
- 建议复盘阈值和优先复盘阈值；
- 最低机会数；
- 证据手牌应选择“发生”还是“错过”的机会。

机会数不足时状态为 `insufficient_sample`。达到最低样本后，引擎计算二项比例的 95% Wilson 区间。只有整个区间都越过建议复盘阈值，状态才是 `detected`；整个区间继续越过更严格阈值时，严重度为 `priority`，否则为 `review`。其余为 `clear`。

这里的 95% 区间表达抽样不确定性，不验证阈值本身是否适合某个具体牌局生态。

## 首批规则

| 规则 | 指标 | 建议复盘 | 优先复盘 | 最低机会数 | 证据选择 |
|---|---|---:|---:|---:|---|
| 3Bet 使用偏少 | 3Bet | ≤ 4% | ≤ 2% | 40 | 错过的 3Bet 机会 |
| 非盲位 Cold Call 偏多 | Cold Call | ≥ 25% | ≥ 35% | 30 | Cold Call 手牌 |
| 面对 3Bet 弃牌偏多 | Fold to 3Bet | ≥ 70% | ≥ 80% | 25 | 弃牌手牌 |
| Flop CBet 使用偏少 | Flop CBet | ≤ 45% | ≤ 30% | 30 | 未 CBet 手牌 |
| Flop CBet 使用偏多 | Flop CBet | ≥ 80% | ≥ 90% | 30 | CBet 手牌 |
| 面对 Flop CBet 弃牌偏多 | Fold to Flop CBet | ≥ 65% | ≥ 75% | 30 | 弃牌手牌 |

相反方向的两条 CBet 规则共享同一统计口径，但 Wilson 区间不可能同时完整越过两侧阈值。

## 证据链

每个 `LeakAssessment` 固定记录规则 ID/版本、玩家、指标、发生数、机会数、观察比例、95% 区间、阈值、样本缺口和状态。只有 `detected` 评估会形成 `LeakCard`。

`SQLiteHandStore.query_leaks()` 使用卡片中的 `metric + evidence_occurred` 重新查询同一玩家、同一赛制/位置/筹码过滤范围的手牌。每张卡默认附带最近 5 手证据，最多可配置为 20 手。证据查询要求该指标确实存在机会，避免把无关弃牌混入“错过 3Bet”一类卡片。

本地 HTML 报告使用原生 `<details>` 展开证据手牌，不加载外部脚本。标题、玩家名、站点、手牌 ID 和解释文本均经过 HTML 转义。

## CLI

```powershell
$env:PYTHONPATH = "src"

# 默认分析牌谱中标记的 Hero
python -m rivermind_core leaks --database data/dev.db

# 获取完整结构化证据，限定 Cash CO/BTN，卡片最多附带 10 手
python -m rivermind_core leaks --database data/dev.db --game-type cash `
  --position CO BTN --evidence-limit 10 --json

# 报告使用相同规则与证据范围
python -m rivermind_core report --database data/dev.db `
  --evidence-limit 10 --output data/report.html
```

JSON 顶层包含 `profile`、`scope`、`summary`、全部 `assessments` 和已触发的 `cards`。卡片内的 `evidence_hands` 沿用相关手牌 API 的金额、位置、有效筹码、统计发生/机会和账本校验字段，可直接交给后续解释层。

## 与 AI 教练的边界

下一阶段的 LLM 只能读取结构化 `LeakCard`，把固定数值和证据组织成自然语言；它不能改变状态、严重度、比例、区间、阈值或证据手牌。输出前需要逐字段忠实性校验，失败时回退到确定性模板。

## 当前限制

- 首批阈值是产品复盘基线，不是按牌面、范围和尺度求得的 GTO 频率；
- 当前统计只覆盖固定翻前和 Flop 指标，尚无 Turn/River、底池类型和人数细分；
- Wilson 区间处理抽样波动，不处理牌局选择偏差、玩家池偏差或时间漂移；
- 同一规则在 Cash、MTT、不同位置和不同有效筹码下可能需要独立配置；
- 当前证据按时间倒序抽取，不做“最典型手牌”排序。
