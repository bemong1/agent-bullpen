"""The report a running `codex exec` was told to write (its `-o FILE`), read from its own command line while the record of the command that started it does not exist yet (it is
written when the command ends). It plans the seat of a child that is already linked by its environment or process; it links nothing, it is never saved, and the record
wins when it comes.

    python3 -m unittest tests.test_exec_out
"""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402
import test_proclink as TP  # noqa: E402
from test_codex_page import INSTRUCTION, FakeSession, codex_call, exec_agent, line as TP_line  # noqa: E402  (a function and two plain classes: no TestCase is imported twice)

from board import lineage  # noqa: E402
from board.codex_parse import cx_decode  # noqa: E402


class ExecOut(unittest.TestCase):
    def out(self, *argv):
        return lineage.exec_out([a.encode() for a in argv])

    def test_the_forms_of_the_option(self):
        self.assertEqual(self.out('codex', 'exec', '-o', '/w/talk/r1/B.md', 'do it'), '/w/talk/r1/B.md')
        self.assertEqual(self.out('codex', 'exec', '--output-last-message', '/w/B.md', 'do it'), '/w/B.md')
        self.assertEqual(self.out('codex', 'exec', '--output-last-message=/w/B.md', 'do it'), '/w/B.md')
        self.assertEqual(self.out('/usr/bin/codex', 'exec', '-m', 'gpt-6.1-sol', '-C', '/w', '-o', '/w/x/../B.md', '-'), '/w/B.md')            # normalised
        self.assertEqual(self.out('node', '/opt/codex/bin/codex.js', 'exec', '-o', '/w/B.md', 'do it'), '/w/B.md')                              # a launcher in front
        self.assertEqual(self.out('codex', 'exec', 'resume', 'abc', '-o', '/w/B.md', 'more'), '/w/B.md')

    def test_what_gives_nothing(self):
        self.assertIsNone(self.out('codex', 'exec', '-o', 'r1/B.md', 'do it'))                                  # a relative path needs the folder of the process, which is not read
        self.assertIsNone(self.out('codex', 'exec', '-o', '/w/A.md', '-o', '/w/B.md', 'do it'))                 # two: which one is the report
        self.assertIsNone(self.out('codex', 'exec', '-o', '/w/A.md', '--output-last-message=/w/A.md', 'x'))      # even the same twice
        self.assertIsNone(self.out('codex', 'exec', 'do it', '--', '-o', '/w/B.md'))                            # after `--` it is a word of the instruction
        self.assertIsNone(self.out('codex', 'exec', '-c', '-o', '/w/B.md'))                                     # `-o` as the value of another option is that option's value
        self.assertIsNone(self.out('codex', '-o', '/w/B.md', 'exec', 'x'))                                      # not at the front: it is not `codex exec`
        self.assertIsNone(self.out('codex', 'resume', '-o', '/w/B.md'))
        self.assertIsNone(self.out('codex', 'exec', 'do it', '-o'))                                             # dangling
        self.assertIsNone(self.out('claude', '-p', '-o', '/w/B.md'))
        self.assertIsNone(self.out('codex'))
        self.assertIsNone(self.out('codex', 'exec', '-o', '/w/' + 'x' * 5000, 'do it'))
        self.assertIsNone(self.out('codex', 'exec', '-o', '/w/a\0b', 'do it'))

    def test_bytes_that_are_not_utf8_give_nothing(self):
        self.assertIsNone(lineage.exec_out([b'codex', b'exec', b'-o', b'/a/\xff.md', b'']))           # a path that cannot be written as text would make the page's answer fail
        self.assertEqual(lineage.exec_out([b'codex', b'exec', b'-o', '/a/\u00e9.md'.encode(), b'']), '/a/\u00e9.md')


