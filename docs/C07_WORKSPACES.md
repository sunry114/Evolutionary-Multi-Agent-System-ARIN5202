# C07：代理工作区与显式工件交换

实现日期：2026-11-15（Asia/Shanghai）。本里程碑提供可供任务执行器接入的文件适配器；
默认 CLI/Web 仍使用既有 SQLite 查询与审阅入口。C09 的 Docker 隔离、C10 的审批状态处理、
C11 的工具分发与任务执行循环需在各自里程碑接入。

## 存储与信任边界

```text
.evo/
├── events.sqlite3
├── artifacts/
│   └── <opaque-id>/
│       ├── manifest.json
│       └── content
└── agents/
    └── <agent>/
        ├── memory.sqlite3
        └── workspace/
```

`WorkspaceFilesystem(state_root, agent)` 由可信应用层构造，绑定一个代理。
文件操作只接收工作区相对路径，不接收调用者选择的其他代理身份。
`workspace` 属性仅供可信 Docker 适配器确定挂载目录；不得把 state_root、agents 的父目录、
记忆数据库或 artifacts 挂入代理容器，也不得把 Python 适配器对象交给代理生成的代码。
同一宿主 UID 下的任意进程不构成安全沙箱；代理代码执行必须经过后续 C09 的隔离。

路径先解析并检查包含关系，然后逐级用目录描述符和 `O_NOFOLLOW` 打开，防止检查后替换为
符号链接的常见竞态。拒绝绝对路径、任何 `..`/`.` 路径分量、空分量、控制字符、Windows/UNC
路径、任何 `.git` 分量和 `.evo-` 内部名称。包括工作区内部链接在内的所有符号链接均拒绝。
只操作单硬链接的普通文件，拒绝 FIFO、设备等特殊文件；读操作带 `O_NONBLOCK`，避免 FIFO
阻塞。所有操作均释放文件描述符。新增目录为 `0700`，新增文件为 `0600`。

## 文件接口与用途

`evo.ports.tools` 定义 `WorkspacePort`、`ArtifactPort` 与不含宿主路径的 `Artifact` 元数据。
既有 `ToolPort.call(agent, name, arguments)` 保持兼容，C11 应按已验证的任务所有者选择绑定
实例，再校验工具参数，不能允许模型选择实例或传入可信审批标志。

| 用途 `purpose` | 允许后缀 | 使用约束 |
|---|---|---|
| `document`（默认） | `.md`, `.txt` | 用户文档、报告 |
| `code` | `.py` | Python 源码 |
| `config` | `.json`, `.yaml`, `.yml` | 配置工件 |
| `data` | `.json`, `.csv` | 数据工件 |

后缀大小写不敏感，内容必须为 UTF-8 文本。用途校验约束文件类型，不能判断文本的实际语义；
应用层必须把用户报告输出绑定为 `document`，不能为了输出报告而使用 `data`/`config`。
不执行、不解释文件内容。单文件上限默认 **1 MiB（UTF-8 字节数）**，可信构造参数可降低，
不能提高；读取与发布同样检查已有文件大小。目录与工件列表每次最多检查 1000 个条目，
超出时报错。目录列表按名称排序，只显示安全目录和允许类型的普通文件，不递归。

```python
from pathlib import Path
from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.artifacts import ArtifactExchange

state = Path(".evo")
alice = WorkspaceFilesystem(state, "alice")
bob = WorkspaceFilesystem(state, "bob")
alice.write_file("reports/result.md", "# Result\nVerified.\n")
assert alice.read_file("reports/result.md").startswith("# Result")
assert bob.list_files() == ()

published = ArtifactExchange(alice).publish("reports/result.md", ["bob"])
exchange = ArtifactExchange(bob)
assert published in exchange.list_available()
exchange.import_artifact(published.id, "incoming/result.md")
assert bob.read_file("incoming/result.md").startswith("# Result")
```

写文件会自动创建安全的父目录，并以完整临时文件原子替换目标；`overwrite=False` 则原子
拒绝覆盖。`delete_file` 默认拒绝执行。只有 C10/C11 从持久化审批记录取得有效批准后，
可信 broker 才可传入 `approved=True`；该参数不是审批凭据，不能直接接受模型提供的值。
本里程碑没有审批请求、等待或恢复机制。

## 发布与导入

发布必须明确给出 1–64 个接收者 ID，复制本代理已有文件，生成 UUID、原文件名、用途、
大小、SHA-256、发布者和接收者元数据。成功后才原子公开整个快照目录；失败时清理临时
发布目录。API 不提供修改快照或修改接收者的操作，后续修改源文件不会改变已发布快照。

代理只可列出自己发布或明确授权给自己的工件。导入必须指定已发布 ID 和目标路径，重新
校验权限、大小与 SHA-256，保留原用途及后缀，并复制为新的普通文件；不自动导入，
不覆盖已有目标，不建立软链接或硬链接。其他代理即使知道 ID 也不能导入。
发布清单保存在可信应用目录中；摘要检测内容损坏，不替代宿主应用目录的访问控制。
记录与快照在正常进程重启后仍可使用。

## 验证

```bash
python -W error::ResourceWarning -m unittest \
  tests.security.test_path_boundary tests.integration.test_artifact_exchange -v
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tests
python scripts/check_secrets.py
```

新增 23 项测试：11 项路径与文件安全测试、12 项工件交换集成测试。覆盖相对/绝对路径
越界、跨代理访问、`.git`、符号链接与解析后替换竞态、硬链接、FIFO/设备链接、非法后缀、
文档用途、字节限额、非 UTF-8、目录列表限额、重复失败后描述符释放、非 Linux 拒绝，
以及发布可见性、指定接收者、独立副本、重启、禁止覆盖、并发导入、完整性损坏、存储链接、
发布失败清理、接收方限额和 C05 数据库共存。

本机验证环境为 Ubuntu 24.04.4 x86_64、Python 3.12.3，属于开发者自测；
全量 **111 项测试通过（无跳过）**，包括上述 23 项新增测试；compileall、
`git diff --check` 和仓库已有的 tracked secret-pattern 检查均通过。
Ubuntu 22.04 / Python 3.10 正式基线由既有 Linux CI 在提交后复验。这些离线测试不需要
API key 或 Docker，也不代表 C09 沙箱验收或官方数据集评测。
