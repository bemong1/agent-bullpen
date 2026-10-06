"""What the tests of the debate judgment (units.assign, debates.judge) build their input with: a folder on disk, and the facts of agents made of structure alone.

An agent is described by what it did, never by what it was told: `writes=` (the events of its writes), `reads=`, `planned=` (the outputs a launch command or a room tag asked it to save), `windows=` (the
commands it ran), `launch=` (the call that launched it), `tag=` (its room tag). The truth is checked on the Judgement of `assign`.

    class T(Fixture):
        def test_x(self):
            f = self.file('talk/r1/A.md')
            jd = self.assign(agent('A', writes=[wr(f, 200)]))
            self.assertEqual(jd.cells[(self.p('talk'), 'r1', 'A')].owner, 'A')
"""
import dataclasses
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import server  # noqa: E402,F401  (sets up the sandbox HOME before the board is imported)

from board import debates, facts as F, units as U  # noqa: E402


def wr(path, ts=200.0, kind='create', evidence='tool', ok=True, proof=None, span=None, text=None, run=1, call=None, agent=''):
    """A WriteEvent. `path` is absolute. The defaults are a sure Write by a tool; a shell command that decides its own exit status is `evidence='shell', proof='exit'`, one that can hide a failure
    `proof='window', span=(start, end)` (then the file must have been saved in that time to count: the mtime of the file), a launch command's output `evidence='planned'` with `proof='sha'|'content'`, `span`
    and `text` (the bytes the run was to save)."""
    import hashlib
    proof = proof or ('tool' if evidence == 'tool' else 'exit')
    shas = (hashlib.sha1(text.encode('utf-8')).hexdigest(),) if text is not None else ()
    return F.WriteEvent(agent=agent, path=path, ts=ts, kind=kind, evidence=evidence, ok=ok, call=call, run=run, proof=proof, span=tuple(span) if span else None, shas=shas)


def rd(path, ts=150.0, via='tool'):
    return F.ReadEvent('', path, ts, via)


def plan(path, op='-o', ts=100.0, run=1, call=None):
    """A request: a run was asked to save `path` (a Codex `-o`, a `claude -p` redirect)."""
    return F.Planned('', path, op, call, run, 'command', ts)


def win(t0, t1, ok=True, reads=(), call='c1'):
    return F.CmdWindow('', call, t0, t1, ok, tuple(reads))


def launch(group='m1', call='t1', provider='claude', tree='S', node=None):
    return F.LaunchKey(provider, tree, node, group, call)


def tag(room, seat=None, source='environ', run=1):
    return F.Tag(room, seat, source, run)


def agent(aid, writes=(), reads=(), planned=(), windows=(), launch=None, tag=None, status='done', start=100.0, last=300.0, provider='claude', origin='subagent', run=1, run_start=None, sent_to=(),
          lost=None, writes_dropped=None, windows_dropped=None, cwd=''):
    """The AgentFacts of an agent. The events are given without the agent (it is filled in)."""
    mine = lambda xs: tuple(__import__('dataclasses').replace(x, agent=aid) for x in xs)          # noqa: E731
    return U.AgentFacts(id=aid, provider=provider, origin=origin, cwd=cwd, start=start, first=start, last=last, status=status, launch=launch, tag=tag, writes=mine(writes), reads=mine(reads), planned=mine(planned),
                        windows=mine(windows), windows_dropped=windows_dropped, writes_dropped=writes_dropped, lost=lost, run=run, run_start=start if run_start is None else run_start, sent_to=tuple(sent_to))


def hint(ts=100.0, calls=(), groups=()):
    """What the orchestrator did in a folder: when, by which calls, in which groups (`groups` are the names of the groups, ('claude', 'S', None, name))."""
    return F.Hint(ts, frozenset(calls), frozenset(('claude', 'S', None, g) for g in groups))


def page_session(*agents, orch=(), hints=None, walked=(), cwd=''):
    """A stand-in of a Session for `debates.judge`: a real `server.Agent` for each of the AgentFacts of `agents` (the `agent(...)` builder), its event log holding what the facts say (writes, reads, planned
    outputs, windows, what was not read), the way `debates.facts_of` reads an agent. `orch` are the write events of the orchestrator, `hints` {folder: hint(...)}, `walked` the folders a walk found, `cwd`
    the folder of the session. Returns (session, {agent id: status})."""
    s = types.SimpleNamespace(agents={}, _file_cache={}, _head_cache={}, cwd=cwd, walked_units=tuple(walked), orch_hint_facts=dict(hints or {}),
                              orch_events=tuple(dataclasses.replace(w, agent='orch') for w in orch))
    for f in agents:
        a = server.Agent(f.id, {})
        a.provider, a.origin, a.cwd, a.spawn_ts, a.first_ts, a.last_ts = f.provider, f.origin, f.cwd, f.start, f.first, f.last
        a.launch, a.room_tag, a.run, a.run_start = f.launch, f.tag, f.run, f.run_start
        for w in f.writes:
            a.ev.add_write(w)
        for r in f.reads:
            a.ev.add_read(r)
            a.reads[r.path] = r.ts                         # what the page shows as the readers of a cell
        for q in f.planned:
            a.ev.add_planned(q)
        for w in f.windows:
            a.ev.add_window(w)
        a.ev.windows_dropped, a.ev.writes_dropped, a.ev.lost = f.windows_dropped, f.writes_dropped, f.lost
        a.sent = [{'ts': f.start, 'to': t, 'ok': True} for t in f.sent_to]
        s.agents[f.id] = a
    return s, {f.id: f.status for f in agents}


class Fixture(unittest.TestCase):
    """A folder on disk (a real path, under the sandbox of the tests) and the judgment of facts about it. A folder of the tests is a project: the scratch folders of the machine are not scratch here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        patch = mock.patch.object(U, 'SCRATCH_DIRS', ())
        patch.start()
        self.addCleanup(patch.stop)

    def p(self, *parts):
        """An absolute path below the folder of the test."""
        return os.path.join(self.root, *parts)

    def dirs(self, *rels):
        for r in rels:
            os.makedirs(self.p(r), exist_ok=True)

    def file(self, rel, text='x\n', mtime=None):
        """A file with the text, in a folder made when it is not there, with the time given (seconds since the epoch, as many decimals as you like). Returns its absolute path."""
        path = self.p(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        if mtime is not None:
            ns = int(round(mtime * 1e9))
            os.utime(path, ns=(ns, ns))
        return path

    def sf(self, *agents, orch=(), windows=(), hints=None, walked=(), orch_lost=None, orch_windows_dropped=None):
        return U.SessionFacts(agents=list(agents), orch_writes=tuple(__import__('dataclasses').replace(w, agent='orch') for w in orch), orch_windows=tuple(windows), hints=hints or {},
                              walked=tuple(walked), orch_lost=orch_lost, orch_windows_dropped=orch_windows_dropped)

    def assign(self, *agents, catalog=None, tops_of=None, **kw):
        """units.assign of the facts of the agents (`kw` is for the SessionFacts: orch, windows, hints, walked, orch_lost)."""
        return U.assign(self.sf(*agents, **kw), catalog or U.Catalog(), tops_of)

    def cell(self, jd, unit, rdir, stem):
        return jd.cells.get((self.p(unit), rdir, stem))

    def page(self, *agents, **kw):
        """`debates.judge` of a stand-in session (`page_session`): the data the page gets (the debates, the members, the finals ...)."""
        return debates.judge(*page_session(*agents, **kw))
