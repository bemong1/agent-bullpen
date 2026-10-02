#!/usr/bin/env python3
"""Measures what the board costs: the first scan, the scans after it, `state()` in the process, and `/api/state` over HTTP split into its parts, and checks them against budgets.

    python3 tools/bench.py run --scale 1              # a synthetic big HOME: 100 sessions, 25 000 Bash calls, 200 `claude -p` children (built in a temp folder)
    python3 tools/bench.py run --real                 # the real HOME, read-only: the same measurements on your own records (numbers only are printed)
    python3 tools/bench.py run --scale 1 --save-baseline FILE      # keep the numbers
    python3 tools/bench.py run --scale 1 --check FILE              # fail (exit 1) when a gating number is worse than the baseline by 25 %
    python3 tools/bench.py run --scale 1 --repo /path/to/copy      # measure another copy of the tree (a before / after comparison)
    python3 tools/bench.py build DIR --scale 1        # only build the synthetic HOME

What it measures, each in a process of its own (the board keeps its folders in module globals, so every run is a fresh interpreter started with HOME set):
  scan     LinkIndex first scan, CodexIndex refresh, scan_sessions, the first open of the busy session (the "ready" time), the scans after it (idle and while the
           records grow), state() in the process. Wall and CPU time: CPU time is what the gate looks at, wall time moves with the load of the machine.
  http     the server of server.py on a free port of 8857-8860, in a thread of the worker, with the registry's poll loop running as in the real server (and a writer
           that grows the busy session's records, as a live orchestrator does). A client process asks `/api/state` and `/api/state?v=` and measures connect, first
           byte, body, decompression. Seams in the worker split the server side into lock wait, state computation, JSON, gzip, and the socket writes.

The seams wrap names (Handler.do_GET, views.state, json.dumps, gzip.compress, the handler's wfile) and give up on one that is not there, so the tool keeps working
when the server is restructured; a part it cannot see is reported as unavailable, never as 0.

It reads a real HOME only with --real (read-only: no link cache, no usage thread, no credential file). It starts only processes it waits for, on 8857-8860,
and never touches 8790.
"""

import argparse
import contextlib
import gzip
import http.client
import json
import math
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PORTS = (8857, 8858, 8859, 8860)
MARK = '.bench-home'
MARK_TEXT = 'synthetic HOME made by tools/bench.py; safe to delete\n'
BASE_SESSIONS, BASE_BASH, BASE_CHILDREN = 100, 25000, 200       # --scale 1
BULK_KB = 3000                                                   # filler lines per main session (about 3 MB): at --scale 1 the records are a few hundred MB, the order the byte scan feels
BUSY_AGENTS = 120                                                # sub-agents of the busy orchestrator at --scale 1: its /api/state is the one the page polls
TOLERANCE = 0.25                                                 # --check: a gating number may be this much worse than the baseline
FLOOR = {'s': 0.002, 'ms': 0.5, 'mb': 8.0}                                  # ... plus this much, so that a number near zero does not trip on noise
SCHEMA = 1


# ---------------------------------------------------------------------------------------------------------------------
# A synthetic big HOME
# ---------------------------------------------------------------------------------------------------------------------
COMMANDS = ('git status --short', 'git diff --stat', 'ls -la', 'python3 -m unittest discover -s tests', 'grep -rn "TODO" src | head -20', 'make -j8 test',
            'cat build/log.txt | tail -40', 'sed -n 1,80p src/app.py', 'find . -name "*.py" -newer notes.md | head', 'pytest -x -q tests/unit',
            'git log --oneline | head -10', 'npm run lint', 'cargo check', 'docker ps --format "{{.Names}}"', 'wc -l src/*.py', 'diff -u a.txt b.txt | head -50')
WORDS = ('amber basin cedar delta ember fjord grove harbor island juniper kelp lagoon meadow nectar orchard prairie quartz ridge summit tundra umber valley willow '
         'xenon yarrow zephyr alloy beacon cobalt dune estuary flint garnet hollow iris jasper knoll lantern marble nickel opal pebble').split()


def _prose(rng, chars):
    out, n = [], 0
    while n < chars:
        w = rng.choice(WORDS)
        out.append(w)
        n += len(w) + 1
    return ' '.join(out)


