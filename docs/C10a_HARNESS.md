# C10a：可替换的单 Agent Harness

实现与本机验证日期：2026-11-15（Asia/Shanghai）。本阶段提供 Harness 契约、
Baseline/Claude Code Adapter、平台 MCP 桥和可恢复检查点。C11 仍负责 DAG 调度、
委派、目标客观验证及 CLI/Web 的完整任务入口；C13 负责正式评测。

## 接口与选择

`evo.ports.harness` 定义 `RunRequest`、`RunHandle`、`HarnessEvent`、`RunResult`、
`ArtifactRef`、`ResumeRef` 和 `HarnessAdapter`。所有 Adapter 实现：

| 方法 | 行为 |
|---|---|
| capabilities() | 声明事件、取消、恢复方式及能力边界 |
| start(request) | 校验任务 owner、Agent、绝对工作区和状态，异步启动，返回 handle |
| events(handle, after=0) | 按序号读取持久化的归一化事件，支持增量游标 |
| wait(handle, timeout=None) | 返回终态结果；等待超时返回 None，不取消运行 |
| cancel(handle) | 停止 CLI，清理容器，等待正在执行的平台动作完成后返回结果 |
| resume(reference, request) | 校验原请求、Agent、任务、会话范围与后端，恢复到新的 run |

`create_harness(HarnessConfig(...), state_root, broker, ...)` 按配置选择实现。
`baseline` 和 `claude_code` 已实现；`pi` 和 `custom` 保留扩展位置，选择它们会明确报错，
不会静默切换其他执行器。Harness 和 SQLite 必须使用相同 state root。

`BaselineHarnessAdapter` 执行 `RunRequest.actions` 中的显式平台动作，复用 C10，
适合兼容测试。当前仓库没有可封装的完整旧模型循环，因此空 actions 会明确失败，
不会把仅接收 prompt 宣称为完成任务。C11 可以通过同一接口接入未来 baseline 循环。

`RunResult.status=success` 只表示本次 Harness 正常结束。它不把数据库 task 标记为
success，不替代 C11 的客观检查或人工 review。工具失败、拒绝、超时和基础设施失败
均有独立结果或错误分类。所有模型、网页和工具内容继续视为不可信数据。

## Claude Code 运行边界

首个受支持 CLI 版本为 **原生 Linux x86_64 Claude Code 2.1.311**。运行时在容器内
检查完整版本字符串，版本不匹配返回 `cli_version_mismatch`。使用其他版本前应更新
配置并重新执行协议测试和显式真实冒烟。npm 的 JS launcher 当前不支持直接挂载。

容器沿用 C09 已锁 digest 的 Python 3.11.11 slim bookworm 镜像，并设置：

- `--network none`、非 root 宿主 UID/GID、只读根文件系统、drop ALL capabilities、
  no-new-privileges、1 CPU、1 GiB 内存、128 PID 和受限 tmpfs。
- 只读挂载 CLI 原生二进制、可信 Python relay 和本次运行的私有 Unix socket 目录。
  **不挂载真实 Agent 工作区、SQLite、记忆数据库、宿主 HOME 或 Docker socket。**
- `/workspace` 是本次运行的临时 scratch。模型通过 MCP 操作绑定到当前 Agent 的真实
  工作区；临时 HOME 和 scratch 随容器销毁。
- `--bare --tools '' --strict-mcp-config --setting-sources ''` 关闭自动发现的 hooks、
  plugins、CLAUDE.md 和原生工具；`--allowedTools` 只列出实际提供的 `mcp__evo__...`。
  不使用绕过权限的标志。初始化输出出现额外工具或 MCP 连接失败会拒绝运行。
- CLI 使用非交互 `--print --output-format stream-json --verbose`；prompt 从 stdin
  传入。stdout 和 stderr 同时排空并共享字节上限，stderr 不原样记录。

CLI 无法直接访问外部网络。可信容器 relay 把内部 loopback 请求转到宿主模型代理的
Unix socket；宿主代理只连接可信配置指定的一个后端，仅开放 Messages/count_tokens
路由及明确的 beta 查询，不允许任意 URL、重定向、其他路径或 CLI 选择模型。
真实认证头由宿主代理注入；CLI 仅持有占位 key。代理不读取环境代理设置，不转发
Cookie/Location，并限制连接数、请求数、响应量和连接超时。

