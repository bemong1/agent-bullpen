"""Tests of linking with a Codex thread as the launcher: a root thread (and each sub-agent of it) that ran `claude -p` or `codex exec` through its commands, the
facts of the Codex index (board/codex_index.py: the commands a thread ran, where they may be missing, the kind of each thread) read by the same judgment as a Claude
session's Bash calls, the environment and the process lineage of a Codex shell, and the graph of who started whom.

Every record is made up: only the shape follows what Codex and Claude Code write. The processes are a fake /proc.

    python3 -m unittest discover -s tests
"""
import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402
from test_stage2 import bash_line, child_lines, dump, result_line  # noqa: E402,F401  (functions only: importing a TestCase would run it twice)
from test_proclink import C, D, P, Q, Fixture, NS, UID  # noqa: E402,F401
from test_codex_facts import activity, cmd_item, exec_call, item_payload, line, output, root_meta, sub_meta  # noqa: E402,F401

from board import affil, lineage, link, procs  # noqa: E402

T0 = 1791100000.0
TEXT = ('Please review the amber basin cedar delta ember fjord grove harbor island juniper kelp lagoon meadow nectar orchard prairie quartz ridge summit '
        'tundra umber valley willow xenon yarrow zephyr and report in plain words.')
CX = 'aaaaaaaa-0000-4000-8000-000000000001'       # a Codex root thread
SUB = 'aaaaaaaa-0000-4000-8000-000000000002'      # its sub-agent
SUB2 = 'aaaaaaaa-0000-4000-8000-000000000003'     # a sub-agent of the sub-agent
CX2 = 'aaaaaaaa-0000-4000-8000-000000000004'      # another Codex root (a `codex exec` child of someone)
CX3 = 'aaaaaaaa-0000-4000-8000-000000000005'
K = '44444444-4444-4444-8444-444444444444'       # a `claude -p` child
K2 = '55555555-5555-4555-8555-555555555555'


def sdk_lines(t, text, cwd='/w'):
    """A `claude -p` child's record with its entry point in the head (the missed candidates look for those)."""
    return [dump({'type': 'user', 'timestamp': link_iso(t), 'cwd': cwd, 'entrypoint': 'sdk-cli', 'message': {'role': 'user', 'content': text}}),
            dump({'type': 'assistant', 'timestamp': link_iso(t + 4), 'cwd': cwd, 'entrypoint': 'sdk-cli',
                  'message': {'model': 'claude-sonnet-5-5', 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'hello'}],
                              'usage': {'input_tokens': 10, 'output_tokens': 5}}})]


class CodexFixture(Fixture):
    """A fake HOME with Codex rollouts that ran commands, Claude records and a fake /proc."""

    def rollout_file(self, tid, lines):
        d = os.path.join(self.codex, 'sessions', '2026', '10', '04')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'rollout-2026-10-04T00-00-00-%s.jsonl' % tid)
        with open(p, 'a') as f:
            f.write('\n'.join(lines) + '\n')
        return p

    def thread(self, tid=CX, t=T0 - 100, cmds=(), origin='codex-tui', cwd='/w', sub_of=None, path='/root/s1', prefix=1, open_turn=False, user=None, until=None):
        """A Codex thread's rollout: its meta, then the commands (start, end, text[, cwd]) as the CommandExecution lines Codex writes when a command ends."""
        if sub_of:
            meta = sub_meta(tid, sub_of, path, start=prefix, cwd=cwd, timestamp=link_iso(t))
            lines = [line(t, 'session_meta', meta, 0)]
            lines += [line(t, 'event_msg', {'type': 'task_started'}, i + 1) for i in range(prefix)]       # the copy of the parent's history (skipped)
            lines.append(line(t + 1, 'event_msg', {'type': 'task_started'}, prefix + 1))
        else:
            meta = root_meta(tid, originator=origin, source='exec' if origin == 'codex_exec' else 'cli', cwd=cwd, timestamp=link_iso(t))
            lines = [line(t, 'session_meta', meta)]
            lines.append(line(t + 1, 'event_msg', {'type': 'task_started'}))
            if user:
                lines.append(line(t + 1, 'response_item', {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': user}]}))
        for i, c in enumerate(cmds):
            lines += self.command_lines(tid, c, i)
        if not open_turn:
            lines.append(line(max([c[1] + 1 for c in cmds] + [until if until is not None else T0 + 60]), 'event_msg', {'type': 'task_complete'}))
        return self.rollout_file(tid, lines)

    def command_lines(self, tid, c, i=0):
        start, end, text = c[0], c[1], c[2]
        cwd = c[3] if len(c) > 3 else '/w'
        item = cmd_item(text, item_id='call-%s-%d' % (tid[-2:], i), pid=str(100 + i), cwd='file://' + cwd, secs=int(end - start), nanos=int(((end - start) % 1) * 1e9))
        return [line(end, 'event_msg', item_payload(item, tid))]

    def claude_run(self, text, cwd='/w'):
        return 'cd %s && claude -p --model m "%s"' % (cwd, text)

    def judge(self, now=T0 + 600):
        with mock.patch('time.time', lambda: now):
            self.scan()
        return self.links


def link_iso(t):
    from test_codex_facts import iso
    return iso(t)


class CodexLauncherContent(CodexFixture):
    def test_a_claude_child_is_linked_to_the_codex_thread_that_ran_it(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        o = self.judge().cli_owners[K]
        self.assertEqual((o['sid'], o['node'], o['rule'], o['certain'], o['parent_kind']), (CX, None, 'content', True, 'codex'))
        self.assertEqual(o['calls'], ['call-01-0'])
        self.assertEqual(o['bash_desc'], '')


class CodexLauncherNodes(CodexFixture):
    def test_the_sub_agent_that_ran_it_is_the_node(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        o = self.judge().cli_owners[K]
        self.assertEqual((o['sid'], o['node'], o['rule'], o['certain']), (CX, SUB, 'content', True))
        self.assertEqual(self.links.tree_kind(o['sid']), 'codex')

    def test_the_commands_of_a_copied_history_are_not_the_sub_agents(self):
        """The first lines of a sub-agent's rollout are its parent's history; a command in them was the parent's."""
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])
        item = cmd_item(self.claude_run(TEXT), item_id='copied-1', cwd='file:///w')
        lines = [line(T0 - 99, 'session_meta', sub_meta(SUB, CX, '/root/s1', start=2, cwd='/w', timestamp=link_iso(T0 - 99)), 0),
                 line(T0 - 98, 'event_msg', item_payload(item, CX), 1), line(T0 - 97, 'event_msg', {'type': 'task_started'}, 2),
                 line(T0 - 90, 'event_msg', {'type': 'task_started'}, 3), line(T0 - 89, 'event_msg', {'type': 'task_complete'}, 4)]
        self.rollout_file(SUB, lines)
        self.write(K, child_lines(T0 + 2, text=TEXT))
        o = self.judge().cli_owners[K]
        self.assertEqual((o['sid'], o['node']), (CX, None))
        self.assertEqual(sorted(f['tid'] for f in self.links.cx_files.values() if f['launch_calls']), [CX])

    def test_a_review_thread_and_an_unreadable_chain_are_no_launcher(self):
        from test_codex_facts import guardian_meta
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.rollout_file(SUB2, [line(T0 - 50, 'session_meta', guardian_meta(SUB2, CX), 0), line(T0 - 1, 'event_msg', item_payload(cmd_item(self.claude_run(TEXT), cwd='file:///w'), SUB2))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        L = self.judge()
        self.assertNotIn(K, L.cli_owners)
        self.assertEqual(sorted(L.cx_files), [CX])

    def test_a_command_that_is_not_a_shell_command_has_no_text(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        item = cmd_item('x', item_id='odd-1', command=['python3', '-c', 'print(1)'], cwd='file:///w')
        self.rollout_file(CX, [line(T0 - 2, 'event_msg', item_payload(item, CX))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        self.assertNotIn(K, self.judge().cli_owners)


class RelayAndLauncher(CodexFixture):
    """The relay of observation 8: a Claude session typed the instruction into a terminal, and a Codex thread ran it."""
    def test_the_codex_thread_that_ran_it_is_the_parent_not_the_session_that_relayed_it(self):
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'pass it on'}}),
                       bash_line(T0 - 3, "tmux send-keys -t w -l 'claude -p \"%s\"' && tmux send-keys -t w Enter" % TEXT, tid='toolu_relay'), result_line(T0 - 2.5, 'toolu_relay')])
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        o = self.judge().cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (CX, 'content', True))

    def test_without_the_codex_commands_the_relay_is_nobody(self):
        self.write(P, [bash_line(T0 - 3, "tmux send-keys -t w -l 'claude -p \"%s\"'" % TEXT, tid='toolu_relay'), result_line(T0 - 2.5, 'toolu_relay')])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        self.assertNotIn(K, self.judge().cli_owners)

    def test_two_real_launchers_with_the_same_instruction_are_a_tie(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])
        self.write(P, [bash_line(T0 - 1, self.claude_run(TEXT), tid='toolu_run'), result_line(T0 + 30, 'toolu_run')])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        L = self.judge()
        self.assertNotIn(K, L.cli_owners)
        self.assertEqual(sorted(L.decisions[K].held_trees), sorted([P, CX]))


class RewrittenRollouts(CodexFixture):
    """A rollout that is written again (it shrank, or its start changed) is read over: the calls, spans, texts and settled judgments of the old one are not what the thread says."""

    def overwrite(self, tid, lines):
        path = self.path_of(tid)
        with open(path, 'w') as fh:
            fh.write('\n'.join(lines) + '\n')

    def path_of(self, tid):
        return os.path.join(self.codex, 'sessions', '2026', '10', '04', 'rollout-2026-10-04T00-00-00-%s.jsonl' % tid)

    def test_a_command_that_is_gone_is_no_launch_any_more(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        L = self.judge(T0 + 700)
        self.assertEqual((L.cli_owners[K]['sid'], L.cli_owners[K]['certain']), (CX, True))
        self.assertEqual(len(L.cx_files[CX]['spans']), 1)
        meta = line(T0 - 100, 'session_meta', root_meta(CX, originator='codex-tui', source='cli', cwd='/w', timestamp=link_iso(T0 - 100)))
        self.overwrite(CX, [meta, line(T0 - 99, 'event_msg', {'type': 'task_started'}), line(T0 - 98, 'event_msg', {'type': 'task_complete'})])        # shorter
        L = self.judge(T0 + 800)
        self.assertEqual(L.cx_files[CX]['spans'], {})
        self.assertEqual((L.cli_owners.get(K) or {}).get('rule'), 'cache')                          # the words are not evidence any more: only the link remembered from before is left
        self.assertEqual(L.out.by_path, {})

    def test_the_same_command_id_with_other_words_is_read_again(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run('Summarise the failing tests of the lagoon module and list every suspect file today please, carefully.'))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        self.assertNotIn(K, self.judge(T0 + 700).cli_owners)
        meta = line(T0 - 100, 'session_meta', root_meta(CX, originator='codex-tui', source='cli', cwd='/w', timestamp=link_iso(T0 - 100)))
        self.overwrite(CX, [meta, line(T0 - 99, 'event_msg', {'type': 'task_started'})] + self.command_lines(CX, (T0 - 1, T0 + 30, self.claude_run(TEXT)), 0)
                       + [line(T0 + 31, 'event_msg', {'type': 'task_complete'})])
        o = self.judge(T0 + 800).cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (CX, 'content', True))                # the item id is the same: the new text is the one used
        self.overwrite(CX, [meta, line(T0 - 99, 'event_msg', {'type': 'task_started'})] + self.command_lines(CX, (T0 - 1, T0 + 30, self.claude_run('Another instruction altogether, said at length so that it is long enough to count as one here.')), 0)
                       + [line(T0 + 31, 'event_msg', {'type': 'task_complete'})])
        self.assertEqual((self.judge(T0 + 900).cli_owners.get(K) or {}).get('rule'), 'cache')       # not `content`: the words of the new command do not fit; what is left is the remembered link

    def test_a_thread_that_is_gone_takes_its_calls_with_it(self):
        path = self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        L = self.judge(T0 + 700)
        self.assertIn(K, L.cli_owners)
        os.unlink(path)
        L = self.judge(T0 + 800)
        self.assertEqual(L.cx_files, {})
        self.assertNotIn(K, L.cli_owners)                                                           # no record of the launcher to show it in


class BlindCompetitor(CodexFixture):
    """A Codex thread that may have run a command its facts do not hold (a turn that is open: a command is written when it ends) is a competitor nobody can read."""
    def launcher(self):
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'run it'}}),
                       bash_line(T0 - 1, self.claude_run(TEXT), tid='toolu_run'), result_line(T0 + 30, 'toolu_run')])

    def judged(self):
        self.launcher()
        self.write(K, child_lines(T0 + 2, text=TEXT))
        L = self.judge()
        return L, L.cli_owners.get(K), [d['code'] for d in L.diags if d['subject'] == K]

    def test_an_open_turn_of_a_live_thread_in_the_folder_makes_the_match_a_guess(self):
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
        self.codex_proc(300, 1, path)
        L, o, diags = self.judged()
        self.assertEqual((o['sid'], o['rule'], o['certain']), (P, 'content_short', False))
        self.assertIn('fingerprint_incomplete', diags)
        self.assertNotIn(K, L.lineage.saved)                                                        # a guess is not kept

    def test_a_folder_above_or_the_same_counts_and_another_folder_does_not(self):
        for cwd, certain in (('/w', False), ('/', False), ('/elsewhere', True), ('/w/sub', True)):
            with self.subTest(cwd):
                self.setUp()
                path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd=cwd)
                self.codex_proc(300, 1, path)
                L, o, diags = self.judged()
                self.assertEqual((o['sid'], o['certain']), (P, certain), cwd)
                self.assertEqual('fingerprint_incomplete' in diags, not certain, cwd)

    def test_a_turn_that_never_ended_in_a_thread_nobody_runs_does_not_blind_it_for_ever(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')                  # no codex process: the turn ended with the process
        L, o, diags = self.judged()
        self.assertEqual((o['sid'], o['rule'], o['certain']), (P, 'content', True))
        self.assertNotIn('fingerprint_incomplete', diags)

    def test_a_thread_nobody_has_open_is_gone_even_when_other_codex_processes_run(self):
        """The open files can be seen (/proc) and none holds this rollout: the turn that never ended stopped with the process, whatever else is running."""
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
        other = self.thread(CX2, cmds=[(T0 - 50, T0 - 49, 'ls')], cwd='/elsewhere')
        self.codex_proc(300, 1, other)
        L, o, diags = self.judged()
        self.assertEqual((o['sid'], o['rule'], o['certain']), (P, 'content', True))
        self.assertNotIn('fingerprint_incomplete', diags)

    def test_a_gap_that_starts_at_minus_infinity_starts_when_the_thread_did(self):
        self.thread(CX, t=T0 - 5000, cmds=[(T0 - 4990, T0 - 4989, 'ls')], cwd='/w')
        L = self.judge()
        meta_ts = L.cx_files[CX]['entry']['meta_ts']
        L.cx_files[CX]['gaps'] = ((float('-inf'), T0 - 4000, 'partial'), (T0 - 100, None, 'turn'))
        L._cx_gaps = None
        self.assertEqual(L._cx_gap_map()[CX][0], (meta_ts, T0 - 4000, 'partial'))
        self.assertTrue(L._cx_blind(L.cx_files[CX], meta_ts - 1000 + 1000, meta_ts + 10))              # the beginning of the thread is inside it
        self.assertFalse(L._cx_blind(L.cx_files[CX], meta_ts - 100, meta_ts - 50))                      # what came before the thread is not

    def test_the_gap_has_to_overlap_the_time_the_child_started(self):
        path = self.thread(CX, t=T0 - 5000, cmds=[(T0 - 4000, T0 - 3999, 'ls')], open_turn=True, cwd='/w')
        self.codex_proc(300, 1, path)
        # the turn opened long before; a closed turn with a quick command later is no gap: only the open one counts, and it is open now
        L, o, diags = self.judged()
        self.assertEqual((o['sid'], o['certain']), (P, False))

    def test_a_thread_of_the_winners_own_tree_is_no_competitor(self):
        """The root has an open turn (it waits for its sub-agent) and the sub-agent ran the launch: the tree cannot change, the node is the thread whose commands are known."""
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
        self.codex_proc(300, 1, path)
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))], cwd='/w')
        self.write(K, child_lines(T0 + 2, text=TEXT))
        o = self.judge().cli_owners[K]
        self.assertEqual((o['sid'], o['node'], o['rule'], o['certain']), (CX, SUB, 'content', True))

    def test_a_thread_of_another_tree_still_is_when_the_winner_has_no_tree_of_its_own_in_common(self):
        path = self.thread(CX2, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
        self.codex_proc(300, 1, path)
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))], cwd='/w')
        self.write(K, child_lines(T0 + 2, text=TEXT))
        o = self.judge().cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (CX, 'content_short', False))

    def test_a_child_whose_environment_holds_no_name_of_codex_has_no_codex_competitor(self):
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
        self.codex_proc(300, 1, path)
        for env, want in ((dict(claude=Q, claude_pid=100), True),                            # read, no name of Codex, and Claude's two names name a live session
                          (dict(), False),                                                   # no name at all and nothing above it: a detached child says nothing
                          (dict(thread=CX2), False),                                         # the names are there (a thread nobody knows is not a reason to forget it)
                          (None, False)):                                                    # never read: nothing is known
            with self.subTest(env=env):
                self.setUp()
                path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
                self.codex_proc(300, 1, path)
                self.launcher()
                self.write(K, child_lines(T0 + 2, text=TEXT))
                if env is not None:
                    self.proc.add(100, 1, ['claude'])
                    self.session_file(100, Q, entrypoint='cli')
                    self.proc.add(103, 1, ['claude', '-p', 'x'])
                    self.session_file(103, K)
                    environ(self.proc, 103, **env)
                L = self.judge()
                o = L.cli_owners[K]
                self.assertEqual((o['sid'], o['certain']), (P, want), env)
                self.assertEqual('fingerprint_incomplete' in [d['code'] for d in L.diags if d['subject'] == K], not want, env)

    def test_what_makes_a_childs_environment_free_of_codex(self):
        """Only when something says why no Codex shell can be behind it: a live Claude session named by both of Claude's names (a shell that threw Codex's away: `env -i`, `sudo`,
        `ssh localhost`, `tmux new-window` keeps Claude's), or ancestors that can be followed to the end with no codex among them. An environment with no name at all and a cut chain,
        or a codex among the ancestors, or the names of Codex: not free."""
        def free_of(setup, **env):
            self.setUp()
            path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
            self.codex_proc(300, 1, path)
            self.launcher()
            self.write(K, child_lines(T0 + 2, text=TEXT))
            self.proc.add(100, 1, ['claude'])
            self.session_file(100, Q, entrypoint='cli')
            setup()
            environ(self.proc, 103, **env)
            L = self.judge()
            return L.cli_owners[K]['certain'], L.lineage.codex_free.get(K)

        def chain_to_init():
            self.proc.add(103, 1, ['claude', '-p', 'x'])                                     # detached: init is its parent
            self.session_file(103, K)

        def under_claude():
            self.proc.add(103, 100, ['claude', '-p', 'x'])
            self.session_file(103, K)

        def cut_chain():
            self.proc.add(103, 555, ['claude', '-p', 'x'])                                    # its parent is not in the table
            self.session_file(103, K)

        def under_codex():
            self.proc.add(210, 1, ['codex', 'exec', 'x'], fds=[])
            self.proc.add(103, 210, ['claude', '-p', 'x'])
            self.session_file(103, K)
        for setup, env, want in ((under_claude, dict(), (True, True)),                           # complete ancestry that reaches a live Claude, no codex in it
                                 (chain_to_init, dict(), (False, False)),                        # a detached child: its parent is init, which says nothing about the shell before it
                                 (cut_chain, dict(), (False, False)),                            # a parent that cannot be read: nothing says it was not Codex
                                 (cut_chain, dict(claude=Q, claude_pid=100), (True, True)),     # ... unless Claude's two names name a live session
                                 (cut_chain, dict(claude=Q), (False, False)),                    # one name alone is no pin
                                 (cut_chain, dict(claude=Q, claude_pid=555), (False, False)),    # a pid that is no such session
                                 (chain_to_init, dict(thread=CX, session=CX), (True, False)),    # the names of Codex are there: not free (the thread's turn is open with `ls` only and the launcher's words fit: the names are out, the launcher is certain)
                                 (under_codex, dict(), (False, False))):                         # a codex among the ancestors
            with self.subTest(setup=setup.__name__, env=env):
                self.assertEqual(free_of(setup, **env), want)

    def test_the_environment_is_remembered_after_the_process_is_gone_and_not_after_a_restart(self):
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
        self.codex_proc(300, 1, path)
        self.launcher()
        self.write(K, child_lines(T0 + 2, text=TEXT))
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, Q, entrypoint='cli')
        self.proc.add(103, 100, ['claude', '-p', 'x'])
        self.session_file(103, K)
        environ(self.proc, 103)
        self.assertTrue(self.judge().cli_owners[K]['certain'])
        self.proc.remove(103)
        os.unlink(os.path.join(self.sess_dir, '103.json'))
        self.assertTrue(self.judge().cli_owners[K]['certain'])                              # the process ended: what was read stays
        again = server.LinkIndex()
        self.links = again
        with mock.patch.object(link, 'LINKS', again):
            self.assertFalse(self.judge().cli_owners[K]['certain'])                         # a board that started later never saw it: nothing is known

    def test_a_codex_thread_above_the_winners_tree_in_the_chain_is_no_competitor(self):
        """Codex TUI (turn open) → `claude -p` P (by its process) → a `claude -p` child of P that P's own command shows: the TUI's open turn is how the launch of P looks from above."""
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
        self.proc.add(300, 1, ['codex'], fds=[path])
        self.proc.add(301, 300, ['/bin/bash', '-lc', 'claude -p x'])
        self.proc.add(302, 301, ['claude', '-p', 'x'])
        self.session_file(302, P)
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'promptSource': 'sdk', 'message': {'role': 'user', 'content': 'first'}}),
                       bash_line(T0 - 1, self.claude_run(TEXT), tid='toolu_run'), result_line(T0 + 30, 'toolu_run')])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        L = self.judge()
        self.assertEqual((L.cli_owners[P]['sid'], L.cli_owners[P]['rule']), (CX, 'proc'))
        o = L.cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (P, 'content', True))
        self.assertNotIn('fingerprint_incomplete', [d['code'] for d in L.diags if d['subject'] == K])

    def test_a_closed_turn_is_no_gap(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], cwd='/w')
        L, o, diags = self.judged()
        self.assertEqual((o['sid'], o['certain']), (P, True))

    def test_the_output_file_and_the_environment_are_not_touched(self):
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
        self.codex_proc(300, 1, path)
        self.launcher()
        self.write(K, child_lines(T0 + 2, text=TEXT))
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.proc.add(103, 100, ['claude', '-p', 'x'])
        self.session_file(103, K)
        L = self.judge()
        o = L.cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (P, 'proc', True))


