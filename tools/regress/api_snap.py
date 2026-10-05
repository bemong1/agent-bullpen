# Compare Agent Bullpen API responses of two ports (old A, new B) for the given sessions.
# usage: python3 api_snap.py [--raw] 8811 8812 <sid> [<sid> ...]
# The only differences accepted are the fields that the language edition, the status and link work and the work rooms add (API_SNAP_ALLOW below); --raw accepts none (every added field is a DIFF, as before the English edition).
# Not accepted, on purpose, and so a DIFF for a session that has them: a new VALUE of an old field (agents[].status `interrupted`/`unknown`, orch.state `limit_wait`), a state that no longer reads as before
# (an agent that is now judged by its own error line), and the system lines (kind `sys`) that the feed and /api/talk now carry for the API-error lines of the record: they make the list longer and move every
# later event number (`idx`). Compare against a session whose record has none of these, or read the DIFF as the intended change.
# Also not accepted: the fields that went away with the account usage query (/api/plans claude: `usage_api`, `error`, `error_info`), so an older server shows a DIFF there.
import json, sys, time, urllib.request, urllib.parse, urllib.error
from collections import Counter
DROP_TOP = {'now', 'version', 'boot'}

# What the new fields are in the answers, as rules (name, where, keys). A key of the NEW answer is ignored only when
#  - a rule names it, and `where` says that the dict holding it is that kind of object (x = the dict, endpoint = e.g. 'api/plans', path = its place in the answer), and
#  - the OLD answer has no such key at the same place (a changed value of an old field is still a DIFF, and so is a new field in any other place).
is_event = lambda x: all(k in x for k in ('kind', 'from', 'to', 'title'))          # a feed event: state feed, /api/talk items, /api/event
API_SNAP_ALLOW = [
    ('event title_i18n / title_is_default / questions', lambda x, endpoint, path: is_event(x), ('title_i18n', 'title_is_default', 'questions')),
    ('notify event status', lambda x, endpoint, path: is_event(x) and x['kind'] == 'notify', ('status',)),     # agents[].status is an old field of another object
    ('event text_i18n', lambda x, endpoint, path: is_event(x), ('text_i18n',)),                                  # a body the server wrote itself (the spawn of a Codex sub-agent: its instruction is ciphertext), with the key the page words it from
    ('alert title_i18n / text_i18n', lambda x, endpoint, path: all(k in x for k in ('level', 'title', 'id', 'ts')), ('title_i18n', 'text_i18n')),
    ('alert title_params (the grouped limit notice)', lambda x, endpoint, path: all(k in x for k in ('level', 'title', 'id', 'ts')), ('title_params',)),
    ('tokens model_costs', lambda x, endpoint, path: path.rsplit('/', 1)[-1] == 'tokens' and 'models' in x, ('model_costs',)),
    ('JSON error error_code', lambda x, endpoint, path: path == '' and 'error' in x, ('error_code',)),
    # /api/state: what each agent says about how it stopped and where it hangs, how sure its link is, the orchestrator waiting on a limit, the count of diagnostics
    ('agent reason / resets_at / node / by / parent / runs / work_units', lambda x, endpoint, path: path.rsplit('/', 1)[-1] == 'agents[]' and 'tokens' in x and 'status' in x,
     ('reason', 'resets_at', 'node', 'by', 'parent', 'runs', 'work_units')),
    ('agent link rule_class / incomplete / assumed / tree', lambda x, endpoint, path: path.rsplit('/', 1)[-1] == 'link' and 'rule' in x and 'certain' in x, ('rule_class', 'incomplete', 'assumed', 'tree', 'parent_kind')),
    ('orch resets_at / auto (limit_wait)', lambda x, endpoint, path: path == '/orch' and 'state' in x and 'tokens' in x, ('resets_at', 'auto')),
    ('state diag', lambda x, endpoint, path: path == '' and 'orch' in x and 'agents' in x and 'session' in x, ('diag',)),
    # a topic that is a work room (people working together in a folder, no debate shape): which kind of room, and the instruction file it was recognised from
    ('room topic room / guide', lambda x, endpoint, path: path.rsplit('/', 1)[-1] == 'topics[]' and all(k in x for k in ('dir', 'key', 'rows', 'kind')), ('room', 'guide')),
    # a debate that stands for copies of its folder (a link into it, the same place in the repository's linked worktrees): how many were folded into it
    ('debate copies', lambda x, endpoint, path: path.rsplit('/', 1)[-1] == 'debates[]' and all(k in x for k in ('root', 'topics', 'finals')), ('copies',)),
    # a tool entry (activity, last_tool, current, pending) whose summary is the board's own wording says which dictionary key words it
    ('tool text_i18n (activity, last_tool, current, pending)', lambda x, endpoint, path: all(k in x for k in ('ts', 'name', 'text')), ('text_i18n',)),
]


