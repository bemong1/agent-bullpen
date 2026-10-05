#!/usr/bin/env python3
"""Fixture for the browser-less UI checks (tools/regress/ui_checks.js, lounge_checks.js, game_snap.js ...) made from a synthetic HOME.
Builds the HOME (tools/synth_home.py), starts server.py on it, saves what the pages would fetch, stops the server.

    python3 tools/make_synth_fixture.py /tmp/synth-fx [--port 8811] [--live]
    node tools/regress/ui_checks.js static /tmp/synth-fx/synth
    node tools/regress/lounge_checks.js static /tmp/synth-fx/synth

Writes <out>/synth_state.json, _sessions.json, _talk.json (user talk), _atalk.json (agent talk), _plans.json; the HOME is <out>/home.
A second HOME, <out>/home_stopped (the same scene + `synth_home.py --stopped --live`: a usage limit the orchestrator waits on, runs that stopped for each reason, a paused debate cell,
grandchildren, system lines, diagnostics), is saved as synth_stopped_state.json, _sessions, _talk, _atalk, _plans and _diag (what /api/diag answers); tools/regress/state_checks.js reads it.
--no-stopped leaves it out.
A third HOME, <out>/home_codex (`synth_home.py --codex-orch --live`: a Codex orchestrator with two native sub-agents, a guardian, two `claude -p` runs and a `codex exec` run, a small debate), is saved as
synth_codex_state.json, _sessions, _talk, _atalk, _plans and _agent (what /api/agent answers for its `codex exec` run); tools/regress/codex_checks.js reads it. --no-codex leaves it out.
--port 0 (default) takes any free port; only the server this script started is stopped (by its own handle).

Two things are done to the saved answers so the checks do not depend on when they run:
  - the clock is frozen: every epoch time in the files is shifted so that state.now == FROZEN_NOW, so every run sees the same
    times (the page checks pass with the real clock too). Use --no-freeze to keep the real clock.
  - solo sessions are left out of _sessions.json: ui_checks.js builds its own solo variants and expects the plain list to have no groups
    (the solo conversation is covered by tests/test_synth.py).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import synth_home  # noqa: E402

SERVER = os.path.join(os.path.dirname(HERE), 'server.py')
FROZEN_NOW = 1790800000.0          # 2026-09-30 21:06:40 UTC; any fixed value will do
EPOCH = (1.5e9, 3e9)               # numbers in this range are times (token counts, indexes and sizes are far below)


def freeze(x, delta):
    """x with every epoch time shifted by delta seconds."""
    if isinstance(x, dict):
        return {k: freeze(v, delta) for k, v in x.items()}
    if isinstance(x, list):
        return [freeze(v, delta) for v in x]
    if isinstance(x, (int, float)) and not isinstance(x, bool) and EPOCH[0] < x < EPOCH[1]:
        return round(x + delta, 3) if isinstance(x, float) else int(round(x + delta))
    return x


def get(port, path):
    with urllib.request.urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=60) as r:
        return json.load(r)


class StoppedNotReady(Exception):
    """The second fixture could not be taken; the first one is already written and stays good."""


def stopped_ready(port, sid):
    """True once the board has linked the `claude -p` runs of the stopped scene, including the ones a sub-agent and another run started (the second phase of the first scan)."""
    try:
        st = get(port, '/api/state?session=' + sid)
    except OSError:
        return False
    cli = [a for a in st['agents'] if a['origin'] == 'cli']
    return len(cli) >= 5 and any(a['parent'] for a in cli) and all(a['link']['rule_class'] == 'certain' for a in cli)


def codex_ready(port, root):
    """True once the board shows the whole team of the Codex orchestrator (two sub-agents, two `claude -p` runs, the `codex exec` run)."""
    try:
        st = get(port, '/api/state?session=' + root)
    except OSError:
        return False
    return len(st['agents']) >= 5


def snapshot(args, out, prefix, stopped=False, codex=False):
    """Build a HOME under <out>/home[_stopped|_codex], serve it, save what the pages fetch as synth_<prefix>*.json, stop the server and the fake processes."""
    info = synth_home.build(os.path.join(out, 'home_stopped' if stopped else 'home_codex' if codex else 'home'), stopped=stopped, codex_orch=codex)
    pids = synth_home.start_live(info) if args.live or stopped or codex else []
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'XDG_CACHE_HOME', 'AGENT_BULLPEN_LOG', 'AGENT_BULLPEN_TOKEN')}
    env.update(HOME=info['home'], PYTHONDONTWRITEBYTECODE='1', AGENT_BULLPEN_LANG='ko')     # the ready line below is matched in Korean, whatever LANG the runner has
    log = tempfile.TemporaryFile('w+')
    srv = subprocess.Popen([sys.executable, SERVER, '--port', str(args.port)], stdout=log, stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL)
    try:
        end, port = time.time() + 60, None
        while time.time() < end and srv.poll() is None:
            log.seek(0)
            text = log.read()
            if '세션 %s 읽음' % info['orch'] in text:
                port = int(text.split('http://localhost:')[1].split('/')[0])
                break
            time.sleep(0.1)
        if port is None:
            log.seek(0)
            raise SystemExit('server did not come up:\n' + log.read())
        if stopped:
            end = time.time() + 60
            while time.time() < end and not stopped_ready(port, info['orch']):
                time.sleep(0.5)
            if not stopped_ready(port, info['orch']):
                raise StoppedNotReady('the stopped scene was not linked in 60 s')
        if codex:
            end = time.time() + 60
            while time.time() < end and not codex_ready(port, info['codex_orch']['root']):
                time.sleep(0.5)
            if not codex_ready(port, info['codex_orch']['root']):
                raise StoppedNotReady('the team of the Codex orchestrator was not linked in 60 s')
        sid = '?session=' + (info['codex_orch']['root'] if codex else info['orch'])
        files = {'sessions': '/api/sessions', 'state': '/api/state' + sid, 'talk': '/api/talk?limit=80&' + sid[1:],
                 'atalk': '/api/talk?scope=agents&limit=80&' + sid[1:], 'plans': '/api/plans'}
        if stopped:
            files['diag'] = '/api/diag' + sid
        if codex:
            files['agent'] = '/api/agent?id=%s&%s' % (info['codex_orch']['ids']['reviewer_b'], sid[1:])
        data = {name: get(port, path) for name, path in files.items()}
        keep = [x for x in data['sessions']['sessions'] if not x.get('solo')]
        data['sessions'].update(sessions=keep, projects=[p for p in data['sessions']['projects'] if p['rep'] in {x['id'] for x in keep}])
        if not args.no_freeze:
            delta = FROZEN_NOW - data['state']['now']
            data = {name: freeze(v, delta) for name, v in data.items()}
        for name, v in data.items():
            with open(os.path.join(out, 'synth_%s%s.json' % (prefix, name)), 'w') as f:
                json.dump(v, f, ensure_ascii=False)
        st = data['state']
        print('fixture %s%s: %d agents (%d Codex), %d feed events, %d debate(s)%s' % (
            os.path.join(out, 'synth'), '_' + prefix.rstrip('_') if prefix else '', len(st['agents']), sum(a['provider'] == 'codex' for a in st['agents']), len(st['feed']), len(st['debates']),
            ', %d diagnostics' % st['diag']['n'] if stopped else ''))
    finally:
        srv.terminate()
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
        log.close()
        if pids:
            synth_home.stop_live(info['home'], quiet=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('out', help='output folder (made if missing)')
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--live', action='store_true', help='with fake claude processes: sessions and agents read as working')
    ap.add_argument('--no-freeze', action='store_true', help='keep the real clock in the saved answers')
    ap.add_argument('--no-stopped', action='store_true', help='leave out the second fixture (synth_stopped_*.json)')
    ap.add_argument('--no-codex', action='store_true', help='leave out the third fixture (synth_codex_*.json)')
    args = ap.parse_args()
    out = os.path.realpath(args.out)
    os.makedirs(out, exist_ok=True)
    snapshot(args, out, '')
    if not args.no_stopped:
        try:
            snapshot(args, out, 'stopped_', stopped=True)
        except (StoppedNotReady, SystemExit, OSError) as e:           # only state_checks.js needs it: it skips itself without the files on a laptop and fails when CI is set
            print('note: the stopped fixture (synth_stopped_*.json) was not made: %s' % (e or type(e).__name__), file=sys.stderr)
    if not args.no_codex:
        try:
            snapshot(args, out, 'codex_', codex=True)
        except (StoppedNotReady, SystemExit, OSError) as e:           # only codex_checks.js needs it: it skips itself without the files on a laptop and fails when CI is set
            print('note: the Codex fixture (synth_codex_*.json) was not made: %s' % (e or type(e).__name__), file=sys.stderr)


if __name__ == '__main__':
    main()