class LateFacts(CodexFixture):
    """A command is written when it ends: it can come long after the child began, and the judgment of a settled child is made again."""
    def test_a_late_command_gives_a_child_its_parent(self):
        self.write(K, child_lines(T0 + 2, text=TEXT))
        self.assertNotIn(K, self.judge(T0 + 600).cli_owners)                    # settled (600 s after it began): remembered as unlinked
        self.thread(CX, cmds=[(T0 - 1, T0 + 500, self.claude_run(TEXT))])        # the command ended 500 s later and was written then
        o = self.judge(T0 + 700).cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (CX, 'content', True))

    def test_a_late_command_undoes_a_settled_link_it_ties_with(self):
        self.write(P, [bash_line(T0 - 1, self.claude_run(TEXT), tid='toolu_run'), result_line(T0 + 30, 'toolu_run')])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        o = self.judge(T0 + 600).cli_owners[K]
        self.assertEqual((o['sid'], o['certain']), (P, True))
        self.thread(CX, cmds=[(T0 - 1, T0 + 400, self.claude_run(TEXT))])
        L = self.judge(T0 + 700)
        self.assertNotIn(K, L.cli_owners)                                       # two real launchers: held, no longer P
        self.assertEqual(sorted(L.decisions[K].held_trees), sorted([P, CX]))

    def test_an_old_child_is_not_judged_again_for_a_new_command(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True)
        self.write(K, child_lines(T0 + 2, text=TEXT))
        L = self.judge(T0 + 600)
        dec = L.decisions[K]
        self.assertIs(L._memo.get(K, (None, None))[1], dec)
        self.rollout_file(CX, [line(T0 + 5002, 'event_msg', item_payload(cmd_item('ls', item_id='later-1', cwd='file:///w'), CX))])      # a command long after the child
        self.judge(T0 + 6000)
        self.assertIs(L._memo.get(K, (None, None))[1], dec)                     # the settled judgment was kept
        self.rollout_file(CX, [line(T0 + 60, 'event_msg', item_payload(cmd_item('ls', item_id='late-2', cwd='file:///w', secs=59), CX))])     # one that was running when it began
        self.judge(T0 + 6001)
        self.assertIsNot(L._memo.get(K, (None, None))[1], dec)                  # the child is judged again


def environ(fp, pid, claude=None, claude_pid=None, thread=None, session=None):
    """A fake /proc/<pid>/environ with secrets next to the variables the lineage reads (the Codex shell's CODEX_THREAD_ID and CODEX_SESSION_ID, Claude's two)."""
    items = ['HOME=/home/u', 'OPENAI_API_KEY=sk-SECRETVALUE111', 'GITHUB_TOKEN=ghp_SECRET222', 'CODEX_VERSION=0.160.0', 'CODEX_CI=1']
    for name, value in (('CLAUDE_CODE_SESSION_ID', claude), ('CLAUDE_PID', claude_pid), ('CODEX_THREAD_ID', thread), ('CODEX_SESSION_ID', session)):
        if value is not None:
            items.append('%s=%s' % (name, value))
    with open(os.path.join(fp.root, str(pid), 'environ'), 'wb') as f:
        f.write(b'\0'.join(i.encode() for i in items) + b'\0')


