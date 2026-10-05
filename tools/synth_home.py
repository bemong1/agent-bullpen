#!/usr/bin/env python3
"""Build a synthetic HOME for screenshots, CI and tests. No real records are read or copied; every name is generic (acme, demo).

    python3 tools/synth_home.py /tmp/acme-home            # write the HOME
    python3 tools/synth_home.py /tmp/acme-home --live     # + fake `claude` processes so the sessions show as working
    python3 tools/synth_home.py /tmp/acme-home --busy --live   # the large scene (acme-robot): what the screenshots in docs/images show
    python3 tools/synth_home.py /tmp/acme-home --busy --live --links   # + `claude -p` children the board can only guess or could not link (UI: guess mark, "not linked" box)
    python3 tools/synth_home.py /tmp/acme-home --busy --live --stopped  # + work that stopped: a usage limit, API errors, runs cut off, a paused debate cell, grandchildren, diagnostics
    python3 tools/synth_home.py /tmp/acme-home --codex-orch --live     # + a Codex orchestrator: native sub-agents, `claude -p` and `codex exec` runs started from its shell, a small debate
    python3 tools/synth_home.py /tmp/acme-home --stop     # stop the fake processes and remove the registration files this tool wrote (only in a folder it made)
    env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME=/tmp/acme-home python3 server.py --port 8811

All times are relative to "now" (the moment this runs), so the screen reads "recent". Re-run it before looking at the board again.
Inside the HOME:
  .claude/projects/<acme-app>/   an orchestrator session: debate folder (common brief + two topics, r1/r2 reports, one topic closed by
                                 rulings.md, one still running), four sub-agents (meta + transcript), a Codex cross-check launched by Bash
  .claude/projects/<demo-notes>/ a plain conversation with no sub-agents (solo)
  .codex/sessions/Y/M/D/         the Codex rollout that pairs with `codex exec -o .../r1/C.md` (round 2 continues it with `exec resume`)
  .claude.json                   plan tier + usage cache (no account id or e-mail)
  work/acme-app/docs/review/     the debate folder
Start the board with HOME=<folder> (as above), not with --claude-config-dir/--codex-home alone: prompts and messages in the records say ~/work/...,
which the server expands with the HOME it runs under; with a real HOME the debate splits into extra, empty ones.
The folder path must be plain ([A-Za-z0-9_./+-], no dot-folders): records hold absolute paths and /api/file refuses dot-paths.
"""
import argparse
import calendar
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.scenarios import cx_record                    # the one place the shape of a Codex rollout lives (stdlib only, imports nothing of the board)

MARK = '.synth-home'                      # written into a folder we made, so a re-run may replace it (and nothing else)
MARK_TEXT = 'synthetic HOME made by tools/synth_home.py; safe to delete\n'
ORCH = 'a11ce000-0000-4000-8000-000000000001'
SOLO = '5010a000-0000-4000-8000-000000000002'
CODEX_THREAD = '019a7c0d-3e1b-7c2a-9d41-5f6a1b2c3d4e'
CODEX_MODEL, CLAUDE_MODELS = 'gpt-6.1-sol', {'opus': 'claude-opus-5-5', 'sonnet': 'claude-sonnet-5-5'}
LIVE_SECONDS = 4 * 3600                   # the fake processes end by themselves
LIVE_FILE = '.synth-live.json'            # --busy --live: the fake codex processes (they have no ~/.claude/sessions file)
OWN = ('.claude', '.codex', '.claude.json', 'work', MARK, LIVE_FILE)
# tag, description, model, topic, letter
SPEC = [('T1-A', 'Compatibility', 'opus', 't1_env', 'A'), ('T1-B', 'Ergonomics', 'sonnet', 't1_env', 'B'),
        ('T2-A', 'Backoff design', 'opus', 't2_retry', 'A'), ('T2-B', 'Failure modes', 'sonnet', 't2_retry', 'B')]
R1 = {'T1-A': (113.9, 105.0), 'T1-B': (113.8, 104.0), 'T2-A': (113.7, 103.0), 'T2-B': (113.6, 101.0)}     # round 1 start, end (minutes ago)
T1_R2 = {'A': (96.9, 91.0), 'B': (96.8, 89.5)}                                                              # T1 round 2 start, end
COORD = 'The coordinator sent a message while you were working:'


def hx(*parts, n=16):
    return hashlib.sha1(':'.join(parts).encode()).hexdigest()[:n]


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + '.%03dZ' % int((t % 1) * 1000)


def dump(d):
    return json.dumps(d, separators=(',', ':'), ensure_ascii=False)     # like the real records: no spaces (the readers look at bytes first)


