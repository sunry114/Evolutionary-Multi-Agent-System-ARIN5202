# 课程连续场景测试

课程 Mid Test 使用三个独立场景。每个场景配置四个角色，按顺序执行 Task 1 和 Task 2，同一场景的两次任务复用对话、记忆和工作区。请在本地保留教师提供的原始数据和实际交付物。

## 准备与执行

1. 运行 `python -m evo prepare-midtest Mid_Test.zip --output .evo/midtest`，使用新的输出目录。查看生成的 manifest、各场景角色与任务原文。
2. 为每个场景选用独立的 `--state`。使用 `agents --file` 加载该场景的角色，通过群聊确认交付路径和验收条件，再用 `group --execute` 开始。第二个任务沿用同一个状态目录。
3. 按实际对话回答澄清、决定审批，保存工单 ID、对话、运行事件和产物。用 `metrics TASK_ID` 查看耗时与状态；质量评审需结合实际产物和任务说明。
4. 审计导出包时使用 `python -m evo audit-midtest EXPORT.zip --output .evo/mid-audit`，指定新的输出目录。该命令只读取并整理证据。

独立 JSONL 案例可使用 `python -m evo eval examples/smoke.jsonl --output .evo/reports/smoke.json`。评测器对每个案例使用新的临时状态。跨任务记忆效果应通过同场景连续任务记录。[评测实现](../src/evo/evaluation/runner.py)
