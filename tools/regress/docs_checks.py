#!/usr/bin/env python3
"""Checks that the public text says what the program does. Standard library only (3.9+).

    python3 tools/regress/docs_checks.py          # prints PASS/FAIL per check, exit code 1 if any FAIL

  - no remnant of the removed usage request in README, CHANGELOG, docs/*.md and the issue templates (a first release has no "earlier" to explain)
  - statusline.json: both READMEs name the spending limit (gateway accounts) among what the file keeps
  - one sentence about macOS in the changelog and the README tables, and no claim that the Mac was tested
  - the start output shown in the READMEs and in configuration.md is the one the dictionary makes
  - the plan bar text: what the status line does not carry (model weeks, extra usage) is said to come from the cache with its own time
"""
import glob
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
fails = 0


def read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def check(name, ok, detail=''):
    global fails
    if not ok:
        fails += 1
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  ' + str(detail)))


def section(text, start, end):
    """The text from the line that starts with `start` up to the next line that starts with `end`."""
    i = text.index(start)
    j = text.find('\n' + end, i + len(start))
    return text[i:j if j >= 0 else len(text)]


PUBLIC = ['README.md', 'README.ko.md', 'CHANGELOG.md'] + sorted(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, 'docs', '*.md'))) \
    + sorted(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, '.github', 'ISSUE_TEMPLATE', '*')))

# ---- no remnant of the removed usage request, no "earlier version" the first release does not have
REMNANTS = [r'claude-usage-api', r'usage API', r'usage\.json', r'API failure', r'internal build', r'Loopback is as before', r'works as before', r'browsing to the LAN address gets a 403']
for rel in PUBLIC:
    text = read(rel)
    hits = [(pat, m.start()) for pat in REMNANTS for m in [re.search(pat, text)] if m]
    check('%s: no removed-API remnant or "as before" about an earlier release' % rel, not hits, [(p, text[max(0, i - 40):i + 60].replace('\n', ' ')) for p, i in hits])

# ---- statusline.json keeps the spending limit of a gateway account, and the READMEs say so
en, ko = read('README.md'), read('README.ko.md')
leaves_en = section(en, '- **What it leaves:**', '- **On the screen**')
leaves_ko = section(ko, '- **남기는 것:**', '- **화면:**')
check('README.md: "What it leaves" names spend_limit with its dollar amounts', 'spend_limit' in leaves_en and 'dollar' in leaves_en and 'gateway' in leaves_en, leaves_en[:300])
check('README.ko.md: "남기는 것" names spend_limit with its dollar amounts', 'spend_limit' in leaves_ko and ('달러' in leaves_ko or '금액' in leaves_ko) and '게이트웨이' in leaves_ko, leaves_ko[:300])

def intro_bullets(text):
    head = text[:text.index('![')]
    return [ln for ln in head.splitlines() if ln.startswith('- ')]


check('both READMEs open with the same four-point introduction before the first picture', len(intro_bullets(en)) == 4 and len(intro_bullets(ko)) == 4, [len(intro_bullets(en)), len(intro_bullets(ko))])
for rel in ('docs/guide.md', 'docs/guide.ko.md'):
    check('%s: the statusline.json description names spend_limit' % rel, 'spend_limit' in read(rel), '')

# ---- macOS: the same claim everywhere, and no claim that a Mac was tried
change = read('CHANGELOG.md')
check('CHANGELOG: does not say it was tested on macOS', 'Tested on Linux and macOS' not in change and not re.search(r'[Tt]ested on[^.\n]*macOS', change), change[-400:])
SAME = 'not yet verified on a Mac'
check('CHANGELOG and the README table: the same sentence, "%s"' % SAME, SAME in change and SAME in en, [SAME in change, SAME in en])
row_ko = next((ln for ln in ko.splitlines() if ln.startswith('| macOS')), '')
check('README.ko.md: the macOS row says it has not been checked on a Mac, and the unit tests run in CI', 'Mac에서는 아직 확인하지 않았' in row_ko and 'CI' in row_ko, row_ko)
check('README.md: the macOS row says the unit tests run in CI', 'CI' in next((ln for ln in en.splitlines() if ln.startswith('| macOS')), ''), '')

# ---- the start output in the docs is what the dictionary makes (the Codex line counts standalone sessions only)
words = {lang: json.loads(read('static/locales/%s.json' % lang))['messages']['cli.start.codex_counts'] for lang in ('en', 'ko')}
line = {lang: words[lang].format(days=7, sessions=0) for lang in words}
check('README.md start output: the Codex line is "%s"' % line['en'], line['en'] in en, '')
check('README.ko.md start output: the Codex line is "%s"' % line['ko'], line['ko'] in ko, '')
check('docs/configuration.md start output: the Codex line is "%s"' % line['en'], line['en'] in read('docs/configuration.md'), '')

# ---- the plan bar: the cache cells that the status line does not carry
conf = read('docs/configuration.md')
check('docs/configuration.md: model weeks and extra usage come from .claude.json with their own recorded time', re.search(r'extra usage[^.\n]*`\.claude\.json`', conf) and 'own' in conf[conf.index('The plan tier'):conf.index('The plan tier') + 500], conf[conf.index('The plan tier'):conf.index('The plan tier') + 400])

print('ALL PASS' if not fails else 'FAILED %d' % fails)
sys.exit(1 if fails else 0)
