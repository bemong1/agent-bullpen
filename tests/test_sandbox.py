"""The tests never read the real home or the real ~/.cache: compat pins HOME and XDG_CACHE_HOME to a throwaway folder before board is imported, and children get
isolated_env. A status line reading in the real cache (statusline.py leaves one) changes the plan bar, so a machine that has one must give the same results.

    python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compat  # noqa: E402
from compat import isolated_env  # noqa: E402

from board import lineage, plans, util  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Sandbox(unittest.TestCase):
    def test_the_paths_the_modules_fixed_at_import_are_in_the_throwaway_folder(self):
        for name, value in (('plans.STATUSLINE_STATE', plans.STATUSLINE_STATE), ('lineage.LINK_CACHE', lineage.LINK_CACHE), ('util.HOME', util.HOME),
                            ('util.CLAUDE_HOME', util.CLAUDE_HOME), ('util.CODEX_HOME', util.CODEX_HOME)):
            self.assertTrue(value.startswith(compat.SANDBOX + os.sep), '%s = %s is outside %s' % (name, value, compat.SANDBOX))

    def test_a_real_looking_status_line_file_is_not_read(self):
        with tempfile.TemporaryDirectory() as real:
            folder = os.path.join(real, '.cache', 'agent-bullpen')
            os.makedirs(folder, mode=0o700)
            path = os.path.join(folder, 'statusline.json')
            now = time.time()
            with open(path, 'w') as f:
                json.dump({'v': 1, 'at': now - 5, 'rate_limits': {'five_hour': {'used_percentage': 41, 'resets_at': now + 3600}}}, f)
            os.chmod(path, 0o600)
            code = 'import sys; sys.path.insert(0, %r); import compat; from board import plans; print(plans.statusline_usage())' % os.path.dirname(os.path.abspath(__file__))
            env = dict(os.environ, HOME=real, XDG_CACHE_HOME=os.path.join(real, '.cache'), PYTHONDONTWRITEBYTECODE='1')
            env.pop('AB_SRC', None)
            out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
            self.assertEqual((out.returncode, out.stdout.strip()), (0, 'None'), out.stderr)
            direct = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, %r); from board import plans; print(plans.statusline_usage() is not None)' % ROOT],
                                    capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
            self.assertEqual(direct.stdout.strip(), 'True', direct.stderr)           # and the file is a good one: without compat the module would have read it

    def test_children_get_their_own_home_and_cache(self):
        os.environ['CLAUDE_CONFIG_DIR'] = '/real/claude'
        os.environ['AGENT_BULLPEN_TOKEN'] = 'x' * 20
        try:
            env = isolated_env('/h/x', AGENT_BULLPEN_LANG='ko')
        finally:
            del os.environ['CLAUDE_CONFIG_DIR'], os.environ['AGENT_BULLPEN_TOKEN']
        self.assertEqual((env['HOME'], env['XDG_CACHE_HOME'], env['AGENT_BULLPEN_LANG'], env['PYTHONDONTWRITEBYTECODE']), ('/h/x', '/h/x/.cache', 'ko', '1'))
        for name in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'AGENT_BULLPEN_LOG', 'AGENT_BULLPEN_TOKEN'):
            self.assertNotIn(name, env)


if __name__ == '__main__':
    unittest.main()