class LineageFixture(CodexFixture):
    def claude_child(self, pid, ppid, sid=K, **env):
        """A live `claude -p` process (its session file written) and the environment it was started with."""
        self.proc.add(pid, ppid, ['claude', '-p', 'x'])
        self.session_file(pid, sid)
        environ(self.proc, pid, **env)
        self.write(sid, child_lines(T0 + 2, text='go there'))

    def codex_tui(self, pid, tid=CX, **kw):
        path = self.thread(tid, **kw)
        self.proc.add(pid, 1, ['codex'], fds=[path])
        return path

    def pin(self, sid=K):
        o = self.judge().cli_owners.get(sid)
        return o and (o['sid'], o['node'], o['rule'], o['certain'], o['parent_kind'])


class CodexEnvironment(LineageFixture):
    """C9: the environment of a Codex shell says which thread ran the command; a sub-agent is the node, exactly."""
    def test_the_root_thread_is_the_tree_and_no_node(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.claude_child(103, 1, thread=CX, session=CX)
        self.assertEqual(self.pin(), (CX, None, 'env', True, 'codex'))

    def test_a_sub_agent_is_the_tree_s_node_and_it_is_certain(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.claude_child(103, 1, thread=SUB, session=CX)
        L = self.judge()
        o = L.cli_owners[K]
        self.assertEqual((o['sid'], o['node'], o['rule'], o['certain']), (CX, SUB, 'env', True))
        d = L.decisions[K]
        self.assertTrue(d.relation.certain['node'])
        self.assertNotIn('node_unresolved', [x for x, _ in d.diags])

    def test_a_deeper_sub_agent_has_the_root_as_its_tree(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.thread(SUB2, sub_of=SUB, path='/root/s1/s2', cmds=[(T0 - 30, T0 - 29, 'ls')])
        self.claude_child(103, 1, thread=SUB2)
        self.assertEqual(self.pin()[:2], (CX, SUB2))

    def test_what_cannot_be_trusted_gives_nothing(self):
        from test_codex_facts import guardian_meta
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(CX2, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.rollout_file(SUB2, [line(T0 - 50, 'session_meta', guardian_meta(SUB2, CX), 0)])
        for env in (dict(thread='aaaaaaaa-0000-4000-8000-0000000000ff', session=CX),                 # a thread the index does not know
                    dict(thread=SUB2, session=CX),                                                    # a review thread
                    dict(thread='not-an-id')):
            with self.subTest(env):
                self.setUp()
                self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
                self.rollout_file(SUB2, [line(T0 - 50, 'session_meta', guardian_meta(SUB2, CX), 0)])
                self.claude_child(103, 1, **env)
                self.assertIsNone(self.pin(), env)

    def test_a_session_id_that_is_not_the_root_of_the_thread_is_a_conflict_not_an_absence(self):
        """`CODEX_THREAD_ID=T` of one tree and `CODEX_SESSION_ID=S` of another: the environment contradicts itself. The child is held (a tie at rank 2), `evidence_conflict` says so,
        and the words of a launcher of the same instruction (which would link it if the environment were not there) do not break the tie."""
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}}),
                       bash_line(T0 - 1, self.claude_run(TEXT), tid='toolu_run'), result_line(T0 + 30, 'toolu_run')])
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(CX2, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.claude_child(103, 1, thread=CX, session=CX2)
        self.write(K, child_lines(T0 + 2, text=TEXT))
        L = self.judge()
        self.assertNotIn(K, L.cli_owners)
        d = L.decisions[K]
        self.assertEqual((d.tree, d.held, sorted(d.held_trees)), (None, 'ambiguous', sorted([CX, CX2])))
        self.assertEqual(sorted(x['other'] for x in L.diags if x['code'] == 'evidence_conflict' and x['subject'] == K), sorted([CX, CX2]))
        self.assertIn(K, L.lineage.pending)

    def test_a_conflict_inside_the_codex_environment_next_to_a_codex_process_of_another_root_is_a_conflict_too(self):
        p1 = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(CX2, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(200, 1, ['codex'], fds=[p1])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201, thread=CX2, session=CX2)                                 # a process of CX, an environment of CX2
        L = self.judge()
        self.assertNotIn(K, L.cli_owners)
        self.assertEqual(sorted(L.decisions[K].held_trees), sorted([CX, CX2]))

    def test_a_conflict_inside_the_codex_environment_below_a_codex_process_holds_it_too(self):
        p1 = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(CX2, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(200, 1, ['codex'], fds=[p1])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201, thread=CX, session=CX2)                                  # the process says CX, the two names say CX and CX2
        L = self.judge()
        self.assertNotIn(K, L.cli_owners)
        self.assertEqual(sorted(L.decisions[K].held_trees), sorted([CX, CX2]))

    def test_a_remembered_line_does_not_count_against_a_conflict_the_process_shows_now(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(CX2, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.write(K, child_lines(T0 + 2, text='go there'))
        path = os.path.join(self.t, 'xdg', 'agent-bullpen', 'links.json')
        os.makedirs(os.path.dirname(path), mode=0o700)
        os.chmod(os.path.dirname(path), 0o700)
        with open(path, 'w') as fh:
            json.dump({'version': lineage.CACHE_VERSION, 'links': [{'child': K, 'parent': CX, 'kind': 'cli', 'rule': 'env', 'seen': time.time() - 10, 'started': T0,
                                                                    'parent_kind': 'codex'}]}, fh)
        os.chmod(path, 0o600)
        self.links.lineage.enable_cache(path)
        self.assertEqual(self.judge().cli_owners[K]['rule'], 'file')                         # the line alone links it
        self.claude_child(103, 1, thread=CX, session=CX2)
        L = self.judge()
        self.assertNotIn(K, L.cli_owners)                                                    # now the process says two trees: held, the line is not read as an answer
        self.assertIn(K, L.lineage.pending)

    def test_only_the_four_names_are_asked_for_and_nothing_else_is_kept(self):
        self.assertEqual(sorted(n.decode() for n in lineage.ENV_NAMES), ['CLAUDE_CODE_SESSION_ID', 'CLAUDE_PID', 'CODEX_SESSION_ID', 'CODEX_THREAD_ID'])
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.claude_child(103, 1, thread=CX, session=CX)
        got = procs.env_values(103, lineage.ENV_NAMES)
        self.assertEqual(sorted(got), ['CODEX_SESSION_ID', 'CODEX_THREAD_ID'])
        self.assertNotIn('SECRET', repr(got))
        self.assertNotIn('0.160.0', repr(got))

    def test_the_link_is_kept_in_the_file_with_whose_parent_it_is(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.claude_child(103, 1, thread=SUB, session=CX)
        path = os.path.join(self.t, 'xdg', 'agent-bullpen', 'links.json')
        self.links.lineage.enable_cache(path)
        self.judge()
        with open(path) as fh:
            d = json.load(fh)
        self.assertEqual(d['version'], 6)
        self.assertEqual(d['links'][0]['parent_kind'], 'codex')
        self.assertEqual((d['links'][0]['parent'], d['links'][0]['node'], d['links'][0]['rule']), (CX, SUB, 'env'))
        self.assertNotIn('SECRET', json.dumps(d))
        again = server.LinkIndex()
        again.lineage.enable_cache(path)
        self.assertEqual((again.lineage.cli[K]['sid'], again.lineage.cli[K]['pk'], again.lineage.cli[K]['node'], again.lineage.cli[K]['exact']), (CX, 'codex', SUB, True))


class StaleCodexEnvironment(LineageFixture):
    """The names of a Codex thread stay in the environment of what a shell of it left running (a tmux server, a daemon): a child begun long after the thread's turns is not its."""

    def child_at(self, t0, **env):
        self.proc.add(103, 1, ['claude', '-p', 'x'])
        self.session_file(103, K)
        environ(self.proc, 103, **env)
        self.write(K, child_lines(t0, text='go there'))

    def judge_k(self, now=T0 + 5000):
        L = self.judge(now)
        return L, L.cli_owners.get(K), [(d['code'], d.get('other')) for d in L.diags if d['subject'] == K]

    def test_a_child_begun_long_after_the_turn_is_not_pinned_by_its_names(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], until=T0 - 48)                      # the turn ends 48 s before T0
        self.child_at(T0 + 2000, thread=CX, session=CX)
        L, o, diags = self.judge_k()
        self.assertIsNone(o)
        self.assertEqual(diags, [('evidence_conflict', CX)])
        self.assertEqual(L.lineage.cli[K]['rule'], 'env')                                    # the lineage saw the names; the judgment does not take them for this child

    def test_the_turn_and_a_grace_after_it_count(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], until=T0 - 48)
        for t0, want in ((T0 - 100, True), (T0 - 60, True), (T0 - 40, True), (T0 - 30, False), (T0 + 500, False)):
            with self.subTest(t0=t0):
                self.proc.remove(103) if os.path.exists(os.path.join(self.proc.root, '103')) else None
                self.child_at(t0, thread=CX, session=CX)
                self.links = server.LinkIndex()
                with mock.patch.object(link, 'LINKS', self.links):
                    self.assertEqual(self.judge_k()[1] is not None, want, t0)
                os.unlink(os.path.join(self.t, 'home', '.claude', 'projects', '-w', K + '.jsonl'))

    def test_an_open_turn_counts_while_its_process_is_there_and_not_after_it_is_gone(self):
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True)
        self.codex_proc(300, 1, path)
        self.child_at(T0 + 2000, thread=CX, session=CX)
        L, o, diags = self.judge_k()
        self.assertEqual((o['sid'], o['rule']), (CX, 'env'))
        self.setUp()
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True)                     # nobody runs it: the turn ended with the process, at the last line
        self.child_at(T0 + 2000, thread=CX, session=CX)
        L, o, diags = self.judge_k()
        self.assertIsNone(o)
        self.assertEqual(diags, [('evidence_conflict', CX)])

    def test_it_is_the_turn_of_the_thread_the_names_say_not_of_its_root(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True)
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')], until=T0 - 38)
        self.proc.add(300, 1, ['codex'], fds=[os.path.join(self.codex, 'sessions', '2026', '10', '04', 'rollout-2026-10-04T00-00-00-%s.jsonl' % CX)])
        self.child_at(T0 + 2000, thread=SUB, session=CX)                                     # the root is working; the sub-agent's turn is long over
        L, o, diags = self.judge_k()
        self.assertIsNone(o)
        self.assertEqual(diags, [('evidence_conflict', CX)])

    def test_a_turn_that_began_after_the_child_is_not_its_turn(self):
        self.thread(CX, t=T0 + 100, cmds=[(T0 + 110, T0 + 111, 'ls')], until=T0 + 200)
        self.child_at(T0 + 2, thread=CX, session=CX)
        self.assertIsNone(self.judge_k()[1])

    def test_a_codex_exec_thread_is_placed_by_the_names_only_within_a_turn(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], until=T0 - 48)
        p2 = self.thread(CX2, t=T0 + 2000, origin='codex_exec', cmds=[(T0 + 2001, T0 + 2002, 'ls')], user='something else entirely')
        self.proc.add(220, 1, ['codex', 'exec', 'x'], fds=[p2])
        environ(self.proc, 220, thread=CX, session=CX)
        L = self.judge(T0 + 5000)
        self.assertNotIn(CX2, L.owners)
        self.assertEqual([(d['code'], d['other']) for d in L.diags if d['subject'] == CX2], [('evidence_conflict', CX)])
        self.setUp()
        self.thread(CX, cmds=[(T0 - 50, T0 + 3000, 'sleep 3000')], until=T0 + 3001)
        p2 = self.thread(CX2, t=T0 + 2000, origin='codex_exec', cmds=[(T0 + 2001, T0 + 2002, 'ls')], user='something else entirely')
        self.proc.add(220, 1, ['codex', 'exec', 'x'], fds=[p2])
        environ(self.proc, 220, thread=CX, session=CX)
        self.assertEqual(self.judge(T0 + 5000).owners[CX2]['sid'], CX)


