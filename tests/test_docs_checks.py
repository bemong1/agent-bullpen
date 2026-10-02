"""The public text says what the program does: tools/regress/docs_checks.py (no removed-API remnant, the statusline.json and macOS wording, the start output shown in the
READMEs and in configuration.md) runs with the unit tests, so the CI that runs them stops a document that contradicts the code.

    python3 -m unittest discover -s tests
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DocsChecks(unittest.TestCase):
    def test_the_public_text_matches_the_program(self):
        out = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'regress', 'docs_checks.py')], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        failed = [ln for ln in out.stdout.splitlines() if ln.startswith('FAIL')]
        self.assertEqual((out.returncode, failed), (0, []), out.stdout + out.stderr)
        self.assertIn('ALL PASS', out.stdout)


if __name__ == '__main__':
    unittest.main()
