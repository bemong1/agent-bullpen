#!/usr/bin/env python3
"""Re-take docs/images/<lang>/*.png from a synthetic HOME (nothing real is read): tools/synth_home.py --live for dashboard.png, office.png, agents.png and debates.png,
an empty HOME for empty.png. Starts two boards, runs tools/shoot_docs.js once per language, stops what it started (own handles only), checks the sizes.

    NODE_PATH=<pw>/node_modules PLAYWRIGHT_BROWSERS_PATH=<pw>/browsers python3 tools/shoot_docs.py [--busy] [--stopped] [--lang en|ko|both] [--src <folder>] [--out docs/images] [--ports 8815 8816]

--busy    the large scene (acme-robot, 33 agents) instead of the small one; the README pictures are taken this way
--stopped  add the stopped and nested work of `synth_home.py --stopped` (a usage limit, API errors, runs cut off, a paused debate cell, grandchildren); the README pictures are taken this way
--lang    the screen language of the pictures (default both): English goes to <out>/en/ (README.md), Korean to <out>/ko/ (README.ko.md)
--src     folder whose server.py (and static/ beside it) is shown; default: this repository. The synthetic HOME always comes from this repository's tools/.
<pw> is any folder where `npm i playwright && npx playwright install chromium` was run. Each picture must stay under 500 KB.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import synth_home  # noqa: E402

LIMIT = 500 * 1024


def start(home, port, ready, src):
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'XDG_CACHE_HOME', 'AGENT_BULLPEN_LOG')}
    env.update(HOME=home, PYTHONDONTWRITEBYTECODE='1', AGENT_BULLPEN_LANG='ko')     # the ready lines matched in main() are Korean, whatever LANG the runner has
    log = tempfile.TemporaryFile('w+')
    p = subprocess.Popen([sys.executable, os.path.join(src, 'server.py'), '--port', str(port)], stdout=log, stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL, cwd=src)
    end = time.time() + 60
    while time.time() < end and p.poll() is None:
        log.seek(0)
        if ready in log.read():
            return p, log
        time.sleep(0.1)
    log.seek(0)
    raise SystemExit('board on port %d did not come up:\n%s' % (port, log.read()))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=os.path.join(ROOT, 'docs', 'images'))
    ap.add_argument('--ports', nargs=2, type=int, default=[8815, 8816], metavar=('SYNTH', 'EMPTY'))
    ap.add_argument('--busy', action='store_true', help='the large scene (see tools/synth_home.py --busy)')
    ap.add_argument('--stopped', action='store_true', help='add stopped and nested work (see tools/synth_home.py --stopped)')
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
        info = synth_home.build(os.path.join(tmp.name, 'acme-home'), busy=args.busy, stopped=args.stopped)
        synth_home.start_live(info)
        empty = os.path.join(os.path.realpath(tmp.name), 'empty-home')
        os.makedirs(empty)
        procs.append(start(info['home'], args.ports[0], '세션 %s 읽음' % info['orch'], src))
        procs.append(start(empty, args.ports[1], '사용량 조회', src))
        for lang in langs:
            os.makedirs(os.path.join(args.out, lang), exist_ok=True)
            r = subprocess.run(['node', os.path.join(HERE, 'shoot_docs.js'), 'http://localhost:%d' % args.ports[0], 'http://localhost:%d' % args.ports[1], os.path.join(args.out, lang), lang])
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
