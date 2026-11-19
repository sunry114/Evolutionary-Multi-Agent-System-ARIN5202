"""Prepare instructor-style sequential scenarios privately; never auto-score them."""
import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

from evo.interfaces.agent_registry import default_registry, parse_agents, save_registry


def prepare_midtest(archive, output):
    archive, output = Path(archive), Path(output).resolve()
    if ".evo" not in output.parts or output.exists():
        raise ValueError("Choose a new private output directory below .evo")
    scenarios = {}
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        if len(infos) > 100 or sum(i.file_size for i in infos) > 2 * 1024 * 1024:
            raise ValueError("Archive is oversized")
        for item in infos:
            name = PurePosixPath(item.filename)
            if name.is_absolute() or ".." in name.parts or "\\" in item.filename:
                raise ValueError("Unsafe archive path")
            if item.filename.startswith("__MACOSX/") or item.is_dir():
                continue
            if len(name.parts) != 2 or not re.fullmatch(r"scenario_[1-9][0-9]*_.*", name.parts[0]):
                continue
            if name.name not in ("01_role_initialization.md", "02_task_1.md", "03_task_2.md"):
                continue
            if item.file_size > 128 * 1024:
                raise ValueError("Scenario file is oversized")
            ident = re.match(r"(scenario_[0-9]+)_", name.parts[0]).group(1)
            files = scenarios.setdefault(ident, {})
            if name.name in files:
                raise ValueError("Duplicate scenario file")
            files[name.name] = source.read(item).decode("utf-8-sig")
    if not scenarios:
        raise ValueError("No instructor scenarios found")
    prepared = []
    for ident, files in sorted(scenarios.items()):
        if set(files) != {"01_role_initialization.md", "02_task_1.md", "03_task_2.md"}:
            raise ValueError("Incomplete scenario")
        roles = re.findall(r"^## [^\n]+ \(([A-Za-z0-9_-]+)\)\s*\n+\x60\x60\x60text\s*\n(.*?)\n\x60\x60\x60", files["01_role_initialization.md"], re.M | re.S)
        agents = parse_agents([{"name": name, "specialty": prompt.strip()} for name, prompt in roles])
        if len(agents) != 4:
            raise ValueError("Expected four role prompts")
        tasks = []
        for number in (1, 2):
            text = files["02_task_1.md" if number == 1 else "03_task_2.md"]
            blocks = re.findall(r"\x60\x60\x60text\s*\n(.*?)\n\x60\x60\x60", text, re.S)
            if len(blocks) != 1 or not blocks[0].strip():
                raise ValueError("Task needs one verbatim text block")
            tasks.append(blocks[0].strip())
        prepared.append((ident, agents, tasks, files))
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"status": "prepared_not_executed", "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "protocol": "Task 1 then Task 2 in the same scenario state; separate state between scenarios; live human answers",
                "scenarios": []}
    for ident, agents, tasks, files in prepared:
        state = output / ident
        save_registry(state, default_registry(agents))
        for number, task in enumerate(tasks, 1):
            (state / ("task_" + str(number) + ".txt")).write_text(task + "\n", encoding="utf-8")
        manifest["scenarios"].append({"id": ident, "state": str(state), "agents": [a.name for a in agents],
                                     "tasks": ["task_1.txt", "task_2.txt"],
                                     "status": "not_run", "source_sha256": {
                                         name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()}})
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest

def audit_midtest(archive, output):
    """Read exported SQLite evidence privately, without executing archived code."""
    import sqlite3
    import tempfile
    from datetime import datetime
    archive, output = Path(archive), Path(output).resolve()
    if ".evo" not in output.parts or output.suffix != ".json" or output.exists():
        raise ValueError("Choose a new private JSON report below .evo")
    report = {"archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
              "scope": "evidence_audit_not_official_scoring", "scenarios": []}
    with zipfile.ZipFile(archive) as source, tempfile.TemporaryDirectory(prefix="evo-audit-") as temp:
        infos = source.infolist()
        if len(infos) > 200 or sum(i.file_size for i in infos) > 64 * 1024 * 1024:
            raise ValueError("Evidence archive is oversized")
        for index, item in enumerate(infos):
            if item.filename.startswith("__MACOSX/") or PurePosixPath(item.filename).name != "events.sqlite3":
                continue
            if item.file_size > 16 * 1024 * 1024:
                raise ValueError("Evidence database is oversized")
            data = source.read(item)
            database = Path(temp) / (str(index) + ".sqlite3")
            database.write_bytes(data)
            conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA trusted_schema=OFF")
                counts = {table: conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
                          for table in ("tasks", "events", "actions", "messages")}
                rows = conn.execute(
                    "SELECT sender,created_at FROM messages WHERE kind='group' ORDER BY created_at,id").fetchall()
                turns = []
                start = end = None
                for sender, timestamp in rows:
                    stamp = datetime.fromisoformat(timestamp)
                    if sender == "human":
                        if start is not None:
                            turns.append(max(0, (end - start).total_seconds()) if end else None)
                        start, end = stamp, None
                    elif start is not None:
                        end = stamp
                if start is not None:
                    turns.append(max(0, (end - start).total_seconds()) if end else None)
                statuses = dict(conn.execute("SELECT status,count(*) FROM tasks GROUP BY status").fetchall())
            finally:
                conn.close()
            report["scenarios"].append({
                "scenario": PurePosixPath(item.filename).parent.name, "database_sha256": hashlib.sha256(data).hexdigest(),
                "counts": counts, "technical_task_status_counts": statuses,
                "group_response_span_seconds": turns,
                "task_completion_rate": None, "task_execution_seconds": None,
                "reason": ("No persisted tasks/events/actions; chat spans are not execution times."
                           if not counts["tasks"] and not counts["events"] and not counts["actions"]
                           else "Requires task identity, objective rubric and run provenance; no score inferred.")})
    if not report["scenarios"]:
        raise ValueError("No evidence databases found")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
