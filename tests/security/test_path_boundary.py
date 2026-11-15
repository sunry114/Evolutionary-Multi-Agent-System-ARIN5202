import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo.adapters.tools.filesystem import PathPolicyError, WorkspaceFilesystem


@unittest.skipUnless(sys.platform == "linux", "Linux workspace policy")
class PathBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fs = WorkspaceFilesystem(self.root / "state", "alice", max_file_bytes=32)
        self.other = WorkspaceFilesystem(self.root / "state", "bob")
        self.other.write_file("private.txt", "Bob's secret")

    def test_nested_crud_limits_and_private_modes(self):
        self.fs.write_file("notes/结果.MD", "你好")
        self.assertEqual(self.fs.read_file("notes/结果.MD"), "你好")
        self.assertEqual(self.fs.list_files(), ("notes/",))
        self.assertEqual(self.fs.list_files("notes"), ("结果.MD",))
        self.assertEqual(stat.S_IMODE(self.fs.workspace.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.fs.workspace / "notes/结果.MD").stat().st_mode), 0o600)
        self.fs.write_file("notes/结果.MD", "updated")
        with self.assertRaises(FileExistsError):
            self.fs.write_file("notes/结果.MD", "lost", overwrite=False)
        with self.assertRaises(PermissionError):
            self.fs.delete_file("notes/结果.MD")
        self.assertEqual(self.fs.read_file("notes/结果.MD"), "updated")
        self.fs.delete_file("notes/结果.MD", approved=True)
        self.assertEqual(self.fs.list_files("notes"), ())

    def test_rejects_traversal_absolute_git_device_and_foreign_paths(self):
        paths = ["../bob/workspace/private.txt", "notes/../../private.txt",
                 str(self.other.workspace / "private.txt"), "/dev/null", "",
                 "./report.md", "notes//report.md", "notes/../report.md",
                 "report.md/", ".git/config", ".GIT/info/note.md",
                 "nested/.git/readme.md", "C:\\private.txt", "C:/private.txt",
                 "\\\\server\\share\\private.txt", "note\x00.md", "note\n.md",
                 ".evo-write-fake.md"]
        for path in paths:
            with self.subTest(path=path):
                for operation in (
                    lambda: self.fs.read_file(path),
                    lambda: self.fs.write_file(path, "attack"),
                    lambda: self.fs.delete_file(path, approved=True),
                    lambda: self.fs.list_files(path),
                ):
                    with self.assertRaises((ValueError, OSError)):
                        operation()
        self.assertEqual(self.other.read_file("private.txt"), "Bob's secret")

    def test_purpose_controls_document_and_non_document_suffixes(self):
        for purpose, path in [("document", "report.txt"), ("code", "main.py"),
                              ("config", "config.json"), ("config", "config.yaml"),
                              ("data", "data.json"), ("data", "data.csv")]:
            self.fs.write_file(path, "text", purpose=purpose)
            self.assertEqual(self.fs.read_file(path, purpose=purpose), "text")
        for path in ["report.pdf", "report.docx", "report.html", "script.sh",
                     "report.json", "report.csv", "report.py", "report.yaml"]:
            with self.subTest(path=path), self.assertRaises(PathPolicyError):
                self.fs.write_file(path, "a document")
        for path, purpose in [("main.py", "data"), ("report.md", "config"),
                              ("report.txt", "unknown")]:
            with self.subTest(path=path, purpose=purpose), self.assertRaises(PathPolicyError):
                self.fs.write_file(path, "text", purpose=purpose)

    def test_symlink_files_directories_and_internal_links_are_rejected(self):
        self.fs.write_file("own.txt", "original")
        for name, target in [("linked.txt", self.other.workspace / "private.txt"),
                             ("inside.txt", self.fs.workspace / "own.txt"),
                             ("dangling.txt", self.root / "missing.txt"),
                             ("loop.txt", self.fs.workspace / "loop.txt"),
                             ("foreign", self.other.workspace)]:
            (self.fs.workspace / name).symlink_to(target)
        for path in ["linked.txt", "inside.txt", "dangling.txt", "loop.txt", "foreign/private.txt"]:
            with self.subTest(path=path):
                for operation in [lambda: self.fs.read_file(path),
                                  lambda: self.fs.write_file(path, "attack"),
                                  lambda: self.fs.delete_file(path, approved=True)]:
                    with self.assertRaises((ValueError, OSError)):
                        operation()
        self.assertEqual(self.fs.list_files(), ("own.txt",))
        with self.assertRaises((ValueError, OSError)):
            self.fs.list_files("foreign")
        self.assertEqual(self.other.read_file("private.txt"), "Bob's secret")
        self.assertEqual(self.fs.read_file("own.txt"), "original")
        self.assertFalse((self.root / "missing.txt").exists())

    def test_no_follow_prevents_swap_after_resolve(self):
        self.fs.write_file("folder/secret.txt", "local")
        original_open = os.open
        swapped = False

        def swap_then_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "folder" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                (self.fs.workspace / "folder").rename(self.fs.workspace / "original")
                (self.fs.workspace / "folder").symlink_to(self.other.workspace)
            return original_open(path, flags, *args, **kwargs)

        with patch("evo.adapters.tools.filesystem.os.open", side_effect=swap_then_open):
            with self.assertRaises(OSError):
                self.fs.write_file("folder/private.txt", "attack")
        self.assertTrue(swapped)
        self.assertEqual(self.other.read_file("private.txt"), "Bob's secret")

    def test_hardlinks_fifos_and_device_links_cannot_be_used(self):
        os.link(self.other.workspace / "private.txt", self.fs.workspace / "hard.txt")
        os.mkfifo(self.fs.workspace / "pipe.txt")
        (self.fs.workspace / "device.txt").symlink_to("/dev/zero")
        for path in ["hard.txt", "pipe.txt", "device.txt"]:
            with self.subTest(path=path):
                for operation in [lambda: self.fs.read_file(path),
                                  lambda: self.fs.write_file(path, "attack"),
                                  lambda: self.fs.delete_file(path, approved=True)]:
                    with self.assertRaises((ValueError, OSError)):
                        operation()
        self.assertEqual(self.fs.list_files(), ())
        self.assertEqual((self.other.workspace / "private.txt").read_text(), "Bob's secret")

    def test_size_limit_applies_to_utf8_bytes_reads_and_existing_files(self):
        self.fs.write_file("limit.txt", "x" * 32)
        self.assertEqual(len(self.fs.read_file("limit.txt")), 32)
        for content in ["x" * 33, "你" * 11]:
            with self.assertRaises(PathPolicyError):
                self.fs.write_file("limit.txt", content)
        self.assertEqual(self.fs.read_file("limit.txt"), "x" * 32)
        (self.fs.workspace / "large.txt").write_bytes(b"x" * 33)
        for operation in [lambda: self.fs.read_file("large.txt"),
                          lambda: self.fs.write_file("large.txt", "replace"),
                          lambda: self.fs.delete_file("large.txt", approved=True)]:
            with self.assertRaises(PathPolicyError):
                operation()
        self.assertEqual(self.fs.list_files(), ("limit.txt",))
        (self.fs.workspace / "binary.txt").write_bytes(b"\xff")
        with self.assertRaises(PathPolicyError):
            self.fs.read_file("binary.txt")

    def test_workspace_roots_and_agent_identifiers_are_validated(self):
        for agent in ["..", "a/b", "", ".git", "a\\b", "a" * 65, None]:
            with self.subTest(agent=agent), self.assertRaises(PathPolicyError):
                WorkspaceFilesystem(self.root / "state", agent)
        (self.root / "alias").symlink_to(self.root / "state")
        with self.assertRaises(PathPolicyError):
            WorkspaceFilesystem(self.root / "alias", "alice")
        (self.fs.workspace / "local.txt").write_text("local")
        self.fs.workspace.rename(self.fs.workspace.with_name("old"))
        self.fs.workspace.symlink_to(self.other.workspace)
        with self.assertRaises(PathPolicyError):
            self.fs.read_file("private.txt")
        with self.assertRaises(PathPolicyError):
            WorkspaceFilesystem(self.root / "state", "alice")

    def test_directory_listing_is_bounded(self):
        self.fs.write_file("one.md", "one")
        self.fs.write_file("two.md", "two")
        with patch("evo.adapters.tools.filesystem.MAX_DIRECTORY_ENTRIES", 1):
            with self.assertRaises(PathPolicyError):
                self.fs.list_files()

    def test_repeated_failed_operations_release_file_descriptors(self):
        (self.fs.workspace / "foreign").symlink_to(self.other.workspace)
        os.mkfifo(self.fs.workspace / "pipe.txt")
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(30):
            for path in ["foreign/private.txt", "pipe.txt", "missing/note.md"]:
                with self.assertRaises((ValueError, OSError)):
                    self.fs.read_file(path)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_unsupported_platform_fails_closed(self):
        with patch("evo.adapters.tools.filesystem.sys.platform", "darwin"):
            with self.assertRaisesRegex(RuntimeError, "require Linux"):
                WorkspaceFilesystem(self.root / "state", "alice")