class LiveOutOfTheProcesses(TP.Fixture):
    """Lineage.live_out: only for a thread of `codex exec` whose rollout a live process has open (Linux /proc), only when the processes agree, in memory only."""

    mine = ()

    def processes(self, *argvs, tid=TP.TID):
        path = self.rollout(tid, origin='codex_exec')
        for pid in list(self.mine):                                                        # the processes of an earlier look of this test are gone
            self.proc.remove(pid)
        self.mine = []
        for i, argv in enumerate(argvs):
            self.codex_proc(300 + i, 1, path, argv)
            self.mine.append(300 + i)
        return self.scan().lineage.live_out

    def test_a_live_exec_process_gives_its_output_path(self):
        self.assertEqual(self.processes(['codex', 'exec', '-o', '/w/talk/r1/B.md', 'do it']), {TP.TID: '/w/talk/r1/B.md'})

    def test_no_path_no_entry(self):
        self.assertEqual(self.processes(['codex', 'exec', 'do it']), {})
        self.assertEqual(self.processes(['codex', 'exec', '-o', 'r1/B.md', 'do it'], tid=TP.TID2), {})

    def test_two_processes_with_the_rollout_open_agree_or_there_is_none(self):
        self.assertEqual(self.processes(['codex', 'exec', '-o', '/w/B.md', 'x'], ['codex', 'exec', '-o', '/w/B.md', 'x']), {TP.TID: '/w/B.md'})
        self.assertEqual(self.processes(['codex', 'exec', '-o', '/w/A.md', 'x'], ['codex', 'exec', '-o', '/w/B.md', 'x']), {})
        self.assertEqual(self.processes(['codex', 'exec', '-o', '/w/A.md', 'x'], ['codex', 'exec', 'x']), {})

    def test_a_thread_that_is_no_exec_thread_gives_none(self):
        path = self.rollout(TP.TID, origin='codex-tui')
        self.codex_proc(310, 1, path, ['codex', 'exec', '-o', '/w/B.md', 'x'])
        self.assertEqual(self.scan().lineage.live_out, {})

    def test_processes_that_disagree_are_a_conflict_not_an_absence(self):
        self.assertEqual(self.processes(['codex', 'exec', '-o', '/w/A.md', 'x'], ['codex', 'exec', '-o', '/w/B.md', 'x']), {})
        self.assertEqual(self.links.lineage.live_out_held, {TP.TID})
        self.assertEqual(self.processes(['codex', 'exec', '-o', '/w/A.md', 'x'], ['codex', 'exec', 'x']), {})                # one of them says nothing: the same
        self.assertEqual(self.links.lineage.live_out_held, {TP.TID})
        self.assertEqual(self.processes(['codex', 'exec', '-o', '/w/B.md', 'x'], ['codex', 'exec', '-o', '/w/B.md', 'x']), {TP.TID: '/w/B.md'})
        self.assertEqual(self.links.lineage.live_out_held, set())
        self.assertEqual(self.processes(['codex', 'exec', 'x']), {})                                                          # no `-o` at all is no conflict
        self.assertEqual(self.links.lineage.live_out_held, set())
        self.assertEqual(self.processes(), {})                                                                                # no process: nothing is held
        self.assertEqual(self.links.lineage.live_out_held, set())

    def test_it_is_not_in_the_saved_links(self):
        folder = os.path.join(self.t, 'cache')
        os.makedirs(folder)
        cache = os.path.join(folder, 'links.json')
        self.links.lineage.enable_cache(cache)
        self.assertEqual(self.processes(['codex', 'exec', '-o', '/w/secret-name/B.md', 'do it']), {TP.TID: '/w/secret-name/B.md'})
        if os.path.exists(cache):
            with open(cache) as f:
                self.assertNotIn('secret-name', f.read())

    def test_it_goes_when_the_process_goes(self):
        path = self.rollout(TP.TID, origin='codex_exec')
        self.codex_proc(320, 1, path, ['codex', 'exec', '-o', '/w/B.md', 'x'])
        self.assertEqual(self.scan().lineage.live_out, {TP.TID: '/w/B.md'})
        self.proc.remove(320)
        self.assertEqual(self.scan().lineage.live_out, {})


