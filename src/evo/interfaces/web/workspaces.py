"""One local workbench and issue worker per workspace."""

import shutil
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from threading import RLock
from uuid import uuid4

from evo.interfaces.agent_registry import RegistryValidationError
from evo.interfaces.gateway import ApplicationUnavailable
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.interfaces.workbench import Workbench


DEFAULT_WORKSPACE = {"id": "default", "name": "默认工作区"}


def workspace_name(name):
    name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= 64 or not name.isprintable():
        raise RegistryValidationError("工作区名称须为 1 至 64 个可显示字符。")
    return name


class Workspaces:
    def __init__(self, application, factory=SQLiteApplication, attached=()):
        self.root = application.state.resolve()
        self.factory = factory
        self.lock = RLock()
        self.workers = None
        self.entries = {"default": DEFAULT_WORKSPACE}
        self.benches = {"default": Workbench(application)}
        self.catalog = self.root / "workspaces.sqlite3"
        with closing(sqlite3.connect(self.catalog)) as catalog, catalog:
            catalog.execute(
                "CREATE TABLE IF NOT EXISTS workspaces ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL UNIQUE)"
            )
            for name, state in attached:
                self._attach(catalog, name, Path(state).resolve())
            for workspace_id, name, state in catalog.execute("SELECT id, name, state FROM workspaces ORDER BY rowid"):
                self.benches[workspace_id] = self._bench((self.root / state).resolve())
                self.entries[workspace_id] = {"id": workspace_id, "name": name}

    def _attach(self, catalog, name, state):
        name = workspace_name(name)
        if state == self.root or not (state / "events.sqlite3").is_file():
            raise ApplicationUnavailable("请指定另一个已有 SQLite 数据的工作区目录。")
        previous = catalog.execute("SELECT name FROM workspaces WHERE state = ?", (self._catalog_path(state),)).fetchone()
        if previous:
            if previous[0] != name:
                raise ApplicationUnavailable("该目录已登记为另一个工作区名称。")
            return
        self._insert(catalog, uuid4().hex, name, state)

    def _catalog_path(self, state):
        return str(state.relative_to(self.root) if state.is_relative_to(self.root) else state)

    def _insert(self, catalog, workspace_id, name, state):
        if name.casefold() == DEFAULT_WORKSPACE["name"].casefold():
            raise RegistryValidationError("该工作区名称已存在。")
        try:
            catalog.execute("INSERT INTO workspaces VALUES (?, ?, ?, ?)",
                            (workspace_id, name, name.casefold(), self._catalog_path(state)))
        except sqlite3.IntegrityError:
            raise RegistryValidationError("工作区名称或目录已存在。") from None

    def _bench(self, state):
        if not (state / "events.sqlite3").is_file():
            raise ApplicationUnavailable("一个已登记的工作区目录不可用，请检查本地数据。")
        return self._new_bench(state)

    def _new_bench(self, state):
        application = self.factory(state=state)
        if not isinstance(application, SQLiteApplication) or application.state.resolve() != state:
            raise ApplicationUnavailable("工作区需要独立的 SQLite 应用。")
        return Workbench(application)

    @contextmanager
    def worker(self):
        with ExitStack() as workers:
            with self.lock:
                for bench in self.benches.values():
                    workers.enter_context(bench.worker())
                self.workers = workers
            try:
                yield
            finally:
                with self.lock:
                    self.workers = None

    def get(self, workspace_id):
        with self.lock:
            return self.benches.get(workspace_id)

    def snapshot(self):
        with self.lock:
            return {"workspaces": list(self.entries.values())}

    def create(self, payload):
        if set(payload) != {"name"}:
            raise RegistryValidationError("请输入工作区名称。")
        name = workspace_name(payload["name"])
        workspace_id = uuid4().hex
        state = self.root / "workspaces" / workspace_id
        workspace = {"id": workspace_id, "name": name}
        with self.lock, closing(sqlite3.connect(self.catalog)) as catalog, ExitStack() as worker:
            with catalog:
                self._insert(catalog, workspace_id, name, state)
                state.mkdir(parents=True, mode=0o700)
                try:
                    bench = self._new_bench(state)
                    if self.workers is not None:
                        worker.enter_context(bench.worker())
                    catalog.commit()
                except Exception:
                    worker.close()
                    shutil.rmtree(state)
                    raise
            if self.workers is not None:
                self.workers.callback(worker.pop_all().close)
            self.entries[workspace_id] = workspace
            self.benches[workspace_id] = bench
        return {"workspace": workspace}
