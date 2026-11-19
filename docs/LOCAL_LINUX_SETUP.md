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

## 执行与检查

```bash
python -m evo --state .evo/local-workbench run 'Create hello.md containing hello' --expect-file hello.md --expect-text hello
python -m evo --state .evo/local-workbench tasks
```

按返回的任务 ID 使用 `task`、`trace`、`metrics` 查看结果。遇到审批时，先用 `answer REQUEST_ID --approve` 或 `--deny` 记录决定，再用 `resume ROOT_TASK_ID` 恢复根任务。这些命令使用与启动任务相同的 `--state`。
