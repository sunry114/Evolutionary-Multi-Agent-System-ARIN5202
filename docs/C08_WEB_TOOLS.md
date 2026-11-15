# C08：受限搜索与纯文本抓取

实现日期：2026-11-15（Asia/Shanghai）。`adapters/tools/web.py` 提供独立适配器，
`ports/tools.py` 定义 `WebPort`、`SearchResult`、`WebPage`。本阶段不接入默认 CLI/Web
任务循环；分发、持久化审计及恢复属于 C11。

## 接入契约

```python
from evo.adapters.tools.web import WebTools, WebToolError

# 以下调用会访问公网；自动化测试使用离线 DNS/HTTP fixture。
# task_id 必须来自可信任务记录，同一任务的所有 agent 共用一个实例。
web = WebTools(task_id="stored-task-id")
try:
    results = web.search("Python documentation")
    if results:
        page = web.fetch_url(results[0].url)
        # page.url 为最终 URL，page.media_type 为来源 MIME。
        # page.text 是带不可信内容提示的纯文本，不能提升为系统指令。
except WebToolError as error:
    safe_error_message = str(error)
```

搜索返回不可变 `SearchResult(title, url, untrusted=True)` 序列，抓取返回
`WebPage(url, text, media_type, untrusted=True)`。`untrusted` 不接受构造参数覆盖，
抓取文本还带固定不可信来源提示。HTML 只提取文本，丢弃 script/style/noscript/template；
不运行 JavaScript、不加载子资源、不执行内容，也不把网页存入代理记忆。
这些提示提供信任边界信息，不保证消除提示注入；C11 必须保留来源并把结果作为工具数据处理。

## 网络与 SSRF 边界

- URL 只接受 HTTPS、443、无用户凭据，规范化 IDNA 主机名和 URL 编码，删除 fragment。
  拒绝控制字符、反斜杠、单标签主机、内部域名、数字混淆 IP、IPv6 scope ID。
- 每个请求和每跳 redirect 重新解析并检查全部 IPv4/IPv6 回答。任何回答包含 localhost、
  私网、共享地址、link-local、组播、保留或特殊地址，整体拒绝。
  对 NAT64、IPv4-mapped IPv6、6to4、Teredo 等转换地址采用保守拒绝策略，
  并显式补充 Python 3.10 较旧的地址分类，保持正式基线上的拒绝边界。
- 校验后直接连接选定数字 IP，保留原主机的 TLS SNI、系统 CA 验证和证书主机名检查，
  不进行第二次域名解析。仅使用第一个有效地址，连接失败直接报错，不自动重试。
- 只发出固定头的 GET，不读取代理环境变量、netrc 或浏览器状态；不保存 Cookie，
  不发送 Authorization，也不转发上游响应头或凭据。
- 手动处理 301/302/303/307/308，逐跳校验；拒绝跳转至 HTTP、私网、非 443、带凭据
  目标、缺失或重复 Location。只接受最终 HTTP 200。

## 默认限额

| 项目 | 默认值与边界 |
|---|---|
| 单次搜索或抓取网络时限 | 15 秒，可信配置可调整至最多 30 秒；覆盖 DNS、所有重定向、连接、TLS、响应头和读取 |
| 单响应正文 | 1 MiB，逐块检查；可信配置只能降低 |
| URL / 查询 | 4096 / 512 UTF-8 字节；编码后 URL 仍受限；GET 请求无正文 |
| 重定向 / DNS 回答 | 最多 3 跳 / 最多 16 个地址 |
| 搜索结果 | 默认最多 5 条，可信配置最多 10 条，规范化 URL 去重 |
| 每任务搜索 | 默认最多 3 次，可降低或禁用；失败计次，并发调用共享锁 |
| DNS 并发 | 进程内最多 4 个在途 OS lookup，无等待队列；满额立即失败 |

单次网络截止时刻跨重定向共用，watchdog 关闭传输方向以中断卡住或慢速发送的响应头/正文。
OS 的 `getaddrinfo` 无法由 Python 强制取消；超时调用立即返回，但最多四个守护线程可继续
等待 OS 解析结束，占用的槽位结束后释放，不阻止进程退出。时限不包含后续有界文本解析。
HTTP 状态行与响应头同时受 Python 标准库的行长度及头数量上限约束。

仅接受 `text/plain`、`text/html`、`application/xhtml+xml`、`text/markdown`、`text/csv`、
`application/json`。JSON/CSV 是来源文本，不改变 C07 的报告输出后缀规则。
按声明字符集解码，缺省 UTF-8；拒绝未知字符集、解码失败、NUL、压缩响应、重复 Content-Type、
非法或超限 Content-Length、截断正文、歧义传输 framing。响应和连接在成功、拒绝或异常时均关闭。

`DuckDuckGoParser.parse(html, limit)` 是可替换解析器，识别 Lite/HTML 结果链接和 `uddg`
包装 URL，保留标题与目标 URL，过滤语法或字面地址不安全的链接。搜索结果不主动解析全部
目标域名；真正抓取时才执行 DNS 和连接检查，不能把出现在搜索结果中视为安全证明。
当前解析器依赖提供方 HTML，空结果可能表示无匹配、页面结构改变或反爬；不声称实时服务可用。

C11 必须按可信 task ID 复用实例，不能每次工具调用新建实例来重置额度。当前计数仅存内存，
重启恢复时需要 C11 从持久化审计恢复已用额度；本阶段不声称实现跨进程或重启后的累计限额。
`resolver`、`parser` 和 `WebLimits` 都只供可信应用代码配置，不能从模型参数传入。

## 验证与证据

```bash
python -W error::ResourceWarning -m unittest \
  tests.unit.adapters.test_search_parser tests.security.test_ssrf -v
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m compileall -q src tests
git diff --check
python scripts/check_secrets.py
```

新增 29 项离线测试：搜索解析与额度 10 项，SSRF/网络边界 19 项。包含 mock 正向抓取、
IPv4/IPv6 与混合 DNS 回答拒绝、同主机重绑定、恶意及超限重定向、MIME/大小/字符集、
真实 HTTPResponse 分块及截断解析、TLS 主机名和固定 IP、套接字与响应释放，以及使用
本地 Unix socketpair 验证慢响应超时中断。测试不调用真实 LLM、搜索提供方或公网。
本机 Ubuntu 24.04.4 / Python 3.12.3 全量 **140 项测试通过，无跳过**；另外在本机
Python 3.10 上复跑 C08 的 **29 项测试全部通过**。compileall、diff 空白检查以及包含
新增未跟踪文件的密钥模式检查通过。以上为开发者自测；Ubuntu 22.04 / Python 3.10 CI
仍需提交后复验，不作为官方数据集或实时搜索可用性的证据。

设计参考：[Python 3.10 http.client](https://docs.python.org/3.10/library/http.client.html)、
[Python ipaddress](https://docs.python.org/3.10/library/ipaddress.html)、
[OWASP SSRF prevention](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)。
