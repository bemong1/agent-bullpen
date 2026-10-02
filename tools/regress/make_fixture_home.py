# Freeze one Claude session (+ its linked Codex rollouts and their guardian children) into a fixture HOME.
# usage: AB_SRC=/tmp/ab-old python3 make_fixture_home.py <session id> /tmp/ab-home
# Copies transcripts only (never ~/.codex/auth.json, sqlite, history.jsonl, ~/.claude/.credentials.json). Delete the fixture after use.
import sys, os, glob, shutil, json
sys.path.insert(0, os.environ.get('AB_SRC', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')))   # default: this repository
import server
sid, dst = sys.argv[1], os.path.abspath(sys.argv[2])
main = glob.glob(os.path.join(server.PROJECTS, '*', sid + '.jsonl'))[0]
proj = os.path.basename(os.path.dirname(main))
out = os.path.join(dst, '.claude', 'projects', proj)
os.makedirs(out, exist_ok=True)
shutil.copy2(main, out)
if os.path.isdir(main[:-6]):
    shutil.copytree(main[:-6], os.path.join(out, sid), dirs_exist_ok=True)
server.LINKS.scan()
tids = {t for t, o in server.LINKS.owners.items() if o['sid'] == sid}
tids |= {e['id'] for e in server.CODEX.entries() if e['parent'] in tids}          # guardian children
n = 0
for e in server.CODEX.entries():
    if e['id'] in tids:
        rel = os.path.relpath(e['path'], server.CODEX_SESSIONS)
        os.makedirs(os.path.join(dst, '.codex', 'sessions', os.path.dirname(rel)), exist_ok=True)
        shutil.copy2(e['path'], os.path.join(dst, '.codex', 'sessions', rel)); n += 1
print('fixture %s: session %s, %d Codex rollouts' % (dst, sid[:8], n))
