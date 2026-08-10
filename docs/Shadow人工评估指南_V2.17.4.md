# OpsPilot Shadow 人工评估指南（V2.17.4）

## 文件位置

- 原始 Shadow 日志：`data/problem_understanding_shadow.jsonl`
- 人工评估表：`data/problem_understanding_review.csv`
- 汇总结果：`data/problem_understanding_review_summary.json`

人工评估表和原始日志分开保存，以 `record_id` 关联。重新导出会补充新记录，同时保留 CSV 中已有的人工标注。

## 第一步：导出评估表

在项目目录运行：

```powershell
python tools/shadow_evaluation.py export
```

也可以指定路径：

```powershell
python tools/shadow_evaluation.py export --log data/problem_understanding_shadow.jsonl --output data/problem_understanding_review.csv
```

## 第二步：人工填写

用 Excel 或 LibreOffice 打开 CSV。以下正确性字段填写 `1`（正确）或 `0`（错误），留空表示尚未评价：

- `intent_correct`
- `continuation_correct`
- `domains_correct`
- `candidate_kbs_correct`
- `completeness_correct`
- `suggested_action_correct`

发现错误时填写期望值：

- `expected_intent`
- `expected_domains`：建议用 JSON 数组，如 `["办公网络"]`
- `expected_kb_ids`：建议用 JSON 数组，如 `["kb-id-1"]`
- `reviewer_note`

注意：Shadow 日志不保存完整问题正文。人工复核应结合测试用例清单或受控会话记录，不能仅凭模型输出猜测“正确”。

## 第三步：生成汇总

```powershell
python tools/shadow_evaluation.py summary
```

没有人工标注的维度会返回 `accuracy: null`，不会把模型自身输出当成准确率。候选知识库命中率和漏选率只使用已人工评价、且填写了期望知识库 ID 的记录。

## 指标口径

- 意图、补充归属、知识领域、候选知识库、完整度、建议动作准确率：只统计对应人工正确性字段不为空的样本。
- 候选知识库命中率：预测与期望知识库交集数 / 期望知识库总数。
- 候选知识库漏选率：漏选期望知识库数 / 期望知识库总数。
- 低置信度数量：`degrade_reason=low_confidence`。
- 技术降级率：存在 `error_type` 或 `schema_valid=false` 的样本占比，不把单纯低置信度算作技术故障。
- 平均模型耗时：全部导出 Shadow 样本的 `model_latency_ms` 平均值。