def _scenarios():
    """tools/scenarios.build (the records' shapes are the generator's, so a record the board reads is one it reads in the tests)."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from tools.scenarios import build
    return build


def plan(scale=1.0, sessions=None, bash=None, children=None, busy_agents=None):
    """(main sessions, Bash calls, claude -p children, sub-agents of the busy session) for a scale: 1 = 100 / 25 000 / 200."""
    return (sessions or max(2, int(round(BASE_SESSIONS * scale))), bash or max(40, int(round(BASE_BASH * scale))),
            children if children is not None else max(2, int(round(BASE_CHILDREN * scale))),
            busy_agents if busy_agents is not None else max(2, int(round(BUSY_AGENTS * scale))))


def build_big_home(dest, scale=1.0, sessions=None, bash=None, children=None, busy_agents=None, codex=None, bulk_kb=BULK_KB, seed=7, now=None):
    """Writes the synthetic HOME `dest`/home and returns its manifest (counts, ids, bytes). Every name, path, id and text is made up. The times are relative to `now`
    (default: the moment of the call): the busy session was written a few seconds ago, the rest over the last ten days."""
    B = _scenarios()
    sessions, bash, children, busy_agents = plan(scale, sessions, bash, children, busy_agents)
    codex = codex if codex is not None else max(0, int(round(40 * scale)))
    now = now or time.time()
    rng = random.Random(seed)
    home = os.path.join(dest, 'home')
    claude = os.path.join(home, '.claude')
    projects = os.path.join(claude, 'projects')
    os.makedirs(projects, exist_ok=True)
    os.makedirs(os.path.join(claude, 'sessions'), exist_ok=True)
    with open(os.path.join(dest, MARK), 'w') as f:
        f.write(MARK_TEXT)
    projs = ['proj%02d' % i for i in range(max(2, min(12, sessions // 8 or 2)))]
    work = os.path.join(home, 'work')

    def cwd_of(i):
        d = os.path.join(work, projs[i % len(projs)])
        os.makedirs(d, exist_ok=True)
        return d

    def project_dir(cwd):
        d = os.path.join(projects, '-' + cwd.strip('/').replace('/', '-'))
        os.makedirs(d, exist_ok=True)
        return d

    man = {'sessions': 0, 'mains': 0, 'subagents': 0, 'children': 0, 'bash_calls': 0, 'codex_threads': 0, 'codex_exec_calls': 0, 'bytes': 0}
    cid = 'bench-%d' % seed

    def tool_id(kind, n):
        return 'toolu_%s%06d' % (kind, n)

    counter = [0]

    def bash_call(tr, t, cmd, end=None, bg=None, notif=None, desc='run'):
        counter[0] += 1
        tr.bash(t, cmd, tool_id('b', counter[0]), end=end if end is not None else t + rng.uniform(0.3, 8), desc=desc, bg=bg, notif=notif)
        man['bash_calls'] += 1
        return tool_id('b', counter[0])

    def plain_work(tr, t, n, cwd, step=40.0):
        """n Bash calls of ordinary work, each followed now and then by a Read, an Edit or a Write and a line of talk, one call every `step` seconds; returns the time after the last."""
        for i in range(n):
            counter[0] += 1
            text = rng.choice(COMMANDS) + (' ' + _prose(rng, 8) if rng.random() < 0.3 else '')
            tid = tool_id('b', counter[0])
            tr.bash(t, text, tid, end=t + rng.uniform(0.3, 6), desc='work')
            if rng.random() < 0.06:
                tr.rows[-1][2]['message']['content'][0]['content'] = _prose(rng, 1500)        # a long tool result
            man['bash_calls'] += 1
            r = rng.random()
            if r < 0.12:
                tr.tool(t + 7, 'Read', {'file_path': os.path.join(cwd, 'src', 'mod%d.py' % (i % 9))}, tool_id('r', counter[0]))
                tr.result(t + 7.4, tool_id('r', counter[0]), _prose(rng, 300))
            elif r < 0.19:
                tr.tool(t + 7, 'Edit', {'file_path': os.path.join(cwd, 'src', 'mod%d.py' % (i % 9)), 'old_string': 'x', 'new_string': _prose(rng, 80)}, tool_id('e', counter[0]))
                tr.result(t + 7.4, tool_id('e', counter[0]), 'edited')
            elif r < 0.24:
                tr.tool(t + 7, 'Write', {'file_path': os.path.join(cwd, 'notes', 'n%d.md' % (i % 5)), 'content': _prose(rng, 400)}, tool_id('w', counter[0]))
                tr.result(t + 7.4, tool_id('w', counter[0]), 'written')
            if i % 6 == 5:
                tr.say(t + 8, _prose(rng, 120), 'end_turn')
            t += step * rng.uniform(0.5, 1.5)
        return t

    def child_records(path_cwd, sid, text, t0, finished=True, bulk=0):
        tr = B.Transcript(os.path.join(project_dir(path_cwd), sid + '.jsonl'), sid, path_cwd, 'sdk-cli', cid=cid)
        tr.prompt(t0, text, source='sdk', turn=1)
        t = plain_work(tr, t0 + 3, rng.randint(2, 6), path_cwd, step=10)
        if finished:
            tr.say(t, 'Finished the review.', 'end_turn')
            tr.cost_state(t + 1, int((t - t0) * 1000))
        tr.save()
        man['children'] += 1
        man['bytes'] += os.path.getsize(tr.path)

    def sub_agent(main_tr, main_sid, cwd, t_spawn, n_calls, live=False):
        aid = 'a' + B.digest(cid, 'sub', man['subagents'], n=16)
        tu = tool_id('s', 500000 + man['subagents'])
        main_tr.tool(t_spawn, 'Agent', {'description': 'Analysis helper %d' % man['subagents'], 'prompt': _prose(rng, 160)}, tu)
        d = os.path.join(os.path.dirname(main_tr.path), main_sid, 'subagents')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'agent-%s.meta.json' % aid), 'w') as f:
            f.write(B.dump({'description': 'Analysis helper %d' % man['subagents'], 'agentType': 'general-purpose', 'toolUseId': tu}))
        S = B.Transcript(os.path.join(d, 'agent-%s.jsonl' % aid), main_sid, cwd, 'cli', side=True, agent=aid, cid=cid)
        S.prompt(t_spawn + 1, _prose(rng, 160), source='human')
        t = plain_work(S, t_spawn + 3, n_calls, cwd, step=25)
        if not live:
            S.say(t, 'Report written.', 'end_turn')
        S.save()
        man['subagents'] += 1
        man['bytes'] += os.path.getsize(S.path)
        return aid, tu, S

    n_mains = sessions
    # how many calls each kind of record gets: children and sub-agents have a few, the mains the rest
    n_subs_main = {i: (2 if i % 3 == 0 else 0) for i in range(1, n_mains)}
    sub_calls, child_calls = 24, 4
    est_subs = busy_agents + sum(n_subs_main.values())
    reserved = est_subs * sub_calls + children * child_calls + children * 2 + codex
    per_main = max(5, (bash - reserved) // max(1, n_mains))
    # which main launches which child: spread over the mains (the busy one takes a third of them)
    busy_children = children // 3
    launch_at = {}
    for k in range(children):
        launch_at.setdefault(0 if k < busy_children else 1 + (k - busy_children) % max(1, n_mains - 1), []).append(k)
    t_end_busy = now - 5

    first_ids = {}
    for i in range(n_mains):
        sid = B.sid_of(cid, 'main-%d' % i)
        cwd = cwd_of(i)
        live = i == 0
        span = 3 * 3600
        t_start = (t_end_busy - 6 * 3600) if live else now - 86400 * rng.uniform(0.2, 10) - span
        tr = B.Transcript(os.path.join(project_dir(cwd), sid + '.jsonl'), sid, cwd, 'cli', cid=cid)
        tr.prompt(t_start, 'Start the work and keep me posted.', source='human')
        calls = per_main + (len(launch_at.get(i, [])) * 0)
        t = t_start + 10
        step = (span if not live else 6 * 3600) / float(max(1, calls + 40))
        t = plain_work(tr, t, calls // 2, cwd, step=step)
        # children launched by this main
        for k in launch_at.get(i, []):
            csid = B.sid_of(cid, 'child-%d' % k)
            text = _prose(rng, rng.choice((90, 200, 400, 900)))
            kind = k % 5
            tc = t + rng.uniform(5, 60)
            if kind == 0:               # a background launch with the instruction in the command
                tid = bash_call(tr, tc, 'cd %s && claude -p --model claude-sonnet-5-5 "%s"' % (cwd, text), end=tc + 0.4, bg='b%08d' % k, notif=tc + 14, desc='launch child')
            elif kind == 1:             # a blocking launch with the output in a json file that names the child
                outf = os.path.join(work, 'out%d.json' % k)
                os.makedirs(work, exist_ok=True)
                with open(outf, 'w') as f:
                    f.write(B.dump({'type': 'result', 'session_id': csid, 'result': 'ok'}))
                tid = bash_call(tr, tc, 'cd %s && claude -p --model claude-sonnet-5-5 --output-format json "%s" > %s' % (cwd, text, outf), end=tc + 12, desc='launch child')
            elif kind == 2:             # the instruction written by an earlier call, read back with $(cat ...)
                pf = os.path.join(work, 'p%d.txt' % k)
                os.makedirs(work, exist_ok=True)
                with open(pf, 'w') as f:
                    f.write(text)
                bash_call(tr, tc - 30, "cat > %s <<'EOF'\n%s\nEOF" % (pf, text), desc='prepare')
                tid = bash_call(tr, tc, 'cd %s && claude -p --model claude-sonnet-5-5 "$(cat %s)"' % (cwd, pf), end=tc + 12, desc='launch child')
            elif kind == 3:             # a script
                sf = os.path.join(work, 'run%d.sh' % k)
                body = 'cd %s\nclaude -p --model claude-sonnet-5-5 "%s"\n' % (cwd, text)
                os.makedirs(work, exist_ok=True)
                with open(sf, 'w') as f:
                    f.write(body)
                bash_call(tr, tc - 30, "cat > %s <<'EOF'\n%sEOF" % (sf, body), desc='prepare')
                tid = bash_call(tr, tc, 'bash %s' % sf, end=tc + 12, desc='run script')
            else:                       # a loop of two
                other = _prose(rng, 120)
                tid = bash_call(tr, tc, 'cd %s && for x in "%s" "%s"; do claude -p --model claude-sonnet-5-5 "$x"; done' % (cwd, other, text), end=tc + 40, desc='launch two')
            child_records(cwd, csid, text, tc + 2.4, finished=True)
            t = tc + 20
        # `codex exec` calls (and the rollouts they started), a few per main
        if codex and i % max(1, n_mains // max(1, codex)) == 0 and man['codex_exec_calls'] < codex:
            ctext = _prose(rng, 160)
            tc = t + 20
            bash_call(tr, tc, 'cd %s && codex exec -m gpt-6.1-sol "%s"' % (cwd, ctext), end=tc + 30, desc='launch codex')
            man['codex_exec_calls'] += 1
            _codex_rollout(home, B, cid, man['codex_threads'], tc + 2.4, cwd, ctext)
            man['codex_threads'] += 1
            t = tc + 40
        t = plain_work(tr, t, calls - calls // 2, cwd, step=step)
        # sub-agents
        subs = busy_agents if live else n_subs_main.get(i, 0)
        for j in range(subs):
            sub_agent(tr, sid, cwd, t_start + 100 + 20 * j, sub_calls, live=live and j % 3 == 0)
        tr.say(t + 5, _prose(rng, 80), 'end_turn')
        if bulk_kb:
            pad = B.dump({'type': 'attachment', 'timestamp': B.iso(t + 6), 'sessionId': sid, 'attachment': {'type': 'hook_success', 'content': 'x' * 1000}})
            for _ in range(bulk_kb):
                tr.raw(t + 6, json.loads(pad))
        tr.save()
        if live:
            os.utime(tr.path, (t_end_busy, t_end_busy))
            first_ids['busy'] = sid
            first_ids['busy_path'] = tr.path
        else:
            first_ids.setdefault('plain', sid)
        man['mains'] += 1
        man['sessions'] += 1
        man['bytes'] += os.path.getsize(tr.path)
    man['sessions'] += man['children']
    man.update(busy_session=first_ids['busy'], busy_path=first_ids['busy_path'], home=home, root=dest, now=now, scale=scale, busy_agents=busy_agents,
               children_requested=children)
    return man


def _codex_rollout(home, B, cid, k, t, cwd, text):
    tid = '019a%04d-0000-7000-8000-%012d' % (k % 10000, k)
    d = os.path.join(home, '.codex', 'sessions', time.strftime('%Y/%m/%d', time.gmtime(t)))
    os.makedirs(d, exist_ok=True)
    meta = {'id': tid, 'originator': 'codex_exec', 'cwd': cwd, 'timestamp': B.iso(t), 'cli_version': '0.157.0'}
    rows = [(t, {'timestamp': B.iso(t), 'type': 'session_meta', 'payload': meta}),
            (t + 0.4, {'timestamp': B.iso(t + 0.4), 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}}),
            (t + 6, {'timestamp': B.iso(t + 6), 'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Done.'}]}}),
            (t + 7, {'timestamp': B.iso(t + 7), 'type': 'event_msg', 'payload': {'type': 'task_complete', 'last_agent_message': 'Done.'}})]
    path = os.path.join(d, 'rollout-%s-%s.jsonl' % (time.strftime('%Y-%m-%dT%H-%M-%S', time.gmtime(t)), tid))
    with open(path, 'w') as f:
        f.write(''.join(B.dump(r) + '\n' for _, r in rows))
    os.utime(path, (t + 7, t + 7))


def is_ours(dest):
    return os.path.exists(os.path.join(dest, MARK))


# ---------------------------------------------------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------------------------------------------------
def pct(values, p):
    """The p-th percentile (0-100) by nearest rank; None for no values."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, max(0, int(math.ceil(p / 100.0 * len(xs))) - 1))]