def get(port, path):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%s/%s' % (port, path), timeout=120) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def norm(x):
    if isinstance(x, dict):
        x = {k: v for k, v in x.items() if k not in DROP_TOP}
        if isinstance(x.get('agents'), list):
            x['agents'] = [{k: v for k, v in a.items() if k != 'spark'} for a in x['agents']]
        if isinstance(x.get('lanes'), list):          # timeline: lane order follows link timing; the screen sorts by name
            x['lanes'] = sorted(x['lanes'], key=lambda l: l['id'])
    return json.dumps(x, sort_keys=True, ensure_ascii=False)


def strip_new(old, new, endpoint, allow=API_SNAP_ALLOW, dropped=None, path=''):
    """`new` without the fields API_SNAP_ALLOW accepts, walked together with `old` (which has them in no place): lists are paired by position,
    so a list of another length is left as it is (and shows as a DIFF). `dropped` (a Counter) counts the rules that were used."""
    if isinstance(new, dict) and isinstance(old, dict):
        out = {}
        for k, v in new.items():
            rule = next((name for name, where, keys in allow if k in keys and k not in old and where(new, endpoint, path)), None)
            if rule:
                if dropped is not None:
                    dropped[rule] += 1
                continue
            out[k] = strip_new(old[k], v, endpoint, allow, dropped, path + '/' + k) if k in old else v
        return out
    if isinstance(new, list) and isinstance(old, list) and len(new) == len(old):
        return [strip_new(o, n, endpoint, allow, dropped, path + '[]') for o, n in zip(old, new)]
    return new


def first_diff(a, b, p=''):
    if type(a) != type(b): return p or '/'
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b: return p + '/' + k
            d = first_diff(a[k], b[k], p + '/' + k)
            if d: return d
    elif isinstance(a, list):
        if len(a) != len(b): return p + '[len %d vs %d]' % (len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, '%s[%d]' % (p, i))
            if d: return d
    elif a != b: return p or '/'
    return None


def compare(A, B, path, tries=3, allow=API_SNAP_ALLOW, dropped=None, quiet=False, wait=6):
    endpoint = path.split('?')[0]
    for i in range(tries):   # sandwich A,B,A; live data may move, and each server refreshes the Codex index on its own 5 s clock
        sa1, a1 = get(A, path); sb, b = get(B, path); sa2, a2 = get(A, path)
        ok = False
        for sa, a in ((sa1, a1), (sa2, a2)):
            used = Counter()
            if sb == sa and norm(strip_new(a, b, endpoint, allow, used)) == norm(a):
                ok = True
                break
        if ok or i == tries - 1:
            break
        time.sleep(wait)
    if ok and dropped is not None:
        dropped.update(used)
    if not quiet:
        print('%-4s %s' % ('ok' if ok else 'DIFF', path[:110]) + ('' if ok else '  first diff at ' + str(first_diff(json.loads(norm(a2)), json.loads(norm(strip_new(a2, b, endpoint, allow)))))))
    return ok


def main(argv):
    raw = '--raw' in argv
    A, B, sids = [a for a in argv if a != '--raw'][0], [a for a in argv if a != '--raw'][1], [a for a in argv if a != '--raw'][2:]
    allow = [] if raw else API_SNAP_ALLOW
    dropped = Counter()
    bad = 0
    for p in ('api/sessions', 'api/plans'):
        bad += not compare(A, B, p, allow=allow, dropped=dropped)
    for sid in sids:
        q = 'session=' + urllib.parse.quote(sid)
        _, st = get(A, 'api/state?' + q)
        paths = ['api/state?' + q, 'api/talk?limit=80&' + q, 'api/timeline?since=%d&%s' % (int(st['now']) - 7200, q)]
        paths += ['api/agent?id=%s&%s' % (a['id'], q) for a in st['agents']]
        if st['feed']:
            paths += ['api/event?idx=%d&%s' % (st['feed'][0]['idx'], q), 'api/talk?limit=80&before=%d&%s' % (st['feed'][-1]['idx'], q)]
        cell = next((c['path'] for d in st['debates'] for t in d['topics'] for r in t['rows'] for c in r['cells'] if c['state'] == 'done'), None)
        if cell:
            paths.append('api/file?path=%s&%s' % (urllib.parse.quote(cell), q))
        paths.append('api/file?path=%s&%s' % (urllib.parse.quote('/etc/hostname'), q))   # must stay 403
        bad += sum(not compare(A, B, p, allow=allow, dropped=dropped) for p in paths)
    if not raw:
        print('accepted new fields: ' + (', '.join('%s x%d' % (k, n) for k, n in sorted(dropped.items())) or 'none'))
    print('FAILED %d' % bad if bad else 'ALL OK')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
