"""Protect the expensive standalone and import-side-effect boundaries."""

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ArchitectureTests(unittest.TestCase):
    def test_no_gateway_or_workspace_dependencies(self):
        source = Path(__file__).resolve().parents[1] / "src" / "telegram_user_cli"
        for path in source.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                for name in names:
                    self.assertNotIn(name.split(".")[0], {"openclaw", "jarvis", "session_owner"})

    def test_import_does_not_create_state_or_open_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ, XDG_DATA_HOME=directory)
            code = ("from unittest.mock import patch; "
                    "guard = patch('socket.socket.connect', side_effect=AssertionError('network on import')); "
                    "guard.start(); import telegram_user_cli.cli")
            result = subprocess.run([sys.executable, "-c", code], env=environment,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
