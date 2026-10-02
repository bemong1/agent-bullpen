"""tools/regress/i18n_check.py --strict is the CI gate for the dictionaries: the shipped static/ is clean (exit 0), and each kind of mistake makes it exit 1
(without --strict the same mistake is only a report, exit 0). The copies below are made in a temporary folder; nothing in the repository is changed.

    python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(ROOT, 'tools', 'regress', 'i18n_check.py')


def run(static, *flags):
    r = subprocess.run([sys.executable, CHECK, '--static', static] + list(flags), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


class Gate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.static = os.path.join(os.path.realpath(tmp.name), 'static')
        shutil.copytree(os.path.join(ROOT, 'static'), self.static, ignore=shutil.ignore_patterns('fonts'))

    def edit(self, code, fn):
        path = os.path.join(self.static, 'locales', code + '.json')
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        fn(d)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1)

    def append(self, name, text):
        with open(os.path.join(self.static, name), 'a', encoding='utf-8') as f:
            f.write(text)

    def test_the_shipped_files_pass_strict(self):
        rc, out = run(self.static, '--strict')
        self.assertEqual(rc, 0, out)
        self.assertIn('0 finding(s)', out)
        self.assertEqual(run(os.path.join(ROOT, 'static'), '--strict')[0], 0)      # the repository's own folder, not only the copy

    def mistakes(self):
        """name -> (what to break, the finding group that must be named)"""
        key = 'common.you'
        return {
            'a key missing in ko': (lambda: self.edit('ko', lambda d: d['messages'].pop(key)), 'keys'),
            'a key missing in en': (lambda: self.edit('en', lambda d: d['messages'].pop(key)), 'keys'),
            'Hangul in an English text': (lambda: self.edit('en', lambda d: d['messages'].__setitem__(key, 'You 당신')), 'hangul'),
            'a forbidden term in English': (lambda: self.edit('en', lambda d: d['messages'].__setitem__(key, 'Open the drawer')), 'terms'),
            'a placeholder that English lacks': (lambda: self.edit('ko', lambda d: d['messages'].__setitem__('time.ago.min', '{count}분 전')), 'params'),
            'a plural without other': (lambda: self.edit('en', lambda d: d['messages'].__setitem__('unit.agent', {'one': '{count} agent'})), 'plural'),
            'an English text over its limit': (lambda: self.edit('en', lambda d: d['limits'].__setitem__('common.you', 2)), 'limits'),
            'a page that names a key nobody defined': (lambda: self.append('common.js', "\nconst missingKey = t('no.such.key');\n"), 'refs'),
            'Hangul in a page outside comments': (lambda: self.append('common.js', "\nconst leaked = '한글이 새었다';\n"), 'static-hangul'),
        }

    def test_each_kind_of_mistake_fails_strict_and_is_only_a_report_without_it(self):
        for name, (break_it, group) in self.mistakes().items():
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            self.static = os.path.join(os.path.realpath(tmp.name), 'static')         # a fresh copy for every mistake
            shutil.copytree(os.path.join(ROOT, 'static'), self.static, ignore=shutil.ignore_patterns('fonts'))
            break_it()
            rc, out = run(self.static, '--strict')
            self.assertEqual(rc, 1, (name, out))
            self.assertRegex(out, r'(?m)^  %s\s+[1-9]' % group, (name, out))        # the group's count is not 0
            rc, out = run(self.static)
            self.assertEqual(rc, 0, (name, out))
            self.assertIn('warnings only', out)

    def test_a_broken_dictionary_file_fails_strict(self):
        with open(os.path.join(self.static, 'locales', 'en.json'), 'w', encoding='utf-8') as f:
            f.write('{ not json')
        rc, out = run(self.static, '--strict')
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r'(?m)^  syntax\s+[1-9]')


if __name__ == '__main__':
    unittest.main()
