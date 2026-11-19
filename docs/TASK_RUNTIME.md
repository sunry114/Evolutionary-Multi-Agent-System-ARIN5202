# 后端任务执行重构

本次只调整后端执行链路；沿用任务看板、群聊、私聊、审批和最终文件检查的现有 API。

## 参考与取舍

研究了 [Multica](https://github.com/multica-ai/multica) 的公开文档和以下实现：

- [CLI / daemon](https://github.com/multica-ai/multica/blob/main/CLI_AND_DAEMON.md)：任务与运行分离、工作目录隔离、执行日志和生命周期管理。
- [prompt.go](https://github.com/multica-ai/multica/blob/main/server/internal/daemon/prompt.go)：给子任务补充父任务上下文，交付失败后提供明确的修复反馈。
- [dispatch/reason.go](https://github.com/multica-ai/multica/blob/main/server/internal/dispatch/reason.go)：在失败发生的位置记录稳定的原因代码。

这里借鉴架构，没有引入 Multica 服务，也没有复制其 Go 实现。Multica 主要驱动现成 agent CLI；
本项目需要兼容已有 HKGAI / DeepSeek Chat Completions 接口，因此新增轻量 `provider` Harness。
现有 Claude Code Harness 继续可选，不必让所有简单文件任务经过 CLI、容器模型代理和协议转换。

## 执行流程

1. Planner 默认建议一个子任务；只有不同成果确实需要不同技能时才拆分。
2. 根据技能与角色描述选择唯一负责人；默认无需每位 agent 调用模型竞标。
3. Harness 每轮提出一个工具调用，平台验证并执行，将真实结果记入检查点，再反馈给模型。
4. 工具参数错误可以修正；模型声称完成时，平台检查文件内容、用途及 SHA-256。
   `provider` Harness 会把检查失败反馈给模型，在剩余预算内继续修复。
5. 成功子任务的成果立即生成快照。下游收到原始目标、上游结果摘要及快照 ID，
   通过 `import_artifact` 获取对应版本；后续改写同名文件不会改变先前快照。
6. 根任务仍执行独立的最终成果检查，现有人工验收流程保留。

群聊仍是讨论入口，其回复不作为任务完成证据。实际协作由持久化 DAG 的依赖关系驱动；
默认执行 Harness 不暴露递归 `delegate` 工具，避免阻塞等待、工作目录锁冲突和未能恢复的嵌套审批。
旧 baseline 实验接口及直接调用 `Orchestrator.delegate` 的接口仍保留。

## 使用

已有 provider 凭据配置后，正常启动原来的 Web 页面即可使用新默认后端：

```bash
python -m evo --state .evo web
python -m evo --state .evo run 'Create hello.md containing hello' \
  --expect-file hello.md --expect-text hello
```

可选设置：

| 设置 | 默认值 | 用途 |
| --- | --- | --- |
| `EVO_HARNESS_ADAPTER` | `provider` | `claude_code` 使用原 CLI，`baseline` 执行显式动作序列 |
| `EVO_ASSIGNMENT_STRATEGY` | `routing` | `bidding` 恢复全员竞标实验；baseline 默认 bidding |
| `EVO_MAX_TOOL_CALLS` | `80` | 每个子任务 1–128 次，审批恢复不重置 |

新 provider / Claude 任务使用 `.evo/tasks/<root-id>/agents/<agent>/workspace/`。
成果快照位于同一任务的 `artifacts/`，其他任务无法通过成果工具列出或导入它们。
baseline 及旧任务继续使用原来的 agent 工作目录。运行检查点仍位于 `.evo/harness/`。
工作目录锁允许不同任务使用同一个 agent，但同一个工作目录最多有一个执行器。

任务保存 Harness 配置、分派策略和工作目录模式。恢复时采用保存的配置，
后续步骤仍采用任务原来的分派策略。模型身份改变会被 Harness 拒绝；应恢复原 provider / 模型配置。
旧检查点支持缺少新增字段的情况；旧任务根据可信日志识别原 Harness，保留原始提示词及工作目录。

审批仍绑定任务、参数和文件摘要；恢复只消费一次许可。已完成的关键动作不会因恢复重放。
搜索次数和工具调用预算均跨审批恢复累计。副作用发生但结果未知时仍停止等待核对，不盲目重试。

## 验证与边界

### 执行超时与文件类型

默认执行工厂的单次 Harness 时间预算为 600 秒，可在启动服务前通过
`EVO_HARNESS_TIMEOUT` 显式设置（大于 0、不超过 600 秒）。
`EVO_TIMEOUT` 仍只控制单次模型请求。历史任务继续保留其保存的执行配置，
修改默认值不会重置失败任务或自动重跑。

文件 broker 支持 Python、JS/TS、HTML/CSS、SQL 源文件；`write_file`
工具说明包含按用途划分的完整后缀列表。创建源码不代表能执行对应运行时：
当前 `run_python` 只允许 `.py`，隔离环境提供 Python 标准库，没有 Node/npm、
shell 或依赖安装工具。聊天和规划器会获知这些能力，避免自行承诺无法验证的技术栈。
路径拒绝会反馈用途和后缀规则；连续三次无效模型动作会明确报
`invalid_decision_limit`，避免持续空转到超时。真正超时会在 Harness 检查点的
`execution_deadline` 事件中记录时间预算、模型轮数、工具调用及失败次数。
规划器在 `skills` 中给出的精确角色名优先于描述关键词，避免把 `dev` 实现任务分给 `qa`。

### 工具执行前的错误可以修复

关键工具（例如 `run_python`）在请求审批前会读回目标文件并计算摘要。
原先这一准备阶段的错误没有被 broker 分类，未创建脚本就执行会被误记为
`platform_tool_error` 并停止整个 Harness。现在准备阶段显式产生
`tool_file_missing` 或 `tool_preparation_failed`，并反馈给 Agent 修正路径、
补建或导入文件后继续；此时没有执行动作，也没有消耗审批。
SQLite trace 的 `tool_preparation_rejected` 会记录工具名和安全错误码，
不记录原始异常内容。审批范围不匹配、实际执行失败或结果不确定仍按原规则处理。
修复不会自动重置已失败的历史任务或重放动作；历史产物和 trace 保留。

### 确认执行不再等待群聊

创建任务后页面会自动发起群聊。原先群聊在整个模型请求和多轮回复期间
持有 `_chat_lock`，而 `start`、`continue`、`terminate` 也使用这把锁。
因此即使 `start` 已采用后台线程，确认弹窗仍可能等待群聊的全部回复才关闭。

现在聊天只用 `_chat_lock` 保持消息顺序，任务控制改用短时 `_state_lock`，
开始或终止任务无需等待模型。每次聊天消息落库时也在 `_state_lock` 内重查
任务状态；终止与清空消息后，迟到的模型回复不会重新写入消息或触发后续群聊回复。
已经发出的模型请求仍可能持续到返回或超时，但不会阻塞任务控制。
弹窗新增提交中提示，并在弹窗内展示提交错误，交互步骤保持不变。
并发回归测试用阻塞模型验证开始、继续和终止能在模型返回前完成。

### 文件交付的快速收尾

此前 provider 在写入交付文件后还需要模型选择读回文件，再返回 `done`，
这些串行请求会放大模型响应延迟。现在模型可在最终 `write_file` 动作的顶层
设置 `finish: true` 和非空 `text`，表示全部分配工作（包括必要的调研和测试）已完成。
后端仅在当前写入成功、路径匹配 acceptance，并独立读回验证摘要、用途和内容约束
全部通过后直接结束。这个路径可以省去后续读文件及 `done` 的模型往返。
仅仅出现同名文件不会触发提前完成；中间文件、无 acceptance 的任务仍走正常流程。
验证失败会反馈给模型修复，审批与工具预算不变。前端交互和旧动作格式保持兼容。

每轮 provider 调用耗时保存在 `.evo/harness/<run-id>.json` 的 `model_timings`
中，字段为 `turn` 和 `duration_ms`。它衡量完整 provider 调用（包括内部重试），
不代表任务总耗时；审批恢复后的轮次记录在新 run 的检查点中，旧记录可通过
`parent_run_id` 追溯。这个优化不省略规划，也不改变模型服务的响应速度。
新增 8 个离线回归检查快速交付、失败修复、精确内容验证及最大工具预算等行为；
模型往返数的减少已验证，真实服务的耗时收益仍需实测。

`tests/integration/test_task_runtime.py` 用脚本化模型配合真实 SQLite、文件工具、成果交换和 Harness 检查点，
覆盖单任务交付、错误修复、工具预算、任务隔离、并发、快照交接、审批拒绝、恢复和超时后禁止写入。
其余原有回归测试继续验证 CLI、Web API 与旧 Harness 的兼容性。

本次本地验证：新增 20 个执行流程测试通过；完整离线回归 348 项，339 项通过、9 项跳过，
启用 `-W error::ResourceWarning`。编译、`git diff --check` 和仓库密钥模式扫描通过。
跳过项包括需显式启用的真实 Docker 集成测试；本次未进行真实模型任务验收或浏览器自动化验收。

```bash
python -m unittest tests.integration.test_task_runtime -v
python -m unittest discover -s tests -q
```

这些是离线流程测试，不能证明真实模型在“绝大多数简单任务”上的通过率。
适合的范围是当前工具支持的文本写作、翻译、资料整理、HTML / Markdown 报告、JSON / CSV 和简单 Python 任务。
文件工具仍只支持规定后缀的 UTF-8 文本，不支持 PDF、Office 二进制文件或任意 shell。
Python 执行仍需要 Docker 和人工审批；普通文本文件生成不需要 Claude CLI 或 Docker。
provider 请求受 `EVO_TIMEOUT` / `EVO_RETRIES` 限制；Harness 在请求返回后再次检查取消和总时限，
不会执行迟到的工具提案，但不能强制中断已经阻塞在 provider 里的 HTTP 请求。
当前仍是单机串行 DAG，不包含 Multica 的分布式 daemon、消息队列、自动崩溃接管或全部 runtime 功能。