def put(path, text, t=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    if t:
        os.utime(path, (t, t))
    return path


class Jsonl:
    """One .jsonl file: rows are (time, dict); saved in time order, mtime = the last row."""

    def __init__(self, path):
        self.path, self.rows = path, []

    def add(self, t, d):
        self.rows.append((t, len(self.rows), d))

    def rows_into(self, t):
        """A list that behaves like the `into` of a row writer: whatever is appended to it becomes a row of this file at time t."""
        outer = self

        class Rows(list):
            def append(self, d):
                outer.add(t, d)
        return Rows()

    def save(self):
        rows = sorted(self.rows, key=lambda r: r[:2])
        put(self.path, ''.join(dump(d) + '\n' for _, _, d in rows), rows[-1][0] if rows else None)


class CxThread:
    """One Codex `exec` thread of a scene, written in the shapes of the real records (tools/scenarios/cx_record.py): each shell command is a call of the exec tool, its output and,
    when its process ended, a `CommandExecution` item that holds the command; a turn that ended has its `task_complete`, one that is still going has none (and a call that has not
    returned has no `CommandExecution` yet)."""

    def __init__(self, path, tid, cwd, t0, version='0.157.0'):
        self.r = cx_record.Rollout(path, tid, cwd, 'synth:' + tid, origin='codex_exec', version=version)
        self.r.meta(t0)
        self.tid, self.cwd, self.n, self.turn_id, self.started = tid, cwd, 0, None, None

    def turn(self, t, prompt):
        """A turn starts: its `task_started`, the turn's context and the user's message."""
        self.n += 1
        self.turn_id = 'turn-' + hx(self.tid, str(self.n), n=16)
        self.started = t
        self.r.task_started(t, self.turn_id)
        self.r.turn_context(t + 0.1, self.turn_id)
        self.r.user(t + 0.2, prompt, self.turn_id)

    def say(self, t, text):
        self.r.say(t, text, self.turn_id)

    def command(self, t, cmd, output='', read=None, cwd=None, secs=0.6, call_id=None):
        """A shell command called at `t` that ran for `secs`. `read`: a file the command only reads (Codex's own parsing of it says so), its path as the command names it."""
        parsed = [{'type': 'read', 'cmd': cmd, 'name': os.path.basename(read), 'path': read}] if read else None
        self.r.shell(t, cmd, cwd or self.cwd, call_id or 'call_' + hx(self.tid, cmd, str(t), n=16), self.turn_id, end=t + secs, out_at=t + secs + 0.2, text=output, parsed=parsed)

    def running(self, t, cmd):
        """A call that has not returned: the turn is still going, and there is no `CommandExecution` for it yet."""
        self.r.shell(t, cmd, self.cwd, 'call_' + hx(self.tid, 'open', n=16), self.turn_id, end=None, out_at=None)

    def tokens(self, t, inp, out, cached, reasoning, rates, n):
        self.r.usage(t, self.turn_id, inp, out, cached, reasoning, rates, 'resp_' + hx(self.tid, str(n), n=12))

    def finish(self, t, text):
        self.r.complete(t, self.turn_id, text, started=self.started)

    def save(self):
        return self.r.save()


def rate_limits(used, resets):
    """The account's `rate_limits` of a `token_count` line."""
    return {'limit_id': 'codex', 'primary': {'used_percent': used, 'window_minutes': 10080, 'resets_at': int(resets)}, 'secondary': None,
            'credits': {'has_credits': False, 'unlimited': False, 'balance': None}, 'plan_type': 'pro', 'rate_limit_reached_type': None}


# ---------- text of the debate folder ----------
BRIEF = '''# Config loader review (acme-app v0.8)

Common brief for every participant. Two independent topics; each has its own folder, topic brief and reports.

| Topic | Folder | Depends on | Final |
|---|---|---|---|
| T1 Env override naming | `t1_env/` | — | |
| T2 Retry policy | `t2_retry/` | — | |

## Rules
- Round 1: write your report alone. Do not read other participants' reports.
- Round 2: read the others' round-1 reports, then write `r2/<you>.md`: what you now accept, what you still contest.
- Reports are markdown, at most 40 lines. Cite file paths and line numbers, not impressions.
- The orchestrator writes the topic's `rulings.md` once round 2 is in.
'''
TOPIC_BRIEF = {
    't1_env': '''# T1 Env override naming

How should environment variables override keys in `config.toml`? Today the loader accepts flat names (`ACME_DB_HOST`);
the proposal is nested names (`ACME_DB__HOST`).

Participants:
- **A — Compatibility**: what breaks for existing deployments
- **B — Ergonomics**: what is easiest to write, read and script
- **C — Cross-check**: run both schemes against the loader's own tests (Codex)

Reports: `r1/<id>.md`, `r2/<id>.md`.
''',
    't2_retry': '''# T2 Retry policy

How should the loader retry when it fetches remote config? Today: three fixed 1 s retries, no cap on total wait.

Participants:
- **A — Backoff design**: delay curve, jitter, attempt limit
- **B — Failure modes**: what a retry can make worse (thundering herd, hidden outages, slow tests)

Reports: `r1/<id>.md`, `r2/<id>.md`.
''',
}
REPORTS = {
    ('t1_env', 1, 'A'): '''# T1-A · round 1 — Compatibility

**Position:** keep flat names (`ACME_DB_HOST`); add nesting only through an explicit separator.

- 14 flat variables are already exported by the deployment guide (`docs/ops/env.md:12-40`).
- A hard switch to `__` nesting breaks every one of them on upgrade.
- Proposal: accept both for one release and log a deprecation warning for the flat form.

| Option | Breaks existing env | Ambiguity |
|---|---|---|
| flat `ACME_DB_HOST` | no | high (`DB_HOST` vs `DB` + `HOST`) |
| nested `ACME_DB__HOST` | yes | none |

**Risk:** keys that contain `_` themselves (`retry_max`) cannot be flattened without guessing.
''',
    ('t1_env', 1, 'B'): '''# T1-B · round 1 — Ergonomics

**Position:** nested names. `ACME_<SECTION>__<KEY>` reads like the TOML it overrides.

- One rule, no lookup table: `ACME_DB__POOL__SIZE` is `db.pool.size`.
- `.env` files and shell scripts stay greppable; flat names collide as soon as a section grows (`src/config/loader.py:88`).
- The prefix `ACME_` should be mandatory so unrelated variables are never picked up.

**Cost:** a migration note and one release of overlap. I would not keep the flat form longer than that.
''',
    ('t1_env', 1, 'C'): '''# T1-C · round 1 — Cross-check

Ran `pytest tests/config -q` against both schemes with six representative keys.

| Key | flat | nested |
|---|---|---|
| `db.host` | ok | ok |
| `db.pool_size` | **ambiguous** | ok |
| `retry.max_attempts` | **ambiguous** | ok |
| `log.level` | ok | ok |
| `cache.ttl` | ok | ok |
| `auth.token_file` | ok | ok |

Flat parsing is ambiguous for 2 of 6 keys; nested parses all six. Recommendation: nested as canonical, flat as a read-only alias.
''',
    ('t1_env', 2, 'A'): '''# T1-A · round 2 — Compatibility

**Accepted:** nested is the canonical form (C's table settles the ambiguity point).

**Still contested:** the alias window. One release is the minimum; `docs/ops/env.md` needs a migration section before v0.8 ships.

- Longest-prefix match for flat names, with a single warning per variable.
- If both forms are set, the nested one wins.
''',
    ('t1_env', 2, 'B'): '''# T1-B · round 2 — Ergonomics

**Accepted:** one release of flat aliases, warning once per variable (A's proposal).

**Added:** the warning should name the replacement (`ACME_DB_HOST` → `ACME_DB__HOST`) so nobody has to read the docs.

No remaining objections.
''',
    ('t1_env', 2, 'C'): '''# T1-C · round 2 — Cross-check

Read A and B round 1. No new objections.

- The flat alias must be read-only: never write a flat name back when the loader dumps its effective config.
- Added a test for "both forms set, nested wins" (`tests/config/test_env_override.py`).
''',
    ('t2_retry', 1, 'A'): '''# T2-A · round 1 — Backoff design

**Position:** exponential backoff with full jitter.

- Base 200 ms, factor 2, cap 10 s, at most 5 attempts.
- Full jitter spreads clients that fail together, so a recovering config service is not hit by a wave.
- Today's fixed 1 s retry has no cap on total wait (`src/config/remote.py:41`).
''',
    ('t2_retry', 1, 'B'): '''# T2-B · round 1 — Failure modes

**Position:** retry only idempotent fetches, stop early, and say so.

- A retry that succeeds silently hides a flapping config service; log every retry at WARNING.
- Cap the *total* wait (15 s), not only the delay: five attempts at the 10 s cap is 50 s of a blocked start-up.
- Jitter makes tests flaky unless the random source is injectable.
''',
    ('t2_retry', 2, 'A'): '''# T2-A · round 2 — Backoff design

**Accepted:** a total-wait budget of 15 s on top of the per-attempt cap, and WARNING logs on every retry.

**Kept:** full jitter. To answer B's test concern the random source is injectable (`rng=` argument, default `random.random`).

Ask for T2-B: please check clock-skew handling when the budget is measured with `time.time()`; I used `time.monotonic()`.
''',
}
FINAL_MSG = {
    ('t1_env', 1, 'A'): 'Round 1 written to r1/A.md. Position: keep flat names, nesting only through an explicit separator; accept both for one release.',
    ('t1_env', 1, 'B'): 'Round 1 written to r1/B.md. Position: nested `ACME_<SECTION>__<KEY>`, prefix mandatory, one release of overlap at most.',
    ('t1_env', 2, 'A'): 'Round 2 written to r2/A.md. I accept nested as canonical; I still want a migration section in docs/ops/env.md.',
    ('t1_env', 2, 'B'): 'Round 2 written to r2/B.md. I accept one release of flat aliases; the warning should name the replacement.',
    ('t2_retry', 1, 'A'): 'Round 1 written to r1/A.md. Position: exponential backoff with full jitter, base 200 ms, cap 10 s, 5 attempts.',
    ('t2_retry', 1, 'B'): 'Round 1 written to r1/B.md. Position: retry only idempotent fetches, cap total wait at 15 s, log every retry.',
    ('t2_retry', 2, 'A'): 'Round 2 written to r2/A.md. I accept the 15 s total budget and WARNING logs; jitter stays, with an injectable random source.',
}
RULINGS = '''# T1 rulings — env override naming

1. **Canonical form:** `ACME_<SECTION>__<KEY>` (double underscore = nesting). The `ACME_` prefix is mandatory.
2. **Compatibility:** flat names are still read for one release (v0.8 and v0.9). The loader logs one warning per variable that names the replacement.
3. **Conflicts:** if both forms are set, the nested one wins. Flat aliases are never written back.
4. **Docs:** `docs/ops/env.md` gets a migration section before v0.8 ships.
5. **Out of scope:** list-valued variables (tracked for v0.9).

Open items: none. Sign-off: T1-A, T1-B, C.
'''
LOADER_SNIPPET = '''def load_env(environ, prefix='ACME_'):
    out = {}
    for name, value in environ.items():
        if name.startswith(prefix):
            out[name[len(prefix):].lower()] = value   # flat: DB_HOST -> db_host
    return out
'''

CODEX_TURNS = {
    1: ('T1 cross-check: env override naming\nYou are participant C in the T1 review of acme-app. Read the topic brief at %(unit)s/brief.md, '
        'test both naming schemes against the loader tests, and answer with your round-1 report in at most 40 lines of markdown.'),
    2: ('Round 2 of T1\nRead r1/A.md and r1/B.md in %(unit)s, say what you still contest, and answer with your round-2 report '
        'in at most 40 lines of markdown.'),
}


class Synth:
    def __init__(self, home, now):
        self.home, self.now = home, now
        self.claude, self.codex = os.path.join(home, '.claude'), os.path.join(home, '.codex')
        self.work = os.path.join(home, 'work', 'acme-app')
        self.review = os.path.join(self.work, 'docs', 'review')
        self.unit = {k: os.path.join(self.review, k) for k in TOPIC_BRIEF}
        self.agents = {tag: 'a' + hx('acme', tag) for tag, *_ in SPEC}                       # tag -> agent id
        self.spawn_use = {tag: 'toolu_' + hx('spawn', tag, n=24) for tag, *_ in SPEC}        # tag -> tool_use id of the Agent call
        self.sid = ORCH
        self.subs = {}
        self.codex_calls = {}             # round -> (tool_use id, background task id, time)

    PLAN = ('default_claude_max_5x', 34.0, 52.0, 18.0)      # tier, five-hour %, weekly %, weekly Sonnet %
    CODEX_USED = 27.0                                       # Codex weekly %

    def m(self, x):
        """x minutes ago."""
        return self.now - x * 60

    def tilde(self, path):
        """Paths inside prompts and messages are written as ~/work/... (the board expands ~ with HOME); tool inputs keep absolute paths like the real records."""
        return '~' + path[len(self.home):]

    def proj(self, cwd):
        return os.path.join(self.claude, 'projects', re.sub(r'[^A-Za-z0-9]', '-', cwd))

    @staticmethod
    def usage(k, base=24000):
        return {'input_tokens': 4 + k % 3, 'cache_creation_input_tokens': 900 + (k * 37) % 400,
                'cache_read_input_tokens': base + 1800 * k, 'output_tokens': 220 + (k * 53) % 600}

    # ---------- Claude transcript lines ----------
    @staticmethod
    def line(t, typ, cwd, sid, **kw):
        d = {'type': typ, 'timestamp': iso(t), 'cwd': cwd, 'sessionId': sid}
        d.update(kw)
        return d

    def assistant(self, log, t, k, key, cwd, sid, model, content, stop=None, effort='high', **kw):
        msg = {'id': 'msg_' + hx(key, str(k), n=24), 'model': model, 'role': 'assistant', 'content': content, 'stop_reason': stop, 'usage': self.usage(k)}
        log.add(t, self.line(t, 'assistant', cwd, sid, effort=effort, message=msg, **kw))

    def result(self, log, t, cwd, sid, tool_id, text, **kw):
        log.add(t, self.line(t, 'user', cwd, sid, message={'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tool_id, 'content': text}]}, **kw))

    def build(self):
        self.write_debate_folder()
        self.write_orchestrator()
        for tag, desc, model, key, who in SPEC:
            self.write_round1(tag, desc, model, key, who)
        for tag, desc, model, key, who in SPEC[:2]:
            self.write_round2_t1(tag, model, key, who)
        for tag, desc, model, key, who in SPEC[2:]:
            self.write_round2_t2(tag, model, key, who)
        for s in self.subs.values():
            s.save()
        self.write_codex()
        self.write_solo()
        self.write_json()

    def write_debate_folder(self):
        put(os.path.join(self.review, 'brief.md'), BRIEF, self.m(115.4))
        for k, text in TOPIC_BRIEF.items():
            put(os.path.join(self.unit[k], 'brief.md'), text, self.m(115.1))
        put(os.path.join(self.work, 'src', 'config', 'loader.py'), LOADER_SNIPPET, self.m(600))
        put(os.path.join(self.work, 'docs', 'ops', 'env.md'), '# Environment\n\nFlat variables: ACME_DB_HOST, ACME_DB_PORT, ...\n', self.m(600))

    # ---------- the orchestrator's own transcript ----------
    def write_orchestrator(self):
        m, w = self.m, self.work
        main = Jsonl(os.path.join(self.proj(w), ORCH + '.jsonl'))
        k = [0]

        def say(t, text, stop=None, extra=()):
            k[0] += 1
            self.assistant(main, t, k[0], ORCH, w, ORCH, CLAUDE_MODELS['opus'], [{'type': 'text', 'text': text}] + list(extra), stop)

        def tool(t, name, inp, out, tid=None, **kw):
            k[0] += 1
            tid = tid or 'toolu_' + hx('orch', str(k[0]), n=24)
            self.assistant(main, t, k[0], ORCH, w, ORCH, CLAUDE_MODELS['opus'], [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}], 'tool_use')
            self.result(main, t + 1.0, w, ORCH, tid, out, **kw)

        def human(t, text):
            main.add(t, self.line(t, 'user', w, ORCH, origin={'kind': 'human'}, message={'role': 'user', 'content': text}))

        def turn_end(t, pending):
            main.add(t, self.line(t, 'system', w, ORCH, subtype='turn_duration', durationMs=30000 + pending * 4000, pendingBackgroundAgentCount=pending))

        def handback(t, tag, text):
            body = 'Subagent hand-back from %s.\nThe report follows:\n%s' % (tag, '\n'.join('  ' + ln for ln in text.splitlines()))
            main.add(t, self.line(t, 'user', w, ORCH, origin={'kind': 'peer', 'from': self.agents[tag], 'handback': True, 'body': body},
                                  message={'role': 'user', 'content': body}))

        def notify(t, tag, desc, tokens, tools, minutes):
            text = ('<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>completed</status>\n'
                    '<summary>Agent "%s %s" completed</summary>\n<usage><subagent_tokens>%d</subagent_tokens><tool_uses>%d</tool_uses>'
                    '<duration_ms>%d</duration_ms></usage>\n</task-notification>') % (self.agents[tag], self.spawn_use[tag], tag, desc, tokens, tools, int(minutes * 60000))
            main.add(t, self.line(t, 'user', w, ORCH, origin={'kind': 'task-notification'}, message={'role': 'user', 'content': text}))

        def codex_notify(t, rnd):
            use, bg, _ = self.codex_calls[rnd]
            text = ('<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>completed</status>\n'
                    '<summary>Background command "Codex C: T1 round %d" completed (exit code 0)</summary>\n</task-notification>' % (bg, use, rnd))
            main.add(t, self.line(t, 'user', w, ORCH, origin={'kind': 'task-notification'}, message={'role': 'user', 'content': text}))

        def codex_launch(rnd, t):
            unit = self.tilde(self.unit['t1_env'])
            out, prompt = '%s/r%d/C.md' % (unit, rnd), CODEX_TURNS[rnd] % {'unit': unit}
            cmd = ('codex exec -m %s -o %s "%s"' % (CODEX_MODEL, out, prompt)) if rnd == 1 else ('codex exec resume %s -o %s "%s"' % (CODEX_THREAD, out, prompt))
            use, bg = 'toolu_' + hx('codex', str(rnd), n=24), 'b' + hx('bg', str(rnd), n=8)
            tool(t, 'Bash', {'command': cmd, 'description': 'Codex C: T1 round %d' % rnd, 'run_in_background': True},
                 'Command running in background with ID: %s' % bg, tid=use, toolUseResult={'backgroundTaskId': bg})
            self.codex_calls[rnd] = (use, bg, t)

        # --- the conversation ---
        human(m(118), 'Before we ship acme-app v0.8 I want the config loader reviewed. Two topics: T1 how environment variables '
                      'override config keys, T2 the retry policy for remote config fetches. Use independent reviewers and keep a '
                      'written record under docs/review.')
        main.add(m(117.8), {'type': 'ai-title', 'aiTitle': 'Config loader review (acme-app v0.8)'})
        qid, q = 'toolu_' + hx('ask', n=24), 'How many rounds should each topic run?'
        say(m(117.5), 'Plan: two topics. Reviewers write round-1 reports without seeing each other\'s, then a cross-read round. '
                      'T1 gets two Claude reviewers plus a Codex cross-check; T2 gets two Claude reviewers. One question first.',
            'tool_use', [{'type': 'tool_use', 'id': qid, 'name': 'AskUserQuestion', 'input': {'questions': [{
                'header': 'Rounds', 'question': q, 'multiSelect': False, 'options': [
                    {'label': 'Two rounds (Recommended)', 'description': 'Independent reports, then one cross-read round'},
                    {'label': 'One round', 'description': 'Independent reports only'}]}]}}])
        self.result(main, m(116), w, ORCH, qid, 'User has answered your questions: "%s"="Two rounds (Recommended)".' % q,
                    toolUseResult={'answers': {q: 'Two rounds (Recommended)'}})
        say(m(115.9), 'Two rounds it is. Writing the briefs now.')
        tool(m(115.8), 'Write', {'file_path': os.path.join(self.review, 'brief.md'), 'content': BRIEF}, 'File created')
        for key, ago in (('t1_env', 115.3), ('t2_retry', 115.2)):
            tool(m(ago), 'Write', {'file_path': os.path.join(self.unit[key], 'brief.md'), 'content': TOPIC_BRIEF[key]}, 'File created')
        say(m(114.6), 'Briefs are in docs/review/. Launching T1-A, T1-B, T2-A, T2-B and the Codex cross-check for T1.')
        for i, (tag, desc, model, key, who) in enumerate(SPEC):
            tool(m(114.5 - i * 0.05), 'Agent', {'description': '%s %s' % (tag, desc), 'subagent_type': 'general-purpose', 'model': model,
                                                'run_in_background': True, 'prompt': self.spawn_prompt(tag, desc, key, who)},
                 'Async agent launched successfully.\nagentId: %s' % self.agents[tag], tid=self.spawn_use[tag],
                 toolUseResult={'isAsync': True, 'status': 'async_launched', 'agentId': self.agents[tag]})
        codex_launch(1, m(114.2))
        turn_end(m(113.5), 5)

        # --- round 1 comes in ---
        for tag, desc, model, key, who in SPEC:
            start, end = R1[tag]
            handback(m(end - 0.2), tag, FINAL_MSG[(key, 1, who)])
            notify(m(end - 0.6), tag, desc, 38000 + 900 * len(desc) + int(end) * 40, 8, start - end)
        codex_notify(m(106.4), 1)
        say(m(97.5), 'Round 1 is in for both topics. T1: A (flat names) and B (nested names) disagree, and C\'s test run favours nested, so '
                     'round 2 goes ahead. T2: A and B differ on jitter; I will hold T2 round 2 until I know whether you have a preference.')
        for tag, desc, model, key, who in SPEC[:2]:
            tool(m(97.0), 'SendMessage', {'to': self.agents[tag], 'summary': 'Round 2: cross-read and answer',
                                          'message': self.round2_message(key, who)}, 'Message sent')
        codex_launch(2, m(96.8))
        turn_end(m(96.5), 3)
        for tag, desc, model, key, who in SPEC[:2]:
            start, end = T1_R2[who]
            handback(m(end - 0.2), tag, FINAL_MSG[(key, 2, who)])
            notify(m(end - 0.6), tag, desc, 61000 + 700 * len(desc), 6, 97.0 - end)
        codex_notify(m(88.2), 2)
        tool(m(83.8), 'Write', {'file_path': os.path.join(self.unit['t1_env'], 'rulings.md'), 'content': RULINGS}, 'File created')
        put(os.path.join(self.unit['t1_env'], 'rulings.md'), RULINGS, m(83.5))
        say(m(83.2), 'T1 is settled: nested names with a one-release flat alias (docs/review/t1_env/rulings.md). T2 is waiting for round 2; '
                     'do you have a preference on jitter before I start it?')
        turn_end(m(83.0), 0)
        human(m(36), 'Go with jitter unless T2-B finds a concrete problem with it. Keep round 2 short.')
        say(m(35.6), 'Understood - starting T2 round 2 with jitter as the default.')
        for tag, desc, model, key, who in SPEC[2:]:
            tool(m(35.5), 'SendMessage', {'to': self.agents[tag], 'summary': 'Round 2 with jitter as default',
                                          'message': self.round2_message(key, who) + ' The user prefers jitter unless you can name a concrete problem with it. Keep it short.'},
                 'Message sent')
        handback(m(12.0 - 0.2), 'T2-A', FINAL_MSG[('t2_retry', 2, 'A')])
        notify(m(12.0 - 0.6), 'T2-A', 'Backoff design', 52000, 7, 35.5 - 12.0)
        say(m(11.6), 'T2-A has submitted round 2 and accepts a total-wait budget. Waiting for T2-B before drafting the T2 rulings.')
        main.save()

    def spawn_prompt(self, tag, desc, key, who):
        unit, review = self.tilde(self.unit[key]), self.tilde(self.review)
        return ('You are %s (%s) in the %s review of acme-app\'s config loader.\n\nRead these first:\n- %s/brief.md (common brief)\n- %s/brief.md (topic brief)\n\n'
                'Round 1: write your independent report to %s/r1/%s.md (at most 40 lines). Do not read the other participants\' reports in round 1.\n'
                'When you are done, finish with a one-paragraph final report.' % (tag, desc, key.split('_')[0].upper(), review, unit, unit, who))

    def round2_message(self, key, who):
        unit, other = self.tilde(self.unit[key]), 'B' if who == 'A' else 'A'
        files = '%s/r1/%s.md and %s/r1/C.md' % (unit, other, unit) if key == 't1_env' else '%s/r1/%s.md' % (unit, other)
        return 'Round 2: read %s, then write %s/r2/%s.md - what you now accept and what you still contest.' % (files, unit, who)

    # ---------- sub-agents ----------
    def sub(self, tag, desc, model):
        if tag not in self.subs:
            self.subs[tag] = SubWriter(self, tag, desc, model)
        return self.subs[tag]

    def write_round1(self, tag, desc, model, key, who):
        s, m, unit = self.sub(tag, desc, model), self.m, self.unit[key]
        start, end = R1[tag]
        s.user(m(start), self.spawn_prompt(tag, desc, key, who))
        s.say(m(start - 0.3), 'Reading both briefs and the loader before forming a position.')
        s.tool(m(start - 0.5), 'Read', {'file_path': os.path.join(self.review, 'brief.md')}, BRIEF)
        s.tool(m(start - 0.7), 'Read', {'file_path': os.path.join(unit, 'brief.md')}, TOPIC_BRIEF[key])
        s.tool(m(start - 1.1), 'Read', {'file_path': os.path.join(self.work, 'src', 'config', 'loader.py')}, LOADER_SNIPPET)
        s.tool(m(start - 2.0), 'Grep', {'pattern': 'environ|getenv|retry', 'path': os.path.join(self.work, 'src')}, 'src/config/loader.py:7\nsrc/config/remote.py:41')
        s.tool(m(start - 3.5), 'Read', {'file_path': os.path.join(self.work, 'docs', 'ops', 'env.md')}, '# Environment\n\nFlat variables: ACME_DB_HOST, ACME_DB_PORT, ...')
        s.say(m(end + 3.0), 'Position is forming; writing the report now.')
        s.write_report(m(end + 1.0), os.path.join(unit, 'r1', who + '.md'), REPORTS[(key, 1, who)])
        if tag == 'T1-A':
            s.tool(m(end + 0.6), 'SendMessage', {'to': 'T1-B', 'summary': 'Heads-up on flat names',
                                                 'message': 'I am arguing for keeping flat names. Your nested proposal is fine with me if flat aliases stay readable for a release. '
                                                            'Reply only if you see a blocker.'}, 'Message sent')
        if tag == 'T2-B':
            s.tool(m(end + 0.6), 'SendMessage', {'to': 'orchestrator', 'summary': 'Total-wait cap matters more than jitter',
                                                 'message': 'Early note: my main concern is the total wait, not jitter. Details in r1/B.md.'}, 'Message sent')
        s.end(m(end), FINAL_MSG[(key, 1, who)])

    def write_round2_t1(self, tag, model, key, who):
        s, m, unit = self.subs[tag], self.m, self.unit[key]
        other = 'B' if who == 'A' else 'A'
        start, end = T1_R2[who]
        s.user(m(start - 0.1), COORD + ' ' + self.round2_message(key, who))
        s.tool(m(start - 0.5), 'Read', {'file_path': os.path.join(unit, 'r1', other + '.md')}, REPORTS[(key, 1, other)])
        s.tool(m(start - 0.9), 'Read', {'file_path': os.path.join(unit, 'r1', 'C.md')}, REPORTS[(key, 1, 'C')])
        s.say(m(end + 2.5), 'The table settles the ambiguity question; writing what I accept and what I still contest.')
        s.write_report(m(end + 0.8), os.path.join(unit, 'r2', who + '.md'), REPORTS[(key, 2, who)])
        s.end(m(end), FINAL_MSG[(key, 2, who)])

    def write_round2_t2(self, tag, model, key, who):
        s, m, unit = self.subs[tag], self.m, self.unit[key]
        other = 'B' if who == 'A' else 'A'
        s.user(m(35.4), COORD + ' ' + self.round2_message(key, who) +
               ' The user prefers jitter unless you can name a concrete problem with it. Keep it short.')
        s.tool(m(35.0), 'Read', {'file_path': os.path.join(unit, 'r1', other + '.md')}, REPORTS[(key, 1, other)])
        if who == 'A':
            s.say(m(15.0), 'B\'s total-wait point is right; adding a budget and keeping jitter with an injectable random source.')
            s.write_report(m(13.0), os.path.join(unit, 'r2', 'A.md'), REPORTS[(key, 2, 'A')])
            s.tool(m(12.6), 'SendMessage', {'to': 'T2-B', 'summary': 'Clock-skew check on the budget',
                                            'message': 'Round 2 is in r2/A.md. Please check the budget logic for clock skew: I measure it with time.monotonic().'}, 'Message sent')
            s.end(m(12.0), FINAL_MSG[(key, 2, 'A')])
        else:
            s.say(m(30.0), 'A\'s jitter proposal is acceptable if the random source is injectable; checking A\'s round 2 before I write mine.')
            s.tool(m(0.7), 'Read', {'file_path': os.path.join(unit, 'r2', 'A.md')}, None)     # no result yet: B is mid-read

    # ---------- Codex ----------
    def write_codex(self):
        tid, unit, m = CODEX_THREAD, self.unit['t1_env'], self.m
        tu = self.tilde(unit)
        t0 = self.codex_calls[1][2] + 2.4                    # the rollout starts ~2.4 s after the Bash call
        lt = time.localtime(t0)
        path = os.path.join(self.codex, 'sessions', '%04d' % lt.tm_year, '%02d' % lt.tm_mon, '%02d' % lt.tm_mday,
                            'rollout-%s-%s.jsonl' % (time.strftime('%Y-%m-%dT%H-%M-%S', lt), tid))
        th = CxThread(path, tid, self.work, t0)
        rates = rate_limits(self.CODEX_USED, self.now + 3.4 * 86400)

        def tokens(t, n, cached):
            th.tokens(t, 14000 + 2600 * n, 700 + 120 * n, cached, 400 + 60 * n, rates, n)

        def finish(t, rnd):
            text = REPORTS[('t1_env', rnd, 'C')]
            th.finish(t, text)
            put(os.path.join(unit, 'r%d' % rnd, 'C.md'), text, t + 0.3)     # `-o` writes the last message verbatim: the board matches it by sha1

        t = t0 + 0.2
        th.turn(t, CODEX_TURNS[1] % {'unit': tu})
        th.say(t + 3, 'Reading the topic brief, then running the loader tests against both naming schemes.')
        th.command(t + 5, 'sed -n 1,60p %s/brief.md' % tu, '# T1 Env override naming ...')
        tokens(t + 20, 1, 9000)
        th.command(t + 60, 'python -m pytest tests/config -q -k "flat or nested"', '12 passed in 0.9s', secs=0.9)
        tokens(t + 90, 2, 21000)
        finish(m(106.5), 1)
        t2 = self.codex_calls[2][2] + 1.2                    # round 2: `codex exec resume` continues the same rollout
        th.turn(t2, CODEX_TURNS[2] % {'unit': tu})
        th.command(t2 + 3, 'cat r1/A.md', REPORTS[('t1_env', 1, 'A')], read='r1/A.md', cwd=unit)
        th.command(t2 + 8, 'cat r1/B.md', REPORTS[('t1_env', 1, 'B')], read='r1/B.md', cwd=unit)
        tokens(t2 + 30, 3, 30000)
        th.say(t2 + 40, 'No new objections; checking the alias is never written back.')
        th.command(t2 + 120, 'pytest tests/config/test_env_override.py -q', '3 passed in 0.4s', secs=0.4)
        tokens(t2 + 400, 4, 52000)
        finish(m(88.4), 2)
        th.save()
        put(os.path.join(self.codex, 'session_index.jsonl'), dump({'id': tid, 'thread_name': 'T1 round 1 cross-check', 'updated_at': iso(m(88.4))}) + '\n', m(88.4))

    # ---------- the plain conversation ----------
    def write_solo(self):
        cwd, m = os.path.join(self.home, 'work', 'demo-notes'), self.m
        put(os.path.join(cwd, 'src', 'demo_notes', 'dates.py'), 'def week_start(d):\n    return d - timedelta(days=d.weekday() + 1)\n', m(900))
        log, k, model = Jsonl(os.path.join(self.proj(cwd), SOLO + '.jsonl')), [0], CLAUDE_MODELS['sonnet']

        def human(t, text):
            log.add(t, self.line(t, 'user', cwd, SOLO, origin={'kind': 'human'}, message={'role': 'user', 'content': text}))

        def say(t, text, stop=None):
            k[0] += 1
            self.assistant(log, t, k[0], SOLO, cwd, SOLO, model, [{'type': 'text', 'text': text}], stop, effort='medium')

        def tool(t, name, inp, out):
            k[0] += 1
            tid = 'toolu_' + hx('solo', str(k[0]), n=24)
            self.assistant(log, t, k[0], SOLO, cwd, SOLO, model, [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}], 'tool_use', effort='medium')
            self.result(log, t + 1.2, cwd, SOLO, tid, out)

        human(m(24), 'tests/test_dates.py fails on Sundays only. Can you find out why?')
        log.add(m(23.8), {'type': 'ai-title', 'aiTitle': 'Fix Sunday-only date parsing test'})
        say(m(23.6), 'Let me run the test and look at the parser.')
        tool(m(23.4), 'Bash', {'command': 'pytest tests/test_dates.py -q', 'description': 'Run the date tests'},
             'FAILED tests/test_dates.py::test_week_start - assert datetime(2026, 9, 28) == datetime(2026, 10, 5)')
        tool(m(22.6), 'Read', {'file_path': os.path.join(cwd, 'src', 'demo_notes', 'dates.py')}, 'def week_start(d):\n    return d - timedelta(days=d.weekday() + 1)\n')
        say(m(21.8), 'Found it: `week_start` subtracts `weekday() + 1`, which is right only if weeks start on Sunday and today is not one. '
                     'On a Sunday `weekday()` is 6, so the result lands a full week early.')
        tool(m(21.0), 'Edit', {'file_path': os.path.join(cwd, 'src', 'demo_notes', 'dates.py'), 'old_string': 'd.weekday() + 1', 'new_string': '(d.weekday() + 1) % 7'},
             'The file has been updated.')
        tool(m(20.2), 'Bash', {'command': 'pytest tests/test_dates.py -q', 'description': 'Re-run the date tests'}, '1 passed in 0.02s')
        say(m(19.6), 'Fixed: `week_start` now uses `(weekday() + 1) % 7`, and the test passes on every weekday.', 'end_turn')
        human(m(15), 'Add a regression test for it.')
        tool(m(14.4), 'Write', {'file_path': os.path.join(cwd, 'tests', 'test_week_start_sunday.py'), 'content': 'def test_sunday():\n    ...\n'}, 'File created')
        say(m(13.6), 'Added `tests/test_week_start_sunday.py`: it pins the date to a Sunday so the bug cannot come back unnoticed.', 'end_turn')
        log.save()

    # ---------- plan tier and usage cache ----------
    def write_json(self):
        now = self.now
        win = lambda pct, secs: {'utilization': pct, 'resets_at': iso(now + secs)}
        put(os.path.join(self.home, '.claude.json'), json.dumps({
            'numStartups': 42,
            'oauthAccount': {'userRateLimitTier': self.PLAN[0], 'billingType': 'stripe_subscription'},
            'cachedUsageUtilization': {'fetchedAtMs': int((now - 240) * 1000), 'utilization': {
                'five_hour': win(self.PLAN[1], 2.5 * 3600), 'seven_day': win(self.PLAN[2], 3.2 * 86400),
                'limits': [{'kind': 'weekly_scoped', 'scope': {'model': {'display_name': 'Sonnet'}}, 'percent': self.PLAN[3],
                            'resets_at': iso(now + 3.2 * 86400)}]}}}, indent=2) + '\n')


class SubWriter:
    """One sub-agent: its transcript (rounds 1 and 2 go into the same file) and meta file."""

    def __init__(self, syn, tag, desc, model, label=None):
        self.syn, self.tag, self.desc, self.model, self.k, self.aid = syn, tag, desc, model, 0, syn.agents[tag]
        self.label = label or '%s %s' % (tag, desc)      # the meta `description` (a tag-less label makes a worker without a debate seat)
        self.dir = os.path.join(syn.proj(syn.work), syn.sid, 'subagents')
        self.log = Jsonl(os.path.join(self.dir, 'agent-%s.jsonl' % self.aid))

    def user(self, t, text):
        s = self.syn
        self.log.add(t, s.line(t, 'user', s.work, s.sid, message={'role': 'user', 'content': text}, isSidechain=True, agentId=self.aid))

    def _assistant(self, t, content, stop):
        self.k += 1
        s = self.syn
        s.assistant(self.log, t, self.k, self.aid, s.work, s.sid, CLAUDE_MODELS[self.model], content, stop, effort='medium', isSidechain=True, agentId=self.aid)

    def say(self, t, text):
        self._assistant(t, [{'type': 'text', 'text': text}], None)

    def end(self, t, text):
        self._assistant(t, [{'type': 'text', 'text': text}], 'end_turn')

    def tool(self, t, name, inp, out):
        tid = 'toolu_' + hx(self.aid, str(self.k + 1), n=24)
        self._assistant(t, [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}], 'tool_use')
        if out is not None:                      # None: the call has no result yet
            s = self.syn
            s.result(self.log, t + 1.0, s.work, s.sid, tid, out, isSidechain=True, agentId=self.aid)

    def write_report(self, t, path, text):
        self.tool(t, 'Write', {'file_path': path, 'content': text}, 'File created successfully at: ' + path)
        put(path, text, t + 0.5)

    def save(self):
        self.log.save()
        put(os.path.join(self.dir, 'agent-%s.meta.json' % self.aid),
            json.dumps({'agentType': 'general-purpose', 'description': self.label, 'model': self.model,
                        'toolUseId': self.syn.spawn_use[self.tag]}))


# ---------- the busy scene (--busy): a big project in full swing ----------
BUSY_ORCH = 'b05500c0-0000-4000-8000-000000000011'
BUSY_CX_DEBATE = '019a7d11-4c2e-7a31-8f15-2b6c9d0e1a01'        # Codex participant of T3
BUSY_CX_WORKER = '019a7d22-5d3f-7b42-9a26-3c7dae1f2b02'        # Codex worker without a debate seat
BUSY_ROLES = {'A': 'opus', 'B': 'sonnet', 'C': 'sonnet'}        # model of each seat (C of T3 is Codex)
# key, title, question, roles (A, B, C) + one line each, start (minutes ago the topic was launched), final: file under final/ named in the common brief or None (= rulings.md)
BUSY_TOPICS = [
    ('t1_naming', 'Naming conventions', 'How are topics, services and parameters named in the v2 interface?',
     (('Consistency', 'what keeps one scheme everywhere'), ('Brevity', 'what keeps names short and readable'), ('Tooling', 'what can be checked by a tool')), 290, 'naming'),
    ('t2_errors', 'Error handling', 'How do v2 components report and recover from errors?',
     (('Strict', 'no silent failure'), ('Pragmatic', 'what small packages can afford'), ('Observability', 'what an operator can trace')), 262, None),
    ('t3_retry', 'Retry policy', 'How do v2 clients retry calls and bound their waiting time?',
     (('Backoff design', 'delay curve, jitter, attempt limit'), ('Failure modes', 'what a retry can make worse'), ('Cross-check', 'run both policies in the simulator (Codex)')), 90, None),
    ('t4_config', 'Config schema', 'How is package configuration versioned, defaulted and validated?',
     (('Versioning', 'schema versions and migrations'), ('Defaults', 'where defaults live'), ('Validation', 'when and how bad input is rejected')), 236, 'config'),
    ('t5_logging', 'Logging format', 'What does a v2 log line look like, and how much of it do we write?',
     (('Structure', 'fields and encoding'), ('Volume', 'levels and sampling'), ('Privacy', 'what must never be logged')), 24, None),
    ('t6_tests', 'Test layout', 'How are tests organised across the packages?',
     (('Unit', 'fast and local'), ('Integration', 'packages together'), ('Simulation', 'whole robot in the simulator')), 58, None),
    ('t7_packages', 'Package boundaries', 'Which package owns what, and who may depend on whom?',
     (('Coupling', 'dependency direction'), ('Ownership', 'one owner per interface'), ('Release', 'versions that ship together')), 172, None),
    ('t8_release', 'Release checklist', 'What must be true before v2 ships?',
     (('Automation', 'what a script can check'), ('Docs', 'what a user needs to read'), ('Rollback', 'how to undo a bad release')), 140, 'release'),
]
BUSY_TEXT = {      # per topic: r1 = {who: (stance, bullet, bullet)}, r2 = {who: (accepted, contested)}, rule = ruling points
    't1_naming': {
        'r1': {'A': ('one scheme everywhere: snake_case, `<package>/<noun>`.', 'Mixed styles cost every new contributor a day (`docs/style.md:14-30`).', 'Renames are cheap while v2 is unreleased.'),
               'B': ('drop the package prefix inside a package.', '`route_planner/route_planner_goal` repeats itself.', 'Short names read better in logs and dashboards.'),
               'C': ('whatever we pick, a lint rule must enforce it.', 'A 60-line checker covers 90% of the cases.', 'Run it in CI so renames cannot drift back.')},
        'r2': {'A': ('the prefix may be dropped inside the owning package', 'the public name stays fully qualified'), 'B': ('full names at package boundaries', 'short names stay local'),
               'C': ('no objections', 'lint rule drafted: `tools/lint_names.py`')},
        'rule': ['Names are snake_case `<package>/<noun>`; the prefix is dropped only inside the owning package.', 'A lint rule enforces both forms in CI.', 'v1 names keep a deprecated alias for one release.']},
    't2_errors': {
        'r1': {'A': ('typed errors with stable codes; no silent fallback.', 'A swallowed error cost a field test last quarter.', 'Callers branch on the code, never on the message.'),
               'B': ('error codes plus a retry hint.', 'Most callers only need "retry" or "give up".', 'A full hierarchy is too heavy for small packages.'),
               'C': ('every error carries its context in the log line.', 'Package, operation and request id.', 'Otherwise a code alone cannot be traced.')},
        'r2': {'A': ('a retry hint field is fine', 'fallbacks are allowed if they log'), 'B': ('stable codes accepted', 'hierarchy limited to three levels'),
               'C': ('no objections', 'added a log schema for errors')},
        'rule': ['Errors are typed with stable codes (`E_<AREA>_<NAME>`) and carry a retry hint.', 'A fallback is allowed only if it logs the original error.', 'The hierarchy has at most three levels.']},
    't3_retry': {
        'r1': {'A': ('exponential backoff with full jitter.', 'Base 200 ms, factor 2, cap 10 s, at most 5 attempts.', 'Jitter spreads clients that fail together.'),
               'B': ('cap the total wait, not only the delay.', 'Five attempts at the cap is 50 s of a blocked start-up.', 'Log every retry at WARNING.'),
               'C': ('both policies survive the flaky-link scenario.', 'Full jitter recovered 3 s sooner on average.', 'The total-wait cap prevented two stuck start-ups.')}},
    't4_config': {
        'r1': {'A': ('a `schema_version` field and one migration per bump.', 'Old files must keep loading for one release.', 'Migrations are scripts, not code in the loader.'),
               'B': ('defaults live in one file per package.', 'Today they are spread over three modules.', 'A reader should see every default at once.'),
               'C': ('validate at startup and fail fast with the key path.', 'Half of the support questions are a mistyped key.', 'The error names the file, the key and the allowed values.')},
        'r2': {'A': ('one defaults file per package', 'migrations stay scripts'), 'B': ('versioned schema accepted', 'defaults file is read-only at run time'),
               'C': ('no objections', 'startup validation covers nested keys')},
        'rule': ['Every config file has `schema_version`; migrations are scripts under `tools/migrate/`.', 'Defaults live in `defaults.toml` per package.', 'Validation runs at startup; an invalid key stops the node and names its path.']},
    't5_logging': {
        'r1': {'A': ('JSON lines with a fixed set of fields.', 'timestamp, level, package, event, request id.', 'Free text goes into one `msg` field.'),
               'B': ('levels with sampling above INFO.', 'Ten lines per second per node is the budget.', 'Repeated lines are folded with a count.'),
               'C': ('no raw coordinates or user ids at INFO.', 'Positions are rounded; ids are hashed.', 'DEBUG may keep them, off by default.')}},
    't6_tests': {
        'r1': {'A': ('unit tests stay under one second each.', 'They run on every commit.', 'No simulator, no network.'),
               'B': ('integration tests start the real nodes.', 'They catch interface drift between packages.', 'A fixed port range avoids clashes.'),
               'C': ('simulation tests cover whole missions.', 'They are slow, so they run nightly.', 'Failures attach the simulator log.')},
        'r2': {'A': ('markers instead of folders alone', 'unit tests may use fixtures'), 'B': ('nightly simulation is fine', 'integration stays per commit'),
               'C': ('no objections', 'simulation results are tracked per release')},
        'rule': ['Three layers: `unit/`, `integration/`, `sim/`, selected by pytest markers.', 'Simulation tests run nightly, not per commit.', 'A new package ships with at least one test per layer.']},
    't7_packages': {
        'r1': {'A': ('dependencies point downwards only.', 'sensor_fusion may not import route_planner.', 'A cycle is a build error.'),
               'B': ('every interface has exactly one owning package.', 'Shared message types move to `acme_msgs`.', 'The owner approves changes to its interface.'),
               'C': ('packages release in groups that share a version.', 'Mixed versions are the main source of field issues.', 'A compatibility table ships with each group.')},
        'r2': {'A': ('shared messages in `acme_msgs`', 'the dependency check runs in CI'), 'B': ('release groups accepted', 'owners listed in `OWNERS`'),
               'C': ('no objections', 'compatibility table generated from the groups')},
        'rule': ['Dependencies point downwards; a cycle fails the build.', 'Every interface has one owner; shared messages live in `acme_msgs`.', 'Packages release in groups that share one version.']},
    't8_release': {
        'r1': {'A': ('the checklist is a script, not a wiki page.', 'Every item has a command and an expected result.', 'The script runs on the release branch.'),
               'B': ('release notes are written from the rulings.', 'Users read the migration guide, not the changelog.', 'Each breaking change links to its ruling.'),
               'C': ('every release can be rolled back in ten minutes.', 'Keep the previous artifacts and the config migration back-ups.', 'Rehearse the rollback once per release.')},
        'r2': {'A': ('rollback rehearsal is part of the script', 'notes are generated, then edited'), 'B': ('script as the single source', 'the guide lists the aliases'),
               'C': ('no objections', 'back-ups are verified by the script')},
        'rule': ['The release checklist is a script; the release branch must pass it.', 'The migration guide is generated from the rulings and edited by hand.', 'A rollback is rehearsed before every release.']},
}
BUSY_WORKERS = [        # description, model, package
    ('route_planner v2', 'sonnet', 'route_planner'), ('map_server v2', 'sonnet', 'map_server'), ('battery_monitor v2', 'sonnet', 'battery_monitor'),
    ('motion_control v2', 'sonnet', 'motion_control'), ('sensor_fusion v2', 'sonnet', 'sensor_fusion'), ('diagnostics v2', 'sonnet', 'diagnostics'),
    ('teleop_bridge v2', 'sonnet', 'teleop_bridge'), ('docs and examples', 'opus', 'docs'),
]
BUSY_OPEN_ITEM = 'Open item: removing the `plan_route()` alias needs user approval; two external integrations still call it.'
BUSY_FILES = ['node.py', 'client.py', 'params.py', 'planner.py', 'health.py', 'bridge.py']


class OrchLog:
    """Writer for the orchestrator's own transcript of the busy scene."""

    def __init__(self, syn):
        self.syn, self.k = syn, 0
        self.w = syn.work
        self.log = Jsonl(os.path.join(syn.proj(syn.work), syn.sid + '.jsonl'))

    def say(self, t, text, stop=None, extra=()):
        s = self.syn
        self.k += 1
        s.assistant(self.log, t, self.k, s.sid, self.w, s.sid, CLAUDE_MODELS['opus'], [{'type': 'text', 'text': text}] + list(extra), stop)

    def tool(self, t, name, inp, out, tid=None, **kw):
        s = self.syn
        self.k += 1
        tid = tid or 'toolu_' + hx('orch', str(self.k), n=24)
        s.assistant(self.log, t, self.k, s.sid, self.w, s.sid, CLAUDE_MODELS['opus'], [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}], 'tool_use')
        s.result(self.log, t + 1.0, self.w, s.sid, tid, out, **kw)

    def human(self, t, text):
        s = self.syn
        self.log.add(t, s.line(t, 'user', self.w, s.sid, origin={'kind': 'human'}, message={'role': 'user', 'content': text}))

    def turn_end(self, t, pending):
        s = self.syn
        self.log.add(t, s.line(t, 'system', self.w, s.sid, subtype='turn_duration', durationMs=60000 + pending * 4000, pendingBackgroundAgentCount=pending))

    def handback(self, t, tag, text):
        s = self.syn
        body = 'Subagent hand-back from %s.\nThe report follows:\n%s' % (tag, '\n'.join('  ' + ln for ln in text.splitlines()))
        self.log.add(t, s.line(t, 'user', self.w, s.sid, origin={'kind': 'peer', 'from': s.agents[tag], 'handback': True, 'body': body}, message={'role': 'user', 'content': body}))

    def notify(self, t, tag, desc, tokens, tools, minutes):
        s = self.syn
        text = ('<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>completed</status>\n'
                '<summary>Agent "%s %s" completed</summary>\n<usage><subagent_tokens>%d</subagent_tokens><tool_uses>%d</tool_uses>'
                '<duration_ms>%d</duration_ms></usage>\n</task-notification>') % (s.agents[tag], s.spawn_use[tag], tag, desc, tokens, tools, int(minutes * 60000))
        self.log.add(t, s.line(t, 'user', self.w, s.sid, origin={'kind': 'task-notification'}, message={'role': 'user', 'content': text}))

    def codex_notify(self, t, rnd_key):
        use, bg, label = self.syn.codex_calls[rnd_key][0], self.syn.codex_calls[rnd_key][1], self.syn.codex_calls[rnd_key][3]
        text = ('<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>completed</status>\n'
                '<summary>Background command "%s" completed (exit code 0)</summary>\n</task-notification>' % (bg, use, label))
        self.log.add(t, self.syn.line(t, 'user', self.w, self.syn.sid, origin={'kind': 'task-notification'}, message={'role': 'user', 'content': text}))

    def codex_launch(self, key, t, label, command):
        use, bg = 'toolu_' + hx('codex', key, n=24), 'b' + hx('bg', key, n=8)
        self.tool(t, 'Bash', {'command': command, 'description': label, 'run_in_background': True}, 'Command running in background with ID: %s' % bg,
                  tid=use, toolUseResult={'backgroundTaskId': bg})
        self.syn.codex_calls[key] = (use, bg, t, label)

    def save(self):
        self.log.save()


