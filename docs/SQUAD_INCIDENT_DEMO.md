# Squad 首个演示验收记录

本页保存首版课程演示。当前工作台的运行和审查方式见 [本地运行指南](LOCAL_LINUX_SETUP.md#日常工单流程)。

固定响应和真实模型使用同一套合成测试日志、工单目标、三名角色和产物验收条件。每次启动使用新的 SQLite 状态目录，关闭记忆。分析员与验证员应分别从自己的工作区读取同一份日志，委派描述应只传递复核要求。[测试日志](../examples/squad_incident/test-log.txt) · [工单目标](../examples/squad_incident/goal.txt) · [角色配置](../examples/squad_incident/roles.json) · [验收条件](../examples/squad_incident/checks.json)

固定响应和真实 DeepSeek 工单均已验收。以下事件编号来自各自的 SQLite 工单。[工单事件与输入哈希](squad_incident/events.json)

## 固定响应

工单 `8a0bc60297804382ab67cc57a82d20d5`。固定响应替代模型规划、协商和 Claude CLI 输出，仍经过实际 SQLite 工单、编排器、MCP 工具和产物交换。2026 年 11 月 19 日 16:59（北京时间），Codex 对照日志逐项审阅两个产物，并通过 CLI 人工审查入口提交 `accepted`。工单最终状态为 `success`，看板列为 `done`。

| 工单内事件 | 记录 |
|---|---|
| 提名与确认 | #13 队长向分析员发出原职责。#15 分析员提出由验证员独立检查日志，自己负责根因分析和汇总。#17 队长采用原文，#19 分析员接单，#20 将约定职责写入执行描述。 |
| 主动委派 | #28 `analyst` 发起委派 `ca7e48a19773444deb031672172e56dc`。#35 `reviewer` 接单，#41 读取自己的日志，#48 将验证过的 `review.md` 返回分析员。 |
| 汇总与验收 | #52–#56 分析员导入并读取 `review.md`，写出 `report.md`。#59 验证报告快照，#62 记录 `pending → accepted`，事件归在根工单，actor 为 `human`。 |

审阅确认报告准确引用了 A17 的 `ENOSPC`、清理恢复的 67,108,864 字节和 A18 的一次通过，并逐字引用复核结论。证据支持本次重跑恢复，长期稳定性仍待后续测试。[report.md](squad_incident/scripted/report.md) · [review.md](squad_incident/scripted/review.md)

## 真实 DeepSeek

工单 `c612572cb47346ad8ef217ce13bb3a9b` 使用 `https://api.deepseek.com` 的 `deepseek-flash`。规划和协商走 DeepSeek API，执行使用原生 Claude Code CLI 和同一服务的 Anthropic 兼容接口。技术执行耗时 75.2 秒。2026 年 11 月 19 日 17:18（北京时间），Codex 对照原始日志与两份快照，通过 CLI 人工审查入口提交 `accepted`。该工单最终状态为 `success`，看板列为 `done`。

| 工单内事件 | 记录 |
|---|---|
| 提名与确认 | #13 队长提名分析员，#15 分析员反提议独立复核，#17 队长逐字采用反提议，#19–#20 分析员接单并写入最终职责。 |
| 主动委派 | #28 分析员发起委派 `c1e67312c136aa135a9eb951de3332d2`，委派描述只包含复核要求。#35 验证员接单，#41–#42 独立读取日志，#46–#48 完成复核快照校验并交回。 |
| 汇总与验收 | #52–#55 分析员导入并读取复核，#56–#59 写入并校验报告，#62 记录 `pending → accepted`，事件归在根工单，actor 为 `human`。 |

验收核对了故障、清理和重跑的日志行，以及报告中逐字引用的复核结论。临时卷零可用空间、`ENOSPC` 和 A18 的一次通过均有日志依据。过期文件积累、清理机制和容量预检属于待验证推断，已写入验收意见。[report.md](squad_incident/deepseek/report.md) · [review.md](squad_incident/deepseek/review.md)

DeepSeek 首次尝试中，Claude Code 的非初始化系统消息反复占用启动事件额度。重跑又出现反提议缺少 `scope` 字段。执行器改为只在初始化时记录一次启动事件，协商提示补齐反提议的 JSON 示例。[执行器回归测试](../tests/integration/test_claude_code_adapter.py)

## HKGAI 尝试记录

工单 `b2bf8fc76dbc4d97b0300356fa6070c8` 完成职责协商。#30 记录分析员发起委派，#37 记录验证员接单。#43–#44 记录验证员以 `purpose=data` 读取 `test-log.txt`，工作区规则拒绝操作，工单失败。最终场景在共用目标和角色配置中明确 TXT 和 Markdown 使用 `purpose=document`，固定响应与 HKGAI 依次从新工单复跑。

工单 `fceb12e0d8e24317baf7b90af07d2958` 中，验证员以 `purpose=document` 成功读取日志并写出 `review.md`（#41–#44），等待模型结束响应时达到 180 秒执行器限时。#47 记录委派以 `Harness timed_out` 结束，复核初稿留在验证员工作区。[真实 HKGAI 复核初稿](squad_incident/hkgai/reviewer-draft.md)

工单 `1f93bce748fc46ce93bfbfcec48f40c8` 沿用相同输入，模型请求限时为 90 秒，执行器限时为 480 秒。上游规划请求重试后失败（#5–#6）。对课程端点 `https://api1.hkgai.net/v1` 和模型 `t2_hkgai-v3_fp8_1m_e7` 进行独立短 JSON 检查，17:08 和 17:09（北京时间）两次复查均返回 HTTP 502。

## 复跑

固定响应用例使用 `.venv/bin/python -m unittest tests.e2e.test_squad_incident_demo -v`，会检查同一工单的协商职责、委派来源、产物与审查状态。

真实模型复跑使用 DeepSeek 的 `deepseek-flash`、已配置的 `DEEPSEEK_API_KEY` 和宿主机 Claude Code CLI。在仓库根目录激活虚拟环境后运行以下命令。`prepare_issue` 使用相同场景文件，通过评测接口建单，并为两个成员分别放入输入日志。

```bash
mkdir -p .evo
state=$(mktemp -d "$PWD/.evo/squad-incident-deepseek-XXXXXX")
EVO_PROVIDER=deepseek EVO_MODEL=deepseek-flash EVO_BASE_URL=https://api.deepseek.com \
  EVO_HARNESS_ADAPTER=claude_code EVO_TIMEOUT=90 python - "$state" <<'PY'
import sys
from dataclasses import replace
from unittest.mock import patch

from evo.adapters.harness import create_harness
from evo.interfaces.sqlite_application import SQLiteApplication
from tests.e2e.test_squad_incident_demo import prepare_issue

def create_with_longer_timeout(config, state, broker, **options):
    return create_harness(replace(config, timeout=480), state, broker, **options)

app = SQLiteApplication(sys.argv[1])
issue = prepare_issue(app)
(app.state / "issue-id.txt").write_text(issue + "\n")
print(issue, flush=True)
with patch("evo.interfaces.execution_factory.create_harness", side_effect=create_with_longer_timeout):
    result = app.dispatch("start", {"task_id": issue})
print(result["status"])
PY
issue=$(cat "$state/issue-id.txt")
python -m evo --state "$state" task "$issue"
```

工单详情包含任务树、协商、运行事件和产物的 `event_id`。用 `python -m evo --state "$state" artifact "$issue" EVENT_ID` 读取产物快照。历史人工审查随原工单事件保存。