def summary(values):
    xs = [v for v in values if v is not None]
    return {'n': len(xs), 'p50': pct(xs, 50), 'p95': pct(xs, 95), 'max': max(xs) if xs else None, 'mean': (sum(xs) / len(xs)) if xs else None}


def rss_kb():
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def load_avg():
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except OSError:
        return None


class Timer:
    """Wall and CPU (of the process: all threads) of a block."""

    def __init__(self):
        self.wall = self.cpu = None

    def __enter__(self):
        self.t, self.c = time.perf_counter(), time.process_time()
        return self

    def __exit__(self, *a):
        self.wall, self.cpu = time.perf_counter() - self.t, time.process_time() - self.c


# ---------------------------------------------------------------------------------------------------------------------
# Worker: scan (runs in a fresh interpreter whose HOME is the folder under test)
# ---------------------------------------------------------------------------------------------------------------------
def import_board(repo):
    sys.path.insert(0, repo)
    import server  # noqa: F401
    return server


def grow(paths, rng, n_lines=3):
    """Appends a few records (a Bash call and its result, a line of talk) to each file, as a live session does."""
    for p in paths:
        with open(p, 'a') as f:
            for k in range(n_lines):
                t = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()) + '.%03dZ' % int(time.time() % 1 * 1000)
                tid = 'toolu_g%012d' % rng.randrange(10 ** 12)
                base = {'sessionId': os.path.basename(p)[:-6] if not p.endswith('.meta.json') else '', 'timestamp': t, 'cwd': '/tmp', 'entrypoint': 'cli', 'version': '2.1.284',
                        'userType': 'external', 'isSidechain': False, 'parentUuid': None}
                f.write(json.dumps(dict(base, type='assistant', uuid='u%016d' % rng.randrange(10 ** 16), message={
                    'id': 'msg_x', 'role': 'assistant', 'model': 'claude-sonnet-5-5', 'stop_reason': 'tool_use', 'usage': {'input_tokens': 10, 'output_tokens': 5},
                    'content': [{'type': 'tool_use', 'id': tid, 'name': 'Bash', 'input': {'command': 'ls -la', 'description': 'look'}}]}), separators=(',', ':')) + '\n')
                f.write(json.dumps(dict(base, type='user', uuid='u%016d' % rng.randrange(10 ** 16), message={
                    'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': 'total 8'}]}, toolUseResult={'stdout': 'total 8', 'stderr': '', 'interrupted': False}),
                    separators=(',', ':')) + '\n')


