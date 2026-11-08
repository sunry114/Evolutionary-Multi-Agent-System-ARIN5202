### 该表格展示的是测试项目的预定路径，对应项目负责人应该根据此映射表在各自的开发过程中写入测试文件，方便最后的整体测试。
### 对应文件夹中的__init__.py主要作用是让目录成为明确的 Python package，并保证 unittest 的测试发现和跨目录导入稳定。
|        测试        |                 位置                  |  当前状态  | 后续 Commit | 负责人 |
   | :------------------: | :-----------------------------------: | :--------: | :---------: | :----: |
   |   review revision    |      integration/test_sqlite.py       |   待 C05   |     C05     |   B    |
   | group chat isolation |     unit/application/test_chat.py     |   待 C06   |     C06     |   A    |
   |   search filtering   |         security/test_ssrf.py         |   待 C08   |     C08     |   C    |
   |   memory relevance   |    unit/persistence/test_memory.py    |   待 C12   |     C12     |   B    |
   |    Web task flow     |      integration/test_web_api.py      |  部分已有  |     C14     |   D    |
   |   collective task    |      e2e/test_collective_task.py      |   待 C11   |     C11     |   A    |
   |      delegation      |      e2e/test_collective_task.py      |   待 C11   |     C11     |   A    |
   |    path boundary     |    security/test_path_boundary.py     |   待 C07   |     C07     |   C    |
   |   private targets    |         security/test_ssrf.py         |   待 C08   |     C08     |   C    |
   |  artifact exchange   | integration/test_artifact_exchange.py |   待 C07   |     C07     |   C    |
   |   stale file check   |    unit/evaluation/test_checks.py     |   待 C13   |     C13     |   B    |
   | failed run objective |    unit/evaluation/test_checks.py     |   待 C13   |     C13     |   B    |
   | repeated denied tool |   unit/application/test_executor.py   | 待 C10/C11 |   C10/C11   |   C/A  |
   |  approval required   |   unit/application/test_approval.py   |   待 C10   |     C10     |   C    |
   |  evaluation report   |      e2e/test_evaluation_run.py       |   待 C13   |     C13     |   B    |
   |      no-memory       | integration/test_memory_isolation.py  |   待 C12   |     C12     |   B    |
   |  HKGAI credentials   |       unit/test_credentials.py        |   已覆盖   |     C04     |   D    |
   |    HKGAI protocol    |           unit/test_llm.py            |   已覆盖   |     C04     |   D    |