class UnknownCodexThreads(LineageFixture):
    """A `CODEX_THREAD_ID` the index knows nothing of (the thread is new and not indexed yet, or its first line is not whole) names a shell whose commands nobody can read. That is no
    absence of a Codex parent: a launcher of the same instruction found in a Claude session's record is no longer a certain link, and nothing of it is kept."""
    U = 'aaaaaaaa-0000-4000-8000-0000000000ff'

    def launcher(self):
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'run it'}}),
                       bash_line(T0 - 1, self.claude_run(TEXT), tid='toolu_run'), result_line(T0 + 30, 'toolu_run')])

    def child(self, **env):
        self.proc.add(103, 1, ['claude', '-p', 'x'])
        self.session_file(103, K)
        environ(self.proc, 103, **env)
        self.write(K, child_lines(T0 + 2, text=TEXT))

    def test_without_the_names_the_same_launcher_is_a_certain_parent(self):
        self.launcher()
        self.child()
        o = self.judge().cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (P, 'content', True))

    def test_an_unknown_thread_makes_the_content_match_a_guess_and_says_why(self):
        self.launcher()
        self.child(thread=self.U, session=self.U)
        path = os.path.join(self.t, 'xdg', 'agent-bullpen', 'links.json')
        self.links.lineage.enable_cache(path)
        L = self.judge()
        o = L.cli_owners.get(K)
        self.assertFalse(o is not None and o['certain'], o)
        d = L.decisions[K]
        self.assertFalse(d.certain)
        codes = [(x['code'], x.get('other')) for x in L.diags if x['subject'] == K]
        self.assertIn(('evidence_conflict', self.U), codes)
        self.assertIn(('fingerprint_incomplete', None), codes)
        self.assertEqual(L.lineage.pending[K]['unknown'], (self.U,))
        self.assertNotIn(K, L.lineage.saved)                                                       # a guess is not kept
        self.assertFalse(os.path.exists(path) and any(r['child'] == K for r in json.load(open(path))['links']))

    def test_the_names_of_an_unknown_thread_next_to_a_claude_session_hold_it_too(self):
        self.launcher()
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.child(claude=P, claude_pid=100, thread=self.U, session=self.U)
        o = self.judge().cli_owners.get(K)
        self.assertFalse(o is not None and o['certain'], o)

    def test_a_codex_process_above_it_does_not_make_it_known(self):
        self.launcher()
        self.codex_tui(200, CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.proc.add(103, 201, ['claude', '-p', 'x'])
        self.session_file(103, K)
        environ(self.proc, 103, thread=self.U, session=self.U)
        self.write(K, child_lines(T0 + 2, text=TEXT))
        o = self.judge().cli_owners.get(K)
        self.assertFalse(o is not None and o['certain'], o)                                        # the thread the shell belongs to is not the one the process says: nothing is placed on a guess

    def test_the_thread_is_the_pin_when_the_index_catches_up(self):
        self.launcher()
        self.child(thread=self.U, session=self.U)
        L = self.judge()
        self.assertFalse((L.cli_owners.get(K) or {}).get('certain'))
        self.thread(self.U, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])                     # the thread, with the launch it ran
        L = self.judge(T0 + 700)
        o = L.cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain'], o['parent_kind']), (self.U, 'env', True, 'codex'))
        self.assertNotIn(K, L.lineage.pending)

    def test_a_remembered_content_link_is_not_an_answer_for_it(self):
        self.launcher()
        self.child(thread=self.U, session=self.U)
        path = os.path.join(self.t, 'xdg', 'agent-bullpen', 'links.json')
        os.makedirs(os.path.dirname(path), mode=0o700)
        os.chmod(os.path.dirname(path), 0o700)
        with open(path, 'w') as fh:
            json.dump({'version': lineage.CACHE_VERSION, 'links': [{'child': K, 'parent': P, 'kind': 'cli', 'rule': 'content', 'seen': time.time() - 10, 'started': T0,
                                                                    'parent_kind': 'claude'}]}, fh)
        os.chmod(path, 0o600)
        self.links.lineage.enable_cache(path)
        o = self.judge().cli_owners.get(K)
        self.assertFalse(o is not None and o['certain'], o)

    def test_a_remembered_link_is_not_a_fallback_for_it_when_nothing_else_names_a_parent(self):
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'run it'}})])
        self.child(thread=self.U, session=self.U)
        path = os.path.join(self.t, 'xdg', 'agent-bullpen', 'links.json')
        os.makedirs(os.path.dirname(path), mode=0o700)
        os.chmod(os.path.dirname(path), 0o700)
        with open(path, 'w') as fh:
            json.dump({'version': lineage.CACHE_VERSION, 'links': [{'child': K, 'parent': P, 'kind': 'cli', 'rule': 'content', 'seen': time.time() - 10, 'started': T0,
                                                                    'parent_kind': 'claude'}]}, fh)
        os.chmod(path, 0o600)
        self.links.lineage.enable_cache(path)
        L = self.judge()
        self.assertNotIn(K, L.cli_owners)                                                        # without the unknown thread the line would link it (rule `cache`)
        self.setUp()
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'run it'}})])
        self.child()
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self.links.lineage.enable_cache(path)
        self.assertEqual(self.judge().cli_owners[K]['rule'], 'cache')

    def test_what_is_known_but_cannot_start_it_is_still_no_pin(self):
        """A review (guardian) thread, or a name that is no thread id at all, is not the shell of a parent: nothing is claimed and the instruction decides as before."""
        from test_codex_facts import guardian_meta
        for env in (dict(thread='not-an-id'), dict(thread=CX2, session=CX)):
            with self.subTest(env):
                self.setUp()
                self.launcher()
                self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
                self.rollout_file(CX2, [line(T0 - 50, 'session_meta', guardian_meta(CX2, CX), 0)])
                self.child(**env)
                o = self.judge().cli_owners.get(K)
                self.assertEqual((o['sid'], o['rule'], o['certain']), (P, 'content', True), env)

    def test_a_codex_exec_thread_with_such_names_is_placed_by_nothing(self):
        self.write(P, [bash_line(T0 - 1, 'cd /w && codex exec -C /w "%s"' % CodexChildren.PROMPT, tid='toolu_x'), result_line(T0 + 30, 'toolu_x')])
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        p2 = self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=CodexChildren.PROMPT)
        self.proc.add(220, 1, ['codex', 'exec', 'x'], fds=[p2])
        environ(self.proc, 220, thread=self.U, session=self.U)
        L = self.judge()
        self.assertNotIn(CX2, L.owners)
        self.assertEqual(list(L.lineage.pending), [CX2])


