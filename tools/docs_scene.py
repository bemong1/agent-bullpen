#!/usr/bin/env python3
"""The synthetic HOME that the README pictures (docs/images/{en,ko}) are taken from: a calm office, not the crowded stress scene of `synth_home.py --busy`.

    python3 tools/docs_scene.py /tmp/acme-docs --live     # write the HOME + start fake `claude` / `codex` processes so the sessions read as working
    python3 tools/docs_scene.py /tmp/acme-docs --stop     # stop them again (only in a folder this tool made)
    env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME=/tmp/acme-docs python3 server.py --port 8811

What the board shows on it (acme-robot, seven topics; every name is made up, nothing real is read):
  orchestrator     working (the usage-limit tail of `synth_home.add_stopped_scene` is cut off again)
  debate rooms     working: T3 Retry policy (round 2, one of the three is Codex), T6 Test layout (round 1, three) and T7 Package boundaries (round 1, two reviewers): eight people
                   closed: T4 Config schema settled 12 minutes ago, so its room is still there and its three reviewers rest
                   stalled: T5 Logging format, round 2 stopped on API errors: one reviewer has submitted, two rest with a draft nobody is typing
                   T1 and T2 settled long ago (no room)
  lounge           the reviewers who are not working, six: T4 (3) and T5 (3). The two stopped ones of T5 sit on the sofa; the other four pick sofa, tea table, games, gym or coffee,
                   and the pick changes every few minutes (see tools/shoot_docs.js, which fixes the moment it takes the pictures at)
  other work       a release-notes run and the `claude -p` run it started are working; the run that one started is done (the tree of the agent list): two people
  agent list       the stopped ones of `add_stopped_scene`: usage limit, API errors, background time limit, exited early
Built on `synth_home` (the scene classes, `add_stopped_scene`, `start_live`, `stop_live`); this file only says which topic is where.
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import synth_home  # noqa: E402
from synth_home import BUSY_TOPICS, BusySynth, put  # noqa: E402

# (key, minutes ago the topic was launched). Everything else (title, question, roles, final file) is the topic of the same key in synth_home.BUSY_TOPICS.
# Topic start rules (see BusySynth.times): a settled topic has its ruling at start-46 minutes and keeps its room for 20 minutes after its last reviewer's activity.
DOCS_TOPICS = [('t1_naming', 290), ('t2_errors', 262), ('t3_retry', 90), ('t4_config', 58), ('t5_logging', 200), ('t6_tests', 20), ('t7_packages', 38)]
RUNNING = ('t3_retry', 't6_tests', 't7_packages')
TWO = {'t7_packages': 2}          # topics with two reviewers instead of three
STALLED = ('t5_logging',)         # round 2 stopped: add_stopped_scene ends two of its reviewers on an API error (a draft nobody is typing), so it has no ruling and keeps its room
DEPENDS = {'t1_naming': '—', 't2_errors': 'T1', 't3_retry': 'T2', 't4_config': '—', 't5_logging': 'T2', 't6_tests': '—', 't7_packages': 'T1'}


T5_REST = {     # synth_home has only the round-1 texts of T5 (a running topic of its busy scene); here its round 2 was written hours ago
    'r2': {'A': ('a fixed set of fields', 'free text stays in one `msg` field'), 'B': ('fixed fields accepted', 'sampling only above INFO'),
           'C': ('no objections', 'ids are hashed with a per-day salt')},
}


class DocsSynth(BusySynth):
    """acme-robot with seven topics: three running, one settled a few minutes ago, three settled long ago; no package workers (the other-work room comes from the stopped scene)."""

    def __init__(self, home, now):
        BusySynth.__init__(self, home, now)
        start = dict(DOCS_TOPICS)
        keep = {t[0]: t for t in BUSY_TOPICS if t[0] in start}
        self.topics = {k: keep[k][:3] + (keep[k][3][:TWO.get(k, 3)], start[k], keep[k][5]) for k, _ in DOCS_TOPICS}      # same title, question and roles; the launch time is this scene's
        self.unit = {k: v for k, v in self.unit.items() if k in self.topics}
        tags = {self.tag(k, w) for k in self.topics for w in 'ABC'[:len(self.topics[k][3])]}
        self.agents = {t: a for t, a in self.agents.items() if t in tags}
        self.spawn_use = {t: u for t, u in self.spawn_use.items() if t in tags}

    def usage(self, k, base=18000):
        """Token counts of row k: a context that grows slowly (the orchestrator ends at about half of its 200k window, not at its limit)."""
        return {'input_tokens': 5 + k % 4, 'cache_creation_input_tokens': 2600 + (k * 71) % 900, 'cache_read_input_tokens': base + 1500 * k, 'output_tokens': 650 + (k * 97) % 1400}

    def build(self):
        """Lend T5 its round-2 texts for the length of the build (synth_home reads BUSY_TEXT when it writes a report)."""
        texts = synth_home.BUSY_TEXT
        synth_home.BUSY_TEXT = dict(texts, t5_logging=dict(texts['t5_logging'], **T5_REST))
        try:
            BusySynth.build(self)
        finally:
            synth_home.BUSY_TEXT = texts

    # ---------- documents ----------
    def brief_text(self):
        rows = '\n'.join('| T%s %s | `%s/` | %s | %s |' % (k[1], self.topics[k][1], k, DEPENDS[k], '`final/%s.md`' % self.topics[k][5] if self.topics[k][5] else '') for k in self.topics)
        return ('# acme-robot v2 interface migration\n\nCommon brief for every participant. Seven independent topics; each has its own folder, topic brief and reports.\n\n'
                '| Topic | Folder | Depends on | Final |\n|---|---|---|---|\n' + rows + '\n\n## Rules\n- Round 1: write your report alone. Do not read other participants\' reports.\n'
                '- Round 2: read the others\' round-1 reports, then write `r2/<you>.md`: what you now accept, what you still contest.\n'
                '- Reports are markdown, at most 40 lines. Cite file paths and line numbers.\n- The orchestrator writes the ruling once round 2 is in.\n')

    # one topic's three participants: (start, r1 end, r2 start, r2 end) per seat, in minutes ago (None = not yet)
    def times(self, key, who):
        s = self.topics[key][4]
        if key == 't3_retry':
            return {'A': (s, 71.0, 22.0, None), 'B': (s, 70.0, 22.0, None), 'C': (s, 67.5, 21.5, None)}[who]                         # round 2 is being written by all three
        if key in ('t6_tests', 't7_packages'):
            return (s, None, None, None)                                                                                             # round 1 is being written by all three
        return {'A': (s, s - 19.0, s - 24.0, s - 40.0), 'B': (s, s - 20.0, s - 24.0, s - 42.0), 'C': (s, s - 22.0, s - 24.0, s - 44.0)}[who]

    # ---------- the orchestrator's conversation and the topics ----------
    def write_conversation_and_topics(self, main):
        m = self.m
        main.human(m(300), 'We are moving acme-robot to the v2 interface. Seven open questions (naming, errors, retries, config, logging, tests, package boundaries). '
                           'Settle each with independent reviewers, keep the written record under docs/migration, and tell me when a ruling needs me.')
        main.log.add(m(299.8), {'type': 'ai-title', 'aiTitle': 'acme-robot v2 migration'})
        main.say(m(299.5), 'Plan: one folder per topic with a topic brief, three reviewers each, two rounds, then a ruling. I start with the topics nothing else depends on and '
                           'launch the rest as their inputs settle.')
        main.tool(m(299.0), 'Write', {'file_path': os.path.join(self.review, 'brief.md'), 'content': self.brief_text()}, 'File created')
        for key in self.topics:
            self.topic(main, key)
        main.say(m(93.8), 'T1 and T2 are settled (rulings under docs/migration). T5 round 2 stopped: two of its reviewers hit API errors, I will rerun them later. '
                          'Next: T3 retry policy; T4, T6 and T7 do not wait for anything and follow.')
        main.human(m(93.0), 'Good. Start with T3, then the others as soon as a reviewer slot frees up.')
        main.say(m(92.6), 'Starting T3 now; T4, T7 and T6 follow.')
        main.turn_end(m(92.2), 0)
        main.say(m(57.5), 'T3 round 1 is in. T4 config schema starts now.')
        main.say(m(37.5), 'T4 round 1 is in. Starting T7 package boundaries now.')
        main.say(m(20.5), 'T3 and T4 are in round 2. Launching T6 test layout now.')
        main.human(m(13), 'Where do we stand?')
        main.say(m(12.5), 'T4 is about to close. T3 round 2 is being written, T7 and T6 are in round 1. T5 is still waiting for its two reviewers.')
        main.tool(m(1.6), 'Read', {'file_path': self.final_path('t4_config')}, self.rulings_text('t4_config'))
        main.say(m(1.1), 'Status: T3 is in round 2, T6 and T7 in round 1; T4 is closed (final/config.md). A release-notes run is drafting from the settled rulings.')

    def topic(self, main, key):
        m = self.m
        _k, title, _q, roles, start, fin = self.topics[key]
        for who in 'ABC'[:len(roles)]:
            if key == 't3_retry' and who == 'C':
                continue                                       # a Codex thread, not an Agent-tool sub-agent
            tag, role, model = self.tag(key, who), roles['ABC'.index(who)][0], synth_home.BUSY_ROLES[who]
            main.tool(m(start - 0.1 * 'ABC'.index(who)), 'Agent', {'description': '%s %s' % (tag, role), 'subagent_type': 'general-purpose', 'model': model,
                                                                    'run_in_background': True, 'prompt': self.spawn_prompt(tag, role, key, who)},
                      'Async agent launched successfully.\nagentId: %s' % self.agents[tag], tid=self.spawn_use[tag],
                      toolUseResult={'isAsync': True, 'status': 'async_launched', 'agentId': self.agents[tag]})
            self.debate_agent(main, key, who, tag, role, model, key in RUNNING)
        if key == 't3_retry':
            self.codex_debate_launch(main, 1, m(start - 0.4))
            main.say(m(start - 0.3), 'T3 reviewers are launched; C is a Codex cross-check.')
            main.turn_end(m(start - 1), 4)
            main.codex_notify(m(66.6), 'cx1')
            for who in 'AB':
                main.tool(m(22.0), 'SendMessage', {'to': self.agents[self.tag(key, who)], 'summary': 'Round 2: cross-read and answer', 'message': self.round2_message(key, who)}, 'Message sent')
            self.codex_debate_launch(main, 2, m(21.5))
            return
        if key in ('t6_tests', 't7_packages'):
            return                                             # round 1 is still being written
        for who in 'ABC'[:len(roles)]:
            main.tool(m(start - 24.0), 'SendMessage', {'to': self.agents[self.tag(key, who)], 'summary': 'Round 2: cross-read and answer', 'message': self.round2_message(key, who)}, 'Message sent')
        if key in STALLED:
            return                                             # round 2 stopped on API errors: no ruling
        fpath, text, t_fin = self.final_path(key), self.rulings_text(key), start - 46.0
        main.tool(m(t_fin + 0.3), 'Write', {'file_path': fpath, 'content': text}, 'File created')
        put(fpath, text, m(t_fin))
        main.say(m(t_fin - 0.4), 'T%s settled: %s (%s).' % (key[1], synth_home.BUSY_TEXT[key]['rule'][0].rstrip('.'), os.path.relpath(fpath, self.review)))
        main.turn_end(m(t_fin - 0.8), 0)

    def write_workers(self, main):
        pass                                                   # no package workers: the only other work is the release-notes tree of the stopped scene

    def write_codex_threads(self, main):
        """Only the T3 cross-check: round 1 reported, round 2 running (the Codex seat of a debate room)."""
        m = self.m
        unit = self.unit['t3_retry']
        t1 = self.codex_calls['cx1'][2] + 2.4
        t2 = self.codex_calls['cx2'][2] + 1.2
        self.codex_rollout(synth_home.BUSY_CX_DEBATE, t1, [
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


def cut_limit_tail(info):
    """`add_stopped_scene` ends the orchestrator on a usage limit (a 429 line, its notice and the turn end). Drop those lines: the orchestrator is working in this scene,
    while the sub-agent that hit the limit (and its reset time) stays in the agent list."""
    path = os.path.join(info['claude'], 'projects', re.sub(r'[^A-Za-z0-9]', '-', info['work']), info['orch'] + '.jsonl')
    with open(path, encoding='utf-8') as f:
        rows = [json.loads(ln) for ln in f if ln.strip()]
    resets = int(info['stopped']['resets'])
    cut = [i for i, r in enumerate(rows) if (r.get('quotaLimits') or {}).get('resetsAt') == resets]
    if len(cut) != 1:
        raise SystemExit('the usage-limit line of the orchestrator was not found once (found %d): synth_home.add_stopped_scene changed' % len(cut))
    kept = rows[:cut[0]]
    put(path, ''.join(synth_home.dump(r) + '\n' for r in kept), os.stat(path).st_mtime)


def build(home, now=None):
    """Write the docs scene under `home` (a new or previously generated folder). Returns what start_live needs (like synth_home.build)."""
    home = os.path.realpath(home)
    synth_home.check_folder(home)
    if synth_home.is_real_home(home):
        raise SystemExit('refusing to write into your real HOME: %s' % home)
    synth_home.prepare(home)
    put(os.path.join(home, synth_home.MARK), synth_home.MARK_TEXT)
    now = time.time() if now is None else now
    syn = DocsSynth(home, now)
    syn.build()
    info = {'home': home, 'claude': syn.claude, 'codex': syn.codex, 'orch': syn.sid, 'solo': synth_home.SOLO, 'codex_thread': synth_home.BUSY_CX_DEBATE,
            'agents': dict(syn.agents), 'review': syn.review, 'units': dict(syn.unit), 'work': syn.work, 'now': now,
            'solo_cwd': os.path.join(home, 'work', 'demo-notes'), 'busy': True, 'codex_files': list(syn.codex_files)}
    info['stopped'] = synth_home.add_stopped_scene(info)
    cut_limit_tail(info)
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('folder', help='where to write the synthetic HOME (new or empty, or one this tool made before)')
    ap.add_argument('--live', action='store_true', help='also start the fake `claude` and `codex` processes so the sessions show as working')
    ap.add_argument('--stop', action='store_true', help='only stop the fake processes of that folder (a folder this tool made; never your real HOME)')
    args = ap.parse_args()
    home = os.path.realpath(args.folder)
    if args.stop:
        synth_home.check_ours(home)
        synth_home.stop_live(home)
        return
    info = build(home)
    print('docs scene: %s' % home)
    print('  run the board on it: env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME=%s python3 server.py --port 8811' % home)
    if args.live:
        pids = synth_home.start_live(info)
        print('fake processes started (they end by themselves in %d h): %s' % (synth_home.LIVE_SECONDS // 3600, ' '.join(map(str, pids))))
        print('stop them:  python3 %s %s --stop' % (os.path.relpath(os.path.abspath(__file__)), home))


if __name__ == '__main__':
    main()
