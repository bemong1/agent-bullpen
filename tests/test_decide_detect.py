"""Decision-request detection reads English conversations too (board/views.py QUESTION_RE and DECIDE_RE).

The same transcript must give the same alert whatever language the page shows, so English wording is detected like the Korean one:
- QUESTION_RE: the last two lines of the orchestrator's last message (it ended its turn with a question or a request)
- DECIDE_RE: a line of an agent's final report that asks for the user's decision or approval
Cues are requests ("Let me know which option you prefer.", "Needs your approval", "Should I proceed?"). General guidance and remarks about the past
("I approved", "approval was granted earlier") are not. The yes lists lean on sentences without a question mark: `\\?` already caught the others before the English cues were added.
The Korean detection must not change: the old patterns (copied below from before the English cues were added) are compared with the new ones on Korean lines.

    python3 -m unittest discover -s tests
"""
import os
import re
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402
import test_server_i18n as sv  # noqa: E402
from test_preserve import T  # noqa: E402

from board import views  # noqa: E402

# the patterns as they were before the English cues (Korean cues, and the two English ones DECIDE_RE had)
OLD_QUESTION = re.compile(r'\?|까요|할지|하시겠|골라\s*주|선택해\s*주|정해\s*주|알려\s*주|말씀해\s*주|답(을)?\s*주|'
                          r'원하시면|승인하시면|확정하시면|결정하시면|허락하시면|괜찮으시면|고르시면|정하시면|'
                          r'해\s*주실\s*일|주셔야|직접\s*하셔야|결정(이|을)?\s*(필요|내려)|승인(이|을)?\s*(필요|해\s*주)|확인해\s*주세요|사용자\s*결정')
OLD_DECIDE = re.compile(r'사용자\s*(결정|확인|승인|판단)|사용자에게\s*(묻|확인|권고)|승인\s*항목|결정\s*(이\s*)?필요|'
                        r'user decision|needs? (user )?approval', re.I)

# --- QUESTION_RE: one line of an orchestrator's last message ---
QUESTION_YES = [
    'Let me know which option you prefer.',
    'Let me know when you are ready.',
    'Tell me which one to use.',
    'Please tell me whether to keep the old API.',
    'Needs your approval before I continue.',
    'This needs the user\'s decision on the retry policy.',
    'I need your decision on the schema.',
    'Waiting for your go-ahead.',
    'Awaiting your reply.',
    'Waiting on your input before the next round.',
    'Please confirm and I will start.',
    'Please choose A, B or C.',
    'Choose one: A, B or C.',
    'Pick between the two designs.',
    'Your call.',
    'Up to you.',
    'If you want me to continue, say so.',
    "If you'd like me to run the full suite, tell me.",
    'Would you like me to proceed with option B',
    'Do you want the short or the long report',
    'Shall I go ahead',
    'Should we split the work in two',
    'Which option do you prefer, A or B',
    'You will need to decide between A and B.',
    "You'll have to approve the deploy yourself.",
    'Approval is required before I deploy.',
    'Decision needed: SQLite or Postgres.',
    'Waiting for you to pick a direction.',
    'LET ME KNOW WHICH ONE.',
    # with a question mark (caught before the English cues as well)
    'Should I proceed?',
    'Which do you want: A or B?',
]
QUESTION_NO = [
    'I approved the plan and started the build.',
    'Approval was granted earlier, so I continued.',
    'The user approved option B in the last round.',
    'You approved this approach before.',
    'Your approval was recorded in the earlier round.',
    'All three agents finished; the report is in docs/report.md.',
    "I'll let you know when the build finishes.",
    'Done. 12 tests passed, 0 failed.',
    'The decision was made in round 1.',
    'No decisions are pending.',
    'Waiting for the build to finish.',
    'Waiting for your next instruction.',
    'No approval needed for this change; continuing.',
    'No further approval is required.',
    'This does not need your approval.',
    "It doesn't need the user's decision.",
    'Approval not required for read-only checks.',
    'I chose option A because it is simpler.',
    'The reviewers decided on option B.',
    'The file which I changed is docs/guide.md.',
    'The option which you prefer is already implemented.',
    'I asked which option is safer, and the reviewer said A.',
    'If you want to change the port, pass --port 8815.',
    'Choose the model in the settings file (see docs/configuration.md).',
    'He needed your approval last week and received it.',
    'To see the results, open the dashboard.',
    'Approval status: approved.',
    'Here is the summary of what changed.',
    'The decision log is in docs/decisions.md.',
    'Should I/O errors occur, the retry loop handles them.',
]

