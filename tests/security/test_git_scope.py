import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from evo.adapters.tools.filesystem import WorkspaceFilesystem
from evo.adapters.tools.git import HostGitTools, RepositoryToken, validate_repository, validate_branch
from evo.ports.tools import ExecutionResult, ToolError


class GitScopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fs = WorkspaceFilesystem(Path(temp.name), "alice")
        self.root = self.fs.workspace / ".git"
        self.root.mkdir()
        self.config = self.root / "config"
        self.config.write_text("[core]\nrepositoryformatversion = 0\nfilemode = true\nbare = false\nlogallrefupdates = true\n")
        self.repo = "https://github.com/example/project.git"
        self.git = HostGitTools(self.fs)

    def test_repository_requires_exact_canonical_github_https(self):
        self.assertEqual(validate_repository(self.repo), self.repo)
        for repo in ("http://github.com/example/project.git", "git@github.com:example/project.git",
                     "ssh://github.com/example/project.git", "https://github.com.evil/example/project.git",
                     "https://token@github.com/example/project.git", "https://github.com:443/example/project.git",
                     "https://github.com/example/project", self.repo + "/", self.repo + "?x=1", self.repo + "#x",
                     "https://github.com/example/../project.git", "https://github.com/example/%70roject.git",
                     "file:///tmp/repo", "ext::anything", "--force", None):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                validate_repository(repo)

    def test_branch_cannot_inject_refspec_options_or_special_refs(self):
        for branch in ("main", "codex/feature-1", "release/v1.2"):
            self.assertEqual(validate_branch(branch), branch)
        for branch in ("", "HEAD", "--all", "+main", "main:other", "a..b", "a//b", "a/.hidden",
                       "a.lock", "a/x.lock", "a.", "a/", "/a", "a@{b", "a b", "a\\b", "a\nb", None):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                validate_branch(branch)

    def test_executable_and_network_git_configuration_is_rejected_before_execution(self):
        original = self.config.read_text()
        for config in ('[url "https://evil/"]\ninsteadOf=https://github.com/\n',
                       '[url "https://evil/"]\npushInsteadOf=https://github.com/\n',
                       '[credential]\nhelper=!anything\n', '[core]\nhooksPath=/tmp/hooks\n',
                       '[core]\nfsmonitor=anything\n', '[include]\npath=/tmp/config\n',
                       '[includeIf "gitdir:*"]\npath=/tmp/config\n',
                       '[filter "evil"]\nclean=anything\n', '[http]\nsslVerify=false\n',
                       '[remote "origin"]\nurl=https://github.com/example/project.git\n',
                       '[core]\nsshCommand=anything\n', '[extensions]\nworktreeConfig=true\n'):
            # Separate configs also cover duplicated core sections and parser errors.
            self.config.write_text(original + config)
            with patch.object(self.git, "_capture") as capture:
                with self.subTest(config=config), self.assertRaises(ToolError):
                    self.git.status()
                capture.assert_not_called()

    def test_gitdir_file_and_symlink_are_rejected(self):
        self.config.unlink()
        self.root.rmdir()
        self.root.write_text("gitdir: /tmp/elsewhere")
        with self.assertRaises(ToolError):
            self.git.status()
        self.root.unlink()
        self.root.symlink_to(self.fs.workspace, target_is_directory=True)
        with self.assertRaises(ToolError):
            self.git.status()

    def test_linked_and_external_git_metadata_are_rejected(self):
        for name in ("commondir", "gitdir", "config.worktree", "objects/info/alternates", "objects/info/http-alternates"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("/tmp/elsewhere")
            with self.subTest(name=name), self.assertRaises(ToolError):
                self.git.validate_local_repository()
            path.unlink()
        link = self.root / "HEAD"
        link.symlink_to(self.config)
        with self.assertRaises(ToolError):
            self.git.validate_local_repository()
        link.unlink()
        os.link(self.config, link)
        with self.assertRaises(ToolError):
            self.git.validate_local_repository()

    def test_commit_requires_explicit_safe_existing_files(self):
        self.fs.write_file("report.md", "report")
        self.git.validate_commit(["report.md"], "Report")
        for paths, message in (([], "Report"), (["."], "Report"), (["../report.md"], "Report"),
                               ([".git/config"], "Report"), (["report.md", "report.md"], "Report"),
                               ([":(glob)*"], "Report"), (["missing.md"], "Report"),
                               (["report.md"], ""), (["report.md"], "line\nbreak")):
            with self.subTest(paths=paths), self.assertRaises((ValueError, OSError)):
                self.git.validate_commit(paths, message)

    def test_push_denial_and_missing_token_do_not_start_push(self):
        with patch.object(self.git, "head", return_value="a" * 40), patch.object(self.git, "_run") as run:
            with self.assertRaises(PermissionError):
                self.git.push(self.repo, "main", "a" * 40)
            with self.assertRaises(ToolError):
                self.git.push(self.repo, "main", "a" * 40, approved=True)
            run.assert_not_called()

    def test_token_provider_matches_exact_repository_and_rejects_control_characters(self):
        loader = Mock(return_value="fixture_token")
        provider = RepositoryToken(self.repo, loader)
        with self.assertRaises(ToolError):
            provider.read("https://github.com/example/other.git")
        loader.assert_not_called()
        self.assertEqual(provider.read(self.repo), "fixture_token")
        for token in (None, "", "bad\nheader", "bad:header", "x" * 2049):
            loader.return_value = token
            with self.assertRaises(ToolError):
                provider.read(self.repo)

    def test_changed_head_never_loads_token(self):
        provider = Mock()
        self.git.token_provider = provider
        with patch.object(self.git, "head", return_value="b" * 40):
            with self.assertRaises(ToolError):
                self.git.push(self.repo, "main", "a" * 40, approved=True)
        provider.read.assert_not_called()

    def test_local_command_disables_hooks_credentials_and_repository_discovery(self):
        ok = ExecutionResult(0, "", "")
        with patch("shutil.which", return_value="/usr/bin/git"), patch.object(self.git, "_capture", return_value=ok) as capture:
            self.git.status()
        command = capture.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/git")
        self.assertEqual(capture.call_args.kwargs["environment"]["GIT_CEILING_DIRECTORIES"], str(self.fs.workspace.parent))
        for setting in ("core.hooksPath=/dev/null", "core.fsmonitor=false", "credential.helper=",
                        "protocol.allow=never", "http.followRedirects=false", "http.sslVerify=true"):
            self.assertIn(setting, command)

    def test_push_token_is_scoped_to_git_environment_and_output_is_sanitized(self):
        token = "fixture_token"
        encoded = base64.b64encode(("x-access-token:" + token).encode()).decode("ascii")
        self.git.token_provider = RepositoryToken(self.repo, lambda: token)
        with patch.object(self.git, "head", return_value="a" * 40), patch("shutil.which", return_value="/usr/bin/git"), patch.object(
            self.git, "_capture", return_value=ExecutionResult(0, token + encoded, token)) as capture:
            result = self.git.push(self.repo, "codex/test", "a" * 40, approved=True)
        command = capture.call_args.args[0]
        env = capture.call_args.kwargs["environment"]
        self.assertNotIn(token, str(command))
        self.assertNotIn(encoded, str(command))
        self.assertNotIn(token, str(result))
        self.assertNotIn(encoded, str(result))
        self.assertIn("a" * 40 + ":refs/heads/codex/test", command)
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http." + self.repo + ".extraHeader")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "Authorization: Basic " + encoded)

    def test_missing_git_reports_an_unavailable_executable(self):
        with patch("shutil.which", return_value=None), patch.object(self.git, "_capture") as capture:
            with self.assertRaisesRegex(ToolError, "Git executable is unavailable"):
                self.git.status()
            capture.assert_not_called()

    def test_timeout_and_start_error_are_reported(self):
        for outcome in (ExecutionResult(-9, "", "", timed_out=True), ToolError("start failed")):
            with patch("shutil.which", return_value="/usr/bin/git"), patch.object(self.git, "_capture", side_effect=[outcome]):
                if isinstance(outcome, Exception):
                    with self.assertRaises(ToolError):
                        self.git.status()
                else:
                    self.assertTrue(self.git.status().timed_out)