class BusySynth(Synth):
    """--busy: acme-robot, a v2 migration with eight topics (six settled, two running), nine workers, two Codex threads. All names are made up.
    T6 settled a few minutes ago, so its room is still in the office and its three reviewers rest in the lounge."""
    PLAN = ('default_claude_max_20x', 47.0, 61.0, 33.0)
    CODEX_USED = 38.0

    def __init__(self, home, now):
        Synth.__init__(self, home, now)
        self.sid = BUSY_ORCH
        self.work = os.path.join(home, 'work', 'acme-robot')
        self.review = os.path.join(self.work, 'docs', 'migration')
        self.unit = {t[0]: os.path.join(self.review, t[0]) for t in BUSY_TOPICS}
        self.topics = {t[0]: t for t in BUSY_TOPICS}
        self.agents, self.spawn_use, self.models = {}, {}, {}
        for key, _title, _q, roles, _start, _fin in BUSY_TOPICS:
            for who in 'ABC':
                if key == 't3_retry' and who == 'C':
                    continue                                   # a Codex thread, not an Agent-tool sub-agent
                tag = self.tag(key, who)
                self.agents[tag] = 'a' + hx('acme-robot', tag)
                self.spawn_use[tag] = 'toolu_' + hx('spawn', tag, n=24)
        for desc, model, pkg in BUSY_WORKERS:
            self.agents[pkg] = 'a' + hx('acme-robot', pkg)
            self.spawn_use[pkg] = 'toolu_' + hx('spawn', pkg, n=24)
        self.codex_files = []                                  # rollouts the --live fake codex processes keep open

    @staticmethod
    def tag(key, who):
        return 'T%s-%s' % (key[1], who)

    def usage(self, k, base=60000):
        return {'input_tokens': 5 + k % 4, 'cache_creation_input_tokens': 2600 + (k * 71) % 900,
                'cache_read_input_tokens': base + 2600 * k, 'output_tokens': 650 + (k * 97) % 1400}

    # ---------- documents ----------
    def final_path(self, key):
        name = self.topics[key][5]
        return os.path.join(self.review, 'final', name + '.md') if name else os.path.join(self.unit[key], 'rulings.md')

    def rulings_text(self, key):
        t = self.topics[key]
        pts = BUSY_TEXT[key]['rule']
        tail = BUSY_OPEN_ITEM if key == 't7_packages' else 'none'
        return '# T%s rulings — %s\n\n%s\n\nOpen items: %s. Sign-off: %s.\n' % (key[1], t[1], '\n'.join('%d. %s' % (i + 1, p) for i, p in enumerate(pts)), tail,
                                                                          ', '.join(self.tag(key, w) for w in 'ABC'))

    def brief_text(self):
        rows = '\n'.join('| T%s %s | `%s/` | %s | %s |' % (k[1], t, k, dep, '`final/%s.md`' % f if f else '')
                         for (k, t, _q, _r, _s, f), dep in zip(BUSY_TOPICS, ['—', 'T1', 'T2', '—', 'T2', '—', 'T1', 'T1, T2, T4, T7']))
        return ('# acme-robot v2 interface migration\n\nCommon brief for every participant. Eight independent topics; each has its own folder, topic brief and reports.\n\n'
                '| Topic | Folder | Depends on | Final |\n|---|---|---|---|\n' + rows + '\n\n## Rules\n- Round 1: write your report alone. Do not read other participants\' reports.\n'
                '- Round 2: read the others\' round-1 reports, then write `r2/<you>.md`: what you now accept, what you still contest.\n'
                '- Reports are markdown, at most 40 lines. Cite file paths and line numbers.\n- The orchestrator writes the ruling once round 2 is in.\n')

    def topic_brief(self, key):
        _k, title, q, roles, _s, _f = self.topics[key]
        parts = '\n'.join('- **%s — %s**: %s' % (w, r[0], r[1]) for w, r in zip('ABC', roles))
        return '# T%s %s\n\n%s\n\nParticipants:\n%s\n\nReports: `r1/<id>.md`, `r2/<id>.md`.\n' % (key[1], title, q, parts)

    def report(self, key, who, rnd):
        title, roles = self.topics[key][1], self.topics[key][3]
        role = roles['ABC'.index(who)][0]
        if rnd == 1:
            stance, b1, b2 = BUSY_TEXT[key]['r1'][who]
            return '# %s · round 1 — %s\n\n**Position:** %s\n\n- %s\n- %s\n' % (self.tag(key, who), role, stance, b1, b2)
        acc, rest = BUSY_TEXT[key]['r2'][who]
        return '# %s · round 2 — %s\n\n**Accepted:** %s.\n\n**Still contested:** %s.\n' % (self.tag(key, who), role, acc, rest)

    def final_msg(self, key, who, rnd):
        if rnd == 1:
            return 'Round 1 written to r1/%s.md. Position: %s' % (who, BUSY_TEXT[key]['r1'][who][0])
        text = 'Round 2 written to r2/%s.md. I accept: %s.' % (who, BUSY_TEXT[key]['r2'][who][0])
        return text + ' ' + BUSY_OPEN_ITEM if key == 't7_packages' and who == 'B' else text

    def spawn_prompt(self, tag, desc, key, who):
        unit, review = self.tilde(self.unit[key]), self.tilde(self.review)
        return ('You are %s (%s) in the %s review of the acme-robot v2 migration.\n\nRead these first:\n- %s/brief.md (common brief)\n- %s/brief.md (topic brief)\n\n'
                'Round 1: write your independent report to %s/r1/%s.md (at most 40 lines). Do not read the other participants\' reports in round 1.\n'
                'When you are done, finish with a one-paragraph final report.' % (tag, desc, key.split('_')[0].upper(), review, unit, unit, who))

    def round2_message(self, key, who):
        unit = self.tilde(self.unit[key])
        others = [o for o in 'ABC' if o != who]
        return 'Round 2: read %s, then write %s/r2/%s.md - what you now accept and what you still contest.' % (
            ' and '.join('%s/r1/%s.md' % (unit, o) for o in others), unit, who)

    # ---------- build ----------
    def build(self):
        put(os.path.join(self.review, 'brief.md'), self.brief_text(), self.m(299))
        for key in self.unit:
            put(os.path.join(self.unit[key], 'brief.md'), self.topic_brief(key), self.m(self.topics[key][4] + 1))
        for pkg in [w[2] for w in BUSY_WORKERS if w[2] != 'docs'] + ['acme_msgs']:
            for f in BUSY_FILES[:3]:
                put(os.path.join(self.work, 'packages', pkg, 'src', pkg, f), '# %s/%s\n' % (pkg, f), self.m(900))
            put(os.path.join(self.work, 'packages', pkg, 'config', 'defaults.toml'), 'schema_version = 1\n', self.m(900))
        put(os.path.join(self.work, 'docs', 'style.md'), '# Style\n', self.m(900))
        main = OrchLog(self)
        self.main = main
        self.write_conversation_and_topics(main)
        self.write_workers(main)
        self.write_codex_threads(main)
        for sw in self.subs.values():
            sw.save()
        main.save()
        self.write_solo()
        self.write_json()

    # one topic's three participants: (start, r1 end, r2 start, r2 end) per seat, in minutes ago
    def times(self, key, who):
        s = self.topics[key][4]
        if key == 't3_retry':
            return {'A': (s, 71.0, 22.0, None), 'B': (s, 70.0, 22.0, None), 'C': (s, 67.5, 21.5, None)}[who]
        if key == 't5_logging':
            return {'A': (s, None, None, None), 'B': (s, None, None, None), 'C': (s, 5.0, None, None)}[who]
        return {'A': (s, s - 19.0, s - 24.0, s - 40.0), 'B': (s, s - 20.0, s - 24.0, s - 42.0), 'C': (s, s - 22.0, s - 24.0, s - 44.0)}[who]

    def write_conversation_and_topics(self, main):
        m = self.m
        main.human(m(300), 'We are moving acme-robot to the v2 interface. Eight open questions (naming, errors, retries, config, logging, tests, package boundaries, release). '
                           'Settle each with independent reviewers, keep the written record under docs/migration, and tell me when a ruling needs me.')
        main.log.add(m(299.8), {'type': 'ai-title', 'aiTitle': 'acme-robot v2 migration'})
        main.say(m(299.5), 'Plan: one folder per topic with a topic brief, three reviewers each, two rounds, then a ruling. I start with the topics nothing else depends on and '
                           'launch the rest as their inputs settle.')
        main.tool(m(299.0), 'Write', {'file_path': os.path.join(self.review, 'brief.md'), 'content': self.brief_text()}, 'File created')
        for key in [t[0] for t in BUSY_TOPICS]:
            self.topic(main, key)
        main.say(m(93.8), 'T1, T2, T4, T7 and T8 are settled (rulings under docs/migration). Next: T3 retry policy, then T5 logging and T6 test layout, and in parallel the package workers.')
        main.human(m(93.0), 'Good. Go ahead with T3, T5 and T6, and start migrating the packages.')
        main.say(m(92.6), 'Starting T3 now; T5 and T6 follow as their inputs settle.')
        main.turn_end(m(92.2), 0)
        main.say(m(27.5), 'T3 round 2 is running and T5 is launching. Starting the package workers now: route_planner, map_server, battery_monitor, motion_control, sensor_fusion, '
                          'diagnostics, teleop_bridge, the docs, and a Codex run of the compatibility matrix.')
        main.human(m(13), 'What is blocking route_planner?')
        main.say(m(12.5), 'route_planner waits for map_server\'s v2 message types; the map_server worker is on them. T3 round 2 is running, T5 round 1 is half in and T6 closes soon.')
        main.tool(m(1.6), 'Read', {'file_path': os.path.join(self.unit['t6_tests'], 'rulings.md')}, self.rulings_text('t6_tests'))
        main.say(m(1.1), 'Status: the nine workers are mid-migration, T3 round 2 and T5 round 1 are running, T6 is closed. One item from T7 is waiting in the alerts.')

    def topic(self, main, key):
        m = self.m
        _k, title, _q, roles, start, fin = self.topics[key]
        running_topic = key in ('t3_retry', 't5_logging')
        for who in 'ABC':
            if key == 't3_retry' and who == 'C':
                continue
            tag = self.tag(key, who)
            _s, r1e, r2s, r2e = self.times(key, who)
            role = roles['ABC'.index(who)][0]
            model = BUSY_ROLES[who]
            # orchestrator side: spawn
            main.tool(m(start - 0.1 * 'ABC'.index(who)), 'Agent', {'description': '%s %s' % (tag, role), 'subagent_type': 'general-purpose', 'model': model,
                                                                    'run_in_background': True, 'prompt': self.spawn_prompt(tag, role, key, who)},
                      'Async agent launched successfully.\nagentId: %s' % self.agents[tag], tid=self.spawn_use[tag],
                      toolUseResult={'isAsync': True, 'status': 'async_launched', 'agentId': self.agents[tag]})
            self.debate_agent(main, key, who, tag, role, model, running_topic)
        if key == 't3_retry':
            self.codex_debate_launch(main, 1, m(start - 0.4))
            main.say(m(start - 0.3), 'T3 reviewers are launched; C is a Codex cross-check.')
        if running_topic:
            if key == 't3_retry':
                main.turn_end(m(start - 1), 4)
                main.codex_notify(m(66.6), 'cx1')
                for who in 'AB':
                    tag = self.tag(key, who)
                    main.tool(m(22.0), 'SendMessage', {'to': self.agents[tag], 'summary': 'Round 2: cross-read and answer', 'message': self.round2_message(key, who)}, 'Message sent')
                self.codex_debate_launch(main, 2, m(21.5))
            return
        for who in 'ABC':
            tag = self.tag(key, who)
            main.tool(m(start - 24.0), 'SendMessage', {'to': self.agents[tag], 'summary': 'Round 2: cross-read and answer', 'message': self.round2_message(key, who)}, 'Message sent')
        # the ruling
        fpath = self.final_path(key)
        text = self.rulings_text(key)
        t_fin = start - 46.0
        main.tool(m(t_fin + 0.3), 'Write', {'file_path': fpath, 'content': text}, 'File created')
        put(fpath, text, m(t_fin))
        main.say(m(t_fin - 0.4), 'T%s settled: %s (%s).' % (key[1], BUSY_TEXT[key]['rule'][0].rstrip('.'), os.path.relpath(fpath, self.review)))
        main.turn_end(m(t_fin - 0.8), 0)

    def debate_agent(self, main, key, who, tag, role, model, running_topic):
        m = self.m
        s = self.sub(tag, role, model)
        unit, review = self.unit[key], self.review
        start, r1e, r2s, r2e = self.times(key, who)
        pkgs = ['route_planner', 'map_server', 'battery_monitor', 'motion_control', 'sensor_fusion', 'diagnostics', 'teleop_bridge']
        s.user(m(start - 0.3), self.spawn_prompt(tag, role, key, who))
        s.say(m(start - 0.6), 'Reading both briefs and the code that touches this question.')
        s.tool(m(start - 0.9), 'Read', {'file_path': os.path.join(review, 'brief.md')}, self.brief_text())
        s.tool(m(start - 1.2), 'Read', {'file_path': os.path.join(unit, 'brief.md')}, self.topic_brief(key))
        # a few reads of the code
        n_code = 3 if r1e else 6
        end1 = r1e if r1e else 0.8
        span = (start - 2.0) - (end1 + 3.0)
        for i in range(n_code):
            pkg = pkgs[(ord(who) + int(key[1]) + i) % len(pkgs)]
            t = m(start - 2.0 - span * (i + 1) / (n_code + 1))
            if i % 2:
                s.tool(t, 'Grep', {'pattern': 'legacy|deprecated|retry|topic_name', 'path': os.path.join(self.work, 'packages', pkg, 'src')},
                       'packages/%s/src/%s/node.py:%d\npackages/%s/src/%s/client.py:%d' % (pkg, pkg, 20 + i * 7, pkg, pkg, 41 + i * 3))
            else:
                s.tool(t, 'Read', {'file_path': os.path.join(self.work, 'packages', pkg, 'src', pkg, BUSY_FILES[i % 3])}, '# %s/%s\n' % (pkg, BUSY_FILES[i % 3]))
        if not r1e:                                 # round 1 still running: the last call has no result yet
            s.tool(m(0.45 + 0.1 * 'ABC'.index(who)), 'Read', {'file_path': os.path.join(self.work, 'packages', 'acme_msgs', 'src', 'acme_msgs', 'params.py')}, None)
            return
        s.say(m(r1e + 3.0), 'Position is forming; writing the report now.')
        s.write_report(m(r1e + 1.0), os.path.join(unit, 'r1', who + '.md'), self.report(key, who, 1))
        if key == 't2_errors' and who == 'C':
            s.tool(m(r1e + 0.6), 'SendMessage', {'to': 'orchestrator', 'summary': 'Log schema for errors', 'message': 'Early note: I will propose a small log schema for errors in round 2.'}, 'Message sent')
        if key == 't1_naming' and who == 'A':
            s.tool(m(r1e + 0.6), 'SendMessage', {'to': 'T1-B', 'summary': 'Prefix inside a package', 'message': 'I can live with dropping the prefix inside the owning package. Reply only if you see a blocker.'}, 'Message sent')
        s.end(m(r1e), self.final_msg(key, who, 1))
        # orchestrator: hand-back + notification
        main.handback(m(r1e - 0.2), tag, self.final_msg(key, who, 1))
        main.notify(m(r1e - 0.6), tag, role, 38000 + 900 * len(role) + int(r1e) * 7, 9, start - r1e)
        if r2s is None:
            return
        others = [o for o in 'ABC' if o != who]
        s.user(m(r2s - 0.1), COORD + ' ' + self.round2_message(key, who))
        for i, o in enumerate(others):
            if key == 't3_retry' and o == 'C':
                path = os.path.join(unit, 'r1', 'C.md')
                body = self.report('t3_retry', 'C', 1)
            else:
                path, body = os.path.join(unit, 'r1', o + '.md'), self.report(key, o, 1)
            s.tool(m(r2s - 0.5 - 0.4 * i), 'Read', {'file_path': path}, body)
        if r2e is None:                             # T3 round 2 is running: the last call has no result yet
            s.say(m(r2s - 3.0), 'Both reports read; drafting what I accept and what I still contest.')
            s.tool(m(0.6 + 0.15 * 'AB'.index(who)), 'Read', {'file_path': os.path.join(unit, 'r1', 'C.md')}, None)
            return
        s.say(m(r2e + 2.5), 'The other reports settle most of it; writing what I accept and what I still contest.')
        s.write_report(m(r2e + 0.8), os.path.join(unit, 'r2', who + '.md'), self.report(key, who, 2))
        s.end(m(r2e), self.final_msg(key, who, 2))
        main.handback(m(r2e - 0.2), tag, self.final_msg(key, who, 2))
        main.notify(m(r2e - 0.6), tag, role, 61000 + 700 * len(role), 7, start - r2e)

    # ---------- the package workers ----------
    def write_workers(self, main):
        m = self.m
        for i, (desc, model, pkg) in enumerate(BUSY_WORKERS):
            spawn = 27.0 - i * 2.6                             # minutes ago, oldest first
            s = self.sub(pkg, desc, model)
            s.label = desc                                      # no `T1-A`-style prefix: these sit in the "other work" room
            prompt = ('Migrate the `%s` package to the v2 interface.\n\nWork in ~/work/acme-robot/packages/%s. Follow the rulings under ~/work/acme-robot/docs/migration/final/ '
                      'and the topic rulings next to them. Keep the unit tests passing and finish with a short report.' % (pkg, pkg)) if pkg != 'docs' else (
                      'Update the docs and examples for the v2 interface.\n\nWork in ~/work/acme-robot/docs. Use the rulings under ~/work/acme-robot/docs/migration/final/ and '
                      'the topic rulings next to them. Every example must run; finish with a short report.')
            main.tool(m(spawn), 'Agent', {'description': desc, 'subagent_type': 'general-purpose', 'model': model, 'run_in_background': True, 'prompt': prompt},
                      'Async agent launched successfully.\nagentId: %s' % self.agents[pkg], tid=self.spawn_use[pkg],
                      toolUseResult={'isAsync': True, 'status': 'async_launched', 'agentId': self.agents[pkg]})
            s.user(m(spawn - 0.3), prompt)
            s.say(m(spawn - 0.6), 'Looking at the package layout and its tests first.')
            end = 0.35 + 0.17 * i                               # last activity (minutes ago), all inside the last two minutes
            n = 7 + (i * 3) % 7
            base = (spawn - 1.0 - end) / n
            base_dir = os.path.join(self.work, 'packages', pkg) if pkg != 'docs' else os.path.join(self.work, 'docs')
            for j in range(n):
                t = m(spawn - 1.0 - base * j)
                f = BUSY_FILES[(i + j) % 3]
                kind = (i + j) % 5
                if pkg == 'docs':
                    path = os.path.join(base_dir, 'guide', 'step_%d.md' % (j % 4 + 1))
                else:
                    path = os.path.join(base_dir, 'src', pkg, f)
                last = j == n - 1
                if kind == 0:
                    s.tool(t, 'Read', {'file_path': path}, None if last else '# %s\n' % os.path.basename(path))
                elif kind == 1:
                    s.tool(t, 'Grep', {'pattern': 'legacy|old_topic|deprecated', 'path': base_dir}, None if last else '%s:%d' % (os.path.relpath(path, self.work), 12 + j))
                elif kind == 2:
                    s.tool(t, 'Edit', {'file_path': path, 'old_string': 'legacy_name', 'new_string': 'v2_name'}, None if last else 'The file has been updated.')
                elif kind == 3:
                    s.tool(t, 'Bash', {'command': 'pytest packages/%s/tests -q' % pkg if pkg != 'docs' else 'python tools/check_examples.py', 'description': 'Run the tests'},
                           None if last else ('%d passed in 1.%ds' % (8 + j, j) if j > 3 else '2 failed, %d passed' % (6 + j)))
                else:
                    s.tool(t, 'Bash', {'command': 'python tools/lint_names.py %s' % os.path.relpath(base_dir, self.work), 'description': 'Check the names'},
                           None if last else 'ok: %d names checked' % (30 + j))
        # `last` rows above are pending; nothing is written to the report folders

    # ---------- Codex ----------
    def codex_debate_launch(self, main, rnd, t):
        unit = self.tilde(self.unit['t3_retry'])
        prompt = self.codex_debate_prompt(rnd)
        out = '%s/r%d/C.md' % (unit, rnd)
        cmd = ('codex exec -m %s -o %s "%s"' % (CODEX_MODEL, out, prompt)) if rnd == 1 else ('codex exec resume %s -o %s "%s"' % (BUSY_CX_DEBATE, out, prompt))
        main.codex_launch('cx%d' % rnd, t, 'Codex C: T3 round %d' % rnd, cmd)

    def codex_debate_prompt(self, rnd):
        unit = self.tilde(self.unit['t3_retry'])
        if rnd == 1:
            return ('T3 cross-check: retry and timeouts\nYou are participant C in the T3 review of the acme-robot v2 migration. Read the topic brief at %s/brief.md, '
                    'run both retry policies in the simulator scenarios and answer with your round-1 report in at most 40 lines of markdown.' % unit)
        return ('Round 2 of T3\nRead r1/A.md and r1/B.md in %s, say what you still contest, and answer with your round-2 report in at most 40 lines of markdown.' % unit)

    def codex_worker_prompt(self, turn):
        if turn == 1:
            return ('Compatibility matrix for the v2 interface\nRun the compatibility matrix of the eight migrated packages against the v1 clients in ~/work/acme-robot/tools/compat '
                    'and report which combinations fail and why. Keep the report under 30 lines.')
        return 'Second pass on the failing combinations\nRe-run the failing combinations from your first report with the alias layer enabled and report what still fails.'

    def write_codex_threads(self, main):
        m = self.m
        unit = self.unit['t3_retry']
        # --- the T3 cross-check (round 1 done, round 2 running)
        t1 = self.codex_calls['cx1'][2] + 2.4
        t2 = self.codex_calls['cx2'][2] + 1.2
        self.codex_rollout(BUSY_CX_DEBATE, t1, [
            dict(start=t1 + 0.2, prompt=self.codex_debate_prompt(1),
                 cmds=[(5, 'sed -n 1,60p %s/brief.md' % self.tilde(unit), None, '# T3 Retry and timeouts ...'), (60, 'ls tools/sim/scenarios', None, 'flaky_link.toml  slow_start.toml'),
                       (300, 'python tools/sim/run.py --scenario flaky_link --policy full_jitter', None, '12 runs, 0 stuck'),
                       (600, 'python tools/sim/run.py --scenario flaky_link --policy capped_total', None, '12 runs, 0 stuck'),
                       (900, 'python tools/sim/run.py --scenario slow_start --policy both', None, '12 runs, 2 stuck without the cap'), (1200, 'python tools/sim/summary.py', None, 'written')],
                 tokens=[(30, 1, 12000), (350, 2, 28000), (700, 3, 41000), (1000, 4, 52000), (1250, 5, 60000)], done=(m(67.5), self.report('t3_retry', 'C', 1)), out=os.path.join(unit, 'r1', 'C.md')),
            dict(start=t2, prompt=self.codex_debate_prompt(2),
                 cmds=[(3, 'cat r1/A.md', 'r1/A.md', self.report('t3_retry', 'A', 1)), (8, 'cat r1/B.md', 'r1/B.md', self.report('t3_retry', 'B', 1)),
                       (240, 'python tools/sim/run.py --scenario flaky_link --policy full_jitter --seed 7', None, '12 runs, 0 stuck'),
                       (600, 'python tools/sim/run.py --scenario slow_start --policy full_jitter --seed 7', None, '12 runs, 1 stuck'),
                       (900, 'python tools/sim/summary.py --round 2', None, 'written')],
                 tokens=[(30, 6, 62000), (500, 7, 70000), (1000, 8, 78000)], done=None, pending=(m(2.2), 'python tools/sim/run.py --scenario flaky_link --policy both')),
        ])
        # --- the compatibility-matrix worker (first pass reported, second pass running)
        w1 = self.now - 24.0 * 60                                                          # the Bash call
        main.codex_launch('cxw1', w1, 'Codex: compatibility matrix', 'cd ~/work/acme-robot && codex exec -m %s "%s"' % (CODEX_MODEL, self.codex_worker_prompt(1)))
        main.codex_notify(self.now - 9.0 * 60, 'cxw1')
        w2 = self.now - 8.6 * 60
        main.codex_launch('cxw2', w2 - 0.8, 'Codex: second pass', 'cd ~/work/acme-robot && codex exec resume %s "%s"' % (BUSY_CX_WORKER, self.codex_worker_prompt(2)))
        report = ('Compatibility matrix: 14 of 18 combinations pass.\n\n| Package | v1 client | result |\n|---|---|---|\n| sensor_fusion | 1.2 | fails: renamed topic |\n'
                  '| sensor_fusion | 1.3 | fails: renamed topic |\n| sensor_fusion | 1.4 | fails: renamed topic |\n| teleop_bridge | 1.2 | fails: removed parameter |\n\n'
                  'The other 14 pass. Details in `build/compat_matrix.md`.')
        self.codex_rollout(BUSY_CX_WORKER, w1 + 2.4, [
            dict(start=w1 + 2.6, prompt=self.codex_worker_prompt(1),
                 cmds=[(6, 'ls tools/compat', None, 'run_matrix.py  clients/'), (50, 'python tools/compat/run_matrix.py --packages all', None, '18 combinations, 4 failed'),
                       (160, 'python tools/compat/report.py --out build/compat_matrix.md', None, 'written'), (420, 'sed -n 1,40p build/compat_matrix.md', None, '| Package | v1 client | result |'),
                       (700, 'git diff --stat tools/compat', None, '2 files changed')],
                 tokens=[(20, 1, 15000), (120, 2, 34000), (250, 3, 52000), (600, 4, 60000)], done=(self.now - 9.0 * 60 - 3, report), out=None),
            dict(start=w2, prompt=self.codex_worker_prompt(2),
                 cmds=[(4, 'python tools/compat/run_matrix.py --packages sensor_fusion,teleop_bridge --alias-layer on', None, '8 combinations, 2 failed')],
                 tokens=[(30, 5, 66000)], done=None, pending=(self.now - 2.8 * 60, 'python tools/compat/report.py --diff build/compat_matrix.md')),
        ])
        # handbacks of the Codex turns that finished are made from their rollouts by the board; the T3 round-1 report is matched by sha1

    def codex_rollout(self, tid, t0, turns):
        """One Codex rollout: session_meta, then per turn task_started / turn context / user / commands / tokens, and either task_complete or a call that has not returned."""
        lt = time.localtime(t0)
        path = os.path.join(self.codex, 'sessions', '%04d' % lt.tm_year, '%02d' % lt.tm_mon, '%02d' % lt.tm_mday,
                            'rollout-%s-%s.jsonl' % (time.strftime('%Y-%m-%dT%H-%M-%S', lt), tid))
        th = CxThread(path, tid, self.work, t0)
        rates = rate_limits(self.CODEX_USED, self.now + 3.0 * 86400)
        for turn in turns:
            t = turn['start']
            th.turn(t, turn['prompt'])
            for dt, cmd, read, out in turn['cmds']:
                th.command(t + dt, cmd, out, read=read, cwd=self.unit['t3_retry'] if read else None)
            for dt, n, cached in turn['tokens']:
                th.tokens(t + dt, 22000 + 3800 * n, 1100 + 180 * n, cached, 600 + 90 * n, rates, n)
            if turn.get('done'):
                td, text = turn['done']
                th.finish(td, text)
                if turn.get('out'):
                    put(turn['out'], text, td + 0.3)                  # `-o` writes the last message verbatim: the board matches it by sha1
            else:
                tp, cmd = turn['pending']                              # a call that has not returned: the turn is still running
                th.running(tp, cmd)
        th.save()
        self.codex_files.append(path)