# --- DECIDE_RE: one line of an agent's final report ---
DECIDE_YES = [
    'Needs your approval before merging',
    'Needs user approval to delete the old tables',
    'Needs approval',
    'User decision: pick A or B',
    'Requires your decision on the retry policy',
    'Awaiting your go-ahead to deploy',
    'Pending user approval',
    'Blocked on the user\'s decision',
    'Approval required before the destructive migration',
    'Decision needed: SQLite or Postgres',
    'Your confirmation is required',
    'Needs a decision from you',
    'Please confirm the rollback plan',
    'Ask the user whether to keep the legacy flag',
    'Recommend asking the user before proceeding',
    'Items for user approval:',
    'Open questions for the user: which license?',
    'Approval items',
    'Marked for the user\'s sign-off',
    'Requires a human decision',
    'I need you to confirm the cutover date',
    'NEEDS YOUR APPROVAL',
    'Consult the user before deleting the data',
    'Check with the user first',
    'Confirm with the user which branch to cut',
    'Escalate to the human owner',
    'Points for the user: license, naming',
    "Left for the user's call",
    'Flagged for your input',
    'Need your go-ahead to deploy',
    'Your approval is necessary',
]
DECIDE_NO = [
    'I approved the schema change and merged it.',
    'Approval was granted earlier, so the migration ran.',
    'The user approved option B in round 1.',
    'The user decided to keep SQLite.',
    'Summary: 12 files changed, all tests pass.',
    'No approval needed for read-only checks.',
    'This does not need your approval.',
    "Doesn't need approval.",
    'Do not ask the user again; the answer is in notes.md.',
    "Don't ask the user for it.",
    'The agent asked the user earlier; the answer was A.',
    'He needed your approval last week and received it.',
    'Waiting for the build to finish before the next step.',
    'Decisions are recorded in docs/decisions.md.',
    'The approval flow is documented in the README.',
    'Fixed the pending-approval bug in queue.py.',
    'Confirmation email sent.',
    'Please see section 3 for details.',
    'No decision is required.',
    'No further approval needed.',
    'Approval not required.',
    'Let me know is not a cue in a report.',
]

# Korean lines: the detection is the same as before the English cues (these include the cues, and plain sentences)
KOREAN = [
    '어느 쪽으로 할까요', '진행해도 될까요?', '이대로 진행할지 알려 주세요.', '옵션을 골라 주세요.', '선택해 주세요', '정해 주세요', '말씀해 주세요', '답을 주세요', '답 주세요',
    '원하시면 계속하겠습니다.', '승인하시면 배포합니다.', '확정하시면 시작합니다.', '결정하시면 진행합니다.', '허락하시면 지웁니다.', '괜찮으시면 넘어갑니다.',
    '고르시면 바로 하겠습니다.', '정하시면 알려 주세요', '직접 해 주실 일이 있습니다.', '사용자께서 확인해 주셔야 합니다.', '직접 하셔야 합니다.', '결정이 필요합니다.',
    '결정을 내려 주세요', '승인이 필요합니다', '승인해 주세요', '확인해 주세요', '사용자 결정이 필요합니다', '사용자 승인 항목', '사용자에게 묻고 진행', '사용자에게 확인 필요',
    '사용자에게 권고합니다', '승인 항목: 삭제', '결정 필요: A 또는 B',
    '작업이 끝났습니다.', '보고서는 docs/report.md에 있습니다.', '테스트 12개 통과.', '이전 승인은 기록되었습니다.', '결정은 1라운드에서 내렸습니다.', '사용자가 승인했습니다.',
    '다음 지시를 기다립니다.', '수정한 파일은 세 개입니다.', '', 'ok',
]


def lines_alerts(s, statuses=None, now=T + 100):
    with mock.patch.object(views, 'CODEX', types.SimpleNamespace(limit=lambda: None)):
        return views.alerts(s, statuses or {}, now)


