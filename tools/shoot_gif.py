#!/usr/bin/env python3
"""Re-take docs/images/<lang>/office.gif, the moving picture at the top of the READMEs, from a synthetic HOME (nothing real is read): the "New work" demo of the office page
(static/game-demo.js) on the docs scene (tools/docs_scene.py): somebody gives the orchestrator a task, three agents walk in at the entrance to a new room and write their reports, and when they
hand them in they walk to the lounge to rest. Starts one board, runs tools/shoot_gif.js once per language (it plays the page on a clock it steps by hand and saves one PNG per step), stops what it
started (own handles only) and joins the frames into the GIF with ffmpeg: one palette for all frames, only the changed rectangle of a frame stored, and the end and the start fading through the colour of the page
so that the loop does not jump.

    NODE_PATH=<pw>/node_modules PLAYWRIGHT_BROWSERS_PATH=<pw>/browsers python3 tools/shoot_gif.py [--lang en|ko|both] [--src <folder>] [--out docs/images] [--port 8815] [--frames <folder>]

--lang    the screen language of the GIF (default both): English goes to <out>/en/office.gif (README.md), Korean to <out>/ko/office.gif (README.ko.md)
--src     folder whose server.py (and static/ beside it) is shown; default: this repository. The synthetic HOME always comes from this repository's tools/.
--frames  keep the PNG frames of each language in <folder>/<lang>/ (to look at them); default: a temporary folder that is removed again
<pw> is any folder where `npm i playwright && npx playwright install chromium` was run; ffmpeg must be on the PATH. Each GIF must stay under 3 MB.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import docs_scene  # noqa: E402
import shoot_docs  # noqa: E402
import synth_home  # noqa: E402

LIMIT = 3 * 1024 * 1024
FADE = 3           # frames at the end that fade out to the colour of the page, and frames at the start that fade in from it (the loop)
PAGE = '0x0f0e1d'  # the background of the office page (--bg of static/game.html)


def encode(frames, out):
    """frames: a folder of f0001.png... and frames.json (written by shoot_gif.js). The frames play as they were shot, the last FADE of them fading out and the first FADE fading in
    from the colour of the page, so that the end of the story does not jump to the start of it (nothing blends two pictures: no text ever lies over other text)."""
    meta = json.load(open(os.path.join(frames, 'frames.json')))
    n, fps = meta['count'], meta['fps']
    if n < 4 * FADE or meta['hold'] < FADE:
        raise SystemExit('too few frames (%d, the last one held %d) to fade %d of them' % (n, meta['hold'], FADE))
    graph = ('fade=t=out:s=%(st)d:n=%(f)d:color=%(c)s,fade=t=in:s=0:n=%(f)d:color=%(c)s,split[s0][s1];'
             '[s0]palettegen=max_colors=256:stats_mode=full[pal];[s1][pal]paletteuse=dither=none:diff_mode=rectangle') % {'st': n - FADE, 'f': FADE, 'c': PAGE}
    cmd = ['ffmpeg', '-v', 'error', '-y', '-framerate', str(fps), '-i', os.path.join(frames, 'f%04d.png'), '-vf', graph, '-loop', '0', out]
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if r.returncode:
        raise SystemExit('ffmpeg failed (%d):\n%s' % (r.returncode, r.stdout))
    return n, fps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=os.path.join(ROOT, 'docs', 'images'))
    ap.add_argument('--port', type=int, default=8815, help='port of the board that is started')
    ap.add_argument('--lang', choices=('en', 'ko', 'both'), default='both', help='screen language of the GIF')
    ap.add_argument('--src', default=ROOT, help='folder with the server.py (and static/) to show (default: this repository)')
    ap.add_argument('--frames', help='keep the PNG frames in <folder>/<lang>/')
    args = ap.parse_args()
    if not shutil.which('ffmpeg'):
        raise SystemExit('ffmpeg is needed (it joins the frames into the GIF)')
    src = os.path.realpath(args.src)
    if not os.path.isfile(os.path.join(src, 'server.py')):
        raise SystemExit('no server.py in %s' % src)
    langs = ['en', 'ko'] if args.lang == 'both' else [args.lang]
    tmp = tempfile.TemporaryDirectory()
    proc = info = None
    done = {}
    try:
        home = os.path.join(tmp.name, 'acme-home')
        info = docs_scene.build(home)
        synth_home.start_live(info)
        proc = shoot_docs.start(info['home'], args.port, '세션 %s 읽음' % info['orch'], src)
        shoot_docs.wait_links(args.port, info)
        for lang in langs:
            frames = os.path.join(args.frames, lang) if args.frames else os.path.join(tmp.name, 'frames-' + lang)
            r = subprocess.run(['node', os.path.join(HERE, 'shoot_gif.js'), 'http://localhost:%d' % args.port, frames, lang])
            if r.returncode:
                raise SystemExit('shoot_gif.js failed (%d) for %s' % (r.returncode, lang))
            os.makedirs(os.path.join(args.out, lang), exist_ok=True)
            path = os.path.join(args.out, lang, 'office.gif')
            done[lang] = (path,) + encode(frames, path)
    finally:
        if proc:
            proc[0].terminate()
            try:
                proc[0].wait(10)
            except subprocess.TimeoutExpired:
                proc[0].kill()
            proc[1].close()
        if info:
            synth_home.stop_live(info['home'], quiet=True)
        tmp.cleanup()
    bad = 0
    for lang in langs:
        path, n, fps = done[lang]
        size = os.path.getsize(path)
        print('%-22s %4d KB  %d frames at %d fps = %.1f s%s' % (lang + '/office.gif', size // 1024, n, fps, float(n) / fps, '' if size <= LIMIT else '  > 3 MB'))
        bad += size > LIMIT
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
