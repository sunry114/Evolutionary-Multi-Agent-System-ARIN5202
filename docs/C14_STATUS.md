# C14 当前实现与验收状态

更新日期：2026-11-15（Asia/Shanghai）。基于 main 7ee0586，接入 A 的 C06、
B 的 SQLite 和 C 的 ApprovalService。本次完成这些现有后端对应的 C14 用户入口。

## 已可使用

| 用户功能 | 实现与验证 |
|---|---|
| CLI/Web 私聊 | 调用真实 ChatService 和 HKGAI 适配器；双方消息存入 SQLite |
| CLI/Web 群聊 | researcher、builder、reviewer 依次回复；不把私聊和任务竞标日志混入群聊 |
| 聊天记录查询 | 新增 messages 命令和 HTTP 路由；切换 Agent 分开展示 DM，刷新保留当前选择 |
| 任务查询与 trace | 使用真实数据库；CLI task/tasks/trace，Web 任务详情和可见日志 |
| 审查与修订 | 接受或要求修改；原子创建关联修订任务，保留原技术结果 |
| 审批与澄清回答 | 通过 ApprovalService 保存批准、拒绝、文字回答；拒绝阻断、超时过期、重复决定拒绝 |
| 重启恢复 | 聊天、任务、审查、修订和人类回答保留；刷新后获取新的 CSRF token |
| 错误反馈 | 无密钥返回可操作的 503；模型故障返回安全的 502，提醒先查看已保存记录再重试 |

群聊和私聊只生成文本回复，不执行工具。批准/澄清仅持久化决定，不会直接调用执行器。
修订仍是 created，必须由 A 的调度器推进。没有默认批准、自动重试危险动作或伪造成功。

## 验证证据

### 真实 HKGAI 聊天

在 Windows/Python 3.12.7 上，用课程模型 t2_hkgai-v3_fp8_1m_e7：

- builder 私聊一次：收到非空回复。
- 三 Agent 群聊一次：三个角色均收到非空回复。
- 重新打开数据库：6 条消息保留（私聊 2 条，群聊 4 条）。
- 共 4 次模型调用，合计 3.518 秒；这是连接/聊天冒烟耗时，不是官方任务执行指标。
- 密钥和回复原文未写入仓库；仅发送简短的角色介绍请求。

### 离线与浏览器

- 新增 10 项网关联调测试：tests/integration/test_c14_chat_gateway.py。
- 本地针对 C14 及相关存储/模型/规划/HTTP 的 95 项回归：93 通过，2 项 POSIX 权限测试跳过。
- 真实 Edge 157.0.4341.60 + Playwright：7 组浏览器验收通过。
- 浏览器使用明确的测试工厂 c14_fixture，注入离线 provider；真实 ChatService、
  HTTP、ApprovalService 和 SQLite 均实际运行。CI 不需要真实密钥。
- 全仓库包含 Linux 专用文件系统、Unix socket 和 Docker 测试，不能以 Windows 全套结果
  宣称通过；完整回归以 Ubuntu CI 为准。本地未运行 Docker/Claude CLI 真机执行。
- 浏览器额外覆盖群聊、DM 隔离、选项保留、模型错误安全反馈，以及批准/拒绝/澄清的实际点击。
  历史审查、修订、trace、刷新、进程重启和手机布局检查保留。

## 使用

支持的 Linux 环境安装后，先配置真实密钥（不会写入源码）：

```bash
python -m evo setup-key --provider hkgai
python -m evo --state .evo web
python -m evo --state .evo dm builder 'Briefly describe your role.'
python -m evo --state .evo group 'Discuss how to divide a review task.'
python -m evo --state .evo messages
python -m evo --state .evo messages --agent builder
python -m evo --state .evo answer REQUEST_ID --approve
python -m evo --state .evo answer REQUEST_ID --deny
python -m evo --state .evo answer REQUEST_ID --text 'Use report.md'
```

无密钥也可以查询、审查和回答已有请求；只有聊天会检查配置并调用模型。缺少密钥时，
不会保存一条无法发出的新聊天。模型在调用过程中失败时，用户消息和部分群聊回复可能
已经保存，界面会刷新展示；不自动重发，以免产生重复消息或费用。

内置三个角色由 DEFAULT_AGENTS 统一定义；A 可通过构造参数传入自己的角色注册表。
单个服务进程的聊天写入序列使用锁，避免并发请求互相穿插。界面最多展示每个 DM
最近 100 条、汇总最近 400 条消息；并非完整历史分页。服务只面向本机人类用户，
这里的 DM 展示隔离不等于多用户账户认证。

## 仍依赖队友的最终 C14 验收

| 后端 | 当前事实 | 接入要求 |
|---|---|---|
| C11 完整任务编排 | 现有 ActionExecutor 只执行单个动作，没有高层任务循环/DAG 调度 | 提供 run 操作及暂停/恢复；成功必须有可验证证据 |
| C12 记忆 | 只有 SQLite memory schema，没有完整读写/召回实现 | 提供授权 memory 查询，保持各 Agent 隔离 |
| C13 评测 | 没有实际 evaluation runner | 提供 eval/metrics，明确 passed、完成率分母、耗时与 no-memory |

run/memory/eval/metrics 当前继续返回 unavailable 并在能力字段中标为 false。
CLI 和 HTTP 已保留相应入口；不会把占位入口当作真实功能。队友可通过
EVO_APPLICATION_FACTORY=module:factory 返回完整 ApplicationGateway，
其操作和 snapshot 必须遵守 [INTEGRATION.md](INTEGRATION.md)。

因此，本次不是“完整 C14 已最终验收”，而是“现有后端可接入的聊天、审批、存储、
审查及修订用户入口已实现并验证”。缺失后端到位后仍需端到端联调。