# ---------- folder, live processes ----------
def check_folder(path):
    if not re.fullmatch(r'/[A-Za-z0-9_./+-]+', path) or any(p.startswith('.') for p in path.split('/') if p):
        raise SystemExit('folder path must be absolute-able and plain ([A-Za-z0-9_./+-], no dot-folders): %s' % path)


def is_real_home(home):
    return os.path.realpath(home) == os.path.realpath(os.path.expanduser('~'))


def is_ours(home):
    """True for a folder this tool made: its marker file holds our text."""
    try:
        with open(os.path.join(home, MARK)) as f:
            return f.read() == MARK_TEXT
    except OSError:
        return False


def check_ours(home):
    """Refuse the real HOME and any folder this tool did not make (anything that deletes or signals starts here)."""
    if is_real_home(home):
        raise SystemExit('refusing to touch your real HOME: %s' % home)
    if not is_ours(home):
        raise SystemExit('not a synthetic HOME made by this tool (no %s marker): %s' % (MARK, home))


def prepare(home):
    """Create the folder, or empty a folder this tool made before. Never touches anything else."""
    if os.path.lexists(home) and not os.path.isdir(home):
        raise SystemExit('not a folder: %s' % home)
    if os.path.isdir(home) and os.listdir(home):
        if not is_ours(home):
            raise SystemExit('refusing to write into a non-empty folder this tool did not create: %s' % home)
        stop_live(home, quiet=True)
        for name in OWN:
            p = os.path.join(home, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            elif os.path.lexists(p):
                os.remove(p)
    os.makedirs(home, exist_ok=True)


def build(home, now=None, busy=False, links=False, stopped=False, codex_orch=False):
    """Write the synthetic HOME under `home` (a new or previously generated folder). busy=True writes the large scene instead of the small one.
    Returns what tests and screenshots need."""
    home = os.path.realpath(home)
    check_folder(home)
    if is_real_home(home):
        raise SystemExit('refusing to write into your real HOME: %s' % home)
    prepare(home)
    put(os.path.join(home, MARK), MARK_TEXT)     # first, so a failed run can be re-run
    now = time.time() if now is None else now
    syn = (BusySynth if busy else Synth)(home, now)
    syn.build()
    info = {'home': home, 'claude': syn.claude, 'codex': syn.codex, 'orch': syn.sid, 'solo': SOLO, 'codex_thread': BUSY_CX_DEBATE if busy else CODEX_THREAD,
            'agents': dict(syn.agents), 'review': syn.review, 'units': dict(syn.unit), 'work': syn.work, 'now': now,
            'solo_cwd': os.path.join(home, 'work', 'demo-notes'), 'busy': busy, 'codex_files': list(getattr(syn, 'codex_files', []))}
    if links:
        info['links'] = add_link_scene(info)
    if stopped:
        info['stopped'] = add_stopped_scene(info)
    if codex_orch:
        info['codex_orch'] = add_codex_orch_scene(info)
    return info


LINK_CHILDREN = ('c11d0001-0000-4000-8000-000000000001', 'c11d0002-0000-4000-8000-000000000002', 'c11d0003-0000-4000-8000-000000000003',
                 'c11d0004-0000-4000-8000-000000000004', 'c11d0005-0000-4000-8000-000000000005')


def add_link_scene(info):
    """--links: the orchestrator runs `claude -p` from Bash twice, runs two more commands, and five `claude -p` sessions (entrypoint sdk-cli) start next to those calls.
    Each launch reads its instruction from a file (`"$(cat docs/prompts/x.md)"`) that no record shows being written, so the board cannot tell the words from the
    record: the children are linked by what it can see, a `claude -p` at a command position in the child's folder, as a guess (the time rule), whatever
    the stage of the judgment. What the board makes of them (board/link.py):
      c11d0001  started 3 s after a `claude -p` call in the same folder          -> linked as an agent, rule `time` (a guess: link.certain is false)
      c11d0002  the second call of a pair: same call, 3 s later                   -> linked, rule `time`
      c11d0003  also next to that call, but a call links one child (no loop)      -> unlinked, `ambiguous`
      c11d0004  started after a Bash call that was not a launch (`ls`), no process -> unlinked, `ended_before_seen`
      c11d0005  the same, with a fake `claude` process still running (start_live)   -> unlinked, `no_matching_call`
    The orchestrator's transcript is rewritten in time order with the new rows in it. Returns what start_live needs."""
    now, work, sid = info['now'], info['work'], info['orch']
    proj = os.path.join(info['claude'], 'projects', re.sub(r'[^A-Za-z0-9]', '-', work))
    path = os.path.join(proj, sid + '.jsonl')
    with open(path, encoding='utf-8') as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    mtime = os.stat(path).st_mtime
    t0 = now - 420                                         # the scene ends about 4 minutes ago
    c1, c2, c3, c4, c5 = LINK_CHILDREN
    calls = [(t0, 'claude -p "$(cat docs/prompts/lockfile.md)"', 'Ask a helper to re-check the pins'),
             (t0 + 40, 'claude -p "$(cat docs/prompts/todos.md)"', 'Ask a helper for a TODO summary'),
             (t0 + 130, 'ls docs/migration', 'List the migration folder'),
             (t0 + 190, 'pwd', 'Print the folder')]
    kids = [(c1, t0 + 3, 'Re-check the lockfile pins and list drift', 'The lockfile pins match, except one patch version that moved.'),
            (c2, t0 + 43, 'Summarize the open TODOs in docs/', 'Three TODOs are open: naming, retry docs, release notes.'),
            (c3, t0 + 48, 'Summarize the open TODOs in docs/ (second try)', 'Same three TODOs.'),
            (c4, t0 + 150, 'Print the build tags', 'The build tags are v2.0-rc1 and v2.0-rc2.'),
            (c5, t0 + 205, 'List the pending migrations', 'Working on it: two migrations are pending.')]
    for k, (t, cmd, desc) in enumerate(calls):
        use = 'toolu_' + hx('link-call', str(k), n=24)
        msg = {'id': 'msg_' + hx('link-call', str(k), n=24), 'model': CLAUDE_MODELS['opus'], 'role': 'assistant', 'stop_reason': 'tool_use',
               'content': [{'type': 'tool_use', 'id': use, 'name': 'Bash', 'input': {'command': cmd, 'description': desc}}], 'usage': Synth.usage(k + 1)}
        rows.append(Synth.line(t, 'assistant', work, sid, effort='high', message=msg))
        rows.append(Synth.line(t + 1.0, 'user', work, sid, message={'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': use, 'content': 'ok'}]}))
    keyed, last = [], ''
    for i, r in enumerate(rows):                          # a row without a time (a title row) stays after the row before it; ISO times sort as text
        last = r.get('timestamp') or last
        keyed.append((last, i, r))
    rows = [r for _, _, r in sorted(keyed, key=lambda x: x[:2])]
    put(path, ''.join(dump(r) + '\n' for r in rows), mtime)
    for cid, t, prompt, answer in kids:
        first = {'type': 'user', 'timestamp': iso(t), 'cwd': work, 'sessionId': cid, 'entrypoint': 'sdk-cli', 'message': {'role': 'user', 'content': prompt}}
        reply = Synth.line(t + 5, 'assistant', work, cid, message={'id': 'msg_' + hx('link-kid', cid, n=24), 'model': CLAUDE_MODELS['sonnet'], 'role': 'assistant',
                                                                   'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': answer}], 'usage': Synth.usage(2)})
        put(os.path.join(proj, cid + '.jsonl'), dump(first) + '\n' + dump(reply) + '\n', t + 5)
    return {'live': (c5, work, t0 + 205)}


