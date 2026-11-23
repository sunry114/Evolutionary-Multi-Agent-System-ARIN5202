# Squad · 多 Agent 协作工作台

Evo 是本地多 Agent 工单工作台。指派给 Agent 或 Squad 会将工作加入后台队列。要唤醒成员，在工单评论里写 `@Agent` 并选择「评论并唤醒」。Squad 队长负责分工和签核，派工使用专用工具。每张工单保留运行进度、产物和状态历史，并提供停止和重试操作。[Multica](https://github.com/multica-ai/multica) · [本地运行指南](docs/LOCAL_LINUX_SETUP.md#日常工单流程)

## 运行当前版本

在 Linux 项目根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m evo --demo web
```

打开 <http://127.0.0.1:8768> 查看内存演示。SQLite 工作台、项目目录和宿主机 Claude Code 的配置见 [Linux 运行指南](docs/LOCAL_LINUX_SETUP.md)。课程连续场景的准备与评估见[测试指南](docs/MID_TEST_RUNBOOK.md)。

开发者可用 `python -m unittest discover -s tests -v` 运行离线测试。重构前的架构材料、贡献记录和验证报告可在[基线版本的文档目录](https://github.com/sunry114/Evolutionary-Multi-Agent-System-ARIN5202/tree/11997ad69c68bf58c39019b5333bfd9ebc6dcf98/docs)查阅。
