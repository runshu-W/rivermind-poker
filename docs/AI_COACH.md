# AI 教练证据、模板与忠实性校验协议

## 当前交付

AI Coach v0.1 完成了模型无关的安全解释层：

1. 把 `LeakCard` 转换为脱敏、可哈希的 `ExplanationEvidence`；
2. 为每张卡生成确定性中文解释和复习任务；
3. 定义候选 LLM JSON schema 和 `{{fact_id}}` 引用规则；
4. 校验 schema、证据身份、证据哈希、数值、引用、隐私和直接行动指令；
5. 校验失败时保留问题代码并回退到确定性模板。

当前版本没有调用任何外部 LLM，也不会发送用户数据。`coach --json` 输出的 `model_input` 是后续模型连接器唯一允许发送的子对象。

## 数据隔离

`build_explanation_evidence()` 在本地使用原始玩家名、站点、桌名和手牌 ID 计算不可逆来源指纹；对模型公开的 payload 只包含：

- `Hero` 或 `Player` 代号；
- 规则 ID、规则版本；
- 观察比例、样本、95% Wilson 区间、阈值和严重度；
- 规则依据与复盘问题；
- `hand.1`、`hand.2` 等匿名证据引用，以及位置、有效筹码和净结果 BB；
- 明确缺失的 GTO、EV、范围、牌面和对手模型证据；
- 输出约束。

模型 payload 不包含玩家名、站点、桌名、原始手牌 ID、原始牌谱或底牌。`evidence_id` 和 SHA-256 `evidence_hash` 用于将候选输出绑定到生成时的证据快照。

## 候选输出 schema

候选输出必须且只能包含：

```json
{
  "schema_version": "coach-candidate/1.0.0",
  "evidence_id": "evidence:...",
  "evidence_hash": "...",
  "headline": "{{signal.title}}",
  "observation": "观察值{{signal.observed_percentage}}，样本{{signal.sample}}，区间{{signal.confidence_interval_95}}，阈值{{rule.trigger}}。",
  "teaching_point": "{{rule.rationale}}{{limitations.context}}",
  "review_plan": ["{{rule.review_prompt}}", "先看{{hand.1}}。"],
  "uncertainty": "{{limitations.no_gto}}",
  "evidence_refs": [
    "signal.title",
    "signal.observed_percentage",
    "signal.sample",
    "signal.confidence_interval_95",
    "rule.trigger",
    "rule.rationale",
    "limitations.context",
    "rule.review_prompt",
    "hand.1",
    "limitations.no_gto"
  ]
}
```

叙述字段不得直接写数字。模型只写占位符，渲染层从可信 `EvidenceFact.display_value` 插值。`evidence_refs` 必须与正文实际使用的占位符完全一致，并覆盖观察值、样本、区间、阈值、规则依据、复盘问题、适用边界和至少一手匿名证据。

## 校验与回退

`validate_coach_candidate()` 返回稳定问题代码，当前检查包括：

- 缺失、未知字段和字段类型；
- candidate/evidence schema、ID 和哈希不一致；
- 未知、重复、漏报或未使用的 `fact_id`；
- 正文中的裸数字、中文数量表达和英文数字词；
- 证据中不存在的 GTO、EV、范围优势、坚果优势、底牌和未来牌主张；
- 原始玩家名、站点、桌名和手牌 ID 回流；
- “必须/立即/总是下注、跟注、加注或弃牌”等直接行动指令；
- 文本为空、过长或复习步骤数量越界。

全部通过时来源标记为 `llm_validated`。失败时来源标记为 `template_fallback`，展示确定性模板，并保留问题代码用于复现。没有请求模型时来源为 `template`。

这套规则能保证结构、证据身份、数值和已知敏感边界；它不能证明任意自由文本的全部扑克因果关系正确。接入外部模型前还需要受控提示词、语义支持度校验器和专家黄金解释集。

## 确定性模板

模板直接读取 `LeakAssessment` 和证据手牌，固定输出：

- 信号标题和复盘优先级；
- 指标观察值、发生数/机会数、95% 区间和触发阈值；
- 规则依据和上下文限制；
- 两步复习计划；
- 缺少 GTO、范围和 EV 时的不确定性声明。

模板不调用模型，适用于免费层、模型超时、供应商故障和任何校验失败场景。

## CLI

```powershell
$env:PYTHONPATH = "src"

# 可读模板解释
python -m rivermind_core coach --database data/dev.db

# 输出脱敏模型输入、最终解释、证据哈希和校验状态
python -m rivermind_core coach --database data/dev.db --json

# 报告自动为每张已触发 Leak Card 嵌入 AI 教练模板
python -m rivermind_core report --database data/dev.db --output data/report.html
```

## 下一步

- 建立 50 个专家黄金解释样例和对抗性候选集；
- 选择模型供应商并实现超时、成本、重试和审计边界；
- 增加第二层语义支持度校验，但不允许校验器修改策略结论；
- 记录提示词版本、模型版本和响应延迟；
- GTO Matcher 上线后扩展策略频率、范围和 EV 的强类型事实。