CX_ORCH_ID = 'synth-codex-orch'
CX_ORCH_CLAUDE = {'helper': 'c0de0001-0000-4000-8000-000000000001', 'reviewer': 'c0de0002-0000-4000-8000-000000000002'}      # the two `claude -p` runs of the Codex orchestrator
CX_ORCH_SITE = 'acme-ledger'
CX_ORCH_BRIEF = """# Ledger rounding review

Common brief for the participants of the round. Each one writes its own report; the orchestrator writes the final text.

**A — Rounding rules**
**B — Currency handling**

Reports are written to `r1/<id>.md`. The final text goes to `CLOSING.md`.
"""


def add_codex_orch_scene(info):
    """--codex-orch: a Codex orchestrator (a TUI thread, `codex-tui`, source "cli") of the repository work/acme-ledger and the team around it, written in the shapes of the
    records of Codex 0.160 (tools/scenarios/cx_record.py writes them):
      native sub-agents   /root/s1 (finished: it handed its answer back, its parent wrote `completed` and then its message) and /root/s2 (still working); the first message of each
                          is a forwarding notice beside ciphertext, so what they were asked cannot be read
      guardian            an approval-review thread with three model calls (no card; its tokens are the page's approval-review tokens)
      `claude -p` runs    a helper started by a foreground call (still running: no record of its command yet; with --live it has a process whose environment names the
                          Codex thread and the root, as a Codex shell leaves it) and participant A of the debate (started with `setsid nohup ... &`, finished, wrote r1/A.md)
      `codex exec` run    participant B of the debate (`setsid nohup ... &`, `-o talk/r1/B.md`, finished)
      debate              work/acme-ledger/talk: a brief, r1/A.md and r1/B.md; no final text yet
    All of it ends a few minutes before now; the sub-agent /root/s2 and the helper are working. With --live a fake `codex` process holds the root's rollout open and a fake
    `claude` process is the helper; without it they read as ended. Returns what start_live needs ({'root', 'live', 'codex_files', ...})."""
    home, now, claude, codex = info['home'], info['now'], info['claude'], info['codex']
    work = os.path.join(home, 'work', CX_ORCH_SITE)
    m = lambda x: now - x * 60                                      # x minutes ago
    cid = CX_ORCH_ID
    os.makedirs(os.path.join(work, '.git'), exist_ok=True)
    put(os.path.join(work, '.git', 'HEAD'), 'ref: refs/heads/main\n')
    put(os.path.join(work, 'talk', 'brief.md'), CX_ORCH_BRIEF, m(40))
    ids = {r: cx_record.tid_of(cid, r) for r in ('root', 's1', 's2', 'guardian', 'reviewer_b')}
    turn = {r: 'turn-' + hx(cid, r, n=16) for r in ids}
    day = lambda t: '%04d/%02d/%02d' % time.localtime(t)[:3]

    def path_of(tid, t):
        lt = time.localtime(t)
        d = os.path.join(codex, 'sessions', *day(t).split('/'))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, 'rollout-%s-%s.jsonl' % (time.strftime('%Y-%m-%dT%H-%M-%S', lt), tid))

    def rollout(role, t, **kw):
        return cx_record.Rollout(path_of(ids[role], t), ids[role], work, cid, **kw)

    task = ('Audit the ledger package. Have one helper list the open ledger TODOs, start two reviewers for the debate in talk/ (A with claude -p, B with codex exec), '
            'and keep me posted.')
    root = rollout('root', m(52), origin='codex-tui')
    root.meta(m(52))
    root.task_started(m(52) + 0.5, turn['root'])
    root.turn_context(m(52) + 0.6, turn['root'])
    root.user(m(52) + 1.0, task, turn['root'])
    root.say(m(51.5), 'Starting with two native helpers.', turn['root'])
    paths = {'root': root.path}

    def sub(role, task_name, nick, at):
        parent_started = m(52)
        root.spawn(at, 'call_' + hx(cid, role, n=16), turn['root'], task_name, ids[role], '/root/' + task_name)
        r = rollout(role, at + 0.06, kind='sub', origin='codex-tui', parent=ids['root'], agent_path='/root/' + task_name, nick=nick, depth=1, root_id=ids['root'])
        r.fork(at + 0.06, root, turn['root'], task, parent_started)
        r.task_started(at + 0.07, turn[role])
        r.turn_context(at + 0.1, turn[role])
        r.agent_message(at + 0.2, '/root', '/root/' + task_name, 'Message from /root: your task is in the attached content.', cipher=True, turn=turn[role])
        r.usage(at + 20, turn[role], 14000, 600, 9000)
        paths[role] = r.path
        return r

    s1 = sub('s1', 's1', 'Atlas', m(50))
    s1.shell(m(49), 'cd %s && ls' % work, work, 'call_' + hx(cid, 's1ls', n=16), turn['s1'], end=m(49) + 0.3, out_at=m(49) + 0.35)
    s1.usage(m(44), turn['s1'], 16000, 900, 11000)
    s1.agent_message(m(42), '/root/s1', '/root', 'Progress: the rounding rules are in two modules.', msg_id='msg-s1-mid', turn=turn['root'])
    root.agent_message(m(42) + 0.01, '/root/s1', '/root', 'Progress: the rounding rules are in two modules.', msg_id='msg-s1-mid', turn=turn['root'])
    answer = 'The rounding rules live in two modules; both round half to even.'
    s1.agent_message(m(41) - 0.1, '/root/s1', '/root', answer, msg_id='msg-s1-end', turn=turn['root'])
    s1.say(m(41) - 0.05, answer, turn['s1'], final=True)
    s1.complete(m(41), turn['s1'], answer)
    root.sub_completed(m(41) + 0.005, turn['root'], turn['s1'], ids['s1'], '/root/s1')                  # `completed` first, the message after it
    root.agent_message(m(41) + 0.01, '/root/s1', '/root', answer, msg_id='msg-s1-end', turn=turn['root'])
    s2 = sub('s2', 's2', 'Birch', m(49))
    s2.shell(m(8), 'cd %s && ls tests' % work, work, 'call_' + hx(cid, 's2ls', n=16), turn['s2'], end=m(8) + 0.3, out_at=m(8) + 0.35)
    s2.usage(m(8), turn['s2'], 18000, 700, 12000)
    g = rollout('guardian', m(45), kind='guardian', origin='codex-tui', parent=ids['root'], root_id=ids['root'])
    g.meta(m(45))
    g.task_started(m(45) + 0.5, turn['guardian'])
    for i in range(3):
        g.usage(m(45) + 1 + i, turn['guardian'], 900 + 10 * i, 40)
    g.complete(m(45) + 5, turn['guardian'], 'approved')
    paths['guardian'] = g.path

    # the debate: A with `claude -p`, B with `codex exec`, both started with `setsid nohup ... &` (the call ends at once, so its record is there at once)
    a_text = 'You are participant A of the debate in talk/. Review the rounding rules of the ledger package and write your report to %s/talk/r1/A.md.' % work
    b_text = 'You are participant B of the debate in talk/. Review the currency handling of the ledger package and report in plain words.'
    a_cmd = 'cd %s && setsid nohup claude -p --model %s "%s" > /dev/null 2>&1 < /dev/null &' % (work, CLAUDE_MODELS['sonnet'], a_text)
    b_cmd = 'cd %s && setsid nohup codex exec -m %s -o %s/talk/r1/B.md "%s" > /dev/null 2>&1 < /dev/null &' % (work, CODEX_MODEL, work, b_text)
    helper_text = 'List the open ledger TODOs in five bullets.'
    h_cmd = 'cd %s && claude -p --model %s "%s"' % (work, CLAUDE_MODELS['sonnet'], helper_text)
    ta, tb, th = m(35), m(34.8), m(2.2)
    root.shell(ta, a_cmd, work, 'call_' + hx(cid, 'a', n=16), turn['root'], end=ta + 0.4, out_at=ta + 0.45)
    root.shell(tb, b_cmd, work, 'call_' + hx(cid, 'b', n=16), turn['root'], end=tb + 0.4, out_at=tb + 0.45)
    root.shell(th, h_cmd, work, 'call_' + hx(cid, 'h', n=16), turn['root'], end=None, out_at=None)          # a foreground call that has not returned: no record of it yet
    root.wait_agent(m(30), m(29.5), 'call_' + hx(cid, 'wait', n=16), turn['root'], [ids['s1']])
    root.usage(m(20), turn['root'], 30000, 1500, 20000)
    root.usage(m(3), turn['root'], 36000, 1800, 25000)
    root.say(m(2.5), 'Waiting for the helper.', turn['root'])
    # B: a `codex exec` thread (a root of its own, source "exec")
    b = rollout('reviewer_b', tb + 2.4, origin='codex_exec')
    b.meta(tb + 2.4)
    b.task_started(tb + 2.6, turn['reviewer_b'])
    b.turn_context(tb + 2.7, turn['reviewer_b'])
    b.user(tb + 3.0, b_text, turn['reviewer_b'])
    b.shell(tb + 20, 'sed -n 1,40p talk/brief.md', work, 'call_' + hx(cid, 'b-brief', n=16), turn['reviewer_b'], end=tb + 20.3, out_at=tb + 20.35, text=CX_ORCH_BRIEF.splitlines()[0],
            parsed=[{'type': 'read', 'cmd': 'sed -n 1,40p talk/brief.md', 'name': 'brief.md', 'path': 'talk/brief.md'}])
    b.usage(m(31), turn['reviewer_b'], 20000, 800, 14000)
    b_report = 'Currency handling: amounts are kept in minor units; two call sites still divide by 100 as floats.\n'
    b.say(m(27) - 0.5, b_report, turn['reviewer_b'], final=True)
    b.complete(m(27), turn['reviewer_b'], b_report)
    put(os.path.join(work, 'talk', 'r1', 'B.md'), b_report, m(27) + 0.3)
    paths['reviewer_b'] = b.path
    for r in (root, s1, s2, g, b):
        r.save()
    # the Claude records: participant A (finished, wrote its report) and the helper (still working)
    proj = os.path.join(claude, 'projects', re.sub(r'[^A-Za-z0-9]', '-', work))
    a_sid, h_sid = CX_ORCH_CLAUDE['reviewer'], CX_ORCH_CLAUDE['helper']
    syn = Synth(home, now)
    a_log, h_log = Jsonl(os.path.join(proj, a_sid + '.jsonl')), Jsonl(os.path.join(proj, h_sid + '.jsonl'))
    a_report = 'Rounding rules: both modules round half to even; the tests cover the ties only for positive amounts.\n'
    a_log.add(ta + 2.4, {'type': 'user', 'timestamp': iso(ta + 2.4), 'cwd': work, 'sessionId': a_sid, 'entrypoint': 'sdk-cli', 'message': {'role': 'user', 'content': a_text}})
    a_use = 'toolu_' + hx(cid, 'a-write', n=24)
    syn.assistant(a_log, ta + 40, 1, 'cx-a-write', work, a_sid, CLAUDE_MODELS['sonnet'], [{'type': 'tool_use', 'id': a_use, 'name': 'Write', 'input': {'file_path': os.path.join(work, 'talk', 'r1', 'A.md'), 'content': a_report}}], stop='tool_use')
    syn.result(a_log, ta + 40.5, work, a_sid, a_use, 'written')
    syn.assistant(a_log, ta + 60, 2, 'cx-a-end', work, a_sid, CLAUDE_MODELS['sonnet'], [{'type': 'text', 'text': 'The report is written.'}], stop='end_turn')
    put(os.path.join(work, 'talk', 'r1', 'A.md'), a_report, ta + 40.4)
    h_log.add(th + 2.4, {'type': 'user', 'timestamp': iso(th + 2.4), 'cwd': work, 'sessionId': h_sid, 'entrypoint': 'sdk-cli', 'message': {'role': 'user', 'content': helper_text}})
    h_use = 'toolu_' + hx(cid, 'h-grep', n=24)
    syn.assistant(h_log, th + 20, 3, 'cx-h-grep', work, h_sid, CLAUDE_MODELS['sonnet'],
                      [{'type': 'tool_use', 'id': h_use, 'name': 'Bash', 'input': {'command': 'grep -rn TODO ledger', 'description': 'Find the open TODOs'}}], stop='tool_use')
    a_log.save()
    h_log.save()
    env = {'CODEX_THREAD_ID': ids['root'], 'CODEX_SESSION_ID': ids['root'], 'CODEX_VERSION': '0.160.0', 'CODEX_CI': '1'}      # what a Codex shell leaves in the environment of what it starts
    return {'root': ids['root'], 'ids': ids, 'paths': paths, 'claude': dict(CX_ORCH_CLAUDE), 'work': work, 'texts': {'a': a_text, 'b': b_text, 'helper': helper_text},
            'live': [(h_sid, work, th + 2.4, env)], 'codex_files': [paths['root']]}


