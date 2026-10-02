"""Debate membership is decided by the debate folder, not by how a report path is spelled.

A debate folder is a real_unit() (it has brief.md or an exact round folder r1, r01, round1). An agent belongs to one when its instructions name the
folder's brief.md or a report path in it, it read that brief.md, or it wrote inside it. A seat (a cell) needs more than that: a report
path it is told to write, else a seat marker in the instruction, else a report write that succeeded; the description tag only together with such a write.
Agents that only read the folder, quote a path as an example, or are told not to write it get no seat.

    python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402

from board import debates  # noqa: E402

BRIEF = ('# review — branch policy\n\n'
         '| topic | folder | deps | final |\n|---|---|---|---|\n| Branch policy | `review_branch_policy/` | — | |\n\n'
         '**A — development flow**\n**B — gate design**\n**C — GitHub operations**\n')
REL = 'docs/records/review_branch_policy'


def write(path, text='x\n'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(text)
    return path


def prompt(letter, name, out=True, round_no=1, extra=''):
    """The kind of instruction that was missed: marker, folder-prefixed brief and report paths, a seat letter."""
    return ('[REVIEW-%s] 너는 review 토론의 %s(%s) 담당이다. 저장소 루트에서 `%s/brief.md`를 읽고 그대로 따른다. ' % (letter, letter, name, REL) +
            ('%d라운드 결과를 `%s/r%d/%s_%s.md`에 쓴다. ' % (round_no, REL, round_no, letter, name) if out else '') + extra)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(os.path.realpath(self.tmp.name), 'repo')
        self.unit = os.path.join(self.repo, *REL.split('/'))
        write(os.path.join(self.unit, 'brief.md'), BRIEF)
        os.makedirs(os.path.join(self.unit, 'r1'))                      # still empty: nobody has written yet
        os.makedirs(os.path.join(self.unit, 'r2'))
        self.agents = {}
        self.t = 1000.0

    def claude(self, aid, text, cwd=None, tag_description=None, origin='subagent'):
        a = server.Agent(aid, {'description': tag_description} if tag_description else {})
        a.origin, a.spawn_prompt, a.cwd = origin, text, self.repo if cwd is None else cwd
        self.t += 1
        a.spawn_ts = a.first_ts = self.t
        self.agents[aid] = a
        return a

    def codex(self, n, text, cwd=None):
        e = {'id': '019a%04d-0000-7000-8000-000000000000' % n, 'meta_ts': 1000.0, 'path': '/nonexistent/rollout-%d.jsonl' % n,
             'cwd': self.repo if cwd is None else cwd, 'model': 'gpt-6.1-sol'}
        a = server.CodexAgent(e, {})
        a.spawn_prompt = text
        self.t += 1
        a.spawn_ts = a.first_ts = self.t
        self.agents[a.id] = a
        return a

    def run_debates(self, status='running'):
        s = types.SimpleNamespace(agents=self.agents, _file_cache={}, _head_cache={})
        return debates.debates(s, {aid: status for aid in self.agents})

    def rows(self, out):
        self.assertEqual(len(out[0]), 1, [d['root'] for d in out[0]])
        d = out[0][0]
        self.assertEqual(d['root'], self.unit)
        return {r['p']: r for r in d['topics'][0]['rows']}


class ReportedCase(Fixture):
    """review: Claude claude -p (A) and two Codex exec (B, C), cwd = repo root, no report written yet, names A_flow / C_github."""

    def setUp(self):
        super().setUp()
        self.a = self.claude('9142d20a-0000-4000-8000-000000000000', prompt('A', 'flow'), origin='cli')
        self.b = self.codex(65, prompt('B', 'gate'))
        self.c = self.codex(66, prompt('C', 'github'))
        self.c.reads[os.path.join(self.unit, 'brief.md')] = 1010.0       # Codex read the brief (command parsing)

    def test_all_three_sit_in_the_room_before_any_file_exists(self):
        out = self.run_debates()
        rows = self.rows(out)
        self.assertEqual(sorted(rows), ['A_flow', 'B_gate', 'C_github'])    # file names as the row names, no stray A/B/C rows
        for stem, agent in (('A_flow', self.a), ('B_gate', self.b), ('C_github', self.c)):
            cell = rows[stem]['cells'][0]
            self.assertEqual((cell['round'], cell['agent'], cell['state']), (1, agent.id, 'writing'), stem)
            self.assertEqual(out[1][agent.id], {self.unit})                  # shown in the room, not as "other work"

    def test_roles_follow_the_leading_letter(self):
        rows = self.rows(self.run_debates())
        self.assertEqual([rows[s]['role'] for s in ('A_flow', 'B_gate', 'C_github')], ['development flow', 'gate design', 'GitHub operations'])

    def test_cells_follow_the_files(self):
        write(os.path.join(self.unit, 'r1', 'A_flow.md'), 'a\nb\n')
        rows = self.rows(self.run_debates(status='done'))
        self.assertEqual((rows['A_flow']['cells'][0]['state'], rows['A_flow']['cells'][0]['lines']), ('done', 2))
        self.assertEqual(rows['B_gate']['cells'][0]['state'], 'missing')    # its agent finished but wrote nothing

    def test_claude_and_codex_mix_in_one_debate(self):
        out = self.run_debates()
        self.assertEqual({a.provider for a in (self.a, self.b, self.c)}, {'claude', 'codex'})
        self.assertEqual(len(out[0]), 1)
        self.assertEqual(sum(len(r['agents']) for r in out[0][0]['topics'][0]['rows']), 3)

    def test_cwd_can_come_from_the_link_entry(self):
        self.a.cwd = ''
        self.a.cli = {'cwd': self.repo}                                      # claude -p child: the link knows the folder
        self.assertEqual(debates.agent_cwd(self.a), self.repo)
        self.assertEqual(self.rows(self.run_debates())['A_flow']['cells'][0]['agent'], self.a.id)

    def test_without_a_cwd_nothing_is_guessed(self):
        self.a.cwd = ''
        self.a.cli = None
        out = self.run_debates()
        self.assertNotIn(self.a.id, out[1])                                  # a folder-prefixed path cannot be resolved without a cwd
        self.assertEqual(sorted(self.rows(out)), ['A', 'B_gate', 'C_github'])    # B and C still sit; A is only the role row of the brief


class FolderPrefixedPaths(Fixture):
    def test_prefix_is_resolved_against_the_agent_cwd(self):
        a = self.claude('a0000000000000001', '`records/review_branch_policy/brief.md`를 읽는다. `records/review_branch_policy/r1/A_flow.md`에 쓴다.',
                        cwd=os.path.join(self.repo, 'docs'))
        rows = self.rows(self.run_debates())
        self.assertEqual(rows['A_flow']['cells'][0]['agent'], a.id)

    def test_dot_segments(self):
        a = self.claude('a0000000000000002', '`../../review_branch_policy/r1/A_flow.md`에 쓴다', cwd=os.path.join(self.unit, 'r2'))
        rows = self.rows(self.run_debates())
        self.assertEqual(rows['A_flow']['cells'][0]['agent'], a.id)

    def test_absolute_and_tilde_still_work(self):
        a = self.claude('a0000000000000003', '결과를 `%s/r1/A.md`에 쓴다' % self.unit, cwd='/nowhere')
        rows = self.rows(self.run_debates())
        self.assertEqual(rows['A']['cells'][0]['agent'], a.id)

    def test_short_relative_path_from_inside_the_debate_folder(self):
        c = self.codex(7, '결과를 r1/B.md에 쓴다', cwd=self.unit)              # existing case: cwd is the debate folder
        rows = self.rows(self.run_debates())
        self.assertEqual(rows['B']['cells'][0]['agent'], c.id)

    def test_claude_tag_with_absolute_path_is_unchanged(self):
        a = self.claude('a0000000000000004', 'write the result to `%s/r1/A.md`' % self.unit, tag_description='A 조사', cwd='/nowhere')
        rows = self.rows(self.run_debates())
        self.assertEqual(rows['A']['cells'][0]['agent'], a.id)

    def test_tag_with_a_report_it_only_reads_is_a_reader_not_a_seat(self):
        a = self.claude('a0000000000000014', 'read `%s/r1/A.md`' % self.unit, tag_description='A 조사', cwd='/nowhere')
        out = self.run_debates()
        self.assertIsNone(self.rows(out)['A']['cells'][0]['agent'])
        self.assertEqual(out[1][a.id], {self.unit})                          # it works in the debate, it just holds no seat


class NameAliases(Fixture):
    def test_unique_alias_merges_role_and_letter(self):
        self.claude('a0000000000000005', prompt('A', 'flow'))
        rows = self.rows(self.run_debates())
        self.assertIn('A_flow', rows)
        self.assertNotIn('A', rows)                                          # the bare role row is the same participant
        self.assertEqual(rows['A_flow']['role'], 'development flow')

    def test_bare_and_named_files_stay_separate(self):
        write(os.path.join(self.unit, 'r1', 'A.md'), 'old\n')
        write(os.path.join(self.unit, 'r1', 'A_flow.md'), 'new\n')
        self.claude('a0000000000000010', 'look at `%s/r1/A.md`' % self.unit, cwd='/nowhere')      # something has to name the debate folder
        rows = self.rows(self.run_debates(status='done'))
        self.assertIn('A', rows)
        self.assertIn('A_flow', rows)
        self.assertEqual(rows['A']['role'], 'development flow')              # the bare letter keeps the role
        self.assertEqual(rows['A_flow']['role'], '')

    def test_two_named_files_of_one_letter_stay_separate(self):
        write(os.path.join(self.unit, 'r1', 'A_flow.md'), '1\n')
        write(os.path.join(self.unit, 'r2', 'A_other.md'), '2\n')
        self.claude('a0000000000000011', 'look at `%s/r1/A_flow.md`' % self.unit, cwd='/nowhere')
        rows = self.rows(self.run_debates(status='done'))
        self.assertEqual(sorted(p for p in rows if p.startswith('A')), ['A', 'A_flow', 'A_other'])

    def test_stem_helpers(self):
        self.assertEqual([debates.stem_letter(x) for x in ('A', 'A_flow', 'A-flow', 'a_x', 'AB', 'Alpha', 'A_', 'review_A')], ['A', 'A', 'A', '', '', '', '', ''])
        self.assertEqual(debates.stem_aliases({'A_flow', 'B', 'C_x', 'C'}), {'A': 'A_flow'})
        self.assertEqual(debates.stem_aliases({'A_flow', 'A_other'}), {})


class SeatsWithoutAPath(Fixture):
    def test_marker_gives_the_seat_and_the_alias_gives_the_stem(self):
        self.claude('a0000000000000006', prompt('A', 'flow'))               # tells where A_flow goes
        c = self.claude('a0000000000000007', prompt('C', 'github', out=False))   # marker, brief named, no output path
        rows = self.rows(self.run_debates())
        self.assertEqual(rows['C']['cells'][0]['agent'], c.id)               # the letter itself when no C_ file is known

    def test_marker_uses_the_known_alias(self):
        write(os.path.join(self.unit, 'r1', 'C_github.md'), 'x\n')
        c = self.claude('a0000000000000008', prompt('C', 'github', out=False))
        rows = self.rows(self.run_debates())
        self.assertEqual(rows['C_github']['cells'][0]['agent'], c.id)
        self.assertNotIn('C', rows)

    def test_marker_must_open_the_spawn_prompt(self):
        # a later message, or a long text that only quotes marker examples, is not a role declaration
        quoting = ('Please document how seats are found. Examples of instructions: ' + 'x' * 400 +
                   ' "[REVIEW-C] 너는 review 토론의 C(GitHub) 담당이다." See `%s/brief.md`.' % REL)
        a = self.claude('a0000000000000012', quoting)
        b = self.claude('a0000000000000013', 'continue the work. `%s/brief.md`' % REL)
        b.received.append({'ts': 5.0, 'text': prompt('C', 'github', out=False)})
        c = self.claude('a0000000000000014', prompt('A', 'flow'))
        out = self.run_debates()
        self.assertNotIn(a.id, out[1])
        self.assertNotIn(b.id, out[1])
        self.assertIn(c.id, out[1])

    def test_round_comes_from_the_instruction(self):
        c = self.claude('a0000000000000009', prompt('B', 'gate', out=False, extra='이번에는 2라운드다.'))
        rows = self.rows(self.run_debates())
        cell = [x for x in rows['B']['cells'] if x['agent'] == c.id]
        self.assertEqual([x['round'] for x in cell], [2])

    def test_description_tag_alone_seats_nobody(self):
        # a tag with an instruction that only names the folder is no seat; it needs a write it is told to make or made
        a = self.claude('a000000000000000a', '`%s/brief.md`를 읽고 조사한다' % REL, tag_description='B 조사')
        self.claude('a0000000000000017', prompt('A', 'flow'))                # something has to list the debate
        out = self.run_debates()
        self.assertIsNone(self.rows(out)['B']['cells'][0]['agent'])
        self.assertNotIn(a.id, out[1])                                       # a guide it merely names is not a place it works in

    def test_tag_with_only_a_read_gets_no_seat(self):
        a = self.claude('a000000000000000b', 'check things', tag_description='B 조사')
        a.reads[os.path.join(self.unit, 'brief.md')] = 1.0
        self.claude('a000000000000000c', prompt('A', 'flow'))
        out = self.run_debates()
        self.assertNotIn(a.id, out[1])

    def test_tag_with_a_write_inside_the_folder(self):
        a = self.claude('a000000000000000d', 'work on it', tag_description='B 조사')
        a.writes.append({'ts': 1.0, 'path': write(os.path.join(self.unit, 'notes.txt'))})
        self.claude('a000000000000000e', prompt('A', 'flow'))
        rows = self.rows(self.run_debates())
        self.assertEqual(rows['B']['cells'][0]['agent'], a.id)

    def test_tag_with_a_write_that_failed_or_left_no_file_gets_no_seat(self):
        a = self.claude('a000000000000000f', 'work on it', tag_description='B 조사')
        a.writes.append({'ts': 1.0, 'path': write(os.path.join(self.unit, 'notes.txt')), 'ok': False})      # the tool result said it failed
        b = self.claude('a0000000000000015', 'work on it', tag_description='C 조사')
        b.writes.append({'ts': 1.0, 'path': os.path.join(self.unit, 'gone.txt')})                              # result unknown and nothing on disk
        self.claude('a0000000000000016', prompt('A', 'flow'))
        out = self.run_debates()
        rows = self.rows(out)
        self.assertIsNone(rows['B']['cells'][0]['agent'])
        self.assertIsNone(rows['C']['cells'][0]['agent'])
        self.assertTrue({a.id, b.id}.isdisjoint(out[1]))


class NoFalseDebates(Fixture):
    def test_report_like_text_in_an_unrelated_repo(self):
        other = os.path.join(os.path.dirname(self.repo), 'other')
        os.makedirs(os.path.join(other, 'docs', 'notes'))
        self.claude('b0000000000000001', '결과를 `docs/notes/r1/x.md`에 쓴다. brief: `docs/notes/brief.md`', cwd=other)
        self.codex(9, 'Write the result to r1/x.md and docs/notes/r2/y.md', cwd=other)
        out = self.run_debates()
        self.assertEqual(out[0], [])
        self.assertEqual(dict(out[1]), {})

    def test_folder_without_brief_or_exact_round_folder_is_not_a_debate(self):
        other = os.path.join(os.path.dirname(self.repo), 'other2')
        os.makedirs(os.path.join(other, 'docs', 'plain', 'r1x'))               # r1x is not r<N>
        os.makedirs(os.path.join(other, 'docs', 'plain2'))
        self.claude('b0000000000000002', '`docs/plain/r1/A.md`에 쓴다 [X-A]', cwd=other)
        self.claude('b0000000000000003', '`docs/plain2/r1/A.md`에 쓴다 [X-A]', cwd=other)
        self.assertEqual(self.run_debates()[0], [])

    def test_marker_text_without_a_folder_seats_nobody(self):
        a = self.claude('b0000000000000004', '[REVIEW-C] 너는 review 토론의 C(x) 담당이다. 아무 폴더도 말하지 않는다')
        out = self.run_debates()
        self.assertNotIn(a.id, out[1])
        self.assertEqual(out[0], [])

    def test_reader_only_reviewer_is_not_a_participant(self):
        self.claude('b0000000000000005', prompt('A', 'flow'))
        write(os.path.join(self.unit, 'r1', 'A_flow.md'), 'report\n')
        rev = self.claude('b0000000000000006', '`%s/r1/A_flow.md`를 읽고 검토한다. 먼저 `%s/brief.md`를 읽는다.' % (REL, REL))
        rev.reads[os.path.join(self.unit, 'brief.md')] = 1.0
        rev.reads[os.path.join(self.unit, 'r1', 'A_flow.md')] = 2.0
        out = self.run_debates(status='done')
        self.assertEqual(out[1][rev.id], {self.unit})                         # it works in the debate, with no seat
        rows = self.rows(out)
        self.assertEqual(sorted(rows), ['A_flow', 'B', 'C'])                  # no row of its own
        for r in rows.values():
            self.assertNotIn(rev.id, r['agents'])
        self.assertTrue(rows['A_flow']['cells'][0]['readers'])                # it still shows as a cross-review "read"

    def test_reviewer_with_a_tag_and_only_reads(self):
        self.claude('b0000000000000007', prompt('A', 'flow'))
        rev = self.claude('b0000000000000008', 'review the debate outputs', tag_description='R 검토')
        rev.reads[os.path.join(self.unit, 'brief.md')] = 1.0
        self.assertNotIn(rev.id, self.run_debates()[1])


class NextRound(Fixture):
    """review round 2: the same session (Codex resume, Claude SendMessage) is told to write r2 through a folder-prefixed relative path
    after it already wrote r1. The written r1 seats it by the path rules, so the r2 instruction has to be read there as well."""

    def r2_text(self, letter, name):
        return ('%d라운드다. `%s/r1/`의 다른 두 사람 1라운드를 읽고(네 1라운드 `%s/r1/%s_%s.md`도 다시 본다) '
                '동의·반박을 `%s/r2/%s_%s.md`에 쓴다.' % (2, REL, REL, letter, name, REL, letter, name))

    def wrote_r1(self, a, stem):
        a.writes.append({'ts': self.t, 'path': write(os.path.join(self.unit, 'r1', stem + '.md'), 'r1\n')})

    def test_codex_resume_moves_to_round_two(self):
        b = self.codex(65, prompt('B', 'gate'))
        self.wrote_r1(b, 'B_gate')
        b.received.append({'ts': self.t + 5, 'text': self.r2_text('B', 'gate')})
        rows = self.rows(self.run_debates())
        r1, r2 = rows['B_gate']['cells']
        self.assertEqual((r1['state'], r2['state'], r2['agent']), ('done', 'writing', b.id))
        self.assertEqual(sorted(rows), ['A', 'B_gate', 'C'])                  # the r1 path it re-reads is not a second seat

    def test_claude_next_round_message(self):
        a = self.claude('d0000000000000001', prompt('A', 'flow'), origin='cli')
        self.wrote_r1(a, 'A_flow')
        a.received.append({'ts': self.t + 5, 'text': self.r2_text('A', 'flow')})
        r1, r2 = self.rows(self.run_debates())['A_flow']['cells']
        self.assertEqual((r1['state'], r2['state'], r2['agent']), ('done', 'writing', a.id))

    def test_prefix_outside_a_debate_folder_is_ignored(self):
        b = self.codex(65, prompt('B', 'gate'))
        self.wrote_r1(b, 'B_gate')
        b.received.append({'ts': self.t + 5, 'text': '결과를 `docs/notes/r2/B_gate.md`에 쓴다.'})
        cells = self.rows(self.run_debates())['B_gate']['cells']
        self.assertEqual([(c['round'], c['state']) for c in cells], [(1, 'draft')])    # still on r1; no seat in a folder that is not a debate


class CwdRecording(unittest.TestCase):
    def test_first_cwd_wins(self):
        a = server.Agent('c0000000000000001', {})
        a.feed({'type': 'user', 'timestamp': '2026-10-01T00:00:00.000Z', 'cwd': '/repo', 'message': {'content': 'hi'}})
        a.feed({'type': 'assistant', 'timestamp': '2026-10-01T00:00:01.000Z', 'cwd': '/repo/sub', 'message': {'content': []}})
        self.assertEqual(a.cwd, '/repo')

    def test_codex_agent_keeps_its_entry_cwd(self):
        e = {'id': '019a0001-0000-7000-8000-000000000000', 'meta_ts': 1.0, 'path': '/x.jsonl', 'cwd': '/repo', 'model': 'm'}
        self.assertEqual(server.CodexAgent(e, {}).cwd, '/repo')


if __name__ == '__main__':
    unittest.main()