class PlannedFromTheCommandLine(unittest.TestCase):
    """What CodexLinker does with it: a seat plan for an open turn of a thread linked by its environment or process, never the way a link is made."""
    PATH = '/w/talk/r1/B.md'

    def linker(self, rule='env', live=PATH, ended=False, calls=(), held=False):
        lk = server.CodexLinker(FakeSession())
        for c in calls:
            lk.calls[c['id']] = c
            lk.order.append(c)
        a = exec_agent(link={'sid': 'R', 'node': None, 'call': 'call-x', 'rule': rule}, ended=ended)
        lk.agents['X'] = lk.s.agents['X'] = a
        for name, value in (('live_out', {'X': live} if live else {}), ('live_out_held', {'X'} if held else set())):
            patch = mock.patch.object(server.LINKS.lineage, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        return lk, a

    def processes(self, live=None, held=False):
        """The next look at the processes: what Lineage now says of the thread."""
        for name, value in (('live_out', {'X': live} if live else {}), ('live_out_held', {'X'} if held else set())):
            setattr(server.LINKS.lineage, name, value)

    def paths(self, a):
        return [(o['path'], o.get('src')) for o in a.out_paths]

    def test_an_open_turn_of_a_thread_linked_by_environment_or_process_is_planned_from_the_command_line(self):
        for rule in ('env', 'proc'):
            lk, a = self.linker(rule)
            lk._derive(a)
            self.assertEqual((a.turns[0]['out'], a.turns[0]['out_state'], self.paths(a), a.report_tag), (self.PATH, 'planned', [(self.PATH, 'argv')], 'B'), rule)
            self.assertIsNone(a.turns[0]['call'])                                           # no call: nothing is linked by this
            self.assertEqual(next(e for e in lk.s.feed if e['kind'] == 'spawn')['title'], 'B.md')

    def test_a_link_that_is_not_the_environment_or_the_process_does_not_take_it(self):
        for rule in ('prompt', 'content', 'time', 'session', 'id', 'file', 'subagent'):
            lk, a = self.linker(rule)
            lk._derive(a)
            self.assertEqual((a.turns[0]['out'], a.out_paths, a.report_tag), (None, [], ''), rule)

    def test_a_turn_that_has_ended_or_has_a_call_is_left_alone(self):
        lk, a = self.linker(ended=True)
        lk._derive(a)
        self.assertEqual((a.turns[0]['out'], a.out_paths), (None, []))
        lk, a = self.linker()
        lk.calls['call-x'] = c = codex_call('call-x', 'R', None, out='/w/talk/r1/B.md')
        lk.order.append(c)
        lk._derive(a)
        self.assertEqual(self.paths(a), [(self.PATH, None)])                                # the record has it: no entry that says it came from the command line

    def test_the_record_that_comes_later_wins(self):
        lk, a = self.linker()
        lk._derive(a)
        self.assertEqual(self.paths(a), [(self.PATH, 'argv')])
        lk.calls['call-x'] = c = codex_call('call-x', 'R', None, out='/w/talk/r1/B.md')       # the same path: the entry stays, as the record's
        lk.order.append(c)
        lk._derive(a)
        self.assertEqual((self.paths(a), a.report_tag, a.turns[0]['call']), ([(self.PATH, None)], 'B', 'call-x'))
        self.assertEqual(len([e for e in lk.s.feed if e['kind'] == 'spawn']), 1)

    def test_a_different_path_in_the_record_replaces_the_one_of_the_command_line_and_the_name(self):
        lk, a = self.linker()
        lk._derive(a)
        lk.calls['call-x'] = c = codex_call('call-x', 'R', None, out='/w/talk/r1/C.md')
        lk.order.append(c)
        lk._derive(a)
        self.assertEqual((self.paths(a), a.report_tag, a.turns[0]['out']), ([('/w/talk/r1/C.md', None)], 'C', '/w/talk/r1/C.md'))
        self.assertEqual(next(e for e in lk.s.feed if e['kind'] == 'spawn')['title'], 'C.md')

    def test_a_conflict_that_comes_later_takes_the_seat_back(self):
        lk, a = self.linker()
        lk._derive(a)
        self.assertEqual((self.paths(a), a.report_tag), ([(self.PATH, 'argv')], 'B'))
        self.assertEqual(next(e for e in lk.s.feed if e['kind'] == 'spawn')['title'], 'B.md')
        self.processes(live=None, held=True)                                               # a second process with the transcript open says another file
        lk._derive(a)
        self.assertEqual((self.paths(a), a.report_tag, a.turns[0]['out'], a.turns[0]['out_state'], a.turns[0].get('out_src')), ([], '', None, None, None))
        self.assertTrue(next(e for e in lk.s.feed if e['kind'] == 'spawn')['title'].startswith('Review the currency'))     # the card is what it was before the plan
        self.processes(live=self.PATH)                                                     # they agree again: the plan comes back
        lk._derive(a)
        self.assertEqual((self.paths(a), a.report_tag), ([(self.PATH, 'argv')], 'B'))

    def test_a_process_that_is_gone_leaves_the_plan_as_it_was(self):
        lk, a = self.linker()
        lk._derive(a)
        self.processes(live=None, held=False)                                              # the command record never came and the process ended: the seat stays
        lk._derive(a)
        self.assertEqual((self.paths(a), a.report_tag), ([(self.PATH, 'argv')], 'B'))

    def test_another_single_process_with_another_file_replaces_the_plan(self):
        lk, a = self.linker()
        lk._derive(a)
        self.processes(live='/w/talk/r1/C.md')
        lk._derive(a)
        self.assertEqual((self.paths(a), a.report_tag, a.turns[0]['out']), ([('/w/talk/r1/C.md', 'argv')], 'C', '/w/talk/r1/C.md'))

    def test_a_conflict_does_not_touch_what_the_record_said(self):
        lk, a = self.linker()
        lk.calls['call-x'] = c = codex_call('call-x', 'R', None, out='/w/talk/r1/C.md')
        lk.order.append(c)
        lk._derive(a)
        self.processes(live=None, held=True)
        lk._derive(a)
        self.assertEqual((self.paths(a), a.report_tag), ([('/w/talk/r1/C.md', None)], 'C'))

    def test_the_report_is_written_once_when_the_record_comes_after_the_end_of_the_turn(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            report = os.path.join(d, 'talk', 'r1', 'B.md')
            os.makedirs(os.path.dirname(report))
            with open(report, 'w') as f:
                f.write('done')                                                           # what the last message of the exec_agent is
            lk, a = self.linker(live=report)
            lk._derive(a)
            a.feed_cx(cx_decode(TP_line(1030.0, 'event_msg', {'type': 'task_complete', 'last_agent_message': 'done'})))
            lk._derive(a)
            self.assertEqual((a.turns[0]['out_state'], len(a.writes)), ('confirmed', 1))
            lk.calls['call-x'] = c = codex_call('call-x', 'R', None, out=report)           # the record of the command comes a poll later
            lk.order.append(c)
            lk._derive(a)
            lk._derive(a)
            self.assertEqual(([w['path'] for w in a.writes], a.turns[0]['out_state']), ([report], 'confirmed'))

    def test_a_record_that_cannot_work_its_path_out_leaves_the_command_lines(self):
        lk, a = self.linker()
        lk._derive(a)
        lk.calls['call-x'] = c = codex_call('call-x', 'R', None, out='$R/B.md')                # a variable nobody gave a value: the shell's own value is what the process has
        lk.order.append(c)
        lk._derive(a)
        self.assertEqual(self.paths(a)[0], (self.PATH, 'argv'))
        self.assertEqual((a.report_tag, a.turns[0]['out']), ('B', self.PATH))


if __name__ == '__main__':
    unittest.main()