STOPPED_KIDS = {name: 'c0b5700%d-0000-4000-8000-00000000000%d' % (i, i) for i, name in enumerate(('exited', 'timelimit', 'crash', 'grand', 'great'), 1)}
STOPPED_PROMPTS = {'exited': 'Summarize the open TODOs under docs/ in five bullets', 'timelimit': 'List the stale branches and who last touched them',
                  'crash': 'Check every package for a missing LICENSE file', 'grand': 'Draft the v2 release notes from the settled rulings, grouped by package',
                  'great': 'List every breaking change that the rulings name, one per line'}


def add_stopped_scene(info):
    """--stopped: what the board shows when work stops and nests. Added to the orchestrator of either scene (the transcript is rewritten in time order, like add_link_scene):
      orchestrator    a usage limit as its last line (+ the notice "continuing automatically"): limit_wait, with fake `claude` process (--live); earlier in its record an API error
                      of each kind (529, 400, a login that expired) and one limit that has passed: the system lines of the message flow
      sub-agents      Lockfile audit: stopped by a usage limit (the same reset time as the orchestrator, so one notice for both) · Docs link check: stopped by a 529 an hour ago
                      (not resumed) · Release notes draft: working, and it starts a `claude -p` run that starts one in turn (a grandchild and a great-grandchild)
      `claude -p` runs  exited (closed without an end turn: interrupted/exited) · timelimit (the background time limit named in the launching call's notice) · crash (the record
                      stops and there is no process: ended) · grand (working, fake process) · great (done)
      debate          the one participant that is working in the running topic stops on a 529: its cell for the next report is paused; another one stops on a 529 after its
                      report was written: its cell is a draft that nobody is typing; a finished one hands its report back through SubagentHandback
    Returns {agents, kids, live, paused, held}; `live` is what start_live gives a fake process (the one working run)."""
    home, now, work, sid = info['home'], info['now'], info['work'], info['orch']
    syn = Synth(home, now)
    syn.sid, syn.work, syn.review = sid, work, info['review']
    m, proj = syn.m, syn.proj(work)
    path = os.path.join(proj, sid + '.jsonl')
    with open(path, encoding='utf-8') as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    last = max((r.get('timestamp') or '' for r in rows), default='')
    extra = []

    def call(t, name, inp, result, tid, **kw):
        msg = {'id': 'msg_' + hx('stopped-call', tid, n=24), 'model': CLAUDE_MODELS['opus'], 'role': 'assistant', 'stop_reason': 'tool_use',
               'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}], 'usage': Synth.usage(1)}
        extra.append(Synth.line(t, 'assistant', work, sid, effort='high', message=msg))
        extra.append(Synth.line(t + 1.0, 'user', work, sid, message={'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tid, 'content': result}]}, **kw))

    def api_error(t, status, kind, resets=None, into=extra, **kw):
        d = Synth.line(t, 'assistant', work, sid, message={'role': 'assistant', 'model': '<synthetic>', 'stop_reason': 'stop_sequence', 'content': [{'type': 'text', 'text': 'Synthetic API error line.'}],
                                                        'usage': {'input_tokens': 0, 'output_tokens': 0}}, error=kind, isApiErrorMessage=True, **kw)
        if status:
            d['apiErrorStatus'] = status
        if resets:
            d['quotaLimits'] = {'status': 'rejected', 'resetsAt': int(resets), 'rateLimitType': 'five_hour'}
        into.append(d)

    def notice(t, text):
        extra.append(Synth.line(t, 'system', work, sid, subtype='informational', content=text, level='warning'))

    # --- the orchestrator's own record: API errors it met along the way ---
    api_error(m(100), 529, 'server_error')                                      # passes: "can be retried"
    api_error(m(95), 400, 'invalid_request')                                    # the server refuses the request
    api_error(m(90), None, 'authentication_failed')                             # a login that expired
    api_error(m(85), 429, 'rate_limit', resets=now - 80 * 60)                   # a limit that has passed since
    notice(m(85) + 0.1, 'Usage limit reached \u00b7 continuing automatically at 10:40am \u00b7 esc or type to cancel')
    notice(m(80), 'Usage limit reset \u00b7 continuing automatically')

    # --- three sub-agents of their own ---
    kinds = (('LOCK', 'Lockfile audit', 'sonnet', 75.5), ('DOCS', 'Docs link check', 'sonnet', 74.5), ('NOTES', 'Release notes draft', 'opus', 50.5))
    for tag, desc, model, ago in kinds:
        syn.agents[tag], syn.spawn_use[tag] = 'a' + hx('stopped', tag), 'toolu_' + hx('stopped-spawn', tag, n=24)
        prompt = 'Task: %s for the v2 migration. Report findings in one paragraph when done.' % desc.lower()
        call(m(ago), 'Agent', {'description': desc, 'subagent_type': 'general-purpose', 'model': model, 'run_in_background': True, 'prompt': prompt},
             'Async agent launched successfully.\nagentId: %s' % syn.agents[tag], syn.spawn_use[tag],
             toolUseResult={'isAsync': True, 'status': 'async_launched', 'agentId': syn.agents[tag]})
    resets = now + 90 * 60                                                      # the reset time of the limit that stops the orchestrator and LOCK alike
    sw = {tag: SubWriter(syn, tag, desc, model, label=desc) for tag, desc, model, _ in kinds}
    s = sw['LOCK']
    s.user(m(75), 'Task: lockfile audit for the v2 migration. Report findings in one paragraph when done.')
    s.tool(m(74), 'Read', {'file_path': os.path.join(work, 'src', 'config', 'loader.py')}, LOADER_SNIPPET)
    s.say(m(60), 'Comparing the pins with the installed versions.')
    api_error(m(41), 429, 'rate_limit', resets=resets, into=s.log.rows_into(m(41)), isSidechain=True, agentId=s.aid)
    s = sw['DOCS']
    s.user(m(74), 'Task: docs link check for the v2 migration. Report findings in one paragraph when done.')
    s.tool(m(73), 'Read', {'file_path': os.path.join(work, 'docs', 'ops', 'env.md')}, '# Environment')
    api_error(m(70), 529, 'server_error', into=s.log.rows_into(m(70)), isSidechain=True, agentId=s.aid)
    # the grandchildren: NOTES starts `claude -p` (grand), and that run starts another (great)
    s = sw['NOTES']
    t_grand, t_great = m(9), m(6)
    s.user(m(50), 'Task: release notes draft for the v2 migration. Report findings in one paragraph when done.')
    s.tool(m(49), 'Read', {'file_path': os.path.join(work, 'docs', 'ops', 'env.md')}, '# Environment')
    s.tool(t_grand, 'Bash', {'command': 'claude -p "%s"' % STOPPED_PROMPTS['grand'], 'description': 'Draft the release notes in a separate run'}, 'ok')
    s.say(m(0.4), 'Waiting for the drafting run.')

    # --- `claude -p` runs that the orchestrator launched ---
    def child(name, t, ends, tools, owner_log=None):
        cid, log = STOPPED_KIDS[name], Jsonl(os.path.join(proj, STOPPED_KIDS[name] + '.jsonl'))
        log.add(t, {'type': 'user', 'timestamp': iso(t), 'cwd': work, 'sessionId': cid, 'entrypoint': 'sdk-cli', 'message': {'role': 'user', 'content': STOPPED_PROMPTS[name]}})
        for i, (dt, text, tool) in enumerate(tools):
            use = 'toolu_' + hx('stopped-kid', name, str(i), n=24)
            content = ([{'type': 'text', 'text': text}] if text else []) + ([{'type': 'tool_use', 'id': use, 'name': tool[0], 'input': tool[1]}] if tool else [])
            log.add(t + dt, Synth.line(t + dt, 'assistant', work, cid, message={'id': 'msg_' + hx('stopped-kid', name, str(i), n=24), 'model': CLAUDE_MODELS['sonnet'], 'role': 'assistant',
                                                                          'stop_reason': 'tool_use' if tool else 'end_turn', 'content': content, 'usage': Synth.usage(i + 1)}))
        if ends:
            log.add(t + ends, {'type': 'cost-state', 'timestamp': iso(t + ends), 'sessionId': cid, 'totalDuration': int(ends * 1000)})
        return log
    t1, t2, t3 = m(62), m(58), m(55)
    call(t1, 'Bash', {'command': 'claude -p "%s"' % STOPPED_PROMPTS['exited'], 'description': 'Ask a helper to summarize the TODOs'}, 'ok', 'toolu_' + hx('stopped-bash', '1', n=24))
    bg = 'b' + hx('stopped-bg', n=8)
    call(t2, 'Bash', {'command': 'claude -p "%s"' % STOPPED_PROMPTS['timelimit'], 'description': 'Ask a helper for the stale branches', 'run_in_background': True},
         'Command running in background with ID: ' + bg, 'toolu_' + hx('stopped-bash', '2', n=24), toolUseResult={'backgroundTaskId': bg})
    call(t3, 'Bash', {'command': 'claude -p "%s"' % STOPPED_PROMPTS['crash'], 'description': 'Ask a helper to check the licenses'}, 'ok', 'toolu_' + hx('stopped-bash', '3', n=24))
    text = ('<task-notification>\n<task-id>%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<status>failed</status>\n'
            '<summary>Background command "Ask a helper for the stale branches" hit the background time limit and was stopped</summary>\n</task-notification>') % (bg, 'toolu_' + hx('stopped-bash', '2', n=24))
    extra.append(Synth.line(m(27), 'user', work, sid, origin={'kind': 'task-notification'}, message={'role': 'user', 'content': text}))
    logs = [child('exited', t1 + 3, 40, [(8, 'Reading the docs folder.', ('Grep', {'pattern': 'TODO', 'path': os.path.join(work, 'docs')}))]),              # closed by cost-state, no end turn
            child('timelimit', t2 + 3, 1790, [(8, 'Listing the branches.', ('Bash', {'command': 'git branch -a'}))]),
            child('crash', t3 + 3, 0, [(8, 'Walking the packages.', ('Glob', {'pattern': 'packages/*/LICENSE*'}))]),                                         # the record just stops
            child('grand', t_grand + 3, 0, [(15, 'Collecting the rulings.', ('Read', {'file_path': os.path.join(info['review'], 'brief.md')}))]),
            child('great', t_great + 3, 60, [(20, 'Done: three breaking changes are named, one per line.', None)])]
    g = logs[3]                                                                  # grand: it starts `great` through Bash (a call in its own record)
    use = 'toolu_' + hx('stopped-kid-bash', n=24)
    g.add(t_great, Synth.line(t_great, 'assistant', work, STOPPED_KIDS['grand'], message={'id': 'msg_' + hx('stopped-kid-bash', n=24), 'model': CLAUDE_MODELS['sonnet'], 'role': 'assistant',
          'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': use, 'name': 'Bash', 'input': {'command': 'claude -p "%s"' % STOPPED_PROMPTS['great'], 'description': 'List the breaking changes in a separate run'}}],
          'usage': Synth.usage(3)}))
    g.add(t_great + 1.0, Synth.line(t_great + 1.0, 'user', work, STOPPED_KIDS['grand'], message={'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': use, 'content': 'ok'}]}))
    g.add(now - 12, Synth.line(now - 12, 'assistant', work, STOPPED_KIDS['grand'], message={'id': 'msg_' + hx('stopped-kid-last', n=24), 'model': CLAUDE_MODELS['sonnet'], 'role': 'assistant',
          'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': 'toolu_' + hx('stopped-kid-last', n=24), 'name': 'Read', 'input': {'file_path': os.path.join(info['review'], 'brief.md')}}], 'usage': Synth.usage(4)}))
    for lg in logs:
        lg.save()
    for sub in sw.values():
        sub.save()

    # --- the debate participant that is still working stops on a 529: its next cell is paused ---
    paused = 'T5-B' if info.get('busy') else 'T2-B'
    pid = info['agents'][paused]
    ppath = os.path.join(proj, sid, 'subagents', 'agent-%s.jsonl' % pid)
    with open(ppath, encoding='utf-8') as f:
        prow = [json.loads(ln) for ln in f if ln.strip()]
    plast = max(parse_iso(r['timestamp']) for r in prow if r.get('timestamp'))
    prow.append(Synth.line(plast + 0.3, 'assistant', work, sid, message={'role': 'assistant', 'model': '<synthetic>', 'stop_reason': 'stop_sequence', 'content': [{'type': 'text', 'text': 'Synthetic API error line.'}],
                                                                      'usage': {'input_tokens': 0, 'output_tokens': 0}}, error='server_error', isApiErrorMessage=True, apiErrorStatus=529, isSidechain=True, agentId=pid))
    put(ppath, ''.join(dump(r) + '\n' for r in prow), plast + 0.3)

    # --- a participant that wrote its report and was stopped by a 529 before it finished: the report file stays, so its cell is a draft that nobody is typing ---
    held = 'T5-C' if info.get('busy') else 'T2-A'
    hid = info['agents'][held]
    hpath = os.path.join(proj, sid, 'subagents', 'agent-%s.jsonl' % hid)
    with open(hpath, encoding='utf-8') as f:
        hrow = [json.loads(ln) for ln in f if ln.strip()]
    while hrow and ((hrow[-1].get('message') or {}).get('stop_reason') != 'end_turn'):
        hrow.pop()
    end = hrow.pop() if hrow else None                                               # its closing message: the 529 comes in its place
    hlast = parse_iso(end['timestamp']) if end else now - 600
    hrow.append(Synth.line(hlast, 'assistant', work, sid, message={'role': 'assistant', 'model': '<synthetic>', 'stop_reason': 'stop_sequence', 'content': [{'type': 'text', 'text': 'Synthetic API error line.'}],
                                                                'usage': {'input_tokens': 0, 'output_tokens': 0}}, error='server_error', isApiErrorMessage=True, apiErrorStatus=529, isSidechain=True, agentId=hid))
    put(hpath, ''.join(dump(r) + '\n' for r in hrow), hlast)

    # --- a finished participant hands its final report back through the SubagentHandback tool (the board words that call itself) ---
    done = info['agents']['T1-A']
    dpath = os.path.join(proj, sid, 'subagents', 'agent-%s.jsonl' % done)
    with open(dpath, encoding='utf-8') as f:
        drow = [json.loads(ln) for ln in f if ln.strip()]
    at = parse_iso(drow[-1]['timestamp']) - 0.5
    use = 'toolu_' + hx('stopped-handback', n=24)
    drow.insert(len(drow) - 1, Synth.line(at, 'assistant', work, sid, message={'id': 'msg_' + hx('stopped-handback', n=24), 'model': CLAUDE_MODELS['sonnet'], 'role': 'assistant', 'stop_reason': 'tool_use',
                                                                          'content': [{'type': 'tool_use', 'id': use, 'name': 'SubagentHandback', 'input': {}}], 'usage': Synth.usage(9)}, isSidechain=True, agentId=done))
    drow.insert(len(drow) - 1, Synth.line(at + 0.2, 'user', work, sid, message={'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': use, 'content': 'Report handed back'}]}, isSidechain=True, agentId=done))
    put(dpath, ''.join(dump(r) + '\n' for r in drow), at + 0.5)

    # --- the orchestrator ends on a usage limit (the last lines of its record) ---
    tl = max(parse_iso(last) + 2 if last else 0, now - 30)
    api_error(tl, 429, 'rate_limit', resets=resets)
    notice(tl + 0.1, 'Usage limit reached \u00b7 continuing automatically at %s \u00b7 esc or type to cancel' % time.strftime('%H:%M', time.gmtime(resets)))
    extra.append(Synth.line(tl + 0.2, 'system', work, sid, subtype='turn_duration', durationMs=30000, pendingBackgroundAgentCount=3))
    keyed, lastk = [], ''
    for i, r in enumerate(rows + extra):
        lastk = r.get('timestamp') or lastk
        keyed.append((lastk, i, r))
    put(path, ''.join(dump(r) + '\n' for _, _, r in sorted(keyed, key=lambda x: x[:2])), now)
    return {'agents': {t: syn.agents[t] for t in sw}, 'kids': dict(STOPPED_KIDS), 'live': [(STOPPED_KIDS['grand'], work, t_grand + 3)], 'paused': paused, 'held': held, 'resets': resets}


