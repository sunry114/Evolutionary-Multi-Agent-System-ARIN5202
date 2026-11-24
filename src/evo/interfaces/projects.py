"""Local workspaces own agent rosters and group directory-bound projects."""

import shutil
import sqlite3
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import uuid4

from evo.interfaces.agent_registry import RegistryValidationError, load_registry, migrate_registry, save_registry
from evo.interfaces.gateway import ApplicationUnavailable
from evo.interfaces.sqlite_application import SQLiteApplication
from evo.interfaces.workbench import Workbench

DEFAULT_WORKSPACE = "default"


def catalog_name(value, label):
    name = value.strip() if isinstance(value, str) else ""
    if not 1 <= len(name) <= 64 or not name.isprintable():
        raise RegistryValidationError(label + "名称须为 1 至 64 个可显示字符。")
    return name


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
        self.workspaces = {}
        self.workspace_apps = {}
        with closing(sqlite3.connect(self.catalog)) as catalog, catalog:
            catalog.execute("PRAGMA foreign_keys=ON")
            catalog.execute("BEGIN IMMEDIATE")
            catalog.execute(
                "CREATE TABLE IF NOT EXISTS workspaces ("
                "id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE)"
            )
            catalog.execute("INSERT OR IGNORE INTO workspaces VALUES (?, ?, ?)",
                            (DEFAULT_WORKSPACE, "默认工作区", "默认工作区"))
            columns = {row[1] for row in catalog.execute("PRAGMA table_info(projects)")}
            if columns and "workspace_id" not in columns:
                catalog.execute("ALTER TABLE projects RENAME TO flat_projects")
                self._create_project_table(catalog)
                self._migrate_projects(catalog, application._default_registry)
                catalog.execute("DROP TABLE flat_projects")
            elif not columns:
                self._create_project_table(catalog)
        self._refresh()

    @staticmethod
    def _create_project_table(catalog):
        catalog.execute(
            "CREATE TABLE projects (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), "
            "name TEXT NOT NULL, name_key TEXT NOT NULL, directory TEXT NOT NULL UNIQUE, "
            "UNIQUE(workspace_id, name_key))")

    def _workspace_state(self, workspace_id):
        return self.root / "workspaces" / workspace_id

    def _migrate_projects(self, catalog, default_registry):
        # Preserve each existing project's roster. Projects with the same roster can share a workspace.
        by_registry = {}
        projects = catalog.execute("SELECT id,name,name_key,directory FROM flat_projects ORDER BY rowid").fetchall()
        reserved_names = {row[2] for row in projects} | {"默认工作区"}
        for project_id, name, name_key, directory in projects:
            state = self.root / "projects" / project_id
            migrate_registry(state)
            registry = load_registry(state, default_registry)
            if registry not in by_registry:
                workspace_id = project_id if by_registry else DEFAULT_WORKSPACE
                if workspace_id != DEFAULT_WORKSPACE:
                    workspace_name = name
                    if name_key == "默认工作区":
                        workspace_name = "原项目工作区 " + project_id
                        while workspace_name.casefold() in reserved_names:
                            workspace_name += "_"
                    catalog.execute("INSERT INTO workspaces VALUES (?, ?, ?)",
                                    (workspace_id, workspace_name, workspace_name.casefold()))
                if registry != default_registry or (self._workspace_state(workspace_id) / "agents.json").exists():
                    save_registry(self._workspace_state(workspace_id), registry)
                by_registry[registry] = workspace_id
            catalog.execute("INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
                            (project_id, by_registry[registry], name, name_key, directory))

    def _refresh(self):
        with closing(sqlite3.connect(self.catalog)) as catalog:
            catalog.execute("BEGIN")
            for workspace_id, name in catalog.execute("SELECT id, name FROM workspaces ORDER BY rowid"):
                self.workspaces[workspace_id] = {"id": workspace_id, "name": name}
            for project_id, workspace_id, name, directory in catalog.execute(
                    "SELECT id, workspace_id, name, directory FROM projects ORDER BY rowid"):
                self.entries[project_id] = {"id": project_id, "workspace_id": workspace_id,
                                            "name": name, "directory": directory}

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
        workspace_id = project["workspace_id"]
        application.use_registry(self._workspace_state(workspace_id))
        chat = self.workspace_application(workspace_id)
        chat.store.import_project_chats(project["id"], application.store)
        return Workbench(application, project=project, workspace=self.workspaces[workspace_id], chat_application=chat)

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

    def workspace(self, value):
        with self.lock:
            self._refresh()
            return self.workspaces.get(value) or next(
                (item for item in self.workspaces.values() if item["name"].casefold() == value.casefold()), None)

    def workspace_application(self, workspace_id):
        with self.lock:
            if workspace_id not in self.workspaces:
                self._refresh()
            if workspace_id not in self.workspaces:
                return None
            if workspace_id not in self.workspace_apps:
                state = self._workspace_state(workspace_id)
                application = self.factory(state=state)
                if not isinstance(application, SQLiteApplication) or application.state.resolve() != state:
                    raise ApplicationUnavailable("工作区需要独立的 SQLite 应用。")
                self.workspace_apps[workspace_id] = application
            return self.workspace_apps[workspace_id]

    def select(self, value, *, workspace_id=None):
        with self.lock:
            self._refresh()
            if value is None:
                raise ApplicationUnavailable("请通过 --project 指定项目。可运行 projects 查看项目。")
            candidates = [entry for entry in self.entries.values()
                          if workspace_id is None or entry["workspace_id"] == workspace_id]
            matches = [entry for entry in candidates if entry["id"] == value] or [
                entry for entry in candidates if entry["name"].casefold() == value.casefold()]
            if not matches:
                raise ApplicationUnavailable("找不到此项目。")
            if len(matches) != 1:
                raise ApplicationUnavailable("多个工作区有同名项目。请指定 --workspace 或使用项目 ID。")
            return self.get(matches[0]["id"])

    def workspace_snapshot(self):
        with self.lock:
            self._refresh()
            return {"workspaces": list(self.workspaces.values())}

    def create_workspace(self, payload):
        if set(payload) != {"name"}:
            raise RegistryValidationError("新建工作区只需填写名称。")
        name = catalog_name(payload["name"], "工作区")
        workspace_id = uuid4().hex
        with self.lock, closing(sqlite3.connect(self.catalog)) as catalog, catalog:
            try:
                catalog.execute("INSERT INTO workspaces VALUES (?, ?, ?)", (workspace_id, name, name.casefold()))
            except sqlite3.IntegrityError:
                raise RegistryValidationError("该工作区名称已存在。") from None
            workspace = {"id": workspace_id, "name": name}
        return {"workspace": workspace}

    def snapshot(self, workspace_id=None):
        with self.lock:
            self._refresh()
            return {"projects": [entry for entry in self.entries.values()
                                 if workspace_id is None or entry["workspace_id"] == workspace_id]}

    def create(self, payload, *, workspace_id=DEFAULT_WORKSPACE):
        if set(payload) != {"name", "directory"}:
            raise RegistryValidationError("请输入项目名称和本机目录。")
        name = catalog_name(payload["name"], "项目")
        project_id = uuid4().hex
        project = {"id": project_id, "workspace_id": workspace_id, "name": name,
                   "directory": str(project_directory(payload["directory"]))}
        state = self.root / "projects" / project_id
        with self.lock, closing(sqlite3.connect(self.catalog)) as catalog, ExitStack() as worker:
            self._refresh()
            if workspace_id not in self.workspaces:
                raise RegistryValidationError("找不到此工作区。")
            catalog.execute("PRAGMA foreign_keys=ON")
            with catalog:
                try:
                    catalog.execute("INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
                                    (project_id, workspace_id, name, name.casefold(), project["directory"]))
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
