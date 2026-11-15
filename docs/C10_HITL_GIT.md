# C10：持久化人工审批与受限 Git 工具

实现日期：2026-11-15（Asia/Shanghai）。本阶段实现单动作审批、执行和审计，以及
Linux Docker 中的 Git init/status/commit/push。默认 CLI/Web 已支持回答持久化请求；
完整模型循环、任务规划调度、委派和自动唤醒仍由 C11 接入。

## 接入接口

```python
import os
from pathlib import Path
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.docker import DockerPythonRunner
from evo.adapters.tools.git import DockerGitTools, RepositoryToken
from evo.application.approvals import ApprovalService
from evo.application.executor import ActionExecutor

state = Path(".evo")
store = SQLiteStore(state / "events.sqlite3")
workspace = WorkspaceFilesystem(state, "builder")
approvals = ApprovalService(store, max_age_seconds=3600)
git = DockerGitTools(workspace)
executor = ActionExecutor(store, approvals, agent="builder", workspace=workspace,
                          python=DockerPythonRunner(workspace), git=git,
                          require_commit_approval=False)

# 可信调度层创建并分配任务，置为 running 后才可执行动作。
# outcome = executor.execute(task_id, "run_python", {"path": "main.py"})
# 首次返回 waiting_human 和 request_id，不执行代码。
# 人类回答后，由可信调度层对相同动作再次调用 execute，或传入原 request_id。

# 只有明确授权具体 repository 时，才为 Git adapter 配置凭据加载器：
# repository = "https://github.com/OWNER/REPOSITORY.git"
# git = DockerGitTools(workspace, token_provider=RepositoryToken(
#     repository, lambda: os.environ.get("GITHUB_TOKEN")))
```

`WorkspacePort`、`PythonRunnerPort`、`GitPort`、`ApprovalPort` 是应用依赖边界。
代理身份、端口实例、凭据加载器、审批时限和高风险策略只能由可信应用代码设置，
不接受模型传入 `approved`、`token`、容器参数或其他代理的工作区。

`ActionExecutor.execute(task_id, name, arguments, request_id=None)` 支持：

| name | arguments | 默认人工审批 |
|---|---|---|
| run_python | path | 必须 |
| delete_file | path，purpose（默认 document） | 必须 |
| git_init | 空对象 | 不需要 |
| git_status | 空对象 | 不需要 |
| git_commit | paths 显式文件列表，message | 高风险策略开启时必须 |
| git_push | repository 精确 HTTPS URL，branch | 必须 |

动作返回 `waiting_human/blocked/success/failed`。`success` 仅表示这次工具调用成功，
任务整体保持 running，由 C11 验证目标后才能标记任务 success。基础设施或工具执行失败
会记为 action failed、task failed；拒绝或过期会使 task blocked。非法参数/任务所有权/
过期或重放动作的状态冲突直接报错，不执行副作用。等待审批期间不接受新的其他动作。

## 审批与审计

沿用 C05 schema v1 的 actions、approval_requests、events 和 tasks 表，无 schema 迁移。
创建请求和动作、任务进入 waiting_human、写入 request_created 事件位于同一事务。
匹配同一 task/agent/tool/规范参数的请求被去重；并发请求只生成一条。
参数包含路径和 purpose，代码执行/删除还包含文件内容 SHA-256；高风险 commit 包含
所选文件摘要，push 包含当前 commit SHA。模型无法把一个批准换用于另一文件、代理、
任务、仓库、分支或已修改的内容。

人类可通过本地 UI 或 CLI 回答：

```bash
python -m evo --state .evo answer REQUEST_ID --approve
python -m evo --state .evo answer REQUEST_ID --deny
python -m evo --state .evo answer CLARIFICATION_ID --text 'report.md'
```

回答必须是明确布尔值，澄清必须是非空、有长度上限的文本，不能混合两种 schema。
pending → approved/denied/answered 是单次原子决策；重复或并发第二次决策冲突，不产生
脏审计事件。持久化 request 保存 agent、task、参数、created_at、answered_at 和回答，
事件记录人类决策。CLI/Web 回答只持久化决定，不自动调用工具或重启整个 Agent 循环。
HTTP 继续强制 Host/Origin/CSRF/JSON 校验。

执行前在写事务内再次检查请求范围、文件摘要/commit、批准状态、年龄和动作状态，
以 requested → running 原子消费批准。一个请求最多执行一次；重复调用已完成动作
不会重跑。同一任务中需要重新执行相同动作时，应由后续调度层创建明确的新工作项，
不能复用旧批准。拒绝后任务立即 blocked，后续重复请求在执行前停止。

默认有效期为 3600 秒，可信配置可调整至 (0, 86400]。回答、轮询和消费时检查年龄；
过期则持久化 expired、action blocked、task blocked，绝不默认为批准。没有后台到期
线程，C11 应定期轮询等待项。创建、回答和消费均可在新进程中继续；应用在消费后被
强制终止时，动作可能处于 running 且副作用未知，必须由 C11/可信运维核实，不能自动
重放旧批准。数据库持久化不等于实现了完整执行恢复。