class StalePins(LineageFixture):
    """The names a tmux server (or any long-lived process) carries on from the shell that started it are no evidence of who started a child later: a pin counts as long as the tree it
    names can have started the run (a call that was running and could be its launcher; a gap in its facts); it is out only when nothing of it can have, its record is whole, and another
    tree's launch fits the run firmly. A pin that is out takes the claim that no Codex shell started the child with it (`codex_free`)."""
    TMUX = 'tmux new-window -d -t w \'claude -p "%s"\'' % TEXT

    def parent(self, calls=()):
        """The Claude session P, alive (a process and a session file); `calls` are written into its record."""
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})] + list(calls))

    def detached(self, **env):
        """A `claude -p` child with the instruction, started by a tmux server (its parent is init) whose environment is `env`."""
        self.proc.add(103, 1, ['claude', '-p', 'x'])
        self.session_file(103, K)
        environ(self.proc, 103, **env)
        self.write(K, child_lines(T0 + 2, text=TEXT))

    def pin(self):
        o = self.judge().cli_owners.get(K)
        return o and (o['sid'], o['rule'], o['certain'], o['parent_kind'])

    def conflict(self):
        return sorted(x['other'] for x in self.links.diags if x['code'] == 'evidence_conflict' and x['subject'] == K)

    def run_call(self, tid='toolu_run', text=None):
        return [bash_line(T0 - 1, text or self.TMUX, tid=tid), result_line(T0 + 30, tid)]

    # ---- a Claude pin ----
    def test_a_claude_session_that_ran_nothing_that_started_it_is_not_the_parent_when_a_thread_did(self):
        self.parent()
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.TMUX)])
        self.detached(claude=P, claude_pid=100)
        self.assertEqual(self.pin(), (CX, 'content', True, 'codex'))
        self.assertEqual(self.conflict(), [P])
        self.assertFalse(self.links.decisions[K].child.codex_free)                            # the names of P were what said that no Codex shell was behind it

    def test_a_claude_pin_stays_when_a_call_of_its_tree_was_running(self):
        self.parent(self.run_call())
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.TMUX)])
        self.detached(claude=P, claude_pid=100)
        self.assertEqual(self.pin(), (P, 'env', True, 'claude'))
        self.assertEqual(self.conflict(), [])
        self.assertTrue(self.links.decisions[K].child.codex_free)

    def test_a_claude_pin_stays_when_a_call_of_one_of_its_sub_agents_was_running(self):
        agent = 'a' + 'b' * 16
        d = os.path.join(self.proj, P, 'subagents')
        os.makedirs(d)
        self.parent()
        with open(os.path.join(d, 'agent-%s.jsonl' % agent), 'w') as f:
            f.write('\n'.join(self.run_call()) + '\n')
        with open(os.path.join(d, 'agent-%s.meta.json' % agent), 'w') as f:
            json.dump({'description': 'helper', 'agentType': 'general-purpose', 'toolUseId': 'toolu_x'}, f)
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.TMUX)])
        self.detached(claude=P, claude_pid=100)
        got = self.pin()
        self.assertEqual(got[:2], (P, 'env'))

    def test_a_claude_pin_stays_when_nothing_else_explains_the_run(self):
        """A launcher the board cannot see (`make run`) is no reason to drop the names: nothing else fits."""
        self.parent([bash_line(T0 - 1, 'make run', tid='toolu_make'), result_line(T0 + 30, 'toolu_make')])
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.detached(claude=P, claude_pid=100)
        self.assertEqual(self.pin(), (P, 'env', True, 'claude'))
        self.assertEqual(self.conflict(), [])

    def test_a_claude_pin_stays_when_the_other_launch_is_only_a_guess(self):
        """Another session ran a `claude -p` with other words at that time: the time rule would pick it, which is a guess and no reason to drop the names."""
        self.parent()
        Q = '77777777-7777-4777-8777-777777777777'
        self.write(Q, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})] + self.run_call('toolu_q', 'cd /w && claude -p --model m "Another instruction altogether."'))
        self.detached(claude=P, claude_pid=100)
        self.assertEqual(self.pin()[:3], (P, 'env', True))
        self.assertEqual(self.conflict(), [])

    # ---- a Codex pin ----
    def test_a_thread_that_ran_nothing_that_started_it_is_not_the_parent_when_another_session_did(self):
        """T's turn is open around the start (its only command is `ls`, and its record is whole): the names are the left-over of an earlier turn."""
        self.parent(self.run_call())
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], until=T0 + 60)
        self.detached(thread=CX, session=CX)
        self.assertEqual(self.pin(), (P, 'content', True, 'claude'))
        self.assertEqual(self.conflict(), [CX])

    def test_a_codex_pin_stays_when_the_thread_ran_a_launch_then(self):
        self.parent(self.run_call())
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.TMUX)])
        self.detached(thread=CX, session=CX)
        self.assertEqual(self.pin(), (CX, 'env', True, 'codex'))
        self.assertEqual(self.conflict(), [])

    def running_now(self, path, call_id='call-running'):
        """An exec call of the open turn that has no output yet: a command that runs now, whose record comes when it ends."""
        with open(path, 'a') as f:
            f.write(line(T0 - 1, 'response_item', exec_call(call_id)) + '\n')

    def test_a_codex_pin_stays_when_a_command_runs_in_the_thread_whose_record_has_not_come(self):
        """The turn is open and an exec call has no output yet: a command written when it ends is not in the facts, and it may be the launch."""
        self.parent(self.run_call())
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True)
        self.running_now(path)
        self.codex_proc(300, 1, path)
        self.detached(thread=CX, session=CX)
        self.assertEqual(self.pin()[:3], (CX, 'env', True))
        self.assertEqual(self.conflict(), [])

    def test_a_codex_pin_stays_when_a_background_command_of_the_thread_has_no_record(self):
        """A cell that answered "running" and a record that has not come: the same, told apart from the turn being open."""
        self.parent(self.run_call())
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True)
        with open(path, 'a') as f:
            f.write(line(T0 - 40, 'response_item', exec_call('call-bg')) + '\n' + line(T0 - 39, 'response_item', output('call-bg', 'Script running with cell ID 7')) + '\n')
        self.codex_proc(300, 1, path)
        self.detached(thread=CX, session=CX)
        self.assertEqual(self.pin()[:3], (CX, 'env', True))
        self.assertEqual(self.conflict(), [])

    def test_a_command_that_has_answered_is_not_one_that_runs_now(self):
        """An exec call whose output came (and whose record is there) is no gap: the open turn alone is no reason to keep the names."""
        self.parent(self.run_call())
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True)
        with open(path, 'a') as f:
            f.write(line(T0 - 30, 'response_item', exec_call('call-done')) + '\n' + line(T0 - 29, 'response_item', output('call-done', 'Script completed', 'ok')) + '\n')
        self.codex_proc(300, 1, path)
        self.detached(thread=CX, session=CX)
        self.assertEqual(self.pin()[:3], (P, 'content', True))
        self.assertEqual(self.conflict(), [CX])

    def test_a_command_that_was_running_in_a_thread_nobody_has_open_any_more_is_no_reason_either(self):
        """Its process is gone: the call with no output ended with the thread (the gap ends where the thread was last written), long before the child started."""
        self.parent(self.run_call())
        path = self.thread(CX, t=T0 - 5000, cmds=[(T0 - 4990, T0 - 4989, 'ls')], open_turn=True)
        with open(path, 'a') as f:
            f.write(line(T0 - 4900, 'response_item', exec_call('call-old')) + '\n')
        self.detached(thread=CX, session=CX)
        self.assertEqual(self.pin()[:3], (P, 'content', True))
        self.assertEqual(self.conflict(), [CX])

    # ---- a child with a resumed run: the names are judged for the run they are about ----
    def resumed_child(self, with_env=True):
        """K's first run (+2 s): the thread CX has a background cell whose record never came (a gap that covers the start), and P ran a launch with the same long instruction. Its resumed run
        (+3600 s): CX has only `ls`, in a turn that covers it, and P resumed K by its id; the names of CX in the environment are the older ones."""
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'run it'}}),
                       bash_line(T0 - 1, self.claude_run(TEXT), tid='toolu_run'), result_line(T0 + 30, 'toolu_run'),
                       bash_line(T0 + 3599, 'cd /w && claude -p --resume %s "go on"' % K, tid='toolu_resume'), result_line(T0 + 3640, 'toolu_resume')])
        meta = root_meta(CX, originator='codex-tui', source='cli', cwd='/w', timestamp=link_iso(T0 - 100))
        self.rollout_file(CX, [line(T0 - 100, 'session_meta', meta), line(T0 - 99, 'event_msg', {'type': 'task_started'}),
                               line(T0 + 1, 'response_item', exec_call('call-bg')), line(T0 + 1.5, 'response_item', output('call-bg', 'Script running with cell ID 7')),
                               line(T0 + 30, 'event_msg', {'type': 'task_complete'}), line(T0 + 3500, 'event_msg', {'type': 'task_started'})]
                              + self.command_lines(CX, (T0 + 3550, T0 + 3551, 'ls'), 0) + [line(T0 + 3700, 'event_msg', {'type': 'task_complete'})])
        self.write(K, child_lines(T0 + 2, text=TEXT) + [dump({'type': 'cost-state', 'timestamp': link_iso(T0 + 40), 'sessionId': K, 'totalDuration': 6000})]
                   + child_lines(T0 + 3602, text='go on'))
        self.proc.add(103, 1, ['claude', '-p', 'x'])
        self.session_file(103, K)
        if with_env:
            environ(self.proc, 103, thread=CX, session=CX)

    def test_the_tree_whose_names_are_out_for_the_resumed_run_is_still_a_competitor_for_the_first(self):
        """The names are judged for the run they belong to (the last): the resumed run drops them (CX ran nothing then, and P's `--resume K` fits), and that clears CX for that run only. In the
        first run CX has a record that never came, so P's match of the words is a guess: not certain, and not kept in links.json."""
        path = os.path.join(self.t, 'xdg', 'agent-bullpen', 'links.json')
        self.links.lineage.enable_cache(path)
        self.resumed_child()
        L = self.judge(T0 + 7200)
        d = L.decisions[K]
        self.assertEqual(d.run_ranks[:1], [4])                                                     # the first run: the content match, as a guess
        o = L.cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (P, 'content_short', False))
        self.assertIn('fingerprint_incomplete', [x['code'] for x in L.diags if x['subject'] == K])
        self.assertIn(CX, [x.get('other') for x in L.diags if x['subject'] == K and x['code'] == 'evidence_conflict'])      # the names were dropped for the resumed run
        self.assertNotIn(K, L.lineage.saved)
        with open(path) as fh:
            rows = [r for r in json.load(fh)['links'] if r['child'] == K]
        self.assertEqual([(r['rule'], r['parent']) for r in rows], [('env', CX)])               # what the lineage saw of the process; no content link to P is kept

    def test_without_the_older_names_the_first_run_is_a_guess_the_same_way(self):
        self.resumed_child(with_env=False)
        L = self.judge(T0 + 7200)
        o = L.cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (P, 'content_short', False))

    def test_a_single_run_whose_names_are_out_still_clears_the_tree_for_that_run(self):
        """(the `stale_turn` shape: one run, the thread's open turn alone)"""
        self.parent(self.run_call())
        self.codex_proc(300, 1, self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True))
        self.detached(thread=CX, session=CX)
        self.assertEqual(self.pin(), (P, 'content', True, 'claude'))

    def test_an_open_turn_whose_commands_are_all_there_is_no_reason_to_keep_the_names(self):
        """The turn the names come from is still open, with `ls` twice and nothing else: it was in a turn when the child started, which says nothing. Another session's call fits the words:
        the names are out, the other launch is firm (the thread is no competitor nobody can read: it was found not to be the one)."""
        self.parent(self.run_call())
        self.codex_proc(300, 1, self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls'), (T0 - 30, T0 - 29, 'ls -la')], open_turn=True))
        self.detached(thread=CX, session=CX)
        self.assertEqual(self.pin(), (P, 'content', True, 'claude'))
        self.assertEqual(self.conflict(), [CX])

    def test_another_open_turn_still_makes_the_other_launch_a_guess(self):
        """C7b is not changed: a thread that may have a command missing in an open turn (not the thread the names are about) keeps a content match from being certain."""
        self.parent(self.run_call())
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], until=T0 + 60)
        self.codex_proc(300, 1, self.thread(CX2, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True))
        self.detached(thread=CX, session=CX)
        self.assertEqual(self.pin()[:3], (CX, 'env', True))                                  # the other open turn keeps the competitor incomplete: the names stay

    def test_a_codex_pin_stays_when_nothing_else_explains_the_run(self):
        self.parent()
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], until=T0 + 60)
        self.detached(thread=CX, session=CX)
        self.assertEqual(self.pin(), (CX, 'env', True, 'codex'))
        self.assertEqual(self.conflict(), [])

    def test_a_thread_that_may_have_a_command_missing_keeps_the_names_whatever_they_say_about_codex(self):
        """The names of P (live, both) would say that no Codex shell started the child; they are what is in question, so a thread of the folder whose facts may lack a command (an open turn
        that something has open) still stops the other launch from being firm: the names stay."""
        self.parent()
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.TMUX)])
        self.codex_proc(300, 1, self.thread(CX2, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True))
        self.detached(claude=P, claude_pid=100)
        self.assertEqual(self.pin()[:3], (P, 'env', True))
        self.assertEqual(self.conflict(), [])

    # ---- a `codex exec` thread ----
    def exec_child(self, **env):
        path = self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=CodexChildren.PROMPT)
        self.proc.add(220, 1, ['codex', 'exec', 'x'], fds=[path])
        environ(self.proc, 220, **env)

    def test_a_codex_exec_thread_is_not_the_child_of_a_session_that_ran_nothing_that_started_it(self):
        self.parent()
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, 'cd /w && codex exec -C /w "%s"' % CodexChildren.PROMPT)])
        self.exec_child(claude=P, claude_pid=100)
        L = self.judge()
        o = L.owners[CX2]
        self.assertEqual((o['sid'], o['rule']), (CX, 'prompt'))
        self.assertIn(('evidence_conflict', P), [(x['code'], x.get('other')) for x in L.diags if x['subject'] == CX2])

    def test_a_codex_exec_thread_keeps_the_names_when_another_thread_may_have_a_command_missing(self):
        self.parent()
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, 'cd /w && codex exec -C /w "%s"' % CodexChildren.PROMPT)])
        self.codex_proc(300, 1, self.thread(SUB, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True))
        self.exec_child(claude=P, claude_pid=100)
        L = self.judge()
        self.assertEqual(L.owners[CX2]['sid'], P)
        self.assertNotIn('evidence_conflict', [x['code'] for x in L.diags if x['subject'] == CX2])

    def test_a_codex_exec_thread_keeps_the_names_of_a_thread_with_a_command_running_that_has_no_record(self):
        """The environment names the thread CX, which has an exec call with no output yet (a command that runs now); another session's call fits the words: it does not take the thread away."""
        self.parent([bash_line(T0 - 1, 'cd /w && codex exec -C /w "%s"' % CodexChildren.PROMPT, tid='toolu_x'), result_line(T0 + 30, 'toolu_x')])
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True)
        self.running_now(path)
        self.codex_proc(300, 1, path)
        self.exec_child(thread=CX, session=CX)
        L = self.judge()
        self.assertEqual(L.owners[CX2]['sid'], CX)
        self.assertNotIn('evidence_conflict', [x['code'] for x in L.diags if x['subject'] == CX2])

    def test_a_codex_exec_thread_does_not_keep_the_names_of_an_open_turn_whose_commands_are_all_there(self):
        self.parent([bash_line(T0 - 1, 'cd /w && codex exec -C /w "%s"' % CodexChildren.PROMPT, tid='toolu_x'), result_line(T0 + 30, 'toolu_x')])
        self.codex_proc(300, 1, self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True))
        self.exec_child(thread=CX, session=CX)
        L = self.judge()
        self.assertEqual((L.owners[CX2]['sid'], L.owners[CX2]['rule']), (P, 'prompt'))
        self.assertIn(('evidence_conflict', CX), [(x['code'], x.get('other')) for x in L.diags if x['subject'] == CX2])

    def test_a_codex_exec_thread_keeps_the_session_whose_call_was_running(self):
        self.parent([bash_line(T0 - 1, 'cd /w && codex exec -C /w "%s"' % CodexChildren.PROMPT, tid='toolu_x'), result_line(T0 + 30, 'toolu_x')])
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, 'cd /w && codex exec -C /w "%s"' % CodexChildren.PROMPT)])
        self.exec_child(claude=P, claude_pid=100)
        L = self.judge()
        self.assertEqual(L.owners[CX2]['sid'], P)                                             # (the words of its own call fill in the details: the rule is then `prompt`)
        self.assertNotIn('evidence_conflict', [x['code'] for x in L.diags if x['subject'] == CX2])

    def test_a_codex_exec_thread_keeps_the_session_when_nothing_else_explains_it(self):
        self.parent()
        self.exec_child(claude=P, claude_pid=100)
        L = self.judge()
        self.assertEqual((L.owners[CX2]['sid'], L.owners[CX2]['rule']), (P, 'env'))

    def test_a_sub_agent_pin_is_read_against_the_whole_tree(self):
        """The names say a sub-agent ran the command; a launch of the root at that time is the tree's own, so the tree is not dropped for the sub-agent's sake."""
        self.parent(self.run_call())
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.TMUX)])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')], until=T0 + 60)
        self.detached(thread=SUB, session=CX)
        got = self.pin()
        self.assertEqual((got[0], got[1]), (CX, 'env'))
        self.assertEqual(self.conflict(), [])

    def test_a_sub_agent_pin_is_out_when_nothing_of_the_tree_ran_a_launch(self):
        self.parent(self.run_call())
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], until=T0 + 60)
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')], until=T0 + 60)
        self.detached(thread=SUB, session=CX)
        self.assertEqual(self.pin()[:3], (P, 'content', True))
        self.assertEqual(self.conflict(), [CX])