Docker 不可用、镜像缺失、CLI 无法启动、协议错误或资源超限均失败关闭，
不会回退为宿主机运行 CLI 或模型生成的代码。

## 模型配置与本机第三方 endpoint

`ProviderConfig` 继续负责原有 Chat Completions 配置。
`HarnessModelConfig` 单独描述 Anthropic wire protocol 后端：

- 正式模式仅允许明确列出的 DeepSeek/HKGAI endpoint；DeepSeek 的 Harness endpoint
  为 `https://api.deepseek.com/anthropic`。HKGAI 的该协议兼容性仍需单独真实验证。
- 第三方或 HTTP endpoint 必须使用 `provider='development', development=True`。
  `evaluation=True` 拒绝开发配置；开发冒烟不属于官方评测结果。
- 凭据由可信 `credential_loader` 提供，配置对象、命令行、Docker 环境和 checkpoint
  不保存真实 key。归一化输出还会过滤已加载的模型凭据。

显式读取本机 Claude 配置：

```python
from evo.adapters.harness.backend import local_claude_backend

model, credential_loader = local_claude_backend(development=True)
```

默认只读取 `~/.claude/settings.json` 中必要的 endpoint、model 和认证信息；
不导入 hooks、plugins、权限或其他环境变量。配置文件必须为当前用户拥有、
不可被组或其他用户写入的普通文件，拒绝文件符号链接，建议使用 `0600`。
读取后若 endpoint/model 改变，旧 loader 会拒绝继续使用。也可传入一个私有配置快照。
用户的全局 Claude 配置不会被本适配器修改。

## 工具桥与审批

`PlatformToolBroker` 由可信应用构造，持有当前 Agent 的 `ActionExecutor`、workspace，
以及可选的 C07 `ArtifactExchange` 和 C08 Web port。MCP 不接收模型提供的 Agent、
task、host path、approved、token 或容器参数。

基础工具包含 read/write/list、Python 和 Git；配置额外端口后才暴露 Web/工件工具。
参数 schema 和执行时类型校验均生效，未知参数拒绝。每次调用均保留 C05/C10 审计。
文件内容、模型文本和完整诊断不进入 SQLite 动作摘要；恢复上下文保存在私有检查点。

`run_python/delete_file/git_push`，以及高风险策略下的 commit，经过原有审批入口。
未批准时不会执行动作：

1. C10 原子保存精确动作和审批请求，task 进入 waiting_human。
2. Harness 保存 pending 工具调用及 request ID，终止并清理 CLI 容器，返回 waiting_human。
3. 人类通过现有 CLI/Web 回答。C10a 不后台自动轮询或批准。
4. 调度方调用 resume：仍未回答则保持 waiting_human；批准后通过原 ActionExecutor
   再次验证摘要与 scope，原子消费批准，执行一次；拒绝/过期不会启动新 CLI。
5. 成功动作的真实返回值进入恢复上下文，CLI 使用新的原生 session ID 继续。

Python 仍在 C09 的独立无网络容器运行；Git 继续使用 C10 的 URL、分支和凭据约束。
默认不向 Harness 配置 GitHub token。

## 恢复、并发与失败处理

检查点位于 `.evo/harness/<run_id>.json`，使用 no-follow 文件访问、私有权限和原子替换。
它保存原请求、授权记忆、平台调用结果、工件摘要、pending 动作和原生 session ID。
SQLite trace 记录对应的 harness_started/harness_finished 和 run ID，可关联具体任务。

本实现明确使用 **context_restart**：传入保存的上下文，启动一个新 CLI 会话。
CLI 使用 `--no-session-persistence`，因此不依赖其私有 session 文件格式，也不使用
模糊的 `--continue`。这符合计划允许的“以保存的任务上下文重新启动”恢复路径。
它不保留未记录的模型内部推理或 CLI 的原生对话历史。

