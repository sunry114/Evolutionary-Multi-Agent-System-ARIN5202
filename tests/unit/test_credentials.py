import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo.adapters.llm.credentials import CredentialError, load_key, save_key


class CredentialTests(unittest.TestCase):
    def test_environment_precedence(self):
        with patch.dict(os.environ, {"HKGAI_API_KEY": "example"}):
            self.assertEqual(load_key("hkgai"), "example")

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and mode contract")
    def test_private_file_and_unsafe_modes(self):
        with tempfile.TemporaryDirectory() as home, patch("pathlib.Path.home", return_value=Path(home)), patch.dict(os.environ, {"HKGAI_API_KEY": ""}):
            path = save_key("hkgai", "example")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_key("hkgai"), "example")
            with self.assertRaises(FileExistsError):
                save_key("hkgai", "different")
            path.chmod(0o644)
            with self.assertRaises(CredentialError):
                load_key("hkgai")
            path.chmod(0o600)
            path.parent.chmod(0o755)
            with self.assertRaises(CredentialError):
                load_key("hkgai")

    @unittest.skipUnless(os.name == "posix", "POSIX symlink contract")
    def test_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as home, patch("pathlib.Path.home", return_value=Path(home)), patch.dict(os.environ, {"HKGAI_API_KEY": ""}):
            path = save_key("hkgai", "example")
            target = path.with_suffix(".target")
            path.rename(target)
            path.symlink_to(target)
            with self.assertRaises(CredentialError):
                load_key("hkgai")