class CodexProcess(LineageFixture):
    """C11: the nearest agent among the ancestors; a codex process names the root of its open rollouts, never the node."""
    def test_a_child_of_a_codex_shell_is_attached_to_the_root_of_that_process(self):
        self.codex_tui(200, CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201)
        self.assertEqual(self.pin(), (CX, None, 'proc', True, 'codex'))

    def test_the_sub_agents_rollouts_the_process_has_open_are_the_same_root(self):
        p1 = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        p2 = self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.proc.add(200, 1, ['codex'], fds=[p1, p2])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201)
        self.assertEqual(self.pin()[:3], (CX, None, 'proc'))

    def test_a_process_that_holds_the_rollouts_of_several_roots_names_none(self):
        p1 = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        p2 = self.thread(CX2, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.proc.add(200, 1, ['codex', 'app-server'], fds=[p1, p2])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201)
        self.assertIsNone(self.pin())                                                  # and the outer Claude is not taken instead: the nearest agent is this codex
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.proc.remove(200)
        self.proc.add(200, 100, ['codex', 'app-server'], fds=[p1, p2])
        self.proc.remove(201)
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.proc.remove(103)
        self.claude_child(103, 201)
        self.assertIsNone(self.pin())

    def test_a_rollout_whose_root_is_not_known_holds_the_tree(self):
        """One codex process holds the rollout of a known root and one whose first line is not complete yet: nothing says the second is not another tree, so the process names no root
        (the remaining root is not taken for the only one). An environment that is checked on its own still decides."""
        p1 = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        p2 = self.rollout_file(CX2, ['{"timestamp":"2026-10-04T00:00:00.000Z","type":"session_meta","payload":{"id":"%s","cwd":"/w' % CX2])       # the first line is cut
        self.proc.add(200, 1, ['codex', 'app-server'], fds=[p1, p2])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201)
        self.assertIsNone(self.pin())
        self.setUp()
        p1 = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        p2 = self.rollout_file(CX2, ['{"timestamp":"2026-10-04T00:00:00.000Z","type":"session_meta","payload":{"id":"%s","cwd":"/w' % CX2])
        self.proc.add(200, 1, ['codex', 'app-server'], fds=[p1, p2])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201, thread=CX, session=CX)
        self.assertEqual(self.pin()[:3], (CX, None, 'env'))

    def test_the_environment_decides_when_the_process_holds_many_roots(self):
        p1 = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        p2 = self.thread(CX2, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.proc.add(200, 1, ['codex', 'app-server'], fds=[p1, p2])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201, thread=CX2, session=CX2)
        self.assertEqual(self.pin()[:3], (CX2, None, 'env'))

    def test_a_codex_whose_open_files_cannot_be_seen_is_an_agent_with_no_known_root(self):
        """macOS (no /proc: `ps` cannot say which rollout a process holds), or a codex with no rollout open: it is the nearest agent, so the Claude above it is not the parent, and what
        names the Codex thread (the environment) decides; without that nothing is linked."""
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
        self.proc.add(200, 100, ['codex', 'exec', 'x'])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201, claude=P, claude_pid=100)                                # Claude's names are left over from the session above the codex
        self.assertIsNone(self.pin())
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.remove(103)
        self.claude_child(103, 201, claude=P, claude_pid=100, thread=CX, session=CX)
        self.assertEqual(self.pin()[:4], (CX, None, 'env', True))
        self.setUp()
        self.proc.add(200, 1, ['node', '/usr/lib/codex/bin/codex.js', 'exec', 'x'])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201)
        self.assertIsNone(self.pin())

    def test_a_claude_between_is_the_nearest_agent(self):
        """Codex → claude -p (A) → bash → claude -p (the grandchild): the grandchild belongs to A, not to the Codex above A."""
        self.codex_tui(200, CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.proc.add(202, 201, ['claude', '-p', 'x'])
        self.session_file(202, Q)
        self.write(Q, child_lines(T0 + 1, text='first'))
        self.proc.add(203, 202, ['/bin/bash', '-c', 'claude -p y'])
        self.claude_child(204, 203, thread=CX, session=CX)                               # the Codex variables are left over from the top
        L = self.judge()
        self.assertEqual((L.cli_owners[K]['sid'], L.cli_owners[K]['rule']), (Q, 'proc'))
        self.assertEqual((L.cli_owners[Q]['sid'], L.cli_owners[Q]['parent_kind']), (CX, 'codex'))


class TwoProviders(LineageFixture):
    """C10: the environment of a shell that has both providers' variables (Claude's from the session above, Codex's from the thread): the nearest agent decides, else the
    chain of certain links does, else nothing is linked."""
    def test_a_codex_between_makes_the_codex_variables_count_and_the_claude_ones_not(self):
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(200, 100, ['codex', 'exec', 'x'], fds=[path])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201, claude=P, claude_pid=100, thread=CX, session=CX)
        self.assertEqual(self.pin()[:4], (CX, None, 'env', True))

    def test_a_claude_between_makes_the_claude_variables_count(self):
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(101, 100, ['/bin/bash', '-c', 'claude -p x'])
        self.claude_child(103, 101, claude=P, claude_pid=100, thread=CX, session=CX)
        self.assertEqual(self.pin()[:4], (P, None, 'env', True))

    def test_the_nearest_codex_and_a_codex_variable_for_another_root_hold_the_link(self):
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(CX2, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(200, 1, ['codex'], fds=[path])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201, thread=CX2, session=CX2)
        self.assertIsNone(self.pin())

    def test_each_row_of_a_detached_process(self):
        """The chain is broken (the parent is init): Codex only, Claude only, both, both that cannot be told apart."""
        for env, want in ((dict(thread=CX, session=CX), (CX, 'env', 'codex')),
                          (dict(claude=P), (P, 'env', 'claude')),
                          (dict(claude=P, thread=CX, session=CX), None),                             # both and nothing says which is below which
                          (dict(claude=P, thread='aaaaaaaa-0000-4000-8000-0000000000ff'), None),    # a Codex variable that cannot be trusted next to a Claude one
                          (dict(thread='aaaaaaaa-0000-4000-8000-0000000000ff'), None)):
            with self.subTest(env):
                self.setUp()
                self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
                self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
                self.claude_child(103, 1, **env)
                got = self.pin()
                self.assertEqual(got and (got[0], got[2], got[4]), want, env)

    def exec_below_claude(self):
        """P (Claude) → codex exec (the thread CX2, found through the process lineage): a certain link from P to CX2."""
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
        path = self.thread(CX2, origin='codex_exec', cmds=[(T0 - 50, T0 - 49, 'ls')], user='fix the thing in the build')
        self.proc.add(101, 100, ['/bin/bash', '-c', 'codex exec x'])
        self.proc.add(210, 101, ['codex', 'exec', 'x'], fds=[path])

    def test_a_session_above_the_codex_root_makes_the_codex_thread_the_direct_parent(self):
        self.exec_below_claude()
        self.claude_child(103, 1, claude=P, claude_pid=100, thread=CX2, session=CX2)
        L = self.judge()
        self.assertEqual(L.owners[CX2]['sid'], P)
        self.assertEqual((L.cli_owners[K]['sid'], L.cli_owners[K]['rule'], L.cli_owners[K]['parent_kind']), (CX2, 'env', 'codex'))
        self.assertEqual(L.page_of(K), P)

    def test_a_codex_root_above_the_session_makes_the_session_the_direct_parent(self):
        """Codex (CX) → claude -p (K2, by its process) → a detached grandchild that has K2's variables and the Codex ones left over."""
        self.codex_tui(200, CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.proc.add(202, 201, ['claude', '-p', 'x'])
        self.session_file(202, K2)
        self.write(K2, child_lines(T0 + 1, text='first'))
        self.claude_child(103, 1, claude=K2, claude_pid=202, thread=CX, session=CX)
        L = self.judge()
        self.assertEqual((L.cli_owners[K2]['sid'], L.cli_owners[K2]['rule']), (CX, 'proc'))
        self.assertEqual((L.cli_owners[K]['sid'], L.cli_owners[K]['rule'], L.cli_owners[K]['parent_kind']), (K2, 'env', 'claude'))
        self.assertEqual(L.page_of(K), CX)

    def test_unrelated_roots_hold_the_link_and_a_guess_is_not_a_link_of_the_chain(self):
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.claude_child(103, 1, claude=P, claude_pid=100, thread=CX, session=CX)
        L = self.judge()
        self.assertNotIn(K, L.cli_owners)
        self.assertEqual(list(L.lineage.pending), [K])
        # a link that is only a guess (the time rule) does not make P the owner of CX2
        self.setUp()
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.thread(CX2, t=T0 - 2, origin='codex_exec', cmds=[(T0 - 1, T0, 'ls')], user='go')
        self.write(P, [bash_line(T0 - 3, 'cd /w && codex exec "go"', tid='toolu_x')])
        self.claude_child(103, 1, claude=P, claude_pid=100, thread=CX2, session=CX2)
        L = self.judge()
        self.assertEqual((L.owners[CX2]['sid'], L.owners[CX2]['rule']), (P, 'time'))
        self.assertFalse(link.certain(L.owners[CX2]['rule']))
        self.assertNotIn(K, L.cli_owners)

    def test_a_held_child_says_why_and_no_lower_rank_breaks_the_tie(self):
        """Both providers' names point to different parents and the chain does not say: the child is held at rank 2. The words of the Codex thread's command, which would link it
        if the environment were not there, do not break it."""
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])
        self.claude_child(103, 1, claude=P, claude_pid=100, thread=CX, session=CX)
        self.write(K, child_lines(T0 + 2, text=TEXT))
        L = self.judge()
        self.assertNotIn(K, L.cli_owners)
        d = L.decisions[K]
        self.assertEqual((d.tree, d.held, sorted(d.held_trees)), (None, 'ambiguous', sorted([P, CX])))
        self.assertEqual(sorted(x['other'] for x in L.diags if x['code'] == 'evidence_conflict' and x['subject'] == K), sorted([P, CX]))
        self.assertEqual(L.page_of(K), K)
        self.assertEqual(L.descendants(CX), [])

    def test_a_held_codex_exec_thread_is_placed_by_nothing_below_the_environment(self):
        self.write(P, [bash_line(T0 - 1, 'cd /w && codex exec -C /w "%s"' % CodexChildren.PROMPT, tid='toolu_x'), result_line(T0 + 30, 'toolu_x')])
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        p2 = self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=CodexChildren.PROMPT)
        self.proc.add(220, 1, ['codex', 'exec', 'x'], fds=[p2])
        environ(self.proc, 220, claude=P, claude_pid=100, thread=CX, session=CX)
        L = self.judge()
        self.assertNotIn(CX2, L.owners)                                                        # the words of P's call would have placed it: held instead
        self.assertEqual(list(L.lineage.pending), [CX2])

    def test_the_pending_child_is_decided_as_soon_as_the_chain_is_known(self):
        self.exec_below_claude()
        self.claude_child(103, 1, claude=P, claude_pid=100, thread=CX2, session=CX2)
        self.proc.remove(210)                                                           # the link of CX2 is not known (yet): its process is not there
        L = self.judge()
        self.assertNotIn(K, L.cli_owners)
        self.proc.add(210, 101, ['codex', 'exec', 'x'], fds=[os.path.join(self.codex, 'sessions', '2026', '10', '04', 'rollout-2026-10-04T00-00-00-%s.jsonl' % CX2)])
        L = self.judge()
        self.assertEqual(L.cli_owners[K]['sid'], CX2)


