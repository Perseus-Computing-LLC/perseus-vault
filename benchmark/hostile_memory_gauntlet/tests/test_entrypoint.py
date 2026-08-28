import os
import subprocess
import sys
import unittest
from pathlib import Path


class EntrypointTests(unittest.TestCase):
    def test_direct_run_script_help_is_importable_from_repository_root(self):
        root = Path(__file__).resolve().parents[3]
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(root / "benchmark/hostile_memory_gauntlet/run.py"), "--help"],
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("usage:", result.stdout)