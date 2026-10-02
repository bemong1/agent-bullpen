#!/usr/bin/env python3
"""Checks of the dictionaries (static/locales/<code>.json) and of the pages' hard-coded Korean. Standard library only (3.9+).

    python3 tools/regress/i18n_check.py                   # report; the exit code is 0 (warnings only, until the checks are made strict in CI)
    python3 tools/regress/i18n_check.py --strict          # exit 1 when anything is reported
    python3 tools/regress/i18n_check.py --list            # every finding, not only the counts
    python3 tools/regress/i18n_check.py --static DIR      # another static folder (default: ../../static)

Checks (the name in brackets is the finding's group):
  syntax         the file parses, no duplicate keys, the four parts meta / messages / formats / limits, meta.code = file name, name, locale
  zones          the section anchors `<zone>._` exist once each in the fixed order, and every key sits in its own section
  keys           every language has the same keys in the same order as English
  params         {name} placeholders: no name that English does not have; the `other` form has exactly the names of English's `other` form
  plural         a plural value {one, other, ...} has `other` and only valid categories
  limits         `limits` names real keys; the English text (placeholders as 2 characters) fits; other languages only note it (Korean glyphs are wider)
  hangul         English values have no Hangul
  terms          English values avoid the forbidden spellings (sub-agent, Notifications, drawer, Command room, a bare "board")
  refs           t('key') / I18N.t('key') / data-i18n="key" in static/* name keys that exist (t('prefix.' + x) needs some key with that prefix)
  static-hangul  Hangul in static/*.js|html|css outside comments. A line with the marker `i18n-ok` is allowed (record-parsing patterns, the old-response layer).
                 These are to be moved into the dictionaries; the count shows what is left.
"""
import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ZONES = ['common', 'status', 'kind', 'time', 'unit', 'board', 'office', 'demo', 'diag', 'page', 'cli', 'alert', 'event']     # the order of the sections
PARTS = ['meta', 'messages', 'formats', 'limits']
CATEGORIES = {'zero', 'one', 'two', 'few', 'many', 'other'}
HANGUL = re.compile(r'[ㄱ-ㆎ가-힣]')
PARAM = re.compile(r'\{([A-Za-z_]\w*)(?::([A-Za-z]+))?\}')
FORBIDDEN = [('sub-agent', re.compile(r'sub-?agent', re.I)), ('Notifications', re.compile(r'notifications?', re.I)), ('drawer', re.compile(r'\bdrawer\b', re.I)),
             ('Command room', re.compile(r'command room', re.I)), ('board (say dashboard or whiteboard)', re.compile(r'\bboard\b', re.I))]
CODE_RE = re.compile(r'[a-z]{2,3}(?:-[A-Za-z0-9]+)*')
MARK = 'i18n-ok'
# Keys whose placeholder names legitimately differ by language: the listed names may be used or left out by either side (the code passes all of them).
# Every other name must match English exactly.
OPTIONAL_PARAMS = {'time.monthDay': {'m', 'mon'}, 'time.dayHeading': {'m', 'mon'}}


def forms(v):
    return {'other': v} if isinstance(v, str) else dict(v)


def names(text):
    return {m.group(1) for m in PARAM.finditer(text)}


def fit_len(text):
    """Length with every placeholder counted as 2 characters (a short value such as a count or a name)."""
    return len(PARAM.sub('xx', text))


# ---------- dictionaries ----------
def load(path, out):
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                out.append(('syntax', os.path.basename(path), 'duplicate key "%s"' % k))
            d[k] = v
        return d
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f, object_pairs_hook=pairs)
    except (OSError, ValueError) as e:
        out.append(('syntax', os.path.basename(path), 'cannot read: %s' % e))
        return None


