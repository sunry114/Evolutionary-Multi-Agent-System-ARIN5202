# Squad · 多 Agent 协作工作台

`squad` 分支以 Multica 的工单协作为产品目标。用户把一个目标指派给 agent 或 Squad；队长提议分工，成员可以接受、调整职责或委派给同伴。用户打开工单，就能查看当前负责人、运行记录、阻塞原因和待审查的交付物。[Multica](https://github.com/multica-ai/multica)

## 分支现状

| 工作环节 | 当前可用 | 首版目标 |
|---|---|---|
| 提交工作 | CLI 和 Web 共用建单、指派和启动操作。列表可按负责人和状态筛选 | 按同一数据集对比记忆开关的完成率与耗时 |
| 分派与协作 | Squad 工单由队长提名，成员协商接单。工单详情聚合讨论、协商、运行记录和委派产物 | 评估角色经验对后续分派及执行的影响 |
| 交付与复核 | 在同一工单处理审批、退回和启动修订，并保留各次交付的产物快照 | 按同一验收条件验证端到端交付 |

首版复用单机 SQLite、私有工作区、agent 记忆和现有执行器。CLI 使用 `configure` 配置 Squad、`create` 建单、`start` 显式启动，再用原工单 ID 执行 `task` 查看结果。完整命令见[本地运行指南](docs/LOCAL_LINUX_SETUP.md#配置-squad创建和启动工单)。阶段顺序、课程约束和验收条件见[重构计划](docs/SQUAD_REFACTOR_PLAN.md)。

## 运行当前版本

在 Linux 项目根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m evo --demo web
```

打开 <http://127.0.0.1:8768> 查看内存演示。使用 SQLite 工作台、真实模型或 Docker 执行器时，请按 [Linux 运行指南](docs/LOCAL_LINUX_SETUP.md) 配置。课程连续场景的准备与评估见[测试指南](docs/MID_TEST_RUNBOOK.md)。

开发者可用 `python -m unittest discover -s tests -v` 运行离线测试。重构前的架构材料、贡献记录和验证报告可在[基线版本的文档目录](https://github.com/sunry114/Evolutionary-Multi-Agent-System-ARIN5202/tree/11997ad69c68bf58c39019b5333bfd9ebc6dcf98/docs)查阅。