def worker_scan(a):
    server = import_board(a.repo)
    from board import catalog, views  # noqa: F401
    rng = random.Random(3)
    out = {'repo': a.repo, 'home': 'real' if a.real else 'synthetic', 'pid_python': sys.version.split()[0], 'load_before': load_avg()}
    LINKS, CODEX, REG = server.LINKS, server.CODEX, server.REG
    t = {}
    # --- ready: what the server does before it answers the first request (main(): LINKS.scan, scan_sessions, the default session) ---
    with Timer() as tm:
        try:
            CODEX.refresh(force=True)
        except TypeError:
            CODEX.refresh()
    t['codex_refresh'] = (tm.wall, tm.cpu)
    with Timer() as tm:
        LINKS.scan()
    t['link_scan_first'] = (tm.wall, tm.cpu)
    with Timer() as tm:
        ss, counts = server.scan_sessions()
    t['scan_sessions_first'] = (tm.wall, tm.cpu)
    projs = server.session_projects(ss)
    sid = a.session or server.default_session(projs, ss)
    if not a.real and a.busy and not a.session:
        sid = a.busy
    with Timer() as tm:
        s = REG.get(sid)
    t['open_session'] = (tm.wall, tm.cpu)
    out['ready'] = {'wall': sum(w for w, c in t.values()), 'cpu': sum(c for w, c in t.values())}
    out['phases'] = {k: {'wall': w, 'cpu': c} for k, (w, c) in t.items()}
    out['session'] = {'agents': len(s.agents) if s else None, 'feed': len(s.feed) if s else None, 'version': s.version if s else None, 'id8': (sid or '')[:8]}
    out['counts'] = {'claude_sessions': counts.get('claude'), 'agent_sessions': counts.get('agent_sessions'), 'link_files': len(LINKS.files),
                     'cli_owners': len(LINKS.cli_owners), 'cx_owners': len(LINKS.owners), 'bash_calls_seen': sum(len(f.get('bash', ())) for f in LINKS.files.values())}
    out['rss_kb_after_ready'] = rss_kb()
    # --- what the process keeps doing after it is ready (a later stage running in the background shows here) ---
    # The server starts the second link pass once its first picture is built; here that is right after "ready". An index without such a pass starts it on its own.
    start_later = getattr(LINKS, 'start_deep', None)
    if start_later:
        start_later()
    c0, w0 = time.process_time(), time.perf_counter()
    last, quiet = c0, 0
    while time.perf_counter() - w0 < a.settle and quiet < 4:
        time.sleep(0.25)
        c = time.process_time()
        quiet = quiet + 1 if c - last < 0.005 else 0
        last = c
    out['background'] = {'cpu': max(0.0, last - c0 - 0.001 * quiet), 'wall': time.perf_counter() - w0, 'settled': quiet >= 4}
    # --- the scans after it: nothing grew, and a few records grew ---
    idle_w, idle_c = [], []
    for _ in range(a.iterations):
        with Timer() as tm:
            LINKS.scan()
            for x in list(REG.sessions.values()):
                x.poll()
        idle_w.append(tm.wall)
        idle_c.append(tm.cpu)
    out['scan_idle'] = {'wall': summary(idle_w), 'cpu': summary(idle_c)}
    grow_w, grow_c, poll_w = [], [], []
    live = []
    if s is not None:
        live = [s.path] + [t.path for t in list(s.agent_tails.values())[:8]]
        live = [p for p in live if p.endswith('.jsonl')]
    if not a.real:
        prof = None
        if a.profile:
            import cProfile
            prof = cProfile.Profile()
        for _ in range(a.iterations):
            grow(live, rng)
            if prof:
                prof.enable()
            with Timer() as tm:
                LINKS.scan()
                for x in list(REG.sessions.values()):
                    x.poll()
            if prof:
                prof.disable()
            grow_w.append(tm.wall)
            grow_c.append(tm.cpu)
        out['scan_grow'] = {'wall': summary(grow_w), 'cpu': summary(grow_c), 'files_grown': len(live)}
        if prof:
            import pstats
            st = pstats.Stats(prof)
            rows = sorted(((v[3], v[0], k) for k, v in st.stats.items()), reverse=True)[:25]       # (cumulative s, calls, (file, line, name))
            out['scan_grow']['profile'] = [{'where': '%s:%s %s' % (os.path.basename(k[0]), k[1], k[2]), 'cum_ms_per_scan': round(c / a.iterations * 1000, 2), 'calls_per_scan': round(n / a.iterations, 1)}
                                           for c, n, k in rows]
    # --- /api/sessions: the list the page asks for every few seconds ---
    ws = []
    for _ in range(max(3, a.iterations // 3)):
        with Timer() as tm:
            server.scan_sessions()
        ws.append((tm.wall, tm.cpu))
    out['scan_sessions'] = {'wall': summary([w for w, c in ws]), 'cpu': summary([c for w, c in ws])}
    # --- state() in the process, nothing else running ---
    sw, sc = [], []
    for _ in range(a.iterations):
        with Timer() as tm:
            body = s.state() if s else None
        sw.append(tm.wall)
        sc.append(tm.cpu)
    out['state_compute'] = {'wall': summary(sw), 'cpu': summary(sc)}
    if a.profile and s is not None:
        import cProfile
        import pstats
        prof = cProfile.Profile()
        prof.enable()
        for _ in range(max(5, a.iterations // 3)):
            s.state()
        prof.disable()
        n_calls = max(5, a.iterations // 3)
        st = pstats.Stats(prof)
        rows = sorted(((v[3], v[1], k) for k, v in st.stats.items()), reverse=True)[:30]
        out['state_compute']['profile'] = [{'where': '%s:%s %s' % (os.path.basename(k[0]), k[1], k[2]), 'cum_ms_per_call': round(c / n_calls * 1000, 2), 'calls_per_call': round(n / n_calls, 1)}
                                           for c, n, k in rows]
    with Timer() as tm:
        raw = json.dumps(body, ensure_ascii=False).encode() if body else b''
    out['state_json'] = {'wall': tm.wall, 'bytes': len(raw)}
    with Timer() as tm:
        gz = gzip.compress(raw, 5)
    out['state_gzip'] = {'wall': tm.wall, 'bytes': len(gz)}
    out['rss_kb_end'] = rss_kb()
    out['load_after'] = load_avg()
    print(json.dumps(out))


# ---------------------------------------------------------------------------------------------------------------------
# Worker: http (the server in a thread of this process, a client in a process of its own)
# ---------------------------------------------------------------------------------------------------------------------
class Seams:
    """Wraps names of the running server so that each request leaves a record of where its time went. A name that is not there is left alone and listed in `missing`."""

    def __init__(self, server):
        import board.views as views
        self.server, self.views = server, views
        self.records = []
        self.background = {}               # label -> durations of the poll loop's own work (Session.poll, LinkIndex.scan)
        self.local = threading.local()
        self.missing = []
        self.lock = threading.Lock()

    def cur(self):
        return getattr(self.local, 'rec', None)

    def add(self, key, dt):
        r = self.cur()
        if r is not None:
            r[key] = r.get(key, 0.0) + dt

    def install(self, stack):
        from unittest import mock
        srv, views = self.server, self.views
        seams = self

        def patch(obj, name, make):
            if not hasattr(obj, name):
                seams.missing.append('%s.%s' % (getattr(obj, '__name__', type(obj).__name__), name))
                return
            stack.enter_context(mock.patch.object(obj, name, make(getattr(obj, name))))

        def do_get(orig):
            def w(handler):
                rec = {'t_in': time.perf_counter(), 'path': handler.path}
                seams.local.rec = rec
                try:
                    return orig(handler)
                finally:
                    rec['handler'] = time.perf_counter() - rec['t_in']
                    seams.local.rec = None
                    with seams.lock:
                        seams.records.append(rec)
            return w
        patch(srv.Handler, 'do_GET', do_get)

        def state(orig):
            def w(s):
                t = time.perf_counter()
                s.lock.acquire()
                seams.add('lock_wait', time.perf_counter() - t)
                try:
                    t = time.perf_counter()
                    r = orig(s)
                    seams.add('compute', time.perf_counter() - t)
                    return r
                finally:
                    s.lock.release()
            return w
        patch(views, 'state', state)

        def dumps(orig):
            def w(*a, **k):
                t = time.perf_counter()
                r = orig(*a, **k)
                seams.add('json', time.perf_counter() - t)
                if seams.cur() is not None and len(r) > 2048:
                    seams.cur()['raw_bytes'] = len(r.encode('utf-8'))         # after the timer: what goes on the wire is bytes
                return r
            return w
        patch(json, 'dumps', dumps)

        def compress(orig):
            def w(data, *a, **k):
                t = time.perf_counter()
                r = orig(data, *a, **k)
                seams.add('gzip', time.perf_counter() - t)
                if seams.cur() is not None:
                    seams.cur()['gz_bytes'] = len(r)
                return r
            return w
        patch(gzip, 'compress', compress)

        class Writer:
            def __init__(self, f):
                self.f = f

            def write(self, data):
                t = time.perf_counter()
                try:
                    return self.f.write(data)
                finally:
                    seams.add('write', time.perf_counter() - t)

            def __getattr__(self, n):
                return getattr(self.f, n)

        def setup(orig):
            def w(handler):
                orig(handler)
                handler.wfile = Writer(handler.wfile)
            return w
        patch(srv.Handler, 'setup', setup)

        def timed(label):
            def make(orig):
                def w(*a, **k):
                    t = time.perf_counter()
                    try:
                        return orig(*a, **k)
                    finally:
                        with seams.lock:
                            seams.background.setdefault(label, []).append(time.perf_counter() - t)
                return w
            return make
        patch(srv.Session, 'poll', timed('session_poll'))
        patch(srv.LinkIndex, 'scan', timed('link_scan'))


def coalesced_send(self, code, body, ctype='application/json; charset=utf-8', cache='no-store'):
    """Handler._send of server.py with one change (an experiment, not the fix): the status line, the headers and the body are written once. Same rules as the original:
    JSON for anything that is not bytes, gzip above 2048 bytes when the client accepts it (not for fonts)."""
    data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    self.send_response(code)
    if len(data) > 2048 and not ctype.startswith('font/') and 'gzip' in (self.headers.get('Accept-Encoding') or ''):
        data = gzip.compress(data, 5)
        self.send_header('Content-Encoding', 'gzip')
    self.send_header('Content-Type', ctype)
    self.send_header('Cache-Control', cache)
    self.send_header('Content-Length', str(len(data)))
    self._headers_buffer.append(b'\r\n')
    self._headers_buffer.append(data)
    self.flush_headers()


class _Stop(Exception):
    pass


def worker_http(a):
    server = import_board(a.repo)
    from unittest import mock
    from board import catalog
    REG, LINKS = server.REG, server.LINKS
    # the same start as main(): link scan, session list, default session; no link cache, no usage thread
    LINKS.scan()
    ss, _ = server.scan_sessions()
    sid = a.session or server.default_session(server.session_projects(ss), ss)
    if not a.real and a.busy and not a.session:
        sid = a.busy
    server.DEFAULT_SESSION = sid
    s = REG.get(sid)
    res = {'repo': a.repo, 'home': 'real' if a.real else 'synthetic', 'agents': len(s.agents), 'load_before': load_avg(), 'runs': [], 'experiment': a.experiment}
    srv, port = None, None
    for p in PORTS:
        try:
            srv = server.BoardServer(('127.0.0.1', p), server.Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print(json.dumps({'error': 'no free port in %d-%d' % (PORTS[0], PORTS[-1])}))
        return
    server.ALLOWED_HOSTS.update(['localhost', '127.0.0.1'])
    if a.experiment == 'nodelay':
        server.Handler.disable_nagle_algorithm = True            # TCP_NODELAY on the accepted socket
    elif a.experiment == 'coalesce':
        server.Handler._send = coalesced_send                    # the status line, the headers and the body leave in one write
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    seams = Seams(server)
    stack = contextlib.ExitStack()
    seams.install(stack)
    res['port'], res['seams_missing'] = port, seams.missing
    stop = threading.Event()
    live_paths = [s.path] + [t.path for t in list(s.agent_tails.values())[:8]]
    live_paths = [p for p in live_paths if p.endswith('.jsonl')]

    class FakeTime:
        """`time` as the registry's loop sees it, but its sleep ends the loop when the run is over."""

        def __getattr__(self, name):
            return getattr(time, name)

        @staticmethod
        def sleep(sec):
            if stop.is_set():
                raise _Stop()
            stop.wait(sec if sec < 0.5 else min(sec, a.poll_every))
            if stop.is_set():
                raise _Stop()

    def poller():
        try:
            with mock.patch.object(catalog, 'time', FakeTime()):
                REG.loop()
        except _Stop:
            pass

    def writer():
        rng = random.Random(5)
        while not stop.wait(a.write_every):
            grow(live_paths, rng, n_lines=2)

    def one_run(label, poll, write, gz, mode, n):
        stop.clear()
        seams.background.clear()
        threads = []
        if poll:
            threads.append(threading.Thread(target=poller, daemon=True))
        if write and not a.real:
            threads.append(threading.Thread(target=writer, daemon=True))
        for t_ in threads:
            t_.start()
        time.sleep(0.5)
        seams.records.clear()
        version = s.version
        env = dict(os.environ)
        cmd = [sys.executable, os.path.abspath(__file__), '_client', '--port', str(port), '--session', sid, '--n', str(n), '--mode', mode]
        cmd += ['--gzip'] if gz else []
        cmd += ['--version', str(version)] if mode == 'unchanged' else []
        cp = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600)
        stop.set()
        for t_ in threads:
            t_.join(timeout=10)
        reqs = [json.loads(x) for x in cp.stdout.splitlines() if x.startswith('{')]
        by_id = {}
        with seams.lock:
            for r in seams.records:
                m = r['path'].split('bench=')
                if len(m) > 1:
                    by_id[int(m[1].split('&')[0])] = r
        rows = []
        for q in reqs:
            r = by_id.get(q['i'], {})
            rows.append({'client_total': q['total'], 'connect': q['connect'], 'ttfb': q['ttfb'], 'body': q['body'], 'decompress': q['decompress'], 'bytes': q['bytes'],
                         'handler': r.get('handler'), 'lock_wait': r.get('lock_wait'), 'compute': r.get('compute'), 'json': r.get('json'), 'gzip': r.get('gzip'),
                         'write': r.get('write'), 'raw_bytes': r.get('raw_bytes'), 'gz_bytes': r.get('gz_bytes')})
        parts = {}
        for key in ('client_total', 'connect', 'ttfb', 'body', 'decompress', 'handler', 'lock_wait', 'compute', 'json', 'gzip', 'write'):
            parts[key] = summary([r[key] for r in rows if r.get(key) is not None])
        # what the client waited for that no seam saw: accept, thread start, request parsing, headers, the kernel
        other = [r['client_total'] - r['connect'] - r['handler'] for r in rows if r.get('handler') is not None]
        parts['outside_handler'] = summary(other)
        sizes = {k: (rows[len(rows) // 2].get(k) if rows else None) for k in ('bytes', 'raw_bytes', 'gz_bytes')}
        with seams.lock:
            background = {k: summary(v) for k, v in seams.background.items()}
        waits = sorted((r['lock_wait'] or 0.0) for r in rows)
        res['runs'].append({'label': label, 'poll': poll, 'write': write and not a.real, 'gzip': gz, 'mode': mode, 'n': len(rows), 'parts': parts, 'sizes': sizes,
                            'rows': rows[:200], 'background': background, 'lock_wait_over_10ms': sum(1 for x in waits if x > 0.010), 'seam_records': len(by_id), 'client_error': cp.stderr[-300:] if cp.returncode else None})

    n = a.requests
    one_run('quiet', False, False, True, 'full', n)
    one_run('quiet_identity', False, False, False, 'full', n)
    one_run('quiet_unchanged', False, False, True, 'unchanged', n)
    one_run('poll', True, False, True, 'full', n)
    one_run('busy', True, True, True, 'full', n)
    one_run('busy_identity', True, True, False, 'full', n)
    stack.close()
    srv.shutdown()
    srv.server_close()
    res['load_after'] = load_avg()
    res['rss_kb'] = rss_kb()
    print(json.dumps(res))


# ---------------------------------------------------------------------------------------------------------------------
# Client (a process of its own: its Python does not share the server's interpreter lock)
# ---------------------------------------------------------------------------------------------------------------------
def client_main(a):
    out = []
    for i in range(a.n):
        q = '/api/state?session=%s&bench=%d' % (a.session, i)
        if a.mode == 'unchanged':
            q += '&v=%s&t=%f' % (a.version, time.time())
        t0 = time.perf_counter()
        c = http.client.HTTPConnection('127.0.0.1', a.port, timeout=60)
        c.connect()
        t1 = time.perf_counter()
        c.request('GET', q, headers={'Accept-Encoding': 'gzip' if a.gzip else 'identity', 'Host': 'localhost:%d' % a.port})
        r = c.getresponse()
        t2 = time.perf_counter()
        body = r.read()
        t3 = time.perf_counter()
        raw = gzip.decompress(body) if r.getheader('Content-Encoding') == 'gzip' else body
        t4 = time.perf_counter()
        json.loads(raw)
        c.close()
        out.append({'i': i, 'connect': t1 - t0, 'ttfb': t2 - t1, 'body': t3 - t2, 'decompress': t4 - t3, 'total': t3 - t0, 'bytes': len(body), 'status': r.status})
        time.sleep(a.gap)
    for o in out:
        print(json.dumps(o))


# ---------------------------------------------------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------------------------------------------------
def worker_env(home):
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'XDG_CACHE_HOME', 'AGENT_BULLPEN_LOG', 'AGENT_BULLPEN_TOKEN')}
    env.update(HOME=home, PYTHONDONTWRITEBYTECODE='1', AGENT_BULLPEN_LANG='en')
    return env


def run_worker(kind, home, repo, extra, timeout=1800):
    cmd = [sys.executable, os.path.abspath(__file__), '_' + kind, '--repo', repo] + extra
    cp = subprocess.run(cmd, capture_output=True, text=True, env=worker_env(home), timeout=timeout)
    lines = [x for x in cp.stdout.splitlines() if x.startswith('{')]
    if cp.returncode or not lines:
        raise RuntimeError('worker %s failed (exit %s): %s' % (kind, cp.returncode, cp.stderr[-600:]))
    return json.loads(lines[-1])


def measure(a):
    """Runs the scan and http workers (as asked) and returns the result document."""
    repo = os.path.abspath(a.repo or REPO)
    doc = {'schema': SCHEMA, 'repo': repo, 'when': time.strftime('%Y-%m-%dT%H:%M:%S'), 'loadavg': load_avg(), 'python': sys.version.split()[0], 'cpus': os.cpu_count()}
    cleanup = None
    if a.real:
        home, extra = os.path.expanduser('~'), ['--real']
        doc['home'] = 'real'
    elif a.home and os.path.isdir(os.path.join(a.home, '.claude')) and not os.path.exists(os.path.join(a.home, 'manifest.json')):
        home, extra = os.path.abspath(a.home), []                       # a HOME made by something else (tools/synth_home.py): measured as it is
        doc['home'] = 'external'
    else:
        dest = a.home or tempfile.mkdtemp(prefix='bench-home-')
        if not (a.home and os.path.isdir(os.path.join(dest, 'home')) and os.path.exists(os.path.join(dest, 'manifest.json'))):
            man = build_big_home(dest, scale=a.scale, bulk_kb=a.bulk_kb, sessions=a.sessions, bash=a.bash, children=a.children, busy_agents=a.busy_agents)
            with open(os.path.join(dest, 'manifest.json'), 'w') as f:
                json.dump(man, f)
        else:
            with open(os.path.join(dest, 'manifest.json')) as f:
                man = json.load(f)
        cleanup = None if a.keep or a.home else dest
        home = man['home']
        extra = ['--busy', man['busy_session']]
        doc['home'] = 'synthetic'
        doc['manifest'] = {k: man[k] for k in ('sessions', 'mains', 'subagents', 'children', 'bash_calls', 'codex_threads', 'codex_exec_calls', 'bytes', 'busy_agents', 'scale')}
    if a.session:
        extra = extra + ['--session', a.session]
    try:
        common = extra + ['--iterations', str(a.iterations), '--settle', str(a.settle)] + (['--profile'] if a.profile else [])
        if 'scan' in a.what:
            doc['scan'] = run_worker('scan', home, repo, common)
        if 'http' in a.what:
            doc['http'] = run_worker('http', home, repo, extra + ['--requests', str(a.requests), '--poll-every', str(a.poll_every), '--write-every', str(a.write_every)] + (['--experiment', a.experiment] if a.experiment != 'none' else []))
    finally:
        if cleanup and is_ours(cleanup):
            shutil.rmtree(cleanup, ignore_errors=True)
    doc['metrics'] = metrics_of(doc)
    return doc


# the numbers --check looks at: name -> (unit, gating). A gating number is a CPU time or an in-process time (the load of the machine moves wall time of a thread, not these);
# the HTTP numbers are advisory unless --strict, because a wall-clock p95 on a loaded machine is a coin flip
METRICS = {
    'ready_cpu': ('s', True), 'ready_wall': ('s', False), 'background_cpu': ('s', False), 'rss_mb': ('mb', False), 'link_scan_first_cpu': ('s', True), 'scan_idle_p95_cpu': ('s', True), 'scan_idle_p95_wall': ('s', False),
    'scan_grow_p95_cpu': ('s', True), 'scan_grow_p95_wall': ('s', False), 'scan_sessions_p50_cpu': ('s', True), 'state_p50_cpu': ('s', True), 'state_p95_wall': ('s', False),
    'json_wall': ('s', True), 'gzip_wall': ('s', True),
    'http_busy_p50': ('s', False), 'http_busy_p95': ('s', False), 'http_quiet_p95': ('s', False), 'http_busy_lock_wait_p95': ('s', False),
    'http_busy_identity_p95': ('s', False),
}


def metrics_of(doc):
    m = {}
    sc, ht = doc.get('scan'), doc.get('http')
    if sc:
        m['ready_cpu'], m['ready_wall'] = sc['ready']['cpu'], sc['ready']['wall']
        m['link_scan_first_cpu'] = sc['phases']['link_scan_first']['cpu']
        m['background_cpu'] = sc['background']['cpu']
        m['rss_mb'] = (sc.get('rss_kb_end') or 0) / 1024.0
        m['scan_idle_p95_cpu'], m['scan_idle_p95_wall'] = sc['scan_idle']['cpu']['p95'], sc['scan_idle']['wall']['p95']
        if 'scan_grow' in sc:
            m['scan_grow_p95_cpu'], m['scan_grow_p95_wall'] = sc['scan_grow']['cpu']['p95'], sc['scan_grow']['wall']['p95']
        m['scan_sessions_p50_cpu'] = sc['scan_sessions']['cpu']['p50']
        m['state_p50_cpu'], m['state_p95_wall'] = sc['state_compute']['cpu']['p50'], sc['state_compute']['wall']['p95']
        m['json_wall'], m['gzip_wall'] = sc['state_json']['wall'], sc['state_gzip']['wall']
    if ht and 'runs' in ht:
        runs = {r['label']: r for r in ht['runs']}
        if 'busy' in runs:
            m['http_busy_p50'], m['http_busy_p95'] = runs['busy']['parts']['client_total']['p50'], runs['busy']['parts']['client_total']['p95']
            lw = runs['busy']['parts'].get('lock_wait', {})
            if lw.get('p95') is not None:
                m['http_busy_lock_wait_p95'] = lw['p95']
        if 'busy_identity' in runs:
            m['http_busy_identity_p95'] = runs['busy_identity']['parts']['client_total']['p95']
        if 'quiet' in runs:
            m['http_quiet_p95'] = runs['quiet']['parts']['client_total']['p95']
    return {k: v for k, v in m.items() if v is not None}


def slim(doc, label=None):
    """The part of a result worth keeping as a baseline: where and when it was taken, the scene, and the numbers the gate reads."""
    keep = {k: doc.get(k) for k in ('schema', 'when', 'loadavg', 'python', 'cpus', 'home', 'manifest', 'metrics')}
    keep['label'] = label or os.path.basename(doc.get('repo', '').rstrip('/'))
    return keep


def median_doc(docs, label=None):
    """The baseline of several runs: every number is the median of the runs that have it (one run's bad moment does not become the gate)."""
    out = slim(docs[0], label)
    names = set().union(*[d['metrics'].keys() for d in docs])
    out['metrics'] = {k: pct([d['metrics'][k] for d in docs if k in d['metrics']], 50) for k in sorted(names)}
    out['runs'] = len(docs)
    out['loadavg'] = [d.get('loadavg') for d in docs]
    return out


def compare(baseline, current, tol=TOLERANCE, strict=False):
    """[(name, baseline, current, ratio, status)] and whether the check passes. A gating number worse than baseline * (1 + tol) + floor fails; an advisory one only warns
    (it fails with strict). A number missing on one side is skipped."""
    rows, ok = [], True
    for name, (unit, gating) in METRICS.items():
        b, c = baseline.get(name), current.get(name)
        if b is None or c is None:
            continue
        limit = b * (1 + tol) + FLOOR[unit]
        bad = c > limit
        status = 'ok'
        if bad and (gating or strict):
            status, ok = 'FAIL', False
        elif bad:
            status = 'warn'
        rows.append((name, b, c, (c / b) if b else None, status))
    return rows, ok


# The budgets, against a baseline taken before the change under test
BUDGETS = (('ready_wall', 1.0, 'seconds more than the baseline: the first screen'), ('background_cpu', 2.0, 'seconds of CPU more than the baseline: the later stage after the first screen'), ('scan_idle_p95_wall', 0.005, 'seconds more than the baseline: a scan after the first'),
           ('scan_grow_p95_wall', 0.005, 'seconds more than the baseline: a scan while records grow'))


def budget_rows(baseline, current):
    rows, ok = [], True
    for name, add, why in BUDGETS:
        b, c = baseline.get(name), current.get(name)
        if b is None or c is None:
            continue
        good = c <= b + add
        ok = ok and good
        rows.append((name, b, c, c - b, add, 'ok' if good else 'FAIL', why))
    c = current.get('http_busy_p95')
    if c is not None:
        good = c <= 0.150
        ok = ok and good
        rows.append(('http_busy_p95', None, c, None, 0.150, 'ok' if good else 'FAIL', 'seconds at most: /api/state over HTTP while the poll loop runs'))
    c = current.get('http_busy_identity_p95')
    if c is not None:
        good = c <= 0.150
        ok = ok and good
        rows.append(('http_busy_identity_p95', None, c, None, 0.150, 'ok' if good else 'FAIL', 'seconds at most: the same without Accept-Encoding: gzip, a plain client or a body past 64 KB'))
    c = current.get('state_p95_wall')
    if c is not None:
        good = c <= 0.020
        ok = ok and good
        rows.append(('state_p95_wall', None, c, None, 0.020, 'ok' if good else 'FAIL', 'seconds at most: state() in the process'))
    return rows, ok


def fmt(x, unit='s'):
    if x is None:
        return '   -   '
    return '%8.1f ms' % (x * 1000) if unit == 's' else '%8.0f MB' % x if unit == 'mb' else '%8.3f' % x


def print_report(doc, out=sys.stdout):
    w = out.write
    w('bench: %s HOME, repo %s, python %s, %s cpus, load average %s\n' % (doc['home'], doc['repo'], doc['python'], doc['cpus'], doc['loadavg']))
    if 'manifest' in doc:
        m = doc['manifest']
        w('  built: %d session records (%d main sessions + %d claude -p children) and %d sub-agent records, %d Bash calls, %d codex threads, %.1f MB, busy session with %d sub-agents\n' % (
            m['sessions'], m['mains'], m['children'], m['subagents'], m['bash_calls'], m['codex_threads'], m['bytes'] / 1e6, m['busy_agents']))
    sc = doc.get('scan')
    if sc:
        w('scan (CPU / wall)\n')
        for k, v in sc['phases'].items():
            w('  %-24s %s / %s\n' % (k, fmt(v['cpu']), fmt(v['wall'])))
        w('  %-24s %s / %s\n' % ('ready (sum)', fmt(sc['ready']['cpu']), fmt(sc['ready']['wall'])))
        w('  after ready: %s CPU in the next %.1f s%s\n' % (fmt(sc['background']['cpu']), sc['background']['wall'], '' if sc['background']['settled'] else ' (still busy)'))
        for k in ('scan_idle', 'scan_grow', 'scan_sessions', 'state_compute'):
            if k in sc:
                w('  %-24s p50 %s / %s   p95 %s / %s\n' % (k, fmt(sc[k]['cpu']['p50']), fmt(sc[k]['wall']['p50']), fmt(sc[k]['cpu']['p95']), fmt(sc[k]['wall']['p95'])))
        w('  state JSON %s (%d bytes), gzip level 5 %s (%d bytes); RSS %.0f MB\n' % (fmt(sc['state_json']['wall']), sc['state_json']['bytes'], fmt(sc['state_gzip']['wall']),
                                                                                      sc['state_gzip']['bytes'], (sc.get('rss_kb_end') or 0) / 1024.0))
        w('  session: %s agents, %s events; links: %s files, %s claude -p owners, %s codex owners, %s Bash calls\n' % (
            sc['session']['agents'], sc['session']['feed'], sc['counts']['link_files'], sc['counts']['cli_owners'], sc['counts']['cx_owners'], sc['counts']['bash_calls_seen']))
    ht = doc.get('http')
    if ht and 'runs' in ht:
        w('http /api/state (milliseconds; p50 / p95)  agents %s, port %s, missing seams: %s\n' % (ht['agents'], ht['port'], ', '.join(ht['seams_missing']) or 'none'))
        keys = ('client_total', 'connect', 'ttfb', 'body', 'decompress', 'handler', 'lock_wait', 'compute', 'json', 'gzip', 'write', 'outside_handler')
        w('  %-16s %s\n' % ('run', ' '.join('%-13s' % k[:13] for k in keys)))
        for r in ht['runs']:
            cells = []
            for k in keys:
                p = r['parts'].get(k)
                cells.append('%-13s' % ('-' if not p or p['p50'] is None else '%.1f/%.1f' % (p['p50'] * 1000, p['p95'] * 1000)))
            w('  %-16s %s   %d B -> %s B\n' % (r['label'], ' '.join(cells), (r['sizes'].get('raw_bytes') or 0), r['sizes'].get('gz_bytes') or r['sizes'].get('bytes')))
        for r in ht['runs']:
            bg = r.get('background') or {}
            if bg:
                w('  %-16s poll loop: %s; requests that waited >10 ms for the lock: %d of %d\n' % (r['label'], '; '.join(
                    '%s n=%d p50 %.1f p95 %.1f max %.1f ms' % (k, v['n'], v['p50'] * 1000, v['p95'] * 1000, v['max'] * 1000) for k, v in sorted(bg.items())), r.get('lock_wait_over_10ms', 0), r['n']))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='Measure the first scan, later scans, state() and /api/state over HTTP.')
    sub = ap.add_subparsers(dest='cmd')
    r = sub.add_parser('run', help='measure')
    r.add_argument('--scale', type=float, default=1.0, help='1 = 100 sessions, 25 000 Bash calls, 200 children')
    r.add_argument('--real', action='store_true', help='the real HOME (read-only)')
    r.add_argument('--home', help='reuse (or build into) this folder instead of a temporary one')
    r.add_argument('--session', help='the session to open (default: the one the server would open)')
    r.add_argument('--keep', action='store_true', help='keep the synthetic HOME')
    r.add_argument('--repo', help='measure this copy of the tree (default: this one)')
    r.add_argument('--what', default='scan,http', help='scan, http or both')
    r.add_argument('--iterations', type=int, default=30)
    r.add_argument('--settle', type=float, default=10.0, help='seconds to watch for a background stage after the first screen (CPU time of the process)')
    r.add_argument('--requests', type=int, default=40)
    r.add_argument('--poll-every', type=float, default=2.0, help='seconds between polls of the registry loop (the real server: 2)')
    r.add_argument('--write-every', type=float, default=1.0, help='seconds between the writer growing the busy records')
    r.add_argument('--experiment', choices=('none', 'nodelay', 'coalesce'), default='none', help='change the server in the worker and measure again: nodelay = TCP_NODELAY, coalesce = one write for headers and body')
    r.add_argument('--bulk-kb', type=int, default=BULK_KB, help='KB of filler lines per main session (0: only the calls)')
    r.add_argument('--sessions', type=int)
    r.add_argument('--bash', type=int)
    r.add_argument('--children', type=int)
    r.add_argument('--busy-agents', type=int)
    r.add_argument('--json', metavar='FILE', help='write the result document here')
    r.add_argument('--profile', action='store_true', help='also profile the scans while the records grow (function names and times only)')
    r.add_argument('--save-baseline', metavar='FILE', help='keep the numbers (no per-request rows) as a baseline')
    r.add_argument('--label', help='what the baseline is of (default: the name of the repo folder)')
    r.add_argument('--check', metavar='BASELINE', help='fail when a gating number is worse than the baseline by %d %%%%' % int(TOLERANCE * 100))
    r.add_argument('--budget', metavar='BASELINE', help='check the budgets against a baseline taken before the change: the first screen +1 s, the later stage +2 s of CPU, a scan +5 ms (p95), /api/state over HTTP at most 150 ms (p95), state() at most 20 ms (p95)')
    r.add_argument('--strict', action='store_true', help='--check also fails on the advisory (wall-clock HTTP) numbers')
    m = sub.add_parser('baseline', help='median of several result files as one baseline file')
    m.add_argument('out')
    m.add_argument('results', nargs='+')
    m.add_argument('--label')
    b = sub.add_parser('build', help='build the synthetic HOME only')
    b.add_argument('dest')
    b.add_argument('--scale', type=float, default=1.0)
    b.add_argument('--bulk-kb', type=int, default=BULK_KB)
    for name in ('scan', 'http'):
        w = sub.add_parser('_' + name)
        w.add_argument('--repo', required=True)
        w.add_argument('--real', action='store_true')
        w.add_argument('--busy')
        w.add_argument('--session')
        w.add_argument('--iterations', type=int, default=30)
        w.add_argument('--requests', type=int, default=40)
        w.add_argument('--poll-every', type=float, default=2.0)
        w.add_argument('--write-every', type=float, default=1.0)
        w.add_argument('--experiment', choices=('none', 'nodelay', 'coalesce'), default='none')
        w.add_argument('--profile', action='store_true')
        w.add_argument('--settle', type=float, default=10.0)
    c = sub.add_parser('_client')
    c.add_argument('--port', type=int, required=True)
    c.add_argument('--session', required=True)
    c.add_argument('--n', type=int, default=20)
    c.add_argument('--mode', default='full')
    c.add_argument('--version', default='0')
    c.add_argument('--gzip', action='store_true')
    c.add_argument('--gap', type=float, default=0.05)
    a = ap.parse_args(argv)
    if a.cmd == 'build':
        if os.path.exists(a.dest) and not is_ours(a.dest) and os.listdir(a.dest):
            ap.error('%s exists and was not made by this tool' % a.dest)
        man = build_big_home(a.dest, scale=a.scale, bulk_kb=a.bulk_kb)
        with open(os.path.join(a.dest, 'manifest.json'), 'w') as f:
            json.dump(man, f)
        print(json.dumps({k: man[k] for k in ('sessions', 'mains', 'subagents', 'children', 'bash_calls', 'codex_threads', 'bytes')}))
        return 0
    if a.cmd == 'baseline':
        docs = []
        for path in a.results:
            with open(path) as f:
                docs.append(json.load(f))
        with open(a.out, 'w') as f:
            json.dump(median_doc(docs, a.label), f, indent=1)
        print('baseline of %d run(s) written: %s' % (len(docs), a.out))
        return 0
    if a.cmd == '_scan':
        worker_scan(a)
        return 0
    if a.cmd == '_http':
        worker_http(a)
        return 0
    if a.cmd == '_client':
        client_main(a)
        return 0
    if a.cmd != 'run':
        ap.print_help()
        return 2
    a.what = [x.strip() for x in a.what.split(',') if x.strip()]
    doc = measure(a)
    print_report(doc)
    if a.json:
        with open(a.json, 'w') as f:
            json.dump(doc, f, indent=1)
    if a.save_baseline:
        with open(a.save_baseline, 'w') as f:
            json.dump(slim(doc, a.label), f, indent=1)
        print('baseline written: %s' % a.save_baseline)
    code = 0
    if a.check:
        with open(a.check) as f:
            base = json.load(f)
        rows, ok = compare(base['metrics'], doc['metrics'], strict=a.strict)
        print('check against %s (tolerance %d %%, gating = CPU and in-process numbers%s)' % (a.check, int(TOLERANCE * 100), ', all numbers' if a.strict else ''))
        for name, b_, c_, ratio, st in rows:
            unit = METRICS[name][0]
            print('  %-26s %s -> %s  %s  %s' % (name, fmt(b_, unit), fmt(c_, unit), ('x%.2f' % ratio) if ratio else '', st))
        print('check: %s' % ('ok' if ok else 'FAILED'))
        code = 0 if ok else 1
    if a.budget:
        with open(a.budget) as f:
            base = json.load(f)
        rows, ok = budget_rows(base['metrics'], doc['metrics'])
        print('budgets against %s' % a.budget)
        for name, b_, c_, delta, limit, st, why in rows:
            print('  %-22s %s -> %s  limit %s  %s  (%s)' % (name, fmt(b_), fmt(c_), fmt(limit), st, why))
        print('budgets: %s' % ('ok' if ok else 'FAILED'))
        code = code or (0 if ok else 1)
    return code


if __name__ == '__main__':
    sys.exit(main())