def check_dictionaries(static, out):
    d = os.path.join(static, 'locales')
    packs = {}
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        m = re.fullmatch(r'(.+)\.json', name)
        if not m or not CODE_RE.fullmatch(m.group(1)):
            continue
        p = load(os.path.join(d, name), out)
        if p is None:
            continue
        code, where = m.group(1), name
        if list(p) != PARTS:
            out.append(('syntax', where, 'parts are %s, want %s' % (list(p), PARTS)))
        meta = p.get('meta') or {}
        if meta.get('code') != code:
            out.append(('syntax', where, 'meta.code is %r, want %r' % (meta.get('code'), code)))
        for k in ('name', 'locale'):
            if not isinstance(meta.get(k), str) or not meta.get(k):
                out.append(('syntax', where, 'meta.%s missing' % k))
        if isinstance(p.get('messages'), dict):
            packs[code] = p
        else:
            out.append(('syntax', where, 'messages is not an object'))
    for code, p in packs.items():
        check_pack(code, p, out)
    en = packs.get('en')
    if not en:
        out.append(('syntax', 'locales', 'no en.json (English is the fallback of every language)'))
        return packs
    for code, p in packs.items():
        if code != 'en':
            compare(code, p, en, out)
    return packs


def check_pack(code, p, out):
    where = code + '.json'
    msgs = p['messages']
    zone, seen = None, []
    for k, v in msgs.items():
        head = k.split('.')[0]
        if k.endswith('._') and k.count('.') == 1:
            zone = head
            seen.append(head)
            if v != '':
                out.append(('zones', where, 'anchor "%s" must be an empty string' % k))
            continue
        if zone is None or head != zone:
            out.append(('zones', where, 'key "%s" is not in its own section (%s)' % (k, 'the section of "%s._"' % head if head in ZONES else 'unknown zone "%s"' % head)))
        if isinstance(v, dict):
            bad = [c for c in v if c not in CATEGORIES]
            if 'other' not in v:
                out.append(('plural', where, '"%s" has no "other" form' % k))
            if bad:
                out.append(('plural', where, '"%s" has invalid plural categories %s' % (k, bad)))
            if not all(isinstance(x, str) for x in v.values()):
                out.append(('plural', where, '"%s" has a non-string form' % k))
                continue
        elif not isinstance(v, str):
            out.append(('syntax', where, '"%s" is neither a string nor a plural object' % k))
            continue
        if code == 'en':
            for form, text in forms(v).items():
                if HANGUL.search(text):
                    out.append(('hangul', where, '"%s" (%s) has Hangul: %s' % (k, form, text)))
                for label, rx in FORBIDDEN:
                    if rx.search(text):
                        out.append(('terms', where, '"%s" (%s) uses "%s": %s' % (k, form, label, text)))
    if seen != [z for z in ZONES if z in seen] or len(set(seen)) != len(seen):
        out.append(('zones', where, 'section anchors are %s, want the order of %s' % (seen, ZONES)))
    for z in ZONES:
        if z not in seen:
            out.append(('zones', where, 'missing section anchor "%s._"' % z))
    for k, mx in (p.get('limits') or {}).items():
        if k not in msgs:
            out.append(('limits', where, 'limit for unknown key "%s"' % k))
            continue
        if not isinstance(mx, int):
            out.append(('limits', where, 'limit of "%s" is not a number' % k))
            continue
        for form, text in forms(msgs[k]).items():
            if fit_len(text) > mx:
                out.append(('limits' if code == 'en' else 'limits-note', where, '"%s" (%s) is %d characters, the limit is %d: %s' % (k, form, fit_len(text), mx, text)))


def compare(code, p, en, out):
    where = code + '.json'
    ks, ke = list(p['messages']), list(en['messages'])
    for k in ke:
        if k not in p['messages']:
            out.append(('keys', where, 'missing key "%s"' % k))
    for k in ks:
        if k not in en['messages']:
            out.append(('keys', where, 'key "%s" is not in en.json' % k))
    common = [k for k in ks if k in en['messages']]
    if common != [k for k in ke if k in p['messages']]:
        out.append(('keys', where, 'keys are in another order than en.json'))
    if (p.get('limits') or {}).keys() != (en.get('limits') or {}).keys():
        out.append(('limits-note', where, 'limits name other keys than en.json'))
    for k in common:
        fe, fk = forms(en['messages'][k]), forms(p['messages'][k])
        if not all(isinstance(x, str) for x in list(fe.values()) + list(fk.values())):
            continue
        opt = OPTIONAL_PARAMS.get(k, set())
        want = names(fe.get('other', '')) - opt
        union = set().union(*[names(t) for t in fe.values()]) | opt if fe else set()
        got = names(fk.get('other', '')) - opt
        if got != want:
            out.append(('params', where, '"%s": the "other" form has {%s}, en.json has {%s}' % (k, ', '.join(sorted(got)), ', '.join(sorted(want)))))
        for form, text in fk.items():
            extra = names(text) - union
            if extra:
                out.append(('params', where, '"%s" (%s) names {%s}, which en.json does not use' % (k, form, ', '.join(sorted(extra)))))


