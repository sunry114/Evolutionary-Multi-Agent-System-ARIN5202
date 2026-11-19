# 课程连续场景测试

课程 Mid Test 使用三个独立场景。每个场景配置四个角色，按顺序执行 Task 1 和 Task 2，同一场景的两次任务复用对话、记忆和工作区。请在本地保留教师提供的原始数据和实际交付物。

## 准备与执行

1. 运行 `python -m evo prepare-midtest Mid_Test.zip --output .evo/midtest`，使用新的输出目录。查看生成的 manifest、各场景角色与任务原文。
2. 为每个场景用生成的独立 `--state` 启动 Web 工作台。准备命令已保存角色和名为 `default` 的 Squad。先建立工单，录入交付要求和必要讨论，再指派给 `default`。队列自动运行。第二个任务沿用同一个状态目录，另建一张工单记录本次讨论。
3. 保存工单 ID、讨论、每次运行记录和产物。需要补充工作时在评论里 `@` 相应 Agent，核对产物后通过工单状态记录结果。质量评审需结合实际产物和任务说明。
4. 审计导出包时使用 `python -m evo audit-midtest EXPORT.zip --output .evo/mid-audit`，指定新的输出目录。该命令只读取并整理证据。

## Squad 私有记忆对照

配置包含 `builder` 的 Squad 后，在同一状态目录运行以下命令。

```bash
python -m evo eval examples/memory_sequence.jsonl --compare-memory \
  --output .evo/reports/squad-memory.json
```

指定其他 `--state` 时，报告路径要在该状态目录的 `reports` 下。两组按 JSONL 顺序执行，模型、角色、验收条件及审批策略相同。两组各自从空记忆开始，每个案例另建任务库和工作区。开启组只把个人经验带入下一案例。评测自动拒绝关键操作审批，澄清停在等待人工的状态。单独运行一组时可用 `--no-memory` 关闭记忆，关键审批停在待处理状态。

报告统计目标、所有层级的子任务和客观验收完成率、耗时、失败类别，以及规划、提名和应答发起的模型调用总数。跨工单检索按协商和执行分别计数，依据经验记录的来源工单 ID。[评测实现](../src/evo/evaluation/runner.py)