动作完成只保存 exit_code、timed_out、output_limited 或固定错误分类，
不保存 stdout/stderr、原始异常或凭据。返回给调用者的工具输出仍为不可信数据，
C11 应继续执行输出脱敏。任务审查 review 状态不被动作状态更新覆盖。

## Git 容器与凭据边界

默认镜像已锁定：

```text
alpine/git:2.49.1@sha256:c0280cf9572316299b08544065d3bf35db65043d5e3963982ec50647d2746e26
```

Git 复用 C09 的非 root、只读根、单工作区挂载、tmpfs、capabilities、资源/时间/输出
限制和强制清理。只调用本地 system Docker socket，不执行宿主 Git，不挂载数据库、
其他工作区或 Docker socket。所有 Docker CLI 使用空临时配置和最小环境。

Git 配置采用保守白名单：只接受标准非 bare 仓库的 core.repositoryformatversion=0、
filemode=true/false、bare=false、logallrefupdates=true。任何 hooksPath、fsmonitor、
credential helper、URL rewrite、remote、include/includeIf、filter、额外 section 或
未知配置均在启动 Git 前拒绝。外部 gitdir/worktree/alternates、符号链接、硬链接、
特殊文件和超过 10000 条目的 Git metadata 也拒绝。此版本不直接接纳带自定义配置的
既有仓库，应使用受控 init 创建仓库；不提供 clone 或配置编辑工具。

运行时再关闭 hooks、fsmonitor、签名、自动维护、全局/系统配置、交互密码提示和
子模块递归；禁用所有传输协议后仅开放 HTTPS。commit 必须使用显式已存在文件清单，
使用 literal pathspec、`--` 分隔和 `commit --only`，即使 index 预先包含其他文件也
不会一并提交。当前 commit 不支持提交删除记录；删除动作由独立审批工具负责。

init/status/commit 使用 `--network none`。push 使用 bridge 网络；这是允许 Git 与
远端 HTTPS 通信的网络模式，**不是 Docker 层的目标地址防火墙**。目标约束由精确
GitHub URL、只执行固定 Git 命令、配置白名单、TLS 校验和禁重定向共同实现。
若部署要求 IP 级出站 ACL，需在宿主额外部署出口策略，不能把当前 bridge 宣称为该能力。

push 只接受 `https://github.com/OWNER/REPO.git` 和受限合法分支。拒绝 URL 凭据、
端口、query、fragment、编码路径、SSH/file/ext、refspec/选项注入及危险分支格式。
只推送批准时固定的 commit 到单个 `refs/heads/branch`，不使用 remote alias、force、
镜像推送或任意 refspec。HEAD 改变会拒绝旧批准。

凭据加载器按完全相同的 repository 字符串绑定；没有 token 或 scope 不匹配不执行
push。token 编码后通过 Docker attach stdin 进入固定 shell wrapper，再放入 Git
进程临时环境中的 URL scoped extraHeader。token 不出现在宿主命令行、Docker create
环境配置、remote、磁盘 Git config 或日志。禁 HTTP redirect，防止跨目标转发授权。
push stdout/stderr 不原样返回，只返回固定成功/失败信息；容器结束后删除。

信任边界沿用 C09：宿主 Docker daemon、固定镜像和可信同 UID 应用进程受信任，
同一工作区执行与变更应串行；不承诺抵御恶意宿主同 UID 进程的并发替换。

## 复现与验证

```bash
python -m pip install -e .
python_image="$(python -c 'from evo.adapters.tools.linux import DEFAULT_IMAGE; print(DEFAULT_IMAGE)')"
git_image="$(python -c 'from evo.adapters.tools.git import DEFAULT_GIT_IMAGE; print(DEFAULT_GIT_IMAGE)')"
docker --host unix:///var/run/docker.sock pull "$python_image"
docker --host unix:///var/run/docker.sock pull "$git_image"
EVO_DOCKER_TESTS=1 python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tests
python scripts/check_secrets.py
git diff --check
EVO_TEST_PYTHON="$(command -v python)" node tests/browser/web_acceptance.cjs
```

浏览器运行依赖 tests/browser 中固定版本的 Playwright 和 Chromium；安装方式见
[浏览器验收文档](WEB_ACCEPTANCE.md)。CI 的 Ubuntu 22.04 / Python 3.10 Docker job
拉取两种固定 digest，强制运行 Python/Git 容器集成测试，保存环境与镜像信息。
普通离线测试允许显式跳过真实 Docker 测试，启用后不允许用 skip 掩盖基础设施失败。

本阶段测试覆盖审批持久化/去重/并发消费、批准/拒绝/过期、摘要和 scope 篡改、
CLI/Web 回答及 CSRF、Git 配置与路径拒绝、凭据传输与输出隐藏、真实 init/status/commit、
禁 hooks、只提交指定文件，以及真实批准后 Python 执行。容器中的 Git URL match
测试验证临时 header 匹配指定仓库且不落盘；它不连接 GitHub，也不推送真实仓库。
真实远端 push 未执行，未使用真实 token；不把测试替身声称为线上成功证据。

技术依据：[Git config（URL 匹配与配置环境）](https://git-scm.com/docs/git-config)、
[Git push（显式 refspec 与递归控制）](https://git-scm.com/docs/git-push)。