class CodexChildren(LineageFixture):
    """A `codex exec` thread started by a Codex thread: the same rules, never the thread itself."""
    PROMPT = 'Audit the amber basin cedar delta ember fjord grove harbor island juniper kelp lagoon meadow and write the findings down in plain words.'

    def exec_run(self, prompt=None):
        return 'cd /w && codex exec -C /w "%s"' % (prompt or self.PROMPT)

    def test_the_environment_of_a_codex_shell_places_an_exec_thread(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        p2 = self.thread(CX2, t=T0, origin='codex_exec', cmds=[(T0 + 1, T0 + 2, 'ls')], user='something else entirely')
        self.proc.add(220, 1, ['codex', 'exec', 'x'], fds=[p2])
        environ(self.proc, 220, thread=SUB, session=CX)
        o = self.judge().owners[CX2]
        self.assertEqual((o['sid'], o['node'], o['rule'], o['parent_kind']), (CX, SUB, 'env', 'codex'))
        self.assertTrue(link.certain(o['rule']))

    def test_the_nearest_codex_process_places_an_exec_thread(self):
        self.codex_tui(200, CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'codex exec x'])
        p2 = self.thread(CX2, t=T0, origin='codex_exec', cmds=[(T0 + 1, T0 + 2, 'ls')], user='something else entirely')
        self.proc.add(220, 201, ['codex', 'exec', 'x'], fds=[p2])
        o = self.judge().owners[CX2]
        self.assertEqual((o['sid'], o['node'], o['rule'], o['parent_kind']), (CX, None, 'proc', 'codex'))

    def test_the_command_of_a_codex_thread_places_it_by_its_instruction(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.exec_run())])
        self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        o = self.judge().owners[CX2]
        self.assertEqual((o['sid'], o['node'], o['rule'], o['call'], o['parent_kind']), (CX, None, 'prompt', 'call-01-0', 'codex'))

    def test_a_sub_agent_that_ran_it_is_the_node(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 1, T0 + 30, self.exec_run())])
        self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        o = self.judge().owners[CX2]
        self.assertEqual((o['sid'], o['node'], o['rule']), (CX, SUB, 'prompt'))

    def test_the_node_the_environment_names_is_not_taken_from_another_sub_agents_command(self):
        """Tree, node and call are decided on their own: the environment of the child's process says the sub-agent SUB ran the shell; the same instruction sits in a command of SUB2.
        The node stays SUB (an environment is rank 2, the words rank 3) and the call of the other node is not the call."""
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.thread(SUB2, sub_of=CX, path='/root/s2', cmds=[(T0 - 1, T0 + 30, self.exec_run())])
        p2 = self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        self.proc.add(220, 1, ['codex', 'exec', 'x'], fds=[p2])
        environ(self.proc, 220, thread=SUB, session=CX)
        o = self.judge().owners[CX2]
        self.assertEqual((o['sid'], o['node'], o['rule'], o['call']), (CX, SUB, 'env', None))
        self.setUp()
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 1, T0 + 30, self.exec_run())])
        p2 = self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        self.proc.add(220, 1, ['codex', 'exec', 'x'], fds=[p2])
        environ(self.proc, 220, thread=SUB, session=CX)
        o = self.judge().owners[CX2]
        self.assertEqual((o['sid'], o['node'], o['call']), (CX, SUB, 'call-02-0'))                         # the call of that same node is filled in
        self.setUp()
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.exec_run())])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        p2 = self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        self.proc.add(220, 1, ['codex', 'exec', 'x'], fds=[p2])
        environ(self.proc, 220, thread=SUB, session=CX)
        o = self.judge().owners[CX2]
        self.assertEqual((o['node'], o['call']), (SUB, None))                                              # the words are in the root's command: not the call of SUB

    def test_launches_of_several_nodes_of_one_tree_hold_the_node_and_the_call(self):
        """Two sub-agents of one tree run `codex exec` with the same instruction in the same folder at once: the tree is sure, the nearest in time is no reason to take one."""
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 2, T0 + 30, self.exec_run())])
        self.thread(SUB2, sub_of=CX, path='/root/s2', cmds=[(T0 - 1, T0 + 30, self.exec_run())])
        self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        L = self.judge()
        o = L.owners[CX2]
        self.assertEqual((o['sid'], o['node'], o['call'], o['rule'], link.certain(o['rule'])), (CX, None, None, 'prompt', True))
        self.assertIn(('node_unresolved', CX2), [(d['code'], d['subject']) for d in L.diags])
        self.setUp()                                                                                       # one node: the call is still the nearest one
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 2, T0 + 30, self.exec_run())])
        self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        o = self.judge().owners[CX2]
        self.assertEqual((o['sid'], o['node'], o['call']), (CX, SUB, 'call-02-0'))

    def test_a_thread_that_may_have_run_an_unknown_command_makes_the_match_an_estimate(self):
        """An open turn of a live Codex thread of another tree in the folder is a competitor nobody can read: the words still match, but that is a guess (and the lineage is not touched)."""
        path = self.thread(CX3, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
        self.codex_proc(300, 1, path)
        self.write(P, [bash_line(T0 - 1, self.exec_run(), tid='toolu_x'), result_line(T0 + 30, 'toolu_x')])
        self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        L = self.judge()
        o = L.owners[CX2]
        self.assertEqual((o['sid'], o['rule']), (P, 'time'))
        self.assertFalse(link.certain(o['rule']))
        self.assertIn(('fingerprint_incomplete', CX2), [(d['code'], d['subject']) for d in L.diags])

    def test_a_blind_thread_in_another_folder_or_one_nobody_runs_or_one_that_cannot_have_started_it_does_not_count(self):
        for label, cwd, alive, child_env in (('another folder', '/elsewhere', True, None), ('nobody runs it', '/w', False, None),
                                              ('the child has no name of Codex and a reason', '/w', True, dict(claude=Q, claude_pid=100))):
            with self.subTest(label):
                self.setUp()
                path = self.thread(CX3, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd=cwd)
                if alive:
                    self.codex_proc(300, 1, path)
                self.write(P, [bash_line(T0 - 1, self.exec_run(), tid='toolu_x'), result_line(T0 + 30, 'toolu_x')])
                p2 = self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
                if child_env is not None:
                    self.proc.add(100, 1, ['claude'])
                    self.session_file(100, Q, entrypoint='cli')
                    self.proc.add(220, 1, ['codex', 'exec', 'x'], fds=[p2])
                    environ(self.proc, 220, **child_env)
                self.assertEqual(self.judge().owners[CX2]['rule'], 'prompt', label)

    def test_a_blind_thread_of_the_launchers_tree_or_above_it_in_the_chain_is_no_competitor(self):
        """A Codex TUI (turn open: it waits for a foreground command) started an exec thread that started another: the top may have run something unknown, but the exec thread that ran
        the child is its own descendant. The same for a sub-agent of the launcher's own tree."""
        path = self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')], open_turn=True, cwd='/w')
        self.codex_proc(300, 1, path)
        self.proc.add(301, 300, ['/bin/bash', '-lc', 'codex exec x'])
        pm = self.thread(CX2, t=T0 - 30, origin='codex_exec', cmds=[(T0 - 1, T0 + 30, self.exec_run())], user='something else entirely', open_turn=True)
        self.proc.add(310, 301, ['codex', 'exec', 'x'], fds=[pm])
        self.thread(CX3, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        L = self.judge()
        self.assertEqual((L.owners[CX2]['sid'], L.owners[CX2]['rule']), (CX, 'proc'))
        o = L.owners[CX3]
        self.assertEqual((o['sid'], o['rule']), (CX2, 'prompt'))                                          # certain: the TUI above is not a rival
        self.assertNotIn(('fingerprint_incomplete', CX3), [(d['code'], d['subject']) for d in L.diags])

    def test_a_thread_is_not_taken_for_what_it_launched_itself(self):
        self.thread(CX, t=T0, origin='codex_exec', cmds=[(T0 - 1, T0 + 30, self.exec_run())], user=self.PROMPT)          # its words are the words of its own command
        self.assertNotIn(CX, self.judge().owners)
        self.thread(CX3, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        self.assertEqual(self.judge().owners[CX3]['sid'], CX)                                # another thread with the same words is

    def test_a_thread_that_resumes_itself_is_not_its_own_parent(self):
        self.thread(CX, origin='codex_exec', cmds=[(T0 - 1, T0 + 30, 'cd /w && codex exec resume %s "go on"' % CX)], user=self.PROMPT)
        self.assertNotIn(CX, self.judge().owners)

    def test_a_claude_session_and_a_codex_thread_that_both_ran_it_are_a_tie(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.exec_run())])
        self.write(P, [bash_line(T0 - 1, self.exec_run(), tid='toolu_x'), result_line(T0 + 30, 'toolu_x')])
        self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        self.assertNotIn(CX2, self.judge().owners)


class CodexOrphans(CodexFixture):
    """A `&` command of a Codex shell takes the run down with it: no thread was written, and the launch with nothing behind it is counted (no card)."""
    PROMPT = CodexChildren.PROMPT

    def orphans(self, now=T0 + 600):
        L = self.judge(now)
        return sorted((d['tree'], d['node'], d['n']) for d in L.diags if d['code'] == 'orphan_launch')

    def test_a_codex_exec_launch_that_left_no_thread_is_an_orphan(self):
        self.thread(CX, cmds=[(T0 - 1, T0, 'cd /w && nohup codex exec -C /w "%s" > /dev/null 2>&1 &' % self.PROMPT)])
        self.assertEqual(self.orphans(), [(CX, None, 1)])
        self.assertEqual(self.links.owners, {})                                                 # and there is no card

    def test_the_launch_of_a_sub_agent_is_counted_for_that_node(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 1, T0, 'cd /w && nohup codex exec -C /w "%s" > /dev/null 2>&1 &' % self.PROMPT)])
        self.assertEqual(self.orphans(), [(CX, SUB, 1)])

    def test_a_thread_that_began_in_its_time_and_folder_accounts_for_it_linked_or_not(self):
        self.thread(CX, cmds=[(T0 - 1, T0, 'cd /w && codex exec -C /w "%s" &' % self.PROMPT)])
        self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user=self.PROMPT)
        self.assertEqual(self.orphans(), [])
        self.setUp()
        self.thread(CX, cmds=[(T0 - 1, T0, 'cd /w && codex exec -C /w "%s" &' % self.PROMPT)])
        self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user='something else entirely, that this launch cannot have said', cwd='/w')
        self.assertEqual(self.orphans(), [(CX, None, 1)])                                       # its literal instruction refutes the thread: it is not this launch's

    def test_a_resume_a_loop_a_call_that_is_not_over_and_a_claude_sessions_launch_are_not_counted_here(self):
        self.thread(CX, cmds=[(T0 - 1, T0, 'cd /w && codex exec resume %s "go on"' % CX2)])
        self.assertEqual(self.orphans(), [])
        self.setUp()
        self.thread(CX, cmds=[(T0 + 595, T0 + 599, 'cd /w && codex exec "%s" &' % self.PROMPT)])        # over 1 s ago: a thread may still be starting
        self.assertEqual(self.orphans(now=T0 + 600), [])
        self.assertEqual(self.orphans(now=T0 + 640), [(CX, None, 1)])                    # (the judgment is made again every 30 s)
        self.setUp()
        self.write(P, [bash_line(T0 - 1, 'cd /w && nohup codex exec -C /w "%s" > /dev/null 2>&1 &' % self.PROMPT, tid='toolu_x'), result_line(T0, 'toolu_x')])
        self.assertEqual(self.orphans(), [])                                                    # the Claude side is as it was

    def test_a_loop_of_unknown_count_counts_one(self):
        self.thread(CX, cmds=[(T0 - 1, T0, 'cd /w && while read p; do codex exec -C /w "$p" & done < prompts.txt')])
        self.assertEqual(self.orphans(), [(CX, None, 1)])


class UnlinkedNearCodex(CodexFixture):
    """The missed candidates of a Codex page: a child that began shortly after a command of the thread but could not be linked anywhere."""

    def listed(self, tree=CX, now=T0 + 600):
        L = self.judge(now)
        return sorted((x['id'], x['provider'], x['reason']) for x in L.unlinked_for(tree))

    def test_a_claude_child_after_a_command_in_its_folder_with_no_launch_to_match(self):
        self.thread(CX, cmds=[(T0 - 5, T0 - 4, 'ls'), (T0 - 3, T0 - 2, 'git status')])
        self.write(K, sdk_lines(T0 + 2, 'run that nobody launched here'))
        self.assertEqual(self.listed(), [(K, 'claude', 'ended_before_seen')])

    def test_the_live_process_of_the_child_says_it_is_there_now(self):
        self.thread(CX, cmds=[(T0 - 3, T0 - 2, 'git status')])
        self.write(K, sdk_lines(T0 + 2, 'run that nobody launched here'))
        self.proc.add(103, 1, ['claude', '-p', 'x'])
        self.session_file(103, K)
        environ(self.proc, 103)
        self.assertEqual(self.listed(), [(K, 'claude', 'no_matching_call')])

    def test_a_launch_that_fits_but_could_not_take_it_is_ambiguous(self):
        self.thread(CX, cmds=[(T0 - 3, T0 - 2, 'cd /w && claude -p --model m "another instruction, said in the command, that this child did not get"')])
        self.write(K, sdk_lines(T0 + 2, 'run that nobody launched here'))
        self.assertEqual(self.listed(), [(K, 'claude', 'ambiguous')])

    def test_a_launch_in_another_folder_or_a_command_far_before_is_not_this_pages_share(self):
        self.thread(CX, cmds=[(T0 - 3, T0 - 2, 'cd /elsewhere && claude -p --model m "x"', '/elsewhere')])
        self.write(K, sdk_lines(T0 + 2, 'run that nobody launched here'))
        self.assertEqual(self.listed(), [])
        self.setUp()
        self.thread(CX, cmds=[(T0 - 3000, T0 - 2999, 'ls')])
        self.write(K, sdk_lines(T0 + 2, 'run that nobody launched here'))
        self.assertEqual(self.listed(), [])

    def test_a_linked_child_and_the_thread_s_own_commands_are_not_listed(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        self.assertEqual(self.listed(), [])

    def test_a_codex_exec_thread_after_a_command_of_a_codex_thread(self):
        self.thread(CX, cmds=[(T0 - 3, T0 - 2, 'cd /w && codex exec -C /w "some instruction that is not what the thread was told at all"')])
        self.thread(CX2, t=T0 + 2, origin='codex_exec', cmds=[(T0 + 3, T0 + 4, 'ls')], user='the words this thread was really told, which nobody said')
        self.assertEqual(self.listed(), [(CX2, 'codex', 'ambiguous')])
        self.assertEqual(self.listed(CX2), [])

    def test_a_claude_session_s_view_is_as_it_was(self):
        self.write(P, [bash_line(T0 - 3, 'ls', tid='toolu_x'), result_line(T0 - 2, 'toolu_x')])
        self.write(K, sdk_lines(T0 + 2, 'run that nobody launched here'))
        self.assertEqual(self.listed(P), [(K, 'claude', 'ended_before_seen')])


class CommandsLikeBash(CodexFixture):
    """C12: the same rules for a Codex command as for a Bash call."""
    def test_a_launch_in_another_folder_is_not_the_parent(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, 'cd /elsewhere && claude -p --model m "%s"' % TEXT, '/elsewhere')])
        self.write(K, child_lines(T0 + 2, text=TEXT, cwd='/w'))
        self.assertNotIn(K, self.judge().cli_owners)

    def test_a_launch_with_another_literal_instruction_is_not_the_parent(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, 'cd /w && claude -p --model m "%s"' % ('Summarise the failing tests of the lagoon module and list every suspect file today please.'))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        self.assertNotIn(K, self.judge().cli_owners)

    def test_words_that_only_travel_are_no_launch_there_either(self):
        for cmd in ("tmux send-keys -t w -l 'claude -p \"%s\"' Enter" % TEXT, "echo 'claude -p \"%s\"'" % TEXT):
            with self.subTest(cmd[:20]):
                self.setUp()
                self.thread(CX, cmds=[(T0 - 1, T0 + 30, cmd)])
                self.write(K, child_lines(T0 + 2, text=TEXT))
                self.assertNotIn(K, self.judge().cli_owners)

    def test_a_script_file_a_command_runs_is_read(self):
        path = os.path.join(self.t, 'run.sh')
        with open(path, 'w') as fh:
            fh.write('#!/bin/bash\nclaude -p --model m "%s"\n' % TEXT)
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, 'bash %s' % path)])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        self.assertEqual(self.judge().cli_owners[K]['sid'], CX)

    def test_the_output_file_of_a_launch_proves_it(self):
        out = os.path.join(self.t, 'out.json')
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, 'claude -p --model m "go there" --output-format json > %s' % out)])
        self.thread(CX2, cmds=[(T0 - 1, T0 + 30, 'claude -p --model m "go there" --output-format json > %s' % os.path.join(self.t, 'other.json'))])
        with open(out, 'w') as fh:
            json.dump({'type': 'result', 'session_id': K}, fh)
        self.write(K, child_lines(T0 + 2, text='go there'))
        o = self.judge().cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain'], o['call']), (CX, 'out', True, 'call-01-0'))

    def test_a_short_instruction_is_a_guess_and_so_is_the_time_of_a_launch_that_reads_it_from_elsewhere(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, 'cd /w && claude -p --model m "go there"')])
        self.write(K, child_lines(T0 + 2, text='go there'))
        o = self.judge().cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (CX, 'content_short', False))        # rank 4: a short instruction equal to the literal argument
        self.setUp()
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, 'cd /w && claude -p --model m "$(cat /nonexistent/p.txt)"')])
        self.write(K, child_lines(T0 + 2, text='something that is not in the command'))
        o = self.judge().cli_owners[K]
        self.assertEqual((o['sid'], o['rule'], o['certain']), (CX, 'time', False))                 # rank 5: one launch in the folder at that time

    def test_the_launch_that_no_child_came_from_is_counted_as_an_orphan(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, 'cd /w && claude -p --model m "%s" &' % TEXT)])
        L = self.judge()
        self.assertEqual([(d['tree'], d['n']) for d in L.diags if d['code'] == 'orphan_launch'], [(CX, 1)])


