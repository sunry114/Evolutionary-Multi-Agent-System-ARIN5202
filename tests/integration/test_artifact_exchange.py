import hashlib
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evo.adapters.persistence.sqlite_memory import SQLiteMemoryStore
from evo.adapters.persistence.sqlite_store import SQLiteStore
from evo.adapters.harness.mcp_bridge import PlatformToolBroker
from evo.adapters.tools.artifacts import ArtifactExchange
from evo.adapters.tools.filesystem import PathPolicyError, WorkspaceFilesystem
from evo.domain.models import Task
from evo.domain.statuses import TaskStatus
from evo.interfaces.sqlite_application import SQLiteApplication


@unittest.skipUnless(sys.platform == "linux", "Linux artifact exchange")
class ArtifactExchangeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.alice = WorkspaceFilesystem(self.root, "alice")
        self.bob = WorkspaceFilesystem(self.root, "bob")
        self.charlie = WorkspaceFilesystem(self.root, "charlie")
        self.publisher = ArtifactExchange(self.alice)
        self.receiver = ArtifactExchange(self.bob)
        self.stranger = ArtifactExchange(self.charlie)

    def test_publish_then_import_is_an_explicit_independent_snapshot(self):
        self.alice.write_file("private/report.md", "v1: 结果")
        self.assertEqual(self.receiver.list_available(), ())
        self.assertEqual(self.bob.list_files(), ())
        with self.assertRaises(FileNotFoundError):
            self.bob.read_file("private/report.md")
        artifact = self.publisher.publish("private/report.md", ["bob"])
        self.assertEqual(artifact.sha256, hashlib.sha256("v1: 结果".encode()).hexdigest())
        self.assertEqual(self.receiver.list_available(), (artifact,))
        self.assertEqual(self.stranger.list_available(), ())
        self.assertEqual(self.bob.list_files(), ())
        self.alice.write_file("private/report.md", "v2")
        imported = self.receiver.import_artifact(artifact.id, "incoming/report.md")
        self.assertEqual(imported, artifact)
        self.assertEqual(self.bob.read_file("incoming/report.md"), "v1: 结果")
        source = self.alice.workspace / "private/report.md"
        destination = self.bob.workspace / "incoming/report.md"
        snapshot = self.root / "artifacts" / artifact.id / "content"
        self.assertEqual(len({source.stat().st_ino, destination.stat().st_ino,
                              snapshot.stat().st_ino}), 3)
        self.bob.write_file("incoming/report.md", "changed by Bob")
        self.assertEqual(self.alice.read_file("private/report.md"), "v2")
        self.assertEqual(snapshot.read_text(), "v1: 结果")

    def test_only_named_recipients_can_import_even_with_known_id(self):
        self.alice.write_file("shared.txt", "shared")
        artifact = self.publisher.publish("shared.txt", ["bob"])
        with self.assertRaises(PermissionError):
            self.stranger.import_artifact(artifact.id, "stolen.txt")
        self.assertEqual(self.charlie.list_files(), ())
        self.assertEqual(self.publisher.list_available(), (artifact,))
        self.publisher.import_artifact(artifact.id, "own-copy.txt")

    def test_restart_preserves_snapshots_permissions_and_metadata(self):
        self.alice.write_file("shared.txt", "snapshot")
        artifact = self.publisher.publish("shared.txt", ["bob"])
        self.alice.delete_file("shared.txt", approved=True)
        restarted = ArtifactExchange(WorkspaceFilesystem(self.root, "bob"))
        self.assertEqual(restarted.list_available(), (artifact,))
        restarted.import_artifact(artifact.id, "copy.txt")
        self.assertEqual(self.bob.read_file("copy.txt"), "snapshot")
        self.assertEqual(ArtifactExchange(self.charlie).list_available(), ())

    def test_import_never_clobbers_and_preserves_purpose_and_suffix(self):
        self.alice.write_file("code.py", "print('hi')", purpose="code")
        artifact = self.publisher.publish("code.py", ["bob"], purpose="code")
        for path in ["report.md", "code.json", "../code.py", "/tmp/code.py", ".git/code.py"]:
            with self.subTest(path=path), self.assertRaises(PathPolicyError):
                self.receiver.import_artifact(artifact.id, path)
        self.receiver.import_artifact(artifact.id, "incoming/code.py")
        self.assertEqual(self.bob.read_file("incoming/code.py", purpose="code"), "print('hi')")
        with self.assertRaises(FileExistsError):
            self.receiver.import_artifact(artifact.id, "incoming/code.py")
        self.assertEqual(self.bob.read_file("incoming/code.py", purpose="code"), "print('hi')")
        with self.assertRaises(PathPolicyError):
            self.publisher.publish("code.py", ["bob"])

    def test_publish_and_import_share_the_workspace_boundary(self):
        self.alice.write_file("shared.md", "shared")
        artifact = self.publisher.publish("shared.md", ["bob"])
        self.bob.write_file("private.md", "secret")
        (self.alice.workspace / "link.md").symlink_to(self.bob.workspace / "private.md")
        os.link(self.bob.workspace / "private.md", self.alice.workspace / "hard.md")
        for path in ["../bob/workspace/private.md", "link.md", "hard.md"]:
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                self.publisher.publish(path, ["bob"])
        (self.bob.workspace / "escape").symlink_to(self.alice.workspace)
        with self.assertRaises((ValueError, OSError)):
            self.receiver.import_artifact(artifact.id, "escape/stolen.md")
        self.assertFalse((self.alice.workspace / "stolen.md").exists())

    def test_bad_ids_recipients_and_unpublished_sources_are_rejected(self):
        self.alice.write_file("ok.md", "ok")
        for recipients in [[], "bob", ["../bob"], ["bob"] * 65]:
            with self.subTest(recipients=recipients), self.assertRaises(PathPolicyError):
                self.publisher.publish("ok.md", recipients)
        for artifact_id in ["../agents/alice/workspace/ok.md", "", "not-an-id", "a" * 32]:
            with self.subTest(artifact_id=artifact_id), self.assertRaises((ValueError, OSError)):
                self.receiver.import_artifact(artifact_id, "copy.md")
        with self.assertRaises(FileNotFoundError):
            self.publisher.publish("missing.md", ["bob"])

    def test_corrupt_snapshot_manifest_and_symlinks_fail_closed(self):
        self.alice.write_file("ok.md", "original")
        artifact = self.publisher.publish("ok.md", ["bob"])
        folder = self.root / "artifacts" / artifact.id
        (folder / "content").write_bytes(b"tampered")
        with self.assertRaisesRegex(PathPolicyError, "integrity"):
            self.receiver.import_artifact(artifact.id, "copy.md")
        (folder / "content").unlink()
        (folder / "content").symlink_to(self.alice.workspace / "ok.md")
        with self.assertRaises((OSError, ValueError)):
            self.receiver.import_artifact(artifact.id, "copy.md")
        manifest = json.loads((folder / "manifest.json").read_text())
        manifest["purpose"] = "executable"
        (folder / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(PathPolicyError):
            self.receiver.import_artifact(artifact.id, "copy.md")
        self.assertEqual(self.bob.list_files(), ())

    def test_workspace_size_limit_also_applies_to_import(self):
        self.alice.write_file("ok.md", "x" * 64)
        artifact = self.publisher.publish("ok.md", ["bob"])
        small = ArtifactExchange(WorkspaceFilesystem(self.root, "bob", max_file_bytes=32))
        with self.assertRaises(PathPolicyError):
            small.import_artifact(artifact.id, "copy.md")
        self.assertEqual(self.bob.list_files(), ())

    def test_concurrent_imports_have_one_complete_winner(self):
        self.alice.write_file("ok.md", "complete snapshot")
        artifact = self.publisher.publish("ok.md", ["bob"])

        def import_once(_):
            try:
                self.receiver.import_artifact(artifact.id, "shared/copy.md")
                return "imported"
            except FileExistsError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(import_once, range(8)))
        self.assertEqual(results.count("imported"), 1)
        self.assertEqual(results.count("conflict"), 7)
        self.assertEqual(self.bob.read_file("shared/copy.md"), "complete snapshot")
        self.assertEqual(tuple(path.name for path in (self.bob.workspace / "shared").iterdir()),
                         ("copy.md",))

    def test_artifact_storage_symlinks_are_rejected(self):
        self.alice.write_file("ok.md", "ok")
        artifact = self.publisher.publish("ok.md", ["bob"])
        folder = self.root / "artifacts" / artifact.id
        folder.rename(folder.with_name("saved"))
        folder.symlink_to(folder.with_name("saved"))
        with self.assertRaises(PathPolicyError):
            self.receiver.import_artifact(artifact.id, "copy.md")
        with self.assertRaises(PathPolicyError):
            self.receiver.list_available()
        folder.unlink()
        (self.root / "artifacts").rename(self.root / "saved-artifacts")
        (self.root / "artifacts").symlink_to(self.root / "saved-artifacts")
        with self.assertRaises(PathPolicyError):
            ArtifactExchange(self.bob)

    def test_failed_publish_has_no_visible_partial_artifact(self):
        self.alice.write_file("ok.md", "ok")
        with patch("evo.adapters.tools.artifacts._write_at", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.publisher.publish("ok.md", ["bob"])
        self.assertEqual(tuple((self.root / "artifacts").iterdir()), ())
        self.assertEqual(self.receiver.list_available(), ())

    def test_published_snapshot_is_previewable_only_in_its_issue(self):
        app = SQLiteApplication(self.root)
        app.store.save_task(Task("source", "Share evidence", owner="alice", status=TaskStatus.RUNNING))
        app.store.save_task(Task("other", "Unrelated task"))
        self.alice.write_file("evidence.md", "source issue evidence")
        executor = SimpleNamespace(agent="alice", workspace=self.alice, repository=app.store)
        broker = PlatformToolBroker(executor, artifacts=self.publisher)
        result = broker.call("source", "publish_artifact", {"path": "evidence.md", "recipients": ["bob"]})
        self.assertEqual(result["status"], "success")

        item, = app.dispatch("task", {"task_id": "source"})["artifacts"]
        self.assertFalse(item["verified"])
        self.assertEqual(item["archive"]["id"], result["result"]["id"])
        preview = app.dispatch("artifact", {"task_id": "source", "event_id": item["event_id"]})
        self.assertEqual(preview["content"], "source issue evidence")
        with self.assertRaises(ValueError):
            app.dispatch("artifact", {"task_id": "other", "event_id": item["event_id"]})

    def test_storage_is_separate_from_existing_sqlite_databases(self):
        store = SQLiteStore(self.root / "events.sqlite3")
        memory = SQLiteMemoryStore(self.root, "alice")
        self.assertEqual(store.schema_version(), 9)
        self.assertEqual(memory.schema_version(), 2)
        self.assertEqual(self.alice.list_files(), ())
        for path in ["../memory.sqlite3", "../../../events.sqlite3", "../../../artifacts/a.md"]:
            with self.subTest(path=path), self.assertRaises(PathPolicyError):
                self.alice.read_file(path)
        self.alice.write_file("ok.md", "ok")
        self.publisher.publish("ok.md", ["bob"])
        self.assertEqual(memory.schema_version(), 2)
        self.assertEqual(store.schema_version(), 9)
