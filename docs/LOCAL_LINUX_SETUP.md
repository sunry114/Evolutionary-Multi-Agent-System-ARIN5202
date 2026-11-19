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

## 配置 Squad、创建和启动工单

将工作区配置保存为 `team.json`。队长必须是 Squad 成员，所有成员必须出现在 `agents` 中。

```json
{
  "agents": [
    {"name": "builder", "specialty": "制作交付物"},
    {"name": "reviewer", "specialty": "验证交付物"}
  ],
  "squad": {"name": "reports", "lead": "builder", "members": ["builder", "reviewer"]}
}
```

```bash
python -m evo --state .evo/local-workbench configure --file team.json
python -m evo --state .evo/local-workbench create 'Create hello.md containing hello' \
  --squad reports --expect-file hello.md --expect-text hello
python -m evo --state .evo/local-workbench tasks
# 将 ISSUE_ID 替换为建单返回的 task.id
python -m evo --state .evo/local-workbench start ISSUE_ID
python -m evo --state .evo/local-workbench task ISSUE_ID
```

建单和查询可以离线完成。用 `--squad reports` 指派后，队长提名、成员协商接单。使用 `--agent builder` 时，由指定成员直接接单。启动会保存本次路由、角色与 Squad 快照。成员在协商和执行时读取自己的相关经验。建单时用 `--no-memory` 关闭本次工单的经验读写。先通过 `group` 讨论，再用 `create --from-group` 将尚未关联工单的讨论绑定到新工单。

用同一工单 ID 执行 `task` 查看最终负责人、职责和阻塞原因，用 `trace` 查看每轮提名、回复和确认。每个子任务最多协商三轮。若需确认是否改变用户目标，用 `answer ISSUE_ID REQUEST_ID --text '回复'` 保存答案。审批使用 `answer ISSUE_ID REQUEST_ID --approve` 或 `--deny`。保存决定后用 `resume` 恢复等待中的运行，并沿用建单时的 `--state`。

## 成员委派与恢复

执行中的成员可以通过 `delegate` 提出有明确验收条件的子任务，由受邀成员协商后接单。每条链最多允许两层成员委派。父任务暂停执行，子任务验收通过后会将产物共享给发起人。父任务恢复时取得原委派结果，可用 `import_artifact` 导入产物并整合。

嵌套审批期间，`task ISSUE_ID` 会显示委派链、审批 ID、执行成员和待批准的操作。记录批准或拒绝后，对根工单执行 `resume ISSUE_ID`。拒绝和验收失败会沿原链返回，依赖失败任务的后续工作会阻塞。相同委派请求复用原子任务和结果，重复审批或恢复不会再次执行工具。

## 工单工作台

在 Web 的「工单列表」按状态、负责人或标题筛选。新建工单时可关闭本工单的个人记忆。进入详情后查看「下一步」，在同一页查看讨论、分派协商、运行记录、产物、审批与审查及修订。产物预览读取对应运行的快照，旧版和修订版可分别查看。

CLI 可以先用 `create PROMPT` 保存草稿，再用 `assign ISSUE_ID --squad reports` 指派，或用 `--agent NAME` 指派给成员。`tasks --owner NAME` 筛选工单，同名时用 `--owner-type agent` 或 `--owner-type squad` 区分。`comment ISSUE_ID MESSAGE` 添加评论，`group MESSAGE --task ISSUE_ID` 在本工单与成员讨论。

退回交付时用 `review RUN_ID changes_requested --feedback MESSAGE` 保存意见，再用 `revision RUN_ID PROMPT` 创建修订。按返回的修订 ID 执行 `start RUN_ID`，审核通过后用 `review RUN_ID accepted` 接受交付。原工单 ID 仍可查看整条修订记录。
