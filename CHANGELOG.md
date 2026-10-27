# CHANGELOG

## 0.2.0 — 2026-10-27

### 新增

- `extract --max-points N`：每块点数上限（默认 6，夹在 1–20）。同时改写 prompt 的
  「请抽出 1–N 个知识点」并按 N 截断落库点——Host 侧（KnowAtlas `[points] max_per_chunk`）
  与工具侧从此同源，不再靠改源码里的硬编码 6。
- `prompt.build_system(n)` / `prompt.clamp_max_points(n)`：prompt 由函数生成，`SYSTEM`
  常量保留（默认 6，逐字等于 0.1.0 的历史行为）。
- `stats.json` 增加 `max_points` 字段（复核时能看到这次的旋钮值）。

### 兼容

- 不传 `--max-points` 时行为与 0.1.0 完全一致（默认 6）；`SYSTEM` 仍可直接 import。
- 单测新增 prompt/CLI 断言，其余 14 项不变（共 16 项，零网络零 key）。

## 0.1.0 — 2026-10-27

首个独立发布版（历史来自 KnowAtlas 的 `tools/kpe`）。

### 功能

- `chunks`：PDF / Markdown / 纯文本分块。PDF 优先解析目录页（印刷页码 + 页偏移），
  按章/节合并到约 6,000 字；无目录时退化为标题或窗口分块；默认跳过前言/目录/
  致谢/思考题/本章小结（`--keep-front` 可保留前言）。
- `extract`：逐块调用任意 OpenAI 兼容模型抽点（1–6 条/块），支持 `--chapter`
  选章、`--limit N` 试跑、`--workers` 并发、`--thinking off|on|omit`、
  `--price-in/--price-out` 成本估算。
- `review`：按章生成 Markdown 复核稿（`--with-sections` 带每节路径与页码）。
- `stats`：汇总 manifest、现算总点数/token，以及最近一次运行统计。
- 校验：JSON 容错（失败自动修复重问一次）、`quote` 去标点模糊匹配、最小自洽
  闸门、书内去重。
- 断点续跑：`cache/<chunk_id>.json` + `done.jsonl`；`--retry-failed` 只补失败块，
  `--redo` 全重跑。

### 实测

《AI-Agents-in-Depth》304 页 / 44 万字：96 块 → 571 点，quote 命中 570/571，
约 ¥0.85、130 秒（DeepSeek-V4.1-Flash，关思考，4 并发）。

### 工程

- 唯一第三方依赖 `pdfplumber`；其余为标准库。
- 14 项 pytest，零网络、零 key。
- MIT 许可。