class Detection(unittest.TestCase):
    def test_question_cues_yes(self):
        for line in QUESTION_YES:
            self.assertIsNotNone(views.QUESTION_RE.search(line), line)

    def test_question_cues_no(self):
        for line in QUESTION_NO:
            self.assertIsNone(views.QUESTION_RE.search(line), line)

    def test_decide_cues_yes(self):
        for line in DECIDE_YES:
            self.assertIsNotNone(views.DECIDE_RE.search(line), line)

    def test_decide_cues_no(self):
        for line in DECIDE_NO:
            self.assertIsNone(views.DECIDE_RE.search(line), line)

    def test_the_lists_are_big_enough_and_test_the_new_cues(self):
        for yes, no in ((QUESTION_YES, QUESTION_NO), (DECIDE_YES, DECIDE_NO)):
            self.assertGreaterEqual((len(yes), len(no)), (10, 10))
        # most yes lines have no question mark and were not caught by the old patterns: the new cues are what pass them
        self.assertGreaterEqual(sum(1 for x in QUESTION_YES if not OLD_QUESTION.search(x)), 25)
        self.assertGreaterEqual(sum(1 for x in DECIDE_YES if not OLD_DECIDE.search(x)), 14)
        # the "no" lines of the question list that carry a question mark would be caught for any text: there are none
        self.assertEqual([x for x in QUESTION_NO if '?' in x], [])

    def test_case_does_not_matter_for_english(self):
        for line in ('let me know', 'LET ME KNOW', 'Let Me Know'):
            self.assertTrue(views.QUESTION_RE.search(line))
        for line in ('needs your approval', 'Needs Your Approval', 'NEEDS YOUR APPROVAL'):
            self.assertTrue(views.QUESTION_RE.search(line) and views.DECIDE_RE.search(line))

    def test_a_negation_before_the_verb_turns_the_cue_off(self):
        for line in ('does not need your approval', "doesn't need your approval", 'never needs your approval', 'no longer needs your approval', 'no approval needed',
                     'no further approval required', 'not awaiting your approval', 'without approval required'):
            self.assertIsNone(views.QUESTION_RE.search(line), line)
            self.assertIsNone(views.DECIDE_RE.search(line), line)
        for line in ('needs your approval', 'still needs your approval', 'It needs your approval.', 'approval needed'):
            self.assertTrue(views.QUESTION_RE.search(line) and views.DECIDE_RE.search(line), line)

    def test_korean_detection_is_unchanged(self):
        hits = 0
        for line in KOREAN:
            self.assertEqual(bool(views.QUESTION_RE.search(line)), bool(OLD_QUESTION.search(line)), line)
            self.assertEqual(bool(views.DECIDE_RE.search(line)), bool(OLD_DECIDE.search(line)), line)
            hits += bool(OLD_QUESTION.search(line))
        self.assertGreater(hits, 25)                                    # the corpus is not trivially empty
        for line in ('어느 쪽으로 할까요', '승인이 필요합니다', '알려 주세요'):
            self.assertTrue(views.QUESTION_RE.search(line), line)
        for line in ('사용자 결정이 필요합니다', '승인 항목: 삭제', '사용자에게 묻고 진행'):
            self.assertTrue(views.DECIDE_RE.search(line), line)
        for line in ('작업이 끝났습니다.', '이전 승인은 기록되었습니다.', '사용자가 승인했습니다.'):
            self.assertFalse(views.QUESTION_RE.search(line) or views.DECIDE_RE.search(line), line)

    def test_the_old_patterns_still_hold_where_the_new_ones_widen(self):
        # whatever the old pattern matched, the new one matches (except the "needs approval" line that now stops at a negation)
        for line in QUESTION_YES + QUESTION_NO + KOREAN:
            if OLD_QUESTION.search(line):
                self.assertTrue(views.QUESTION_RE.search(line), line)
        for line in DECIDE_YES + DECIDE_NO + KOREAN:
            if OLD_DECIDE.search(line) and not re.search(r"n't need|not need", line, re.I):
                self.assertTrue(views.DECIDE_RE.search(line), line)


class Cues(unittest.TestCase):
    """Every English cue has a line in the yes lists that only that kind of cue explains, and the gate in front of DECIDE_RE lets each of them through."""

    def test_every_cue_is_met_by_a_yes_line(self):
        for cues, yes in ((views._NEED_CUES, QUESTION_YES + DECIDE_YES), (views._ASK_CUES, QUESTION_YES), (views._REPORT_CUES, DECIDE_YES)):
            for cue in cues:
                rx = re.compile(cue, re.I)
                self.assertTrue([x for x in yes if rx.search(x)], 'no yes line meets the cue ' + cue)

    def test_the_gate_lets_every_yes_line_through_and_changes_no_answer(self):
        english = re.compile('|'.join(views._NEED_CUES + views._REPORT_CUES), re.I)
        for line in DECIDE_YES + DECIDE_NO + QUESTION_YES + QUESTION_NO + KOREAN:
            if english.search(line):
                self.assertTrue(views._DECIDE_GATE.search(line.lower()), 'the gate stops: ' + line)
            self.assertEqual(bool(views.DECIDE_RE.search(line)), bool(views.DECIDE_RE.korean.search(line) or english.search(line)), line)

    def test_every_cue_alone_is_let_through_by_the_gate(self):
        """The gate is a pre-test: a line that a cue matches must hold one of the gate's words. Checked per cue on the yes lines (a gap would hide a cue in DECIDE_RE only)."""
        for cue in views._NEED_CUES + views._REPORT_CUES:
            rx = re.compile(cue, re.I)
            for line in [x for x in QUESTION_YES + DECIDE_YES if rx.search(x)]:
                self.assertTrue(views._DECIDE_GATE.search(line.lower()), (cue, line))

    def test_search_answers_like_a_pattern(self):
        self.assertIsNone(views.DECIDE_RE.search('Tests pass'))
        self.assertIsNone(views.DECIDE_RE.search(''))
        m = views.DECIDE_RE.search('Needs your approval before merging')
        self.assertTrue(m and m.group(0).lower().startswith('needs your approval'))
        self.assertTrue(views.DECIDE_RE.search('사용자 결정 필요'))


