#!/usr/bin/env python3
"""Merge one area's keys into static/locales/{en,ko}.json without touching the other areas.

Several people can add keys at the same time: each run takes a lock, reads both files,
replaces only the keys of its area (the lines after the area's marker key "<area>._"),
and writes both files back atomically in the usual layout.

usage: python3 tools/i18n_merge.py <area> <fragment.json>

fragment.json:
  {"en": {"board.title": "Agents", ...},
   "ko": {"board.title": "에이전트", ...},
   "limits": {"board.title.short": 12}}          # optional, English character budgets

Every key must start with "<area>." and en/ko must have the same keys. Keys of the area that
are not in the fragment are removed, so always pass the area's full set.
"""
import fcntl
import json
import os
import sys
from collections import OrderedDict

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'static', 'locales')
LOCK = '/tmp/agent-bullpen-locales.lock'
LANGS = ('en', 'ko')


def dump_value(v):
    if isinstance(v, dict):                                    # plural forms, on one line like the hand-kept files
        return '{ ' + ', '.join('%s: %s' % (json.dumps(a), json.dumps(b, ensure_ascii=False)) for a, b in v.items()) + ' }'
    return json.dumps(v, ensure_ascii=False)


def render(d):
    """The hand-kept layout: one key per line, a blank line before each area marker."""
    out = ['{', '  "meta": ' + json.dumps(d['meta'], ensure_ascii=False, indent=2).replace('\n', '\n  ') + ',', '  "messages": {']
    keys = list(d['messages'])
    for i, k in enumerate(keys):
        if k.endswith('._') and i:
            out.append('')
        out.append('    %s: %s%s' % (json.dumps(k), dump_value(d['messages'][k]), ',' if i < len(keys) - 1 else ''))
    out.append('  },')
    out.append('  "formats": {')
    fk = list(d['formats'])
    for i, k in enumerate(fk):
        body = ', '.join('%s: %s' % (json.dumps(a), dump_value(b)) for a, b in d['formats'][k].items())
        out.append('    %s: { %s }%s' % (json.dumps(k), body, ',' if i < len(fk) - 1 else ''))
    out.append('  },')
    out.append('  "limits": {')
    lk = list(d['limits'])
    for i, k in enumerate(lk):
        out.append('    %s: %s%s' % (json.dumps(k), dump_value(d['limits'][k]), ',' if i < len(lk) - 1 else ''))
    out.append('  }')
    out.append('}')
    extra = [k for k in d if k not in ('meta', 'messages', 'formats', 'limits')]
    if extra:
        raise SystemExit('unexpected top-level keys: %s' % extra)
    return '\n'.join(out) + '\n'


def merge(area, frag):
    marker = area + '._'
    for lang in LANGS:
        bad = [k for k in frag.get(lang, {}) if not k.startswith(area + '.') or k == marker]
        if bad:
            raise SystemExit('%s keys outside area %r: %s' % (lang, area, bad[:5]))
    if set(frag.get('en', {})) != set(frag.get('ko', {})):
        raise SystemExit('en and ko keys differ: %s' % sorted(set(frag.get('en', {})) ^ set(frag.get('ko', {})))[:10])
    with open(LOCK, 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        for lang in LANGS:
            path = os.path.join(ROOT, lang + '.json')
            with open(path, encoding='utf-8') as f:
                d = json.load(f, object_pairs_hook=OrderedDict)
            if marker not in d['messages']:
                raise SystemExit('%s: no marker %s' % (path, marker))
            msgs = OrderedDict()
            for k, v in d['messages'].items():
                if k.startswith(area + '.') and k != marker:
                    continue                                   # replaced below
                msgs[k] = v
                if k == marker:
                    for nk, nv in frag[lang].items():
                        msgs[nk] = nv
            d['messages'] = msgs
            lim = OrderedDict((k, v) for k, v in d['limits'].items() if not k.startswith(area + '.'))
            for k, v in (frag.get('limits') or {}).items():
                if not k.startswith(area + '.'):
                    raise SystemExit('limit key outside area: %s' % k)
                lim[k] = v
            d['limits'] = lim
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(render(d))
            os.replace(tmp, path)
    print('merged %d keys into %s (en, ko)' % (len(frag['en']), area))


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    with open(sys.argv[2], encoding='utf-8') as f:
        merge(sys.argv[1], json.load(f, object_pairs_hook=OrderedDict))
