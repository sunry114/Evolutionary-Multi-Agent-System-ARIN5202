# 课程连续场景测试

课程 Mid Test 使用三个独立项目，可并行运行。每个场景配置四个角色，按顺序执行 Task 1 和 Task 2。同一场景沿用一张工单和一个本机目录。请在本地保留教师提供的原始数据和实际交付物。

## 准备与执行

1. 运行 `python -m evo prepare-midtest Mid_Test.zip --output .evo/midtest`，使用新的输出目录。查看生成的 manifest、各场景角色与任务原文。
2. 为每个场景创建工作区，把生成的 `agents.json` 导入对应工作区。在各工作区新建项目并绑定独立目录，再启动同一状态目录下的 Web 工作台。场景可由同一个工作台并行领取。
3. 保存工单 ID、讨论、每次运行记录和产物。需要补充工作时在评论里 `@` 相应 Agent 并选择「评论并唤醒」。CLI 的 `comment` 同样支持 `@`，可加 `--no-notify` 只保存评论。质量评审需结合实际产物和任务说明。
4. 审计导出包时使用 `python -m evo audit-midtest EXPORT.zip --output .evo/mid-audit.json`。该命令读取并整理证据。

```bash
batch="$PWD/.evo/midtest"
state="$batch/state"
for scenario in scenario_1 scenario_2 scenario_3; do
  directory="$batch/files/$scenario"
  mkdir -p "$directory"
  python -m evo --state "$state" create-workspace "$scenario"
  python -m evo --state "$state" --workspace "$scenario" configure --file "$batch/$scenario/agents.json"
  python -m evo --state "$state" --workspace "$scenario" create-project "$scenario" "$directory"
done
python -m evo --state "$state" web --port 8770
```

在对应项目中用 Task 1 原文新建工单，指派给 `default`。队长签核后工单进入待验收。按 Agent 回复中的项目相对路径找到交付并保存第一阶段证据，然后在同一工单用 `@队长名称` 唤醒队长，下一行贴 Task 2 原文。

## Squad 私有记忆对照

配置包含 `builder` 的 Squad 后，在对应项目运行以下命令。

```bash
python -m evo --state STATE_DIRECTORY --project PROJECT_ID eval examples/memory_sequence.jsonl \
  --compare-memory --output STATE_DIRECTORY/projects/PROJECT_ID/reports/squad-memory.json
```

报告路径要在项目状态目录的 `reports` 下。两组按 JSONL 顺序执行，模型、角色、验收条件及审批策略相同。两组各自从空记忆开始，每个案例另建任务库和工作区。开启组只把个人经验带入下一案例。评测自动拒绝关键操作审批，澄清停在等待人工的状态。单独运行一组时可用 `--no-memory` 关闭记忆，关键审批停在待处理状态。

报告统计目标、所有层级的子任务和客观验收完成率、耗时、失败类别，以及规划、提名和应答发起的模型调用总数。跨工单检索按协商和执行分别计数，依据经验记录的来源工单 ID。[评测实现](../src/evo/evaluation/runner.py)