def parse_iso(text):
    """Epoch seconds of one of our timestamps (iso() above: UTC, milliseconds)."""
    return calendar.timegm(time.strptime(text[:19], '%Y-%m-%dT%H:%M:%S')) + float(text[19:-1] or 0)


def fake_cmdline(pid):
    """The command line of pid as text, or None. Only used to make sure --stop signals our own fake processes."""
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as f:
            return f.read().replace(b'\0', b' ').decode('utf-8', 'replace').strip()
    except OSError:
        pass
    try:
        r = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        return r.stdout.decode('utf-8', 'replace').strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def start_live(info):
    """One fake `claude` process per session (a `sleep` whose argv[0] is `claude`) plus ~/.claude/sessions/<pid>.json. Returns the pids.
    With the busy scene, also one fake `codex` process per Codex rollout, holding that rollout open (so a running turn does not read as ended);
    those pids are listed in <home>/.synth-live.json."""
    sleep = shutil.which('sleep')
    if not sleep:
        raise SystemExit('no `sleep` on PATH')
    pids = []
    orch_pid = None
    for sid, cwd, name in ((info['orch'], info['work'], 'config loader review' if not info.get('busy') else 'acme-robot v2 migration'), (info['solo'], info['solo_cwd'], 'demo notes')):
        p = subprocess.Popen(['claude', str(LIVE_SECONDS)], executable=sleep, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        put(os.path.join(info['claude'], 'sessions', '%d.json' % p.pid),
            json.dumps({'pid': p.pid, 'sessionId': sid, 'cwd': cwd, 'startedAt': int(info['now'] * 1000), 'kind': 'interactive', 'name': name,
                        'synthHome': True}))        # --stop removes only files that carry this
        pids.append(p.pid)
        if sid == info['orch']:
            orch_pid = p.pid
    if info.get('links'):                          # --links: the child that is still running (the board cannot link it: no_matching_call)
        sid, cwd, started = info['links']['live']
        p = subprocess.Popen(['claude', str(LIVE_SECONDS)], executable=sleep, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        put(os.path.join(info['claude'], 'sessions', '%d.json' % p.pid),
            json.dumps({'pid': p.pid, 'sessionId': sid, 'cwd': cwd, 'startedAt': int(started * 1000), 'kind': 'sdk-cli', 'entrypoint': 'sdk-cli', 'name': 'helper',
                        'synthHome': True}))
        pids.append(p.pid)
    for sid, cwd, started in (info.get('stopped') or {}).get('live', []):         # --stopped: the run that is still working
        # a shell of a Claude session leaves the session's id and its process number in the environment of what it starts: this run was started from the orchestrator's
        p = subprocess.Popen(['claude', str(LIVE_SECONDS)], executable=sleep, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                             env=dict(PATH=os.environ.get('PATH', ''), CLAUDE_CODE_SESSION_ID=info['orch'], CLAUDE_PID=str(orch_pid)))
        put(os.path.join(info['claude'], 'sessions', '%d.json' % p.pid),
            json.dumps({'pid': p.pid, 'sessionId': sid, 'cwd': cwd, 'startedAt': int(started * 1000), 'kind': 'sdk-cli', 'entrypoint': 'sdk-cli', 'name': 'helper', 'synthHome': True}))
        pids.append(p.pid)
    fake_codex = []                                # the fake `codex` processes of a scene (each holds a rollout open); they are listed in LIVE_FILE
    if info.get('busy'):
        for path in info['codex_files']:
            with open(path, 'rb') as rollout:
                p = subprocess.Popen(['codex', str(LIVE_SECONDS)], executable=sleep, stdin=rollout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            fake_codex.append({'pid': p.pid, 'cmd': 'codex %d' % LIVE_SECONDS})
            pids.append(p.pid)
    co = info.get('codex_orch')
    if co:                                         # --codex-orch: a fake `codex` process holds the root's rollout; a fake `claude` process is the helper, with the environment a Codex shell leaves
        with open(co['paths']['root'], 'rb') as rollout:
            p = subprocess.Popen(['codex', str(LIVE_SECONDS)], executable=sleep, stdin=rollout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        fake_codex.append({'pid': p.pid, 'cmd': 'codex %d' % LIVE_SECONDS})
        pids.append(p.pid)
        for sid, cwd, started, env in co['live']:
            p = subprocess.Popen(['claude', str(LIVE_SECONDS)], executable=sleep, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, env=dict(env, PATH=os.environ.get('PATH', '')))
            put(os.path.join(info['claude'], 'sessions', '%d.json' % p.pid),
                json.dumps({'pid': p.pid, 'sessionId': sid, 'cwd': cwd, 'startedAt': int(started * 1000), 'kind': 'sdk-cli', 'entrypoint': 'sdk-cli', 'name': 'helper', 'synthHome': True}))
            pids.append(p.pid)
    if info.get('busy') or co:
        put(os.path.join(info['home'], LIVE_FILE), json.dumps({'synthHome': True, 'procs': fake_codex}))
    return pids


def stop_live(home, quiet=False):
    """Stop the fake processes of a synthetic HOME and remove their registration files. Does nothing for the real HOME or a folder this tool
    did not make. Only a file named <pid>.json that carries our marker (`synthHome`) and that pid is touched: it is signalled only if the
    process still looks like `claude <seconds>`, and the file is removed either way (the pid may have been reused). Other files stay.
    The fake codex processes of the busy scene are listed in .synth-live.json (same marker, same rule: signalled only while the command line
    is exactly `codex <seconds>`)."""
    base, stopped, kept = os.path.realpath(home), [], 0
    sdir = os.path.join(base, '.claude', 'sessions')
    ours = not is_real_home(base) and is_ours(base)

    def stop(pid, cmd):
        if (fake_cmdline(pid) or '') == cmd:
            try:
                os.kill(pid, signal.SIGTERM)
                stopped.append(pid)
            except OSError:
                pass

    if ours and os.path.isdir(sdir) and os.path.realpath(sdir) == sdir:
        for f in sorted(os.listdir(sdir)):
            p = os.path.join(sdir, f)
            try:
                with open(p) as fh:
                    d = json.load(fh)
                pid = int(d['pid'])
                mine = re.fullmatch(r'(\d+)\.json', f) and int(f[:-5]) == pid and d.get('synthHome') is True and not os.path.islink(p)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                mine = False
            if not mine:
                kept += 1
                continue
            stop(pid, 'claude %d' % LIVE_SECONDS)
            os.remove(p)
    lf = os.path.join(base, LIVE_FILE)
    if ours and os.path.isfile(lf) and not os.path.islink(lf):
        try:
            with open(lf) as fh:
                d = json.load(fh)
            if d.get('synthHome') is True:
                for q in d.get('procs') or []:
                    stop(int(q['pid']), 'codex %d' % LIVE_SECONDS)
                os.remove(lf)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    if not quiet:
        print('stopped %d fake process(es)%s%s' % (len(stopped), ': ' + ' '.join(map(str, stopped)) if stopped else '',
                                                  '; left %d file(s) this tool did not write' % kept if kept else ''))
    return stopped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('folder', help='where to write the synthetic HOME (new or empty, or one this tool made before)')
    ap.add_argument('--live', action='store_true', help='also start fake `claude` processes + ~/.claude/sessions/<pid>.json so the sessions show as working')
    ap.add_argument('--busy', action='store_true', help='the large scene instead of the small one: acme-robot, 8 topics (6 settled, 2 running), 33 agents; for screenshots')
    ap.add_argument('--links', action='store_true', help='add five `claude -p` children next to the orchestrator\'s Bash calls: two the board can only guess (rule time), three it could not link (unlinked); with --live one is still running')
    ap.add_argument('--stopped', action='store_true', help='add work that stopped and nests: a usage limit (the orchestrator waits for the reset), API errors, runs cut off, a paused debate cell, a grandchild; use with --live')
    ap.add_argument('--codex-orch', action='store_true', help='add a Codex orchestrator (a TUI thread of work/acme-ledger): two native sub-agents, a guardian thread, two `claude -p` runs and a `codex exec` run started from its shell, a small debate in talk/; use with --live')
    ap.add_argument('--stop', action='store_true', help='only stop the fake processes of that folder (a folder this tool made; never your real HOME)')
    args = ap.parse_args()
    home = os.path.realpath(args.folder)
    if args.stop:
        check_ours(home)
        stop_live(home)
        return
    info = build(home, busy=args.busy, links=args.links, stopped=args.stopped, codex_orch=args.codex_orch)
    print('synthetic HOME: %s%s' % (home, ' (busy scene)' if args.busy else ''))
    if args.busy:
        print('  Claude sessions: orchestration %s (31 sub-agents + 2 Codex), plain conversation %s' % (info['orch'][:8], info['solo'][:8]))
        print('  debate folder:   %s (8 topics: 6 settled, T3 and T5 running; 9 package workers)' % info['review'])
    else:
        print('  Claude sessions: orchestration %s (4 sub-agents + 1 Codex), plain conversation %s' % (info['orch'][:8], info['solo'][:8]))
        print('  debate folder:   %s (T1 closed by rulings.md, T2 in round 2)' % info['review'])
    if args.links:
        print('  link scene:      2 children linked by a guess (rule time), 3 not linked (ambiguous, ended_before_seen, no_matching_call with --live)')
    if args.stopped:
        print('  stopped scene:   the orchestrator waits on a usage limit (--live), 3 more sub-agents (one limit, one 529, one working), 5 `claude -p` runs (exited, time limit, crashed, grandchild, great-grandchild), debate cell paused')
    if args.codex_orch:
        print('  codex scene:     Codex orchestrator %s (2 native sub-agents, 1 guardian, 2 `claude -p` runs, 1 `codex exec` run) in %s, debate folder talk/' % (info['codex_orch']['root'][:8], info['codex_orch']['work']))
    print('run the board on it (port 8811 is just an example):')
    print('  env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME=%s python3 server.py --port 8811' % home)
    if args.live:
        pids = start_live(info)
        print('fake `claude`%s processes started (they end by themselves in %d h): %s' % (' and `codex`' if args.busy else '', LIVE_SECONDS // 3600, ' '.join(map(str, pids))))
        print('stop them:  python3 %s %s --stop     (or: kill %s)' % (os.path.relpath(os.path.abspath(__file__)), home, ' '.join(map(str, pids))))


if __name__ == '__main__':
    main()
