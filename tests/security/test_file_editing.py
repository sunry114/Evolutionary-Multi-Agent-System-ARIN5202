import tempfile
import unittest
from pathlib import Path

from evo.adapters.tools.filesystem import PathPolicyError, WorkspaceFilesystem


class FileEditingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fs = WorkspaceFilesystem(directory.name, "builder", max_file_bytes=128)

    def test_range_read_preserves_unicode_line_numbers_and_rejects_invalid_ranges(self):
        self.fs.write_file("report.md", "first\n第二行\nthird\n")
        self.assertEqual(self.fs.read_file("report.md", start_line=2, max_lines=1), "第二行\n")
        self.assertEqual(self.fs.read_file("report.md", start_line=9, max_lines=2), "")
        for fields in ({"start_line": 0}, {"start_line": True}, {"max_lines": -1}, {"max_lines": 501}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.fs.read_file("report.md", **fields)

    def test_edits_require_unique_match_and_preserve_original_on_oversize(self):
        self.fs.write_file("report.md", "old old")
        for old, new in (("old", "new"), ("missing", "new"), ("old old", "x" * 129)):
            with self.subTest(old=old), self.assertRaises(ValueError):
                self.fs.edit_file("report.md", old, new)
            self.assertEqual(self.fs.read_file("report.md"), "old old")
        self.fs.edit_file("report.md", "old old", "new")
        self.assertEqual(self.fs.read_file("report.md"), "new")

    def test_edit_search_and_syntax_checks_cannot_follow_foreign_links(self):
        other = WorkspaceFilesystem(self.fs.workspace.parents[2], "other")
        other.write_file("private.py", "secret = 1\n", purpose="code")
        (self.fs.workspace / "foreign.py").symlink_to(other.workspace / "private.py")
        for operation in (lambda: self.fs.edit_file("foreign.py", "1", "2", purpose="code"),
                          lambda: self.fs.check_python("foreign.py"),
                          lambda: self.fs.search_files("secret", "../other")):
            with self.assertRaises((ValueError, OSError)):
                operation()
        self.assertEqual(self.fs.search_files("secret")["matches"], [])
        self.assertEqual(other.read_file("private.py", purpose="code"), "secret = 1\n")

    def test_literal_search_reports_line_and_truncation_with_no_regex_execution(self):
        self.fs.write_file("nested/source.py", "# needle\n# needle\n", purpose="code")
        result = self.fs.search_files("needle", max_results=1)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["matches"][0]["path"], "nested/source.py")
        self.assertEqual(result["matches"][0]["line"], 1)
        self.assertEqual(self.fs.search_files(".*")["matches"], [])

    def test_python_check_compiles_without_importing_or_executing(self):
        marker = self.fs.workspace / "must-not-exist.md"
        source = "from pathlib import Path\nPath('must-not-exist.md').write_text('executed')\n"
        self.fs.write_file("check.py", source, purpose="code")
        self.assertTrue(self.fs.check_python("check.py")["valid"])
        self.assertFalse(marker.exists())
        self.fs.write_file("check.py", "return 1\n", purpose="code")
        result = self.fs.check_python("check.py")
        self.assertFalse(result["valid"])
        self.assertEqual(result["line"], 1)

    def test_execution_digest_includes_data_and_empty_directories_and_refuses_links(self):
        self.fs.write_file("source.py", "VALUE=1\n", purpose="code")
        original = self.fs.input_digest()
        (self.fs.workspace / "bookings.db").write_bytes(b"binary data\x00")
        with_data = self.fs.input_digest()
        self.assertNotEqual(original, with_data)
        (self.fs.workspace / "empty").mkdir()
        self.assertNotEqual(with_data, self.fs.input_digest())
        (self.fs.workspace / "unsafe.py").symlink_to(self.fs.workspace / "source.py")
        with self.assertRaises((ValueError, OSError)):
            self.fs.input_digest()
