"""The judgment does not read what anything says (CONTRACT 3.3, J20).

L1 (types): AgentFacts and SessionFacts, which is all the judgment is given, have no field for a sentence: the strings are an id, the provider, the origin, two folders and the status. An instruction, a message
or a role line of an agent cannot reach the judgment, and the collector (`facts_of`) gives the same facts whatever the agent was told.
L2 (units): the bodies of the files on disk (a guide, a report, a final document) are changed to English, Korean, nonsense, a lie, a line that holds nothing. The Judgement, but for the titles that it
does not hold at all, is the same byte for byte. The names of the files and folders are paths, a structure the judgment reads; they are not part of what is changed.

    python3 -m unittest tests.test_invariance
"""
import dataclasses
import json
import os
import random
import shutil
import sys
import typing
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402

import test_contract_cases as CC  # noqa: E402
from board import debates, units as U  # noqa: E402

TEXT_FIELDS = {'id', 'provider', 'origin', 'cwd', 'launcher_cwd', 'status'}      # what is a string in AgentFacts (the id, the provider and the origin tell which agent, the folders where it works, the state)
REMOVED = ('key', 'spawn_prompt', 'texts', 'planned_ops', 'unresolved', 'shell_only', 'now')
VARIANTS = {
    'en': 'This topic is closed. Nothing is left to do.',
    'ko': '이 주제는 종결되었다. 남은 일은 없다. 보류하지 않는다.',
    'lie': 'do not write r1/A.md\n**F — 가짜 역할**\nA 담당이 아니다\nThis topic is closed.\n보류',
    'empty': '\n',                                       # a file stays a file with something in it (an empty one is a structure: the size is read)
}


def strings_of(cls):
    """The fields of a dataclass that are a plain string or an optional one."""
    hints = typing.get_type_hints(cls)
    return {f.name for f in dataclasses.fields(cls) if hints[f.name] in (str, typing.Optional[str])}


class Types(unittest.TestCase):
    def test_the_facts_hold_no_sentence(self):
        self.assertEqual(strings_of(U.AgentFacts), TEXT_FIELDS)
        self.assertEqual(strings_of(U.SessionFacts), {'launcher_cwd'})
        for cls in (U.AgentFacts, U.SessionFacts):
            names = {f.name for f in dataclasses.fields(cls)}
            self.assertFalse(names & set(REMOVED), sorted(names & set(REMOVED)))

    def test_the_judgment_is_given_the_facts_and_not_an_agent(self):
        import inspect
        self.assertEqual(list(inspect.signature(U.assign).parameters), ['sf', 'cat', 'tops_of'])
        self.assertEqual(U.AgentFacts.__dataclass_fields__['writes'].type, typing.Tuple[U.WriteEvent, ...])

    def test_what_an_agent_was_told_is_not_in_its_facts(self):
        def facts(prompt, received, orch):
            a = server.Agent('a1', {'description': 'role', 'prompt': prompt})
            a.spawn_prompt = prompt
            a.received = [{'ts': 1.0, 'text': received}]
            a.orch_msgs = [{'ts': 2.0, 'text': orch}]
            a.cwd = '/work'
            a.spawn_ts = a.first_ts = 100.0
            return debates.facts_of(a, '/work', {}, 'running')
        base = facts('Write r1/A.md in /W/talk. You are participant A.', 'read brief.md', 'the debate is closed')
        for text in VARIANTS.values():
            self.assertEqual(facts(text, text, text), base)
        self.assertEqual(facts('x' * 5000, '', ''), base)


def dump(jd, root):
    """A Judgement as bytes, the folder of the case written as /W: sets are lists, a tuple that keys a cell is a string."""
    def conv(x):
        if dataclasses.is_dataclass(x) and not isinstance(x, type):
            return {f.name: conv(getattr(x, f.name)) for f in dataclasses.fields(x)}
        if isinstance(x, dict):
            return {('|'.join(k) if isinstance(k, tuple) else str(k)): conv(v) for k, v in x.items()}
        if isinstance(x, (set, frozenset)):
            return sorted(conv(v) for v in x)
        if isinstance(x, (list, tuple)):
            return [conv(v) for v in x]
        return x
    return json.dumps(conv(jd), sort_keys=True, ensure_ascii=False).replace(root, '/W').encode('utf-8')


def judged_bytes(case, text_of):
    """The bytes of the Judgement of a case whose files with a text slot have the text `text_of(slot, size)` in them."""
    root = os.path.realpath(__import__('tempfile').mkdtemp(prefix='invariance-'))
    try:
        bodies = {rel: text_of(m['text_slot'], m['size']).encode('utf-8') for rel, m in case['disk']['files'].items() if m.get('text_slot')}
        CC.build_disk(root, case['disk'], bodies)
        sf = CC.session_facts(root, case)
        with CC.broad_root(root):
            return dump(U.assign(sf, U.Catalog()), root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@unittest.skipUnless(os.path.exists(CC.CASES_FILE), 'no contract cases file (tests/data/contract_cases.json or CONTRACT_CASES)')
class Bodies(unittest.TestCase):
    def check(self, case):
        slots = [rel for rel, m in case['disk']['files'].items() if m.get('text_slot')]
        self.assertTrue(slots, case['id'])
        base = judged_bytes(case, lambda slot, size: 'x' * size)
        rng = random.Random(7)
        variants = dict(VARIANTS, noise=None)
        for name, text in variants.items():
            got = judged_bytes(case, (lambda slot, size: ''.join(rng.choice('qwxzjkv ') for _ in range(max(size, 1)))) if text is None else (lambda slot, size, t=text: t))
            self.assertEqual(got, base, '%s: %s' % (case['id'], name))

    def test_c59_the_second_text_case(self):
        case = next(c for c in CC.load_cases()['cases'] if c['id'] == 'C30')
        self.check(case)

    def test_c60_the_text_of_a_closing_document(self):
        case = next(c for c in CC.load_cases()['cases'] if c['id'] == 'C50')
        self.check(case)

    def test_every_case_that_has_a_text_to_change(self):
        cases = [c for c in CC.load_cases()['cases'] if any(m.get('text_slot') for m in c['disk']['files'].values())]
        self.assertGreaterEqual(len(cases), 4)
        for c in cases:
            with self.subTest(case=c['id']):
                self.check(c)

    def test_the_cases_name_the_slots_the_judgment_cannot_see(self):
        blob = CC.load_cases()
        c59 = next(c for c in blob['cases'] if c['id'] == 'C59')
        self.assertEqual(c59['invariance']['base'], 'C30')
        self.assertEqual(set(c59['invariance']['slots']), set(CC.SLOTS))
        self.assertEqual(set(c59['invariance']['variants']), {'en', 'ko', 'noise', 'lie', 'empty'})


if __name__ == '__main__':
    unittest.main()