# ---------- the pages ----------
def lex_js(text):
    """Per character: True where it is inside a comment. Strings, template literals (with nested ${ }) and regular expression literals are skipped over."""
    n, i, cm = len(text), 0, [False] * len(text)
    stack = []           # 'tpl' entries; a '${' pushes ('code', depth)
    depth_stack = []     # brace depth inside each ${ }
    prev = ''            # last significant character of code (for regex vs divide)
    prev_word = ''
    REGEX_AFTER = set('(,=:[!&|?{};+-*%<>~^')
    KEYWORDS = {'return', 'typeof', 'case', 'in', 'of', 'delete', 'void', 'throw', 'new', 'else', 'do', 'instanceof', 'yield', 'await'}
    in_tpl = False
    while i < n:
        c = text[i]
        if in_tpl:
            if c == '\\':
                i += 2
            elif c == '`':
                in_tpl = False
                prev = '`'
                i += 1
            elif c == '$' and text[i + 1:i + 2] == '{':
                depth_stack.append(0)
                in_tpl = False
                prev = '{'
                i += 2
            else:
                i += 1
            continue
        if c == '/' and text[i + 1:i + 2] == '/':
            j = text.find('\n', i)
            j = n if j < 0 else j
            for k in range(i, j):
                cm[k] = True
            i = j
        elif c == '/' and text[i + 1:i + 2] == '*':
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                cm[k] = True
            i = j
        elif c in '\'"':
            j = i + 1
            while j < n and text[j] != c and text[j] != '\n':
                j += 2 if text[j] == '\\' else 1
            i, prev = j + 1, c
        elif c == '`':
            in_tpl = True
            i += 1
        elif c == '/' and (prev == '' or prev in REGEX_AFTER or prev_word in KEYWORDS):
            j, cls = i + 1, False
            while j < n and text[j] != '\n':
                if text[j] == '\\':
                    j += 2
                    continue
                if text[j] == '[':
                    cls = True
                elif text[j] == ']':
                    cls = False
                elif text[j] == '/' and not cls:
                    break
                j += 1
            j += 1
            while j < n and text[j].isalpha():
                j += 1
            i, prev = j, ')'
        else:
            if c == '{' and depth_stack:
                depth_stack[-1] += 1
            elif c == '}' and depth_stack:
                if depth_stack[-1] == 0:
                    depth_stack.pop()
                    in_tpl = True
                    i += 1
                    continue
                depth_stack[-1] -= 1
            if c.isalnum() or c in '_$':
                j = i
                while j < n and (text[j].isalnum() or text[j] in '_$'):
                    j += 1
                prev_word, prev = text[i:j], 'a'
                i = j
                continue
            if not c.isspace():
                prev, prev_word = c, ''
            i += 1
    return cm