任务级 `flock` 保证同一 state root 下同一工作项同时只有一个 Harness；切换 Adapter、
Agent 或会话范围不能绕过锁。额外的 Agent 锁防止多个工作项并发修改同一个工作区。
resume 要求原请求、Agent、task、会话范围、工作区和
后端身份匹配。完成的关键动作不能通过旧 checkpoint 重放已消费的批准。

在工具副作用与检查点之间丢失进程时，inflight 标记阻止自动重放，需人工核实。
容器清理失败返回 cleanup_failed，并创建 `task-<task_id>.quarantine`，跨重启禁止
继续调度。只有可信运维确认相关容器已停止并核实动作状态后，才能移除该标记。
不要直接重放 running 动作或复用已消费批准。

取消/超时会杀死本地 CLI attach 的进程组，并强制删除 CLI 容器及其子进程。
已经开始的平台工具受各自 C09/C10 超时约束；Harness 等待它返回后才发布终态，
不承诺撤销已发生的副作用。cancel 在清理仍未完成时明确报告 pending，不能视为成功。

默认最多 20 次平台工具调用（跨恢复累计）、4 MiB CLI 输出、180 秒 CLI 运行，
单次工具参数最多 64 KiB、工具结果截断到受限恢复上下文。启动和清理另有独立的
有界超时。Pi/自研 Harness 可复用契约和 broker，但必须提供自己的隔离与生命周期实现。

## 接入示例

```python
from pathlib import Path
from evo.adapters.harness import create_harness
from evo.adapters.harness.mcp_bridge import PlatformToolBroker
from evo.config import HarnessConfig
from evo.ports.harness import RunRequest

# executor 为 C10 的可信 ActionExecutor，任务已创建且 owner 对应，状态为 running。
# model / credential_loader 由正式配置或显式本机开发配置提供。
broker = PlatformToolBroker(executor)
harness = create_harness(HarnessConfig(adapter='claude_code'), Path('.evo'), broker,
                         model=model, credential_loader=credential_loader)
request = RunRequest(task_id, executor.agent, 'conversation-1',
                     broker.workspace.workspace, '生成 report.md 并检查内容。',
                     role='只处理此工作项。', memory=())
handle = harness.start(request)
result = harness.wait(handle, timeout=30)
# result 为 None 时继续 wait；waiting_human 时由现有 CLI/Web 接收明确回答。
# 回答后：handle = harness.resume(result.resume_ref, request)
# C11 根据 result、events 和工作区中的客观证据决定整个 task 的结果。
```

## 复现与验证

普通测试不需要 native CLI、真实模型 key 或公网，默认使用真实假 CLI 子进程：

```bash
python -W error::ResourceWarning -m unittest discover -s tests -v
EVO_DOCKER_TESTS=1 python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tests scripts
```

新增测试覆盖 Baseline、持久审批/恢复/重放拒绝、跨范围恢复、任务独占、取消与子进程
终止、stdout/stderr 上限、错误退出/协议、原生工具/MCP 拒绝、后端白名单、代理路由与
重定向、凭据过滤、Docker 命令边界，以及清理失败的持久隔离。

显式真实开发冒烟（可能产生模型费用）：

```bash
python scripts/harness_smoke.py --live --development --approve-smoke-code
```

该 flag 仅预批准本次隔离任务中固定 `smoke.py` 的精确内容，仍通过 C10 的持久化
审批和一次性消费；不自动批准任意生成代码、删除或 push。可用
`--local-claude-settings /absolute/private/settings.json` 指定私有配置快照。
报告在被 Git 忽略的 `.evo/harness-smoke/latest-smoke.json`；任务 trace 和检查点同目录保留。

本机真实验证使用 Claude Code 2.1.311 与用户授权的第三方 endpoint，完成文件生成、
读取验证、审批暂停、Docker Python 执行和上下文恢复。第三方服务的实际底层模型身份
不作为 DeepSeek/HKGAI 合规证据，Ubuntu 22.04 远端 CI 与正式评测仍待各自验证。

技术依据：[Claude Code 非交互运行](https://code.claude.com/docs/en/headless)、
[CLI 参数](https://code.claude.com/docs/en/cli-reference)、
[DeepSeek 官方 Claude Code 接入](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)、
[MCP stdio 协议](https://ts.sdk.modelcontextprotocol.io/v2/serving/stdio)。
