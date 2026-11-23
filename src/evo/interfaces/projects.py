"""Local project catalog. Each project owns an independent workbench."""

import shutil
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import uuid4

from evo.interfaces.agent_registry import RegistryValidationError
from evo.interfaces.gateway import ApplicationUnavailable
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.interfaces.workbench import Workbench


def project_directory(value):
    if not isinstance(value, str) or not value.strip():
        raise RegistryValidationError("请输入本机项目目录。")
    try:
        directory = Path(value.strip()).expanduser()
        if not directory.is_absolute():
            raise ValueError()
        directory = directory.resolve(strict=True)
        if not directory.is_dir():
            raise ValueError()
    except (OSError, ValueError, RuntimeError) as error:
        raise RegistryValidationError("请选择本机已有目录，使用绝对路径。") from error
    return directory


class Projects:
    def __init__(self, application, factory=SQLiteApplication):
        self.root = application.state.resolve()
        self.factory = factory
        self.catalog = self.root / "projects.sqlite3"
        self.lock = RLock()
        self.workers = None
        self.running = set()
        self.benches = {}
        self.entries = {}
        with closing(sqlite3.connect(self.catalog)) as catalog, catalog:
            catalog.execute(
                "CREATE TABLE IF NOT EXISTS projects ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, "
                "directory TEXT NOT NULL UNIQUE)"
            )
        self._refresh()

    def _refresh(self):
        with closing(sqlite3.connect(self.catalog)) as catalog:
            for project_id, name, directory in catalog.execute("SELECT id, name, directory FROM projects ORDER BY rowid"):
                self.entries[project_id] = {"id": project_id, "name": name, "directory": directory}

    def _start_workers(self):
        for project_id in self.entries.keys() - self.running:
            try:
                self.workers.enter_context(self.get(project_id).worker())
            except BlockingIOError:
                continue
            self.running.add(project_id)

    def _new_bench(self, project):
        state = self.root / "projects" / project["id"]
        application = self.factory(state=state)
        if not isinstance(application, SQLiteApplication) or application.state.resolve() != state:
            raise ApplicationUnavailable("项目需要独立的 SQLite 应用。")
        return Workbench(application, project=project)

    @contextmanager
    def worker(self):
        shutdown = Event()

        def follow_catalog():
            while not shutdown.wait(0.5):
                with self.lock:
                    try:
                        self._refresh()
                        self._start_workers()
                    except sqlite3.OperationalError:
                        continue

        with ExitStack() as workers:
            monitor = Thread(target=follow_catalog, name="evo-project-catalog", daemon=True)
            try:
                with self.lock:
                    self.workers = workers
                    self._start_workers()
                monitor.start()
                yield
            finally:
                shutdown.set()
                if monitor.ident is not None:
                    monitor.join()
                with self.lock:
                    self.workers = None
                    self.running.clear()

    def get(self, project_id):
        with self.lock:
            if project_id and project_id not in self.entries:
                self._refresh()
            if project_id not in self.entries:
                return None
            if project_id not in self.benches:
                self.benches[project_id] = self._new_bench(self.entries[project_id])
            return self.benches[project_id]

    def select(self, value):
        with self.lock:
            self._refresh()
            if value is None:
                raise ApplicationUnavailable("请通过 --project 指定项目。可运行 projects 查看项目。")
            project_id = value if value in self.entries else next(
                (entry["id"] for entry in self.entries.values() if entry["name"].casefold() == value.casefold()), None)
            bench = self.get(project_id)
            if bench is None:
                raise ApplicationUnavailable("找不到此项目。")
            return bench

    def snapshot(self):
        with self.lock:
            self._refresh()
            return {"projects": list(self.entries.values())}

    def create(self, payload):
        if set(payload) != {"name", "directory"}:
            raise RegistryValidationError("请输入项目名称和本机目录。")
        raw_name = payload["name"]
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if not 1 <= len(name) <= 64 or not name.isprintable():
            raise RegistryValidationError("项目名称须为 1 至 64 个可显示字符。")
        project_id = uuid4().hex
        project = {"id": project_id, "name": name, "directory": str(project_directory(payload["directory"]))}
        state = self.root / "projects" / project_id
        with self.lock, closing(sqlite3.connect(self.catalog)) as catalog, ExitStack() as worker:
            with catalog:
                try:
                    catalog.execute("INSERT INTO projects VALUES (?, ?, ?, ?)",
                                    (project_id, name, name.casefold(), project["directory"]))
                except sqlite3.IntegrityError:
                    raise RegistryValidationError("该项目名称或目录已绑定。") from None
                state.mkdir(parents=True, mode=0o700)
                try:
                    bench = self._new_bench(project)
                    if self.workers is not None:
                        worker.enter_context(bench.worker())
                    catalog.commit()
                except Exception:
                    worker.close()
                    shutil.rmtree(state)
                    raise
            if self.workers is not None:
                self.workers.callback(worker.pop_all().close)
                self.running.add(project_id)
            self.entries[project_id] = project
            self.benches[project_id] = bench
        return {"project": project}
