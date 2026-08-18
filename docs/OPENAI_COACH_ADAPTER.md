# OpenAI 教练适配器与专家质量门

## 交付边界

本阶段增加了首个可选供应商适配器，但默认产品行为仍是本地确定性模板。代码和测试没有执行真实网络请求，也没有向 OpenAI 或其他第三方发送牌谱数据。

适配器使用 OpenAI Responses API，并按官方 Structured Outputs 约定通过 `text.format` 提交严格 JSON Schema。官方文档说明 Structured Outputs 保证 schema 形状，并要求调用方单独处理 refusal 和 incomplete；RiverMind 在这层结构保证之后仍运行自己的证据、数值、隐私、无依据主张和直接行动校验。

官方依据：

- [Structured model outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI model catalog](https://developers.openai.com/api/docs/models)

## 默认关闭与显式授权

`coach-openai` 同时满足以下条件才可能发起请求：

1. 命令包含 `--allow-external-model`；
2. `OPENAI_API_KEY` 已在进程环境中设置；
3. 用户显式指定模型与当前输入/输出价格；
4. `--rule-id` 精确选中一张 Leak Card；
5. 最坏尝试费用没有超过本地预算。

API Key 不接受命令行参数，不写入请求体、审计、输出或配置 repr。适配器固定只允许 `https://api.openai.com/v1/responses`，避免把密钥转发到自定义端点。请求设置 `store: false`，但这不替代用户对第三方处理条款、账户配置和数据治理的独立审核。

示例仅展示启用方式；运行前必须在 OpenAI 官方模型页核对模型可用性和当前价格：

```powershell
$env:PYTHONPATH = "src"
$env:OPENAI_API_KEY = "..."

python -m rivermind_core coach-openai `
  --database data/dev.db `
  --rule-id flop_cbet_overuse `
  --model YOUR_MODEL_ID `
  --input-usd-per-million CURRENT_INPUT_RATE `
  --output-usd-per-million CURRENT_OUTPUT_RATE `
  --max-cost-microusd 50000 `
  --allow-external-model `
  --json
```

价格不硬编码在仓库中，避免模型价格变化后静默低估预算。当前费用计算使用供应商返回的 token 数和命令传入的价格；调用前估价使用偏保守的字符上界。供应商侧硬额度和告警仍是生产必需项。

## 版本化提示词与响应处理

- 提示词版本：`coach-prompt/zh-CN/1.0.0`；
- 审计记录提示词版本及内容/schema 的 SHA-256；
- 请求只包含系统约束和脱敏 `ExplanationEvidence.model_payload()`；
- Structured Outputs schema 禁止额外字段；
- refusal 和 incomplete 不重试，直接使用确定性模板；
- 网络超时和临时供应商异常才按运行时策略重试；
- request ID、拒绝正文、错误响应正文和候选正文都不进入审计。

标准库 HTTP 传输设置独立 socket 超时和最大响应字节数。外层异步运行时也有超时；由于阻塞网络调用运行在线程中，取消 await 不能立即终止底层线程，socket 超时是最终边界。生产规模化前应评估原生异步 HTTP 客户端。

## 专家盲审工作流

合法模型解释可以导出为单案例盲审包：

```powershell
python -m rivermind_core coach-openai `
  ... `
  --allow-external-model `
  --review-output review-case.json `
  --variant-id A
```

盲审案例不包含玩家名、站点、原始手牌 ID、桌名、供应商、模型或解释来源。评分文件也禁止额外字段和自由文本，只接受哈希化 reviewer ID、专业资质确认、四项整数评分和固定 fatal error code。

质量门要求：

- 至少 50 个不同证据哈希；
- 每个案例至少 2 名不同专家评分；
- 至少 2 名不同专家参与；
- faithfulness 均值至少 4.8/5；
- teaching value 与 clarity 均值至少 4.0/5；
- uncertainty quality 均值至少 4.5/5；
- fabricated number、错误事实绑定、无依据策略主张、隐私泄漏、直接行动和重大遗漏总数为 0。

评分命令：

```powershell
python -m rivermind_core coach-review-score completed-review.json
python -m rivermind_core coach-review-score completed-review.json --json
```

少于门槛或出现 fatal error 时退出码为 `2`。当前完成的是盲审格式、导出器和质量门，尚未声称已经收集 50 个真实专家案例或达到发布门槛。

## 上线前剩余工作

- 招募并验证至少两名扑克领域专家，完成 50 个不同证据场景的独立盲审；
- 用测试账户执行一次经过批准的真实请求，核对响应结构、计费和数据控制；
- 增加限流、5xx、断网、代理、socket 超时和长响应的集成测试；
- 评审 OpenAI 项目级额度、访问权限、日志保留和删除流程；
- 将通过质量门的提示词/模型组合加入允许列表，而不是让 CLI 接受任意生产模型。
