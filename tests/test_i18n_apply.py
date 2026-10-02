"""static/i18n.js apply(): the English text written in the HTML stays when the dictionary cannot be loaded or lacks the key (no [key] on the page).
The checks are tools/regress/i18n_apply_checks.js (they parse the real index.html and game.html); this runs them with the unit tests. Skipped when node is missing.

    python3 -m unittest discover -s tests
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class HtmlFallback(unittest.TestCase):
    def test_apply_keeps_the_html_text_when_a_key_is_missing(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node is not installed')
        r = subprocess.run([node, os.path.join(ROOT, 'tools', 'regress', 'i18n_apply_checks.js'), os.path.join(ROOT, 'static')],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
        fails = [line for line in r.stdout.splitlines() if line.startswith(('FAIL', 'HARNESS'))]
        self.assertEqual((r.returncode, fails), (0, []), r.stdout[-2000:] + r.stderr[-1000:])
        self.assertIn('ALL PASS', r.stdout)


if __name__ == '__main__':
    unittest.main()
