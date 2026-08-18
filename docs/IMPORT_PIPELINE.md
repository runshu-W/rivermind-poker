# RiverMind 导入管道 v0.1

## 已实现闭环

```text
文件/文件夹 → 文本解码 → 多手拆分 → 格式路由 → 解析与校验
           → site + hand_id 指纹去重 → SQLite → 批次报告
```

每个文件生成独立 `batch_id`。同一文件中的每手牌单独处理，一手损坏不会阻断后续手牌。整个批次的数据库写入使用一个事务；基础设施异常时回滚，不留下半完成批次。

## 结果状态

| 状态 | 含义 | 是否写入 hands |
|---|---|---|
| `imported` | 解析、领域校验和落库成功 | 是 |
| `duplicate` | `site + hand_id` 已存在 | 否 |
| `failed` | 已识别格式，但牌谱损坏或违反领域约束 | 否 |
| `unsupported` | 未知平台，或已知但尚未支持的变体 | 否 |

失败与不支持项保存原始片段、源文件名、起止行号、稳定错误码和可读原因。成功手牌保存规范化 JSON 与原文，可以无损恢复为 `HandHistory`；导入报告也可以从数据库重建。

## 本地使用

```powershell
$env:PYTHONPATH = "src"

# 导入单个文件
python -m rivermind_core import tests/fixtures/pokerstars_cash.txt

# 递归导入文件夹，并输出 JSON 报告
python -m rivermind_core import tests/fixtures --database data/rivermind.db --json
```

目录扫描当前接受 `.txt`、`.log` 和 `.hh`。文本按 UTF-8 BOM、UTF-16、Windows-1252 顺序解码。

## 数据表

- `import_batches`：来源、时间、检测数及四类结果汇总；
- `import_items`：逐手行号、状态、解析器、错误和原始片段；
- `hands`：站点手牌 ID、指纹、解析器版本、规范化 JSON 与原文。

`UNIQUE(site, hand_id)` 和唯一指纹共同保证幂等导入。解析器遇到未知玩家动作会显式失败，避免静默丢动作后污染统计。

## 当前基准与限制

2026-08-18 在当前开发环境运行合成 10,000 手基准：导入 2.340 秒，约 4,274 手/秒；随后计算首批统计用时 0.641 秒，约 15,608 手/秒；SQLite 文件约 51.46 MB。输入是已提交黄金牌谱的不同手牌 ID 变体，只用于发现性能回退，不代表真实牌谱覆盖率。

当前支持 PokerStars 英文现金桌，以及带常规买入或 Freeroll 标识的基础 MTT 格式。第二个平台、更多经过授权的真实匿名黄金牌谱、100k/1M 批量基准和统计分析物理模型是后续工作。`tests/fixtures/manifest.json` 明确记录样本来源，当前样本均为合成代表性牌谱，不冒充真实用户数据。运行基准：

```powershell
python benchmarks/import_benchmark.py --hands 10000
```