def lex_css(text):
    n, i, cm = len(text), 0, [False] * len(text)
    while i < n:
        c = text[i]
        if c == '/' and text[i + 1:i + 2] == '*':
            j = text.find('*/', i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                cm[k] = True
            i = j
        elif c in '\'"':
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == '\\' else 1
            i = j + 1
        else:
            i += 1
    return cm


def lex_html(text):
    """HTML comments are comments; <script> bodies go through lex_js, <style> bodies through lex_css."""
    cm = [False] * len(text)
    for m in re.finditer(r'<!--.*?-->', text, re.S):
        for k in range(m.start(), m.end()):
            cm[k] = True
    for m in re.finditer(r'<(script|style)\b[^>]*>(.*?)</\1\s*>', text, re.S | re.I):
        body, off = m.group(2), m.start(2)
        if any(cm[off:off + 1]):
            continue
        sub = lex_js(body) if m.group(1).lower() == 'script' else lex_css(body)
        for k, v in enumerate(sub):
            if v:
                cm[off + k] = True
    return cm


def hangul_lines(path):
    """[(line number, line)] of the lines that have Hangul outside comments and no `i18n-ok` marker."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    ext = os.path.splitext(path)[1]
    cm = (lex_js if ext == '.js' else lex_css if ext == '.css' else lex_html)(text)
    out, pos = [], 0
    for no, line in enumerate(text.split('\n'), 1):
        code = ''.join(ch for k, ch in enumerate(line, pos) if not cm[k])
        if HANGUL.search(code) and MARK not in line:
            out.append((no, line.strip()))
        pos += len(line) + 1
    return out


def check_static(static, packs, out):
    keys = set(packs['en']['messages']) if 'en' in packs else set()
    counts = collections.OrderedDict()
    for name in sorted(os.listdir(static)):
        path = os.path.join(static, name)
        if not os.path.isfile(path) or os.path.splitext(name)[1] not in ('.js', '.html', '.css'):
            continue
        rows = hangul_lines(path)
        counts[name] = len(rows)
        for no, line in rows:
            out.append(('static-hangul', '%s:%d' % (name, no), line[:110]))
        with open(path, encoding='utf-8') as f:
            text = f.read()
        if name == 'i18n.js':
            continue                                    # its doc comment shows examples
        cm = lex_js(text) if name.endswith('.js') else lex_css(text) if name.endswith('.css') else lex_html(text)
        for m in re.finditer(r"""(?:(?<![\w.$])t\(|\bI18N\.t\(|data-i18n(?:-[a-z-]+)?=)\s*(['"])([A-Za-z][\w.]*)\1\s*([,)+"']?)""", text):
            if cm[m.start()]:
                continue
            key, tail = m.group(2), m.group(3)
            line = text.count('\n', 0, m.start()) + 1
            if tail == '+':
                if not any(k.startswith(key) for k in keys):
                    out.append(('refs', '%s:%d' % (name, line), 'no key starts with "%s"' % key))
            elif key not in keys:
                out.append(('refs', '%s:%d' % (name, line), 'unknown key "%s"' % key))
    return counts


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--static', default=os.path.join(HERE, '..', '..', 'static'))
    ap.add_argument('--strict', action='store_true', help='exit 1 when anything is reported')
    ap.add_argument('--list', action='store_true', help='print every finding')
    args = ap.parse_args()
    static = os.path.abspath(args.static)
    out = []
    packs = check_dictionaries(static, out)
    counts = check_static(static, packs, out)
    groups = collections.OrderedDict((g, []) for g in ('syntax', 'zones', 'keys', 'params', 'plural', 'limits', 'limits-note', 'hangul', 'terms', 'refs', 'static-hangul'))
    for g, where, msg in out:
        groups.setdefault(g, []).append((where, msg))
    n_keys = len(packs['en']['messages']) - sum(1 for k in packs['en']['messages'] if k.endswith('._')) if 'en' in packs else 0
    print('i18n_check: %s — languages %s, %d messages' % (os.path.relpath(static), ', '.join(sorted(packs)) or '-', n_keys))
    for g, rows in groups.items():
        extra = ' (%s)' % ', '.join('%s %d' % (f, c) for f, c in counts.items() if c) if g == 'static-hangul' and rows else ''
        print('  %-14s %d%s' % (g, len(rows), extra))
        if args.list or (rows and g != 'static-hangul' and len(rows) <= 20):
            for where, msg in rows:
                print('      %s: %s' % (where, msg))
    errors = sum(len(r) for g, r in groups.items() if g != 'limits-note')
    print('%d finding(s)%s' % (errors, '' if args.strict else ' — warnings only (use --strict to fail)'))
    return 1 if args.strict and errors else 0


if __name__ == '__main__':
    sys.exit(main())
