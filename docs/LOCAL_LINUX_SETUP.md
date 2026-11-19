# 本地运行指南

CI 基线为 Ubuntu 22.04 x86_64 和 Python 3.10。进入项目目录，按 [README](../README.md) 创建虚拟环境并安装。运行以下命令即可在本机打开 SQLite 工作台，查询工作台与打开网页使用同一个状态目录：

```bash
bash scripts/start_linux.sh 8769
# 在另一个终端打开同一目录
.venv/bin/python -m evo --state .evo/local-workbench tasks
```

浏览器地址为 <http://127.0.0.1:8769>。启动脚本使用非 root Linux 用户。WSL 用户可在 Linux 终端中执行相同命令。

## 模型和执行器

课程后端支持 HKGAI 和 DeepSeek。在同一终端选择提供方，并按隐藏提示保存密钥：

```bash
source .venv/bin/activate
export EVO_PROVIDER=hkgai
python -m evo setup-key --provider hkgai
python -m evo provider-info
```

使用 DeepSeek 时将上面两处 `hkgai` 改为 `deepseek`。也可以通过 `HKGAI_API_KEY` 或 `DEEPSEEK_API_KEY` 提供已有密钥。`provider-info` 验证本地配置；`python -m evo check-provider --live` 会向所选提供方发送真实请求。

真实任务使用原生 Claude Code CLI 作为执行器，模型请求由所选课程提供方处理。安装 CLI 并使 `claude` 位于 PATH，或通过 `EVO_CLAUDE_CLI` 指定路径。运行用户需要访问本地 Docker Engine socket。用项目定义的镜像准备 Python 和 Git 工具：

```bash
docker --host unix:///var/run/docker.sock version
claude --version
python -c 'from evo.adapters.tools.linux import DEFAULT_IMAGE; print(DEFAULT_IMAGE)' | xargs docker pull
python -c 'from evo.adapters.tools.git import DEFAULT_GIT_IMAGE; print(DEFAULT_GIT_IMAGE)' | xargs docker pull
```

安装参考见 [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) 和 [Claude Code](https://code.claude.com/docs/en/setup)。

## 工作区聊天

从一级菜单进入「聊天」，可打开工作区群聊，或在二级菜单点「新建聊天」并选择 Agent。每次新建都会产生独立的私聊，切换会话或刷新浏览器后仍可继续。已有私聊会按 Agent 显示在会话列表中。

点「新建群聊」可以填写群名并选择参与的 Agent。每个群会单独保存历史，只有群成员会收到消息并回复。工单里的「关联工作区待归档的群聊」使用默认的「工作区群聊」。

## 日常工单流程

在「工作区」的「小队配置」表单中修改名称、选择队长和勾选成员。名称支持中文，队长会自动加入成员。改名后，已指派给该小队的工单会显示新名称。在「Agent 专长」中可以单独编辑并保存每个 Agent 的专长。

指派给小队时，第一轮由队长运行。队长通过评论 `@` 成员，成员的运行单独显示在同一张工单，回报后队长再协调下一步。

新建时选负责人，或者在工单详情里指派。可执行的工单会立即加入 SQLite 队列。待办池里已指派的工单移到「待处理」时才入队。用评论里的 `@agent-name` 发起独立运行，原负责人和工单状态保持。Agent 的运行结果会写回讨论区，交付用「待审查」和「已完成」等工单状态确认。

「运行记录」会显示队列状态、Claude Code 原生工具的开始和执行结果。Agent 可以在执行期间请求转入「待审查」，状态在运行成功且产物归档后更新。排队中的运行可以撤销，停止正在运行的任务会清理容器。重试会建立关联的新运行并启动独立沙箱。上次进程中断且无法确认清理的运行会标记为「执行中断」，供人工核对。

CLI 和 Web 使用同一状态目录和队列。下面的 CLI 命令具体由同一状态目录下正在运行的 Web 进程领取。Web 暂未启动时，提交会保留在队列里。

```bash
python -m evo --state .evo/local-workbench configure --file team.json
python -m evo --state .evo/local-workbench create 'Write the report to report.md' --squad reports
python -m evo --state .evo/local-workbench comment ISSUE_ID '@reviewer Check the evidence'
python -m evo --state .evo/local-workbench task ISSUE_ID
python -m evo --state .evo/local-workbench stop-run ISSUE_ID RUN_ID
python -m evo --state .evo/local-workbench retry-run ISSUE_ID RUN_ID
python -m evo --state .evo/local-workbench issue-status ISSUE_ID done
```

工单的 Claude Code 运行使用独立的 Docker 沙箱，在 `/workspace` 中可以调用原生 Bash、Read 和 Write。本工单已归档的文本产物可以作为只读输入。运行成功后，Evo 会归档 `/workspace/output` 中支持的 UTF‑8 文件，包括 Markdown、纯文本、HTML、Python、JSON、YAML 和 CSV。归档上限为每个文件 1 MiB、每轮合计 2 MiB、最多 32 个文件。

课程评测单独从工作区的 JSONL 评测或 `eval` 启动。[课程评测指南](MID_TEST_RUNBOOK.md)
