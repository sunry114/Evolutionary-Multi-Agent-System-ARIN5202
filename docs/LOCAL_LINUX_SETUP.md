# Windows 上使用 WSL Linux 执行环境

本页记录 2026-11-18 的本地安装。依据 C07/C09/C10/C10a 当前实现，实际 Agent
文件操作和代码/Git 执行需要非 root Linux 用户、本地 Docker socket 和固定镜像。
Windows 上的聊天成功不能替代执行验证。无需重装 Windows，也无需额外安装 Docker Desktop。

## 本机安装位置与版本

| 项目 | 本机结果 |
|---|---|
| WSL 发行版 | Ubuntu-24.04，Ubuntu 24.04.5，x86_64 |
| 运行用户 | lxw666，非 root，已加入 docker 组 |
| Docker Engine | 29.10.0，官方 Ubuntu apt 源，本地 Unix socket，systemd 服务已启用 |
| Python | 3.12.3，项目独立 `.venv` |
| Claude Code | 原生 Linux x86_64 2.1.311；官方 npm 原生包，已验证 SHA-512 |
| Linux 项目 | `/home/lxw666/projects/evo-nlp-20261118` |
| Python 虚拟环境 | `/home/lxw666/projects/evo-nlp-20261118/.venv` |
| 工作台状态 | 项目下 `.evo/local-workbench`，与 Windows 旧状态独立 |
| 模型 | 老师提供的 HKGAI `t2_hkgai-v3_fp8_1m_e7` |

Linux 目录是从 main 导出的源码快照，再同步本次接口修复，并非 Git clone。
提交和 PR 仍在 Windows Git 工作副本中进行。以后升级 main 时，需同步 Linux 源码；
不要覆盖或发布 `.evo` 和私有凭据。本地 24.04/Python 3.12 属于开发验证，仓库正式
基线 Ubuntu 22.04/Python 3.10 继续由 CI 验证。

## 启动与访问

Windows PowerShell：

```powershell
wsl -d Ubuntu-24.04 -- bash /home/lxw666/projects/evo-nlp-20261118/scripts/start_linux.sh 8769
```

打开 <http://127.0.0.1:8769>。保持终端运行，Ctrl+C 停止。此入口使用 Linux 状态和
执行器；原来的 Windows 8768 工作台数据不会自动迁移。首次默认角色为 researcher、
builder、reviewer；正式测试前按测试场景重新配置角色和独立状态，见 [MID_TEST_RUNBOOK.md](MID_TEST_RUNBOOK.md)。

也可在 Ubuntu 终端中运行：

```bash
cd /home/lxw666/projects/evo-nlp-20261118
bash scripts/start_linux.sh 8769
```

凭据保存在 Linux 用户的 `~/.config/evo-collab/hkgai.key`，目录 0700、文件 0600。
启动脚本不含密钥，不通过命令行传递密钥。首次设置可在 Ubuntu 项目目录执行
`.venv/bin/python -m evo setup-key --provider hkgai`，按隐藏提示输入。
该命令拒绝覆盖已有文件，更换密钥前应明确移除旧凭据，见 [PROVIDERS.md](PROVIDERS.md)。

## 固定镜像与复验

安装已按 C 的常量拉取 Python 和 Git 镜像的完整 digest。请勿改成浮动 `latest`。
需要复验时，在 Ubuntu 项目目录执行：

```bash
export PATH="$HOME/.local/bin:$PATH"
docker --host unix:///var/run/docker.sock version
claude --version
EVO_DOCKER_TESTS=1 .venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -v
.venv/bin/python scripts/harness_smoke.py --live --hkgai --approve-smoke-code --state .evo/hkgai-installation-smoke
```

最后一条会产生真实 HKGAI API 用量，只预先批准脚本中固定的 `print('C10A_SMOKE_OK')`
测试代码；其他动作仍受原有审批限制。它检查生成文件、准确代码、一次成功执行、
实际 stdout 和审批后恢复。结果保存在指定 state 的 `latest-smoke.json`。

## HKGAI 执行器协议修复

真实检查发现老师地址的 `/v1/messages` 返回 404，而 `/v1/chat/completions` 可用。
宿主固定上游代理因此对 HKGAI 增加文本/工具调用转换，并向 CLI 提供 Messages JSON/SSE。
这是缓冲完整响应后的 SSE 转换，并非实时转发上游 token。模型仍为 HKGAI；没有向
Anthropic 模型 API 发请求。DeepSeek 的原生 Messages 路径保持原有处理。

实际 token usage 来自上游响应；不支持的图片/文档、无实际 usage 或损坏的工具参数
会失败。HKGAI `count_tokens` 返回 501，不伪造计数。原来的固定上游、认证隔离、
请求/字节限额、无网络容器、MCP 工具范围和审批都继续生效。

本机真实冒烟已完成文件生成/读取、固定代码审批、Docker Python 执行和恢复，五项
客观检查通过。它是安装验证，不能作为老师测试集的完成率或正式执行时间。
本地完整 Python 回归包含真实 Docker：297 项通过，无跳过。默认 CLI 编排器另已
成功分配单个 builder 子任务，生成并客观验证 `hello.md`，根任务和子任务均为 success。
测试记录保留在 Linux 项目 `.evo-environment-tests.log` 和 `.evo`，未发布私有对话或密钥。

安装依据：[Docker 官方 Ubuntu 文档](https://docs.docker.com/engine/install/ubuntu/)、
[Claude Code 官方安装文档](https://code.claude.com/docs/en/setup)。
