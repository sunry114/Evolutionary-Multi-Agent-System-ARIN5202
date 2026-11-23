# 本地运行指南

CI 基线为 Ubuntu 22.04 x86_64 和 Python 3.10。进入仓库目录，按 [README](../README.md) 创建虚拟环境并安装，然后在本机启动 SQLite 工作台。

```bash
bash scripts/start_linux.sh 8769
```

浏览器地址为 <http://127.0.0.1:8769>。启动脚本使用非 root Linux 用户。WSL 用户可在 Linux 终端中执行相同命令。

## 创建和切换项目

在首页点「新建项目」，填写项目名称及已有本机目录的绝对路径。进入项目后，可从一级导航切换项目或返回项目列表。Agent、小队、聊天、工单和运行记录按项目独立保存。通过「项目设置」的「管理 Agent」设置成员。

CLI 使用与 Web 相同的状态目录。工单命令通过 `--project` 选择项目名称或 ID。旧版无项目工单已从工作台和工单命令中移除，原 SQLite 文件保留在原状态目录中。

```bash
.venv/bin/python -m evo --state .evo/local-workbench create-project "软件工程" /path/to/existing/project
.venv/bin/python -m evo --state .evo/local-workbench projects
.venv/bin/python -m evo --state .evo/local-workbench --project "软件工程" tasks
```

## 模型和执行器

课程后端支持 HKGAI 和 DeepSeek。在同一终端选择提供方，并按隐藏提示保存密钥：

```bash
source .venv/bin/activate
export EVO_PROVIDER=hkgai
python -m evo setup-key --provider hkgai
python -m evo provider-info
```

使用 DeepSeek 时将上面两处 `hkgai` 改为 `deepseek`。也可以通过 `HKGAI_API_KEY` 或 `DEEPSEEK_API_KEY` 提供已有密钥。`provider-info` 验证本地配置；`python -m evo check-provider --live` 会向所选提供方发送真实请求。

工单使用宿主机的 Claude Code CLI。让 `claude` 位于 PATH，或通过 `EVO_CLAUDE_CLI` 指定路径。DeepSeek 使用官方 Anthropic 兼容端点，HKGAI 使用工作台启动的本地协议代理。安装参考见 [Claude Code](https://code.claude.com/docs/en/setup)。

```bash
claude --version
```

Claude Code 以工作台用户的权限在绑定目录运行，使用宿主机的文件系统、网络和工具。工单需要的 `curl`、Node.js 或 Python 库可在宿主机或项目自己的环境中安装。宿主机 Claude Code 工单默认无单轮时限，`EVO_CLAUDE_TIMEOUT=none` 可显式启用此行为，设为正数可指定最多 600 秒的单轮时限。浏览器检查、网页抓取等单次操作保留独立超时。

在仓库根目录运行 `npm install --prefix tests/browser --ignore-scripts --package-lock=false` 和 `npm exec --prefix tests/browser -- playwright install chromium`，即可让宿主机工单使用现有验收测试的 Playwright。独立安装工作台时，用 `EVO_PLAYWRIGHT_MODULE` 指向对应的 Playwright 包目录。Claude Code 可从项目目录调用 `python -m evo.adapters.harness.browser_check` 检查本地 HTML 或环回 URL，必要时用 `--serve` 启动和回收专用测试服务器。

Midtest 的常用依赖可以一次性装进工作台的虚拟环境。启动脚本会把这个环境放进工单的 PATH。

```bash
.venv/bin/python -m pip install -e '.[midtest]'
install -d -m 700 .venv/nltk_data/corpora
curl -fsSL https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/cmudict.zip \
  -o .venv/nltk_data/corpora/cmudict.zip
.venv/bin/python -m zipfile -e .venv/nltk_data/corpora/cmudict.zip .venv/nltk_data/corpora
chmod -R go-w .venv/nltk_data/corpora/cmudict
```

## 工作区聊天

从一级菜单进入「聊天」，可打开工作区群聊，或在二级菜单点「新建聊天」并选择 Agent。每次新建都会产生独立的私聊，切换会话或刷新浏览器后仍可继续。已有私聊会按 Agent 显示在会话列表中。

点「新建群聊」可以填写群名并选择参与的 Agent。每个群会单独保存历史，只有群成员会收到消息并回复。新建工单时，或打开已有工单的「讨论」后，可以用「关联群聊」带入自建群当前的聊天记录。选择「工作区群聊」会把默认群的待归档消息转入工单。

## 日常工单流程

在「项目设置」的「小队配置」中修改名称、选择队长和勾选成员。名称支持中文，队长会自动加入成员。改名后，已指派给该小队的工单会显示新名称。在「Agent 专长」中可以单独编辑并保存每个 Agent 的专长。

指派给小队时，第一轮由队长运行。工单每轮都会带上当前范围、小队成员名单和各成员专长。队长用 `assign_issue_work` 做明确派工，成员回报会唤醒队长，同一成员的待处理触发会合并。队长在交付就绪时请求验收，成功签核后平台停止剩余的内部重复轮次。

新建时选负责人，或者在工单详情里指派。可执行的工单会立即加入 SQLite 队列。待办池里已指派的工单移到「待处理」时才入队。用 `@agent-name` 配合「评论并唤醒」启动运行，「发表评论」只保存消息。在待审查的工单唤醒负责人会开启后续工作。Agent 的运行结果会写回讨论区。

「运行记录」会显示队列状态和 Claude Code 原生工具的执行结果。Agent 可请求转入「待审查」，状态在运行成功且产物归档后更新。排队中的运行可以撤销，停止正在运行的任务会终止其进程组。同一工单由同一个 Agent 接续时，继续最近一次成功的 Claude Code 会话。显式重试会建立关联的新运行和新会话。

CLI 和 Web 使用同一状态目录和队列。下面的 CLI 命令具体由同一状态目录下正在运行的 Web 进程领取。Web 暂未启动时，提交会保留在队列里。

```bash
e() { python -m evo --state .evo/local-workbench --project "软件工程" "$@"; }
e configure --file team.json
e create 'Write the report to the output directory provided in the issue' --squad reports
e comment ISSUE_ID '@reviewer Check the evidence'
e task ISSUE_ID
e stop-run ISSUE_ID RUN_ID
e retry-run ISSUE_ID RUN_ID
e issue-status ISSUE_ID done
```

Claude Code 的工作目录就是绑定的项目目录，修改项目源文件会直接落盘。每轮工单的交付目录是项目下的 `.evo/artifacts/<工单 ID>/<运行 ID>/`。「运行记录」会列出目录，后续轮次也可读取之前的产物。Evo 为交付目录里的常用 UTF‑8 文件保存网页预览，每个文件最多 1 MiB、每轮合计 2 MiB、最多 32 个文件。其他文件也会留在项目目录。

Claude Code 的 JSON 输出按单条事件检查，默认上限为 32 MiB。工单「运行记录」中的工具结果预览最多保留 8 KiB，超长时会标记已截断。运行日志与恢复检查点分别保存。

课程评测可从项目设置的 JSONL 评测或 `eval` 启动。[课程评测指南](MID_TEST_RUNBOOK.md)
