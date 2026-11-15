# C09：Linux Docker Python 沙箱

实现日期：2026-11-15（Asia/Shanghai）。本阶段提供可信应用代码调用的独立适配器，
不接入默认 CLI/Web 执行入口。C10 负责持久化人工审批及批准与动作的绑定，C11 负责
工具分发、任务状态和审计。传入 `approved=True` 只是适配器的防误用门禁，不代表完整审批系统。

## 接入契约

```python
from pathlib import Path
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.docker import DockerPythonRunner
from evo.ports.tools import ToolError

# agent 来自可信任务记录，不能由模型选择其他 agent 的工作区。
workspace = WorkspaceFilesystem(Path(".evo"), "builder")
workspace.write_file("main.py", "print('hello')", purpose="code")
runner = DockerPythonRunner(workspace)

# C10 验证持久化的人类批准后才可调用；禁止转发模型提供的 approved 参数。
try:
    result = runner.run_python("main.py", approved=True)
except ToolError as error:
    safe_error_message = str(error)
```

`PythonRunnerPort` 和不可变 `ExecutionResult` 位于 `ports/tools.py`。
结果包含 `exit_code/stdout/stderr/timed_out/output_limited`；超时或输出超限时必须检查
对应标记，不能只看 CLI 退出码。普通执行退出码来自 Docker 容器最终状态，代码返回非零
不会被改写为成功。stdout/stderr 是不可信代码输出，C11 应限制展示并执行凭据脱敏。

未批准抛出 `PermissionError`，非法脚本路径沿用 C07 的 `ValueError` 路径策略。
基础设施错误使用不包含原始 Docker stderr 的 `ToolError`。不自动下载镜像，
不执行 shell，不回退到宿主 Python；Docker 缺失、镜像缺失、daemon 不可用或隔离参数
无法满足时直接失败。

## 隔离与限额

| 项目 | 实现边界 |
|---|---|
| 宿主平台与身份 | 仅 Linux；要求非 root UID/GID；正式基线为 Ubuntu 22.04 x86_64 / Python 3.10 |
| Docker 连接 | 显式指定 `unix:///var/run/docker.sock`；不支持远程 context 或 rootless socket |
| 客户端环境 | 仅保留 PATH、固定 LC_ALL 和每次调用的空临时 DOCKER_CONFIG；不读取宿主 Docker 代理/凭据配置，不继承模型 key/token 或 Docker 环境变量 |
| 挂载 | 唯一 bind mount 为当前 agent 的 `agents/<agent>/workspace` → `/workspace`；不挂载 events/memory DB、其他 agent、工件仓库或 Docker socket |
| 容器身份 | 宿主当前非 root UID:GID；drop ALL capabilities、no-new-privileges |
| 网络与文件系统 | `--network none`、只读根；工作区可写；`/tmp` 为 64 MiB tmpfs，noexec/nosuid/nodev |
| 资源 | CPU 1、内存及 memory-swap 各 256 MiB、PID 64、nofile 256 |
| 执行期限 | attach 默认 30 秒，可信配置范围 (0, 300]；image inspect/create/state inspect/cleanup 各自最多 10 秒 |
| 输出 | stdout/stderr 合计最多 65536 原始字节，可信配置最多 1 MiB；超限终止，UTF-8 非法字节按 replacement 解码 |
| 执行命令 | 固定 Python entrypoint，`-I -B -u`；脚本先通过 C07 路径、后缀、大小和文件类型检查 |
| 生命周期 | UUID 容器名、禁 Docker 日志；正常结束、超时、输出超限或启动异常后均尝试强制删除容器；清理失败显式报错 |

默认镜像为：

```text
python:3.11.11-slim-bookworm@sha256:081075da77b2b55c23c088251026fb69a7b2bf92471e491ff5fd75c192fd38e5
```

允许可信部署配置指定固定 Python 3.11 patch/bookworm tag，或 tag 加 digest；
正式评测应使用带 digest 的引用并记录实际镜像 ID。执行前把配置引用解析为本地不可变
image ID，再以 `--pull never` 创建容器，避免 inspect 与 create 之间 tag 变化。

Docker 是可信宿主基础设施，拥有 socket 访问权的宿主进程不在沙箱威胁模型内。
代码可以读写自己工作区中的所有文件；C07 broker 的后缀规则不是容器内任意代码的写入限制。
应用必须串行管理同一工作区的执行与变更；本阶段不承诺抵御恶意同 UID 宿主进程并发替换
挂载目录。宿主应用被 SIGKILL、主机断电或 Docker daemon 故障可能留下容器；
可用 `docker --host unix:///var/run/docker.sock ps -a --filter name=evo-python-` 检查，
由可信运维确认后清理。不存在后台持久化恢复服务。

## 验证与验收证据

```bash
python -m pip install -e .
image="$(python -c 'from evo.adapters.tools.linux import DEFAULT_IMAGE; print(DEFAULT_IMAGE)')"
docker --host unix:///var/run/docker.sock pull "$image"
EVO_DOCKER_TESTS=1 python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
python scripts/check_secrets.py
```

新增 C09 测试共 13 项：10 项单元测试覆盖命令参数、平台/root/路径/镜像拒绝、
无批准不执行、Docker 不可用时 fail closed、退出码验证、超时/输出边界、异常清理以及
宿主环境与 Docker 配置隔离；3 项真实 Docker 测试覆盖非 root 执行、工作区写入、
events/memory/其他 agent/socket 不可见、只读根、无网络、capabilities、no-new-privileges、
非零退出、超时与输出超限后的容器清理，以及默认拒绝无副作用。

普通离线测试显式跳过 3 项真实 Docker 测试。设置 `EVO_DOCKER_TESTS=1` 后不再允许因
Docker 或镜像缺失而跳过，必须实际成功。CI 独立 `docker-integration` job 在
Ubuntu 22.04 / Python 3.10 拉取同一 digest 并执行全部 13 项测试，保存包含 commit SHA、
宿主/Python/Docker 版本和实际镜像 ID/digest 的 `c09-docker-environment` artifact。

本地开发者验证环境为 Ubuntu 24.04.4 x86_64 / Docker Engine 29.1.3；
Python 3.12.3 和 Python 3.10.21 各自执行完整测试集（显式启用 Docker）：
**153 项全部通过，无跳过**。compileall、diff 空白和密钥模式检查通过。
这是本地证据；本提交尚无远端 Ubuntu 22.04 CI 运行结果，不作为官方数据集、
完整任务执行或 C10 审批验收的证据。
