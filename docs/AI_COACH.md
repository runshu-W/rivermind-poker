# AI 教练证据、模板与忠实性校验协议

## 当前交付

AI Coach v0.3 完成了模型无关的安全解释层、调用边界与人工质量门：

1. 把 `LeakCard` 转换为脱敏、可哈希的 `ExplanationEvidence`；
2. 为每张卡生成确定性中文解释和复习任务；
3. 定义候选 LLM JSON schema 和 `{{fact_id}}` 引用规则；
4. 校验 schema、证据身份、证据哈希、数值、引用、隐私和直接行动指令；
5. 校验失败时保留问题代码并回退到确定性模板；
6. 用 50 例合同/对抗性语料持续回归候选校验器；
7. 提供带超时、重试、输入/输出上限和费用预算的异步 Provider 接口；
8. 生成不含输入输出正文和原始牌局标识的调用审计；
9. 固化版本化提示词与严格 Structured Outputs JSON Schema；
10. 提供默认关闭、显式授权的 OpenAI Responses 适配器；
11. 提供 50 个不同证据、双专家评分和零 fatal error 的盲审质量门。

默认 `coach` 和报告命令不调用外部 LLM，也不会发送用户数据。只有单独的 `coach-openai` 命令在显式授权、环境 API Key、精确单卡选择和预算检查全部通过后才可能发送脱敏 payload。本阶段测试没有执行真实请求。

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

这套规则能保证结构、证据身份、数值和已知敏感边界；它不能证明任意自由文本的全部扑克因果关系正确。版本化提示词和严格 JSON Schema 已完成，语义教学质量仍必须通过真实扑克专家盲审。

Provider 运行时、50 例语料的构成和审计字段见 [COACH_RUNTIME_EVALS.md](COACH_RUNTIME_EVALS.md)。该语料是合同/攻击面回归集，不冒充经过专家评分的解释质量黄金集。
OpenAI 适配器的授权、价格、隐私边界和专家质量门见 [OPENAI_COACH_ADAPTER.md](OPENAI_COACH_ADAPTER.md)。

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

# 运行 50 例离线候选评测门（不调用外部模型）
python -m rivermind_core coach-eval

# 报告自动为每张已触发 Leak Card 嵌入 AI 教练模板
python -m rivermind_core report --database data/dev.db --output data/report.html
```

## 下一步

- 招募至少两名扑克专家，完成 50 个不同证据场景的真实盲审；
- 经数据治理批准后用测试账户执行真实连接器验收；
- 用盲审结果比较模型/提示词版本并冻结生产允许列表；
- 为无正文审计增加加密持久化、保留期和用户删除流程；
- GTO Matcher 上线后扩展策略频率、范围和 EV 的强类型事实。
