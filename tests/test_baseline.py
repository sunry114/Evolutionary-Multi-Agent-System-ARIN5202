import unittest

from evo import __version__
from evo.interfaces.cli import main


class BaselineTests(unittest.TestCase):
    def test_version_has_one_source(self):
        from importlib.metadata import version
        self.assertEqual(version("evo-collab"), __version__)

    def test_cli_help(self):
        with self.assertRaises(SystemExit) as result:
            main(["--help"])
        self.assertEqual(result.exception.code, 0)
