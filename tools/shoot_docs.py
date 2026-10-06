#!/usr/bin/env python3
"""Re-take docs/images/<lang>/*.png from a synthetic HOME (nothing real is read): the docs scene (tools/docs_scene.py, live) for dashboard.png, office.png, agents.png and debates.png,
an empty HOME for empty.png. Starts two boards, runs tools/shoot_docs.js once per language, stops what it started (own handles only), checks the sizes.

    NODE_PATH=<pw>/node_modules PLAYWRIGHT_BROWSERS_PATH=<pw>/browsers python3 tools/shoot_docs.py [--scene docs|synth] [--busy] [--stopped] [--lang en|ko|both] [--src <folder>] [--out docs/images] [--ports 8815 8816]

--scene   docs (default): the calm office of tools/docs_scene.py, which the README pictures are taken from: about ten people working, four to six resting in the lounge, two or more of them at the
          tea table (the moment is picked by SHOOT_LOUNGE=pick, see tools/shoot_docs.js). synth: the scene of tools/synth_home.py (the options below)
--busy    (--scene synth) the large scene (acme-robot, 33 agents) instead of the small one
--stopped  (--scene synth) add the stopped and nested work of `synth_home.py --stopped` (a usage limit, API errors, runs cut off, a paused debate cell, grandchildren)
--lang    the screen language of the pictures (default both): English goes to <out>/en/ (README.md), Korean to <out>/ko/ (README.ko.md)
--src     folder whose server.py (and static/ beside it) is shown; default: this repository. The synthetic HOME always comes from this repository's tools/.
The moving picture docs/images/<lang>/office.gif is not taken here: see tools/shoot_gif.py.
<pw> is any folder where `npm i playwright && npx playwright install chromium` was run. Each picture must stay under 500 KB.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import docs_scene  # noqa: E402
import synth_home  # noqa: E402

LIMIT = 500 * 1024


def read_log(f):
    """Everything the server has written so far to `f`, the temporary file it was given as its output. Read with `pread`: `f.seek(0)` would move the position the server writes at too
    (the file is one open file for both), and a line it writes next would land on the text that is already there."""
    fd, pos, chunks = f.fileno(), 0, []
    while True:
        chunk = os.pread(fd, 1 << 16, pos)
        if not chunk:
            return b''.join(chunks).decode('utf-8', 'replace')
        chunks.append(chunk)
        pos += len(chunk)


def start(home, port, ready, src):
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'XDG_CACHE_HOME', 'AGENT_BULLPEN_LOG', 'AGENT_BULLPEN_TOKEN')}
    env.update(HOME=home, PYTHONDONTWRITEBYTECODE='1', AGENT_BULLPEN_LANG='ko')     # the ready lines matched in main() are Korean, whatever LANG the runner has
    log = tempfile.TemporaryFile('w+')
    p = subprocess.Popen([sys.executable, os.path.join(src, 'server.py'), '--port', str(port)], stdout=log, stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL, cwd=src)
    end = time.time() + 60
    while time.time() < end and p.poll() is None:
        if ready in read_log(log):
            return p, log
        time.sleep(0.1)
    raise SystemExit('board on port %d did not come up:\n%s' % (port, read_log(log)))


def wait_links(port, info):
    """The first seconds after a start link the `claude -p` runs of the stopped scene by a guess (the time rule), and the agent list shows them with another title and without the tree:
    wait until the run that the release-notes agent started is linked for sure (it is in the list under its own id)."""
    kid = synth_home.STOPPED_KIDS['grand']
    end = time.time() + 30
    while time.time() < end:
        try:
            with urllib.request.urlopen('http://localhost:%d/api/state?session=%s' % (port, info['orch']), timeout=20) as r:
                if kid in r.read().decode('utf-8'):
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit('the stopped scene was not linked within 30 s on port %d' % port)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=os.path.join(ROOT, 'docs', 'images'))
    ap.add_argument('--ports', nargs=2, type=int, default=[8815, 8816], metavar=('SYNTH', 'EMPTY'))
    ap.add_argument('--scene', choices=('docs', 'synth'), default='docs', help='docs: tools/docs_scene.py (the README pictures); synth: tools/synth_home.py with --busy / --stopped')
    ap.add_argument('--busy', action='store_true', help='(--scene synth) the large scene (see tools/synth_home.py --busy)')
    ap.add_argument('--stopped', action='store_true', help='(--scene synth) add stopped and nested work (see tools/synth_home.py --stopped)')
    ap.add_argument('--lang', choices=('en', 'ko', 'both'), default='both', help='screen language of the pictures')
    ap.add_argument('--src', default=ROOT, help='folder with the server.py (and static/) to show (default: this repository)')
    args = ap.parse_args()
    src = os.path.realpath(args.src)
    if not os.path.isfile(os.path.join(src, 'server.py')):
        raise SystemExit('no server.py in %s' % src)
    langs = ['en', 'ko'] if args.lang == 'both' else [args.lang]
    procs, tmp = [], tempfile.TemporaryDirectory()
    info = None
    try:
        home = os.path.join(tmp.name, 'acme-home')
        info = docs_scene.build(home) if args.scene == 'docs' else synth_home.build(home, busy=args.busy, stopped=args.stopped)
        synth_home.start_live(info)
        empty = os.path.join(os.path.realpath(tmp.name), 'empty-home')
        os.makedirs(empty)
        procs.append(start(info['home'], args.ports[0], '세션 %s 읽음' % info['orch'], src))
        procs.append(start(empty, args.ports[1], '프로세스 판정', src))
        if info.get('stopped'):
            wait_links(args.ports[0], info)
        env = dict(os.environ)
        if args.scene == 'docs':                  # the picks of the lounge and the topics of the debate picture that this scene was made for (a variable already set wins)
            env.setdefault('SHOOT_LOUNGE', 'pick')
            env.setdefault('SHOOT_FOLD_ALERTS', '1')
            env.setdefault('SHOOT_TOPICS', 'T1,T3,T4,T5,T6')
        for lang in langs:
            os.makedirs(os.path.join(args.out, lang), exist_ok=True)
            r = subprocess.run(['node', os.path.join(HERE, 'shoot_docs.js'), 'http://localhost:%d' % args.ports[0], 'http://localhost:%d' % args.ports[1], os.path.join(args.out, lang), lang], env=env)
            if r.returncode:
                raise SystemExit('shoot_docs.js failed (%d) for %s' % (r.returncode, lang))
    finally:
        for p, log in procs:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
            log.close()
        if info:
            synth_home.stop_live(info['home'], quiet=True)
        tmp.cleanup()
    bad = 0
    for lang in langs:
        for name in ('dashboard.png', 'office.png', 'agents.png', 'debates.png', 'empty.png'):
            size = os.path.getsize(os.path.join(args.out, lang, name))
            print('%-22s %4d KB%s' % (lang + '/' + name, size // 1024, '' if size <= LIMIT else '  > 500 KB'))
            bad += size > LIMIT
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
