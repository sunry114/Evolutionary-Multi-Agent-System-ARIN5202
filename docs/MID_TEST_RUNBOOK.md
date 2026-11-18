# Mid Test 执行交接与 D 部分验收

更新：2026-11-18（Asia/Shanghai）。正式测试主责 B（C13），D 负责入口、CI 与报告材料。

## 1. 已核实的仓库进度

本分支基于 main 204882f：C11 编排、C12 记忆、C13 评测和 execution_factory 已合并。
D 此次补齐 memory/metrics 的 CLI/HTTP 实现、网页记忆查询、任务指标按钮和 JSONL
评测表单。保留 run/resume、角色配置、聊天、审批、审查修订及其既有安全边界。

metrics 是数据库中的任务状态和直接子任务统计，耗时为创建到最后终态更新时间，
未结束任务则标 timing_final=false；包含等待人类的时间。它不是老师的质量评分。
memory 只查询选定的已注册 Agent；无记录时不创建数据库，不把失败经验改成成功。
JSONL eval 复用 B 的 runner；其 fresh-case 协议不直接适用于下面的连续场景。

## 2. 老师数据的正确协议

Mid_Test.zip 的 README 要求：3 个场景，各配置 4 个角色；每场景先 Task 1 再 Task 2，
两任务必须保留同组的对话、工作区和记忆；不同场景隔离。人类根据真实对话回答，
不能使用预编写答案或默认批准。任务质量需要完整对话与实际产物证据。

此 ZIP 没有提供机器可执行的官方评分 rubric 或逐子任务断言。不能自行添加几个
关键词并声称那是老师的正式通过标准。现有 C11 要求提交客观 ArtifactCheck；
应先通过群聊协商验收条件，并记录人类确认，再执行任务。若 planner 拆分与人类
确认的检查项不一致，应澄清/调整协议，不能把未验证子任务记作成功。

场景 3 的 HTML 幻灯片使用专用 `slides` 用途写入和交换。执行时使用
`write_file(path="slides.html", purpose="slides", ...)`。每页写成带唯一 ID 的
非空 section，在前一页内用原生片段链接指向下一页。验收会检查本次产物的
哈希、约定内容、页数与链接。另用浏览器检查展示效果，结合研究证据评审内容。

## 3. 本地私有文件位置

原始 ZIP 未更改。实际准备/审计结果均在 Git 忽略的 .evo 下：

- .evo/midtest-20261118/manifest.json：原始 ZIP SHA-256、源文件摘要、场景状态。
- .evo/midtest-20261118/scenario_1/、scenario_2/、scenario_3/：
  每组独立 agents.json，以及 verbatim task_1.txt、task_2.txt。
- .evo/mid-evidence-20261118/audit.json：队友导出数据的只读审计。

原始数据、数据库、聊天内容、任务正文和生成的私有 JSON 不进入 GitHub。
仓库中仅提交通用工具、合成测试和本交接说明。

可复现准备（目标目录必须不存在，且位于 .evo 下）：

```powershell
python -m evo prepare-midtest "D:\Personal\Desktop\Mid_Test.zip" --output .evo/midtest-new
python -m evo audit-midtest "D:\Personal\Desktop\mid-test1&2.zip" --output .evo/mid-audit-new.json
```

两个命令不调用模型、不执行归档代码、不替代正式测试。准备命令校验角色、
完整文件组、路径和体积；拒绝覆盖已有运行状态。ZIP 名称中的空格和 & 要保留引号。

## 4. 队友场景 1/2 包的实际证据

只读检查 mid-test1&2.zip 中两份 events.sqlite3：

| 场景 | 群聊消息 | tasks | events | actions | 可推导正式完成率/执行时间？ |
|---|---:|---:|---:|---:|---|
| scenario-1 | 15 | 0 | 0 | 0 | 不可推导 |
| scenario-2 | 15 | 0 | 0 | 0 | 不可推导 |

这些材料证明群聊发生过，不能证明自动构建、工具执行、测试或产物交付。
审计中的 task_completion_rate 和 task_execution_seconds 均为 null，
不是 0% 或 100%。按消息时间戳算出的 group_response_span_seconds 只是每轮
人类消息至最后 Agent 回复的跨度；不是任务从分配到产物完成的总耗时。
没有推断任务 3 已测试，也没有把文本中的“完成”当作执行证据。

## 5. 交给 B 的正式运行步骤

1. 选统一的 Ubuntu 22.04 x86_64、Python 3.10、Docker 环境，安装约定版本
   Claude Code CLI / Harness，配置课程 HKGAI/DeepSeek。记录版本、commit SHA、
   镜像 digest、数据 ZIP hash、模型和 memory/approval 策略。
2. 在该 Linux 环境重新执行 prepare-midtest。Windows manifest 中的绝对路径
   不能直接复制成 Linux 路径。保持每个场景一个 state，两个任务顺序执行。
3. 使用 python -m evo --state .evo/midtest-new/scenario_1 web。
   核对已配置四角色，先把 task_1.txt 原文发到群聊。根据真实对话回答问题，
   与团队确认目标、交付路径及预期内容，再用 group --execute 发起执行。规划器读取该场景
   的群聊快照，并把协商结果转为子任务及产物检查。也可显式指定 --checks-file；
   经人类审批后显式 resume 根任务。
4. 保留 Task 1 的数据库、工作区和记忆，在同一 state 执行 Task 2。
   只有切换场景时才使用下一场景 state。禁止将这 6 个开放任务直接塞进
   fresh-state JSONL runner 后丢失连续上下文。
5. 使用 task/tasks/trace/messages/memory/metrics 查询与归档真实证据。
   保存文件产物与实际验证输出，不能只导出 group messages。记录失败和阻断。
6. B 按明确 rubric 计算分母、目标/子任务完成率、从分配到最终产物的耗时。
   聊天时间、人类等待、模型/工具时间的口径要说明；D 汇总报告，不改原始结果。
7. 做 memory/no-memory 对照时保留场景顺序，固定模型、审批和评价条件。
   没有真实运行之前，不写记忆提升幅度。

当前这台电脑发现 Ubuntu-24.04 WSL，但没有可用 Docker 命令；这不是团队约定的
正式环境。没有在本次提交中安装/修改系统 Docker，也没有伪造完整六任务评测结果。
B 仍需提供实际使用的 Linux/Docker 环境与任务/产物证据。

## 6. D 的验收边界

本次补的是可用的评测操作入口、记忆/状态指标展示、私有场景准备和可核对的
证据审计。正式 Mid Test 跑分、场景 3 的正式产物和最终记忆消融尚未完成，
不能把这些标为已验收。浏览器/单元测试使用合成夹具，不能代替老师数据的实际表现。