class AlertsFromEnglish(sv.SessionCase):
    """Through views.alerts: an English conversation raises the same alerts as its Korean twin."""

    def session(self, text):
        s = self.claude()
        s._feed_main(sv.say(text))
        s.orch['last_ts'] = T
        return s

    def kinds(self, s, statuses=None):
        return [(a['id'].split(':')[0], a['level'], a['title_i18n']['key']) for a in lines_alerts(s, statuses)]

    def test_an_english_request_in_the_last_lines_raises_the_answer_alert(self):
        for text in ('Both designs are ready.\nLet me know which option you prefer.', 'Plan written to docs/plan.md.\n\nNeeds your approval before I continue.\n',
                     'I can do either.\nIf you want me to start now, say so.'):
            s = self.session(text)
            (a,) = lines_alerts(s)
            self.assertEqual((a['id'].split(':')[0], a['level'], a['title_i18n']['key'], a['text']), ('say', 'decide', 'alert.say.title', text.strip()), text)

    def test_a_plain_english_closing_stays_the_info_alert(self):
        for text in ('All done. I approved the plan and the build passed.', 'Approval was granted earlier, so I continued.\nThe report is in docs/report.md.',
                     'If you want to change the port, pass --port 8815.', 'Waiting for your next instruction.'):
            self.assertEqual(self.kinds(self.session(text)), [('turn', 'info', 'alert.turn.title')], text)

    def test_only_the_last_two_lines_count(self):
        s = self.session('Let me know which option you prefer.\nThe build is running.\nThe tests are running.')
        self.assertEqual(self.kinds(s), [('turn', 'info', 'alert.turn.title')])
        s = self.session('Let me know which option you prefer.\nThe build is running.')
        self.assertEqual(self.kinds(s), [('say', 'decide', 'alert.say.title')])

    def test_the_korean_twin_gives_the_same_alert(self):
        for text in ('두 설계가 준비되었습니다.\n어느 쪽으로 할까요', '계획을 docs/plan.md에 썼습니다.\n계속하려면 승인이 필요합니다.'):
            self.assertEqual(self.kinds(self.session(text)), [('say', 'decide', 'alert.say.title')], text)
        for text in ('작업이 끝났습니다.\n보고서는 docs/report.md에 있습니다.', '이전 승인은 기록되었습니다.'):
            self.assertEqual(self.kinds(self.session(text)), [('turn', 'info', 'alert.turn.title')], text)

    def agent_report(self, text):
        s = self.claude()
        a = server.Agent('a%016x' % 1, {'description': 'T1-A research'})
        a.tag, a.last_ts = 'T1-A', T
        s.agents[a.id] = a
        s.orch['last_ts'] = T
        s.feed.append({'ts': T, 'kind': 'handback', 'from': a.id, 'to': 'orch', 'title': '최종 보고', 'text': text, 'agent': a.id})
        return s, a

    def test_an_english_report_with_a_decision_item_raises_the_report_alert(self):
        text = 'Migration written.\n- Needs your approval before merging\n- Items for user approval: drop table old_users\n* Tests pass (12/12)'
        s, a = self.agent_report(text)
        (x,) = lines_alerts(s, {a.id: 'done'})
        self.assertEqual((x['id'].split(':')[0], x['level'], x['title_i18n'], x['text']),
                         ('hb', 'check', {'key': 'alert.hb.title', 'params': {'name': 'T1-A'}}, 'Needs your approval before merging\nItems for user approval: drop table old_users'))

    def test_an_english_report_without_a_request_raises_nothing(self):
        for text in ('Migration written.\n- I approved the schema change and merged it.\n- Approval was granted earlier.\n- No approval needed for read-only checks.',
                     'Summary: 12 files changed, all tests pass.\nThe user decided to keep SQLite.'):
            s, a = self.agent_report(text)
            self.assertEqual(lines_alerts(s, {a.id: 'done'}), [], text)

    def test_a_korean_report_still_raises_it(self):
        s, a = self.agent_report('마이그레이션을 작성했습니다.\n- 사용자 결정 필요: 옛 테이블 삭제')
        (x,) = lines_alerts(s, {a.id: 'done'})
        self.assertEqual((x['id'].split(':')[0], x['text']), ('hb', '사용자 결정 필요: 옛 테이블 삭제'))


if __name__ == '__main__':
    unittest.main()