class Graph(LineageFixture):
    """C14: one graph of who started whom: owner_of, page_of, descendants."""
    def sub_started(self, call='call-spawn-1', agent=SUB, path='/root/s1'):
        self.rollout_file(CX, [line(T0 - 90, 'event_msg', activity('started', call, agent, path))])

    def test_the_native_sub_agents_of_a_codex_thread(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.thread(SUB2, sub_of=SUB, path='/root/s1/s2', cmds=[(T0 - 30, T0 - 29, 'ls')])
        self.sub_started()
        L = self.judge()
        o = L.owner_of(SUB)
        self.assertEqual({k: o[k] for k in ('parent', 'node', 'kind', 'rule', 'certain', 'call', 'call_certain')},
                         {'parent': CX, 'node': None, 'kind': 'sub', 'rule': 'subagent', 'certain': True, 'call': 'call-spawn-1', 'call_certain': True})
        self.assertEqual(L.owner_of(SUB2)['parent'], SUB)                                    # the sub-agent of a sub-agent hangs below it
        self.assertFalse(L.owner_of(SUB2)['call_certain'])                                  # (no spawn event in the parent's record)
        self.assertIsNone(L.owner_of(CX))
        self.assertEqual((L.page_of(SUB2), L.page_of(SUB), L.page_of(CX)), (CX, CX, CX))
        self.assertEqual(L.descendants(CX), [SUB, SUB2])

    def test_two_spawn_events_for_one_thread_make_the_call_unknown(self):
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.sub_started('call-a')
        self.rollout_file(CX, [line(T0 - 80, 'event_msg', activity('started', 'call-b', SUB))])
        o = self.judge().owner_of(SUB)
        self.assertEqual((o['call'], o['call_certain'], o['certain']), (None, False, True))

    def test_a_thread_of_an_unreadable_chain_is_no_vertex(self):
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])                          # the parent has no rollout
        L = self.judge()
        self.assertIsNone(L.owner_of(SUB))
        self.assertEqual(L.descendants(CX), [])

    def test_claude_exec_claude(self):
        """P → codex exec (CX2) → claude -p (K): the page of the grandchild is P's, its parent the exec thread; the exec thread's own sub-agent is below it."""
        self.proc.add(100, 1, ['claude'])
        self.session_file(100, P, entrypoint='cli')
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
        p2 = self.thread(CX2, origin='codex_exec', cmds=[(T0 - 50, T0 - 49, 'ls')], user='fix it')
        self.thread(SUB, sub_of=CX2, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.proc.add(101, 100, ['/bin/bash', '-c', 'codex exec x'])
        self.proc.add(210, 101, ['codex', 'exec', 'x'], fds=[p2])
        self.proc.add(211, 210, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 211)
        L = self.judge()
        self.assertEqual((L.owner_of(CX2)['parent'], L.owner_of(CX2)['kind']), (P, 'cx'))
        self.assertEqual((L.owner_of(K)['parent'], L.owner_of(K)['kind'], L.owner_of(K)['rule'], L.owner_of(K)['certain']), (CX2, 'cli', 'proc', True))
        self.assertEqual((L.page_of(K), L.page_of(SUB), L.page_of(P)), (P, P, P))
        self.assertEqual(L.descendants(P), [CX2, SUB, K])                                     # parents before children
        self.assertEqual(L.descendants(CX2), [SUB, K])

    def test_codex_claude(self):
        self.codex_tui(200, CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201)
        L = self.judge()
        self.assertEqual(L.descendants(CX), [SUB, K])
        self.assertEqual((L.owner_of(K)['parent'], L.owner_of(K)['node']), (CX, None))

    def test_codex_codex(self):
        self.codex_tui(200, CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'codex exec x'])
        p2 = self.thread(CX2, origin='codex_exec', cmds=[(T0 - 40, T0 - 39, 'ls')], user='fix it')
        self.proc.add(220, 201, ['codex', 'exec', 'x'], fds=[p2])
        L = self.judge()
        self.assertEqual((L.owner_of(CX2)['parent'], L.owner_of(CX2)['kind'], L.page_of(CX2)), (CX, 'cx', CX))
        self.assertEqual(L.descendants(CX), [CX2])

    def test_edges_are_read_only_and_versioned_by_a_whole_number(self):
        self.codex_tui(200, CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.thread(SUB, sub_of=CX, cmds=[(T0 - 40, T0 - 39, 'ls')])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201)
        L = self.judge()
        v, edges = L.edges()
        self.assertIsInstance(v, int)
        self.assertEqual({k: (e['parent'], e['kind']) for k, e in edges.items()}, {SUB: (CX, 'sub'), K: (CX, 'cli')})
        with self.assertRaises(TypeError):
            edges['x'] = {}                                                                         # a view: it cannot be changed
        self.assertEqual(L.edges()[0], v)                                                           # asked again with nothing new: the same version
        self.assertIs(L.edges()[1].get(K), edges.get(K))
        self.write(K2, child_lines(T0 + 3, text='go there too'))                                    # a second child: the links are published again, no Codex thread changed
        self.proc.add(104, 201, ['claude', '-p', 'y'])
        self.session_file(104, K2)
        environ(self.proc, 104)
        L = self.judge(T0 + 700)
        v1, edges1 = L.edges()
        self.assertGreater(v1, v)
        self.assertIn(K2, edges1)
        v = v1
        self.thread(CX2, origin='codex_exec', cmds=[(T0 - 30, T0 - 29, 'ls')], user='fix it')       # a new thread: the graph is made again
        self.proc.add(220, 201, ['codex', 'exec', 'x'], fds=[os.path.join(self.codex, 'sessions', '2026', '10', '04', 'rollout-2026-10-04T00-00-00-%s.jsonl' % CX2)])
        L = self.judge(T0 + 800)
        v2, edges2 = L.edges()
        self.assertGreater(v2, v)
        self.assertIn(CX2, edges2)

    def test_a_circle_and_a_long_chain(self):
        L = server.LinkIndex()
        L.cli_owners = {'a': {'sid': 'b', 'node': None, 'rule': 'time', 'certain': False}, 'b': {'sid': 'c', 'node': None, 'rule': 'time', 'certain': False},
                        'c': {'sid': 'a', 'node': None, 'rule': 'time', 'certain': False}}
        self.assertEqual((L.page_of('a'), L.page_of('b')), ('a', 'b'))                       # the links go round: the id itself
        self.assertEqual(L.descendants('a'), ['c', 'b'])                                      # each once, the circle cut
        ids = ['n%d' % i for i in range(14)]
        L = server.LinkIndex()
        L.cli_owners = {ids[i]: {'sid': ids[i + 1], 'node': None, 'rule': 'proc', 'certain': True} for i in range(13)}
        self.assertEqual(L.page_of('n0'), 'n8')                                               # at most 8 links up
        self.assertEqual(L.descendants('n13'), ['n12', 'n11', 'n10', 'n9', 'n8', 'n7', 'n6', 'n5'])
        self.assertEqual(L.page_of('n13'), 'n13')

    def test_a_reader_sees_one_graph_even_when_the_links_are_published_meanwhile(self):
        """The version, the edges and the children come from one graph taken under the lock: a publication between the reader's two steps is not seen by half."""
        one = {'a': {'sid': 'p', 'node': None, 'rule': 'proc', 'certain': True}}
        L = server.LinkIndex()
        L.cli_owners = dict(one, b={'sid': 'a', 'node': None, 'rule': 'proc', 'certain': True})
        real, swap = L._graph, []

        def graph():
            got = real()
            if swap:
                swap.clear()
                L.cli_owners = {}                                                                 # the links are published again, empty, and another reader makes the graph again
                real()
            return got
        L._graph = graph
        swap.append(1)
        self.assertEqual(L.descendants('p'), ['a', 'b'])                                          # the children of the graph that was asked for, whole
        L._graph = real
        L.cli_owners = dict(one)
        now = real()[0]
        L._graph = graph
        swap.append(1)
        ver, edges = L.edges()
        self.assertEqual(ver, now)                                                                # the version is the one of the edges that come with it
        self.assertEqual(set(edges), {'a'})
        L._graph = real
        self.assertGreater(L.edges()[0], ver)                                                     # (the graph made again meanwhile has the next one)

    def test_the_graph_does_not_go_by_the_address_of_a_dict(self):
        """A dict of links put in place by hand is seen by the object, not by `id()`: the address of a dict that was freed is given to the next one (on Python 3.12 it happens at once),
        and a key made of addresses then says nothing changed. With `id()` made to answer the same for every dict, the graph still follows the dict that is there."""
        one = {'a': {'sid': 'p', 'node': None, 'rule': 'proc', 'certain': True}}
        two = {'b': {'sid': 'q', 'node': None, 'rule': 'proc', 'certain': True}}
        L = server.LinkIndex()
        with mock.patch.object(link, 'id', lambda x: 1, create=True):
            L.cli_owners = one
            self.assertEqual(set(L.edges()[1]), {'a'})
            L.cli_owners = two
            self.assertEqual(set(L.edges()[1]), {'b'})
            L.owners = {'t': {'sid': 'q', 'rule': 'prompt'}}
            self.assertEqual(set(L.edges()[1]), {'b', 't'})
            L.owners = {'u': {'sid': 'q', 'rule': 'prompt'}}
            self.assertEqual(set(L.edges()[1]), {'b', 'u'})

    def test_a_dict_that_took_the_address_of_a_freed_one_is_seen(self):
        """The same without the fake: dicts of the same size put in place one after the other, each given to the graph before the next one is made (the freed one's address is free again)."""
        L = server.LinkIndex()
        link_of = {'sid': 'p', 'node': None, 'rule': 'proc', 'certain': True}
        d = None
        for i in range(300):
            L.cli_owners = None
            d = None                                                                               # the dict the graph was last made from is freed (unless the graph keeps it: the fix)
            d = {'k%d' % i: link_of}                                                               # the next one of the same size takes its place in memory
            L.cli_owners = d
            self.assertEqual(set(L.edges()[1]), {'k%d' % i}, i)

    def test_owner_of_gives_a_copy_and_nothing_for_an_unknown_id(self):
        self.codex_tui(200, CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.proc.add(201, 200, ['/bin/bash', '-lc', 'claude -p x'])
        self.claude_child(103, 201)
        L = self.judge()
        o = L.owner_of(K)
        o['parent'] = 'x'
        self.assertEqual(L.owner_of(K)['parent'], CX)
        self.assertIsNone(L.owner_of('nope'))
        self.assertEqual((L.page_of('nope'), L.descendants('nope')), ('nope', []))


class CacheOfCodexParents(LineageFixture):
    def test_a_codex_parent_is_read_back_and_dropped_when_its_record_is_gone(self):
        path = os.path.join(self.t, 'xdg', 'agent-bullpen', 'links.json')
        os.makedirs(os.path.dirname(path), mode=0o700)
        os.chmod(os.path.dirname(path), 0o700)
        rows = [{'child': K, 'parent': CX, 'kind': 'cli', 'rule': 'env', 'seen': time.time() - 10, 'started': T0, 'parent_kind': 'codex'},
                {'child': K2, 'parent': CX3, 'kind': 'cli', 'rule': 'env', 'seen': time.time() - 10, 'started': T0, 'parent_kind': 'codex'},
                {'child': Q, 'parent': P, 'kind': 'cli', 'rule': 'proc', 'seen': time.time() - 10, 'started': T0}]                    # no parent_kind: a Claude parent
        with open(path, 'w') as fh:
            json.dump({'version': 5, 'links': rows}, fh)
        os.chmod(path, 0o600)
        self.thread(CX, cmds=[(T0 - 50, T0 - 49, 'ls')])
        self.write(K, child_lines(T0 + 2, text='go'))
        self.write(K2, child_lines(T0 + 2, text='go'))
        self.write(Q, child_lines(T0 + 2, text='go'))
        self.write(P, [dump({'type': 'user', 'timestamp': link_iso(T0 - 100), 'cwd': '/w', 'message': {'role': 'user', 'content': 'go'}})])
        self.links.lineage.enable_cache(path)
        self.assertEqual({c: v['pk'] for c, v in self.links.lineage.cli.items()}, {K: 'codex', K2: 'codex', Q: 'claude'})
        L = self.judge()
        self.assertEqual({c: (o['sid'], o['rule'], o['parent_kind']) for c, o in L.cli_owners.items()}, {K: (CX, 'file', 'codex'), Q: (P, 'file', 'claude')})
        self.assertNotIn(K2, L.lineage.cli)                                                  # its Codex parent has no rollout: dropped, as a Claude parent without a record is

    def test_a_row_with_a_parent_kind_it_does_not_know_is_not_read(self):
        path = os.path.join(self.t, 'xdg', 'agent-bullpen', 'links.json')
        os.makedirs(os.path.dirname(path), mode=0o700)
        os.chmod(os.path.dirname(path), 0o700)
        with open(path, 'w') as fh:
            json.dump({'version': 5, 'links': [{'child': K, 'parent': CX, 'kind': 'cli', 'rule': 'env', 'seen': time.time() - 10, 'started': T0, 'parent_kind': 'gemini'}]}, fh)
        os.chmod(path, 0o600)
        self.assertEqual(lineage.read_cache(path), [])

    def test_a_remembered_content_link_of_a_codex_launcher_keeps_whose_parent_it_is(self):
        self.thread(CX, cmds=[(T0 - 1, T0 + 30, self.claude_run(TEXT))])
        self.write(K, child_lines(T0 + 2, text=TEXT))
        path = os.path.join(self.t, 'xdg', 'agent-bullpen', 'links.json')
        self.links.lineage.enable_cache(path)
        self.judge()
        with open(path) as fh:
            rows = json.load(fh)['links']
        self.assertEqual([(r['child'], r['parent'], r['rule'], r['parent_kind']) for r in rows], [(K, CX, 'content', 'codex')])


if __name__ == '__main__':
    unittest.main()
