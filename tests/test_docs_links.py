"""The links in the user-facing documents are alive: every relative link points at a file or folder that exists, and every `#anchor` is a heading of the document it
points at (headings slugged the way GitHub does). The working notes under docs/release, docs/i18n, docs/refactor and docs/plan-codex are history and are not checked.

    python3 -m unittest discover -s tests
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = ['README.md', 'README.ko.md', 'CHANGELOG.md', 'THIRD_PARTY.md'] + sorted(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, 'docs', '*.md')) +
                                                                                    glob.glob(os.path.join(ROOT, 'docs', 'features', '*.md')))
LINK_RE = re.compile(r'!?\[[^\]]*\]\(([^)\s]+)(?:\s+"[^"]*")?\)|<a\s[^>]*href="([^"]+)"|<(https?://[^>\s]+)>')
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')


def read_lines(path):
    with open(path, encoding='utf-8') as f:
        return f.read().split('\n')


def outside_fences(lines):
    """(line number, text) of the lines that are not inside a ``` block."""
    fenced = False
    for n, line in enumerate(lines, 1):
        if line.lstrip().startswith('```'):
            fenced = not fenced
            continue
        if not fenced:
            yield n, line


def slug(heading):
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', heading)             # a link keeps its text
    text = re.sub(r'[`*_~]', lambda m: '_' if m.group(0) == '_' else '', text).lower()
    return re.sub(r'[^\w\- ]', '', text).replace(' ', '-')


def anchors(path):
    seen, out = {}, set()
    for _, line in outside_fences(read_lines(path)):
        m = HEADING_RE.match(line)
        if m:
            s = slug(m.group(2))
            n = seen.get(s, 0)
            seen[s] = n + 1
            out.add(s if n == 0 else '%s-%d' % (s, n))
    return out


class Links(unittest.TestCase):
    def test_the_live_documents_exist(self):
        for rel in LIVE:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), rel)
        self.assertIn('docs/remote.md', LIVE)

    def test_every_relative_link_and_anchor_is_alive(self):
        bad = []
        cache = {}
        for rel in LIVE:
            here = os.path.join(ROOT, rel)
            for n, line in outside_fences(read_lines(here)):
                for m in LINK_RE.finditer(line):
                    target = m.group(1) or m.group(2)
                    if not target or re.match(r'[a-z][a-z0-9+.-]*:', target, re.I):
                        continue                                                    # http(s), mailto, ...
                    path, _, frag = target.partition('#')
                    there = here if not path else os.path.normpath(os.path.join(os.path.dirname(here), path))
                    if not os.path.exists(there):
                        bad.append('%s:%d  %s  (no such file)' % (rel, n, target))
                        continue
                    if frag and there.endswith('.md'):
                        if there not in cache:
                            cache[there] = anchors(there)
                        if frag not in cache[there]:
                            bad.append('%s:%d  %s  (no such heading in %s)' % (rel, n, target, os.path.relpath(there, ROOT)))
        self.assertEqual(bad, [])

    def test_the_slug_rule_matches_github_for_the_headings_we_link_to(self):
        self.assertEqual(slug('Security & privacy'), 'security--privacy')
        self.assertEqual(slug('`statusline.py`: plan usage from the status line'), 'statuslinepy-plan-usage-from-the-status-line')
        self.assertEqual(slug("Plan usage from Claude Code's status line"), 'plan-usage-from-claude-codes-status-line')
        self.assertEqual(slug('보안과 개인정보'), '보안과-개인정보')
        self.assertEqual(slug('Language of the screen and of the terminal'), 'language-of-the-screen-and-of-the-terminal')


if __name__ == '__main__':
    unittest.main()
