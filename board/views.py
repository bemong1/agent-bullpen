"""What the page reads: builds the current picture of a session (Session) in the shape the API returns — state, alerts, agent detail, timeline, and the files that may be opened."""

import glob
import os
import re
import time
from datetime import datetime

from . import diag, runstate as RS
from .util import BOOT, HOME, denied_file, hidden_or_secret, short_path, trunc, write_made
from .codex_index import CODEX
from .link import LINKS, certain, link_brief


# English cues for the two regexes below: the same record gives the same alert whatever language the page shows. Each cue is a request to the user, never a
# remark about the past ("I approved", "approval was granted earlier") or plain guidance ("if you want to change the port, pass --port"): they name the user's
# approval/decision as what is needed, or ask the user to act. A negation before the verb ("does not need your approval", "no approval needed") turns a cue off.
_WHO = r"(?:your|(?:the )?user(?:'s)?|human)"
_WHAT = r"(?:approval|decision|confirmation|sign-?off|go-?ahead|permission|choice|answer|reply|input|feedback|call)"
_NOT = r"(?<!not )(?<!n't )(?<!never )(?<!no longer )"                            # put after \b, so it is only tried at the start of a word
# wording that means "this needs the user's approval or decision": it is found in an orchestrator's last line and in an agent's report alike
_NEED_CUES = [
    r"\b" + _NOT + r"(?:needs?|requires?|awaiting|awaits|pending|blocked (?:on|by)|waiting (?:for|on))\s+" + _WHO + r"\s+(?:explicit\s+)?" + _WHAT + r"\b",     # needs your approval
    r"\b" + _WHO + r"\s+(?:explicit\s+)?" + _WHAT + r"\s+(?:is\s+|are\s+)?(?:needed|required|pending|awaited|necessary)\b",                          # your approval is required
    r"\b(?<!no )(?<!not )(?<!no further )(?<!without )(?:approval|decision|confirmation|sign-?off|go-?ahead)\s+(?:is\s+|are\s+)?(?:needed|required|pending)\b",   # decision needed
    r"\b" + _NOT + r"needs?\s+(?:you|the user)\s+to\s+(?:approve|confirm|decide|choose|pick|sign off|answer)\b",                                            # needs you to decide
    r"\b" + _NOT + r"needs?\s+an?\s+(?:explicit\s+)?(?:decision|approval|confirmation|sign-?off|answer)\b",                                                 # needs a decision
]
# what an orchestrator says when it hands the turn back with a question: asks for a choice, an answer, a go-ahead
_ASK_CUES = [
    r"\blet\s+me\s+know\b",                                                                                                                         # let me know which you prefer
    r"\b(?:tell|inform|advise|notify)\s+me\s+(?:which|what|whether|if|how|when|where|who|your)\b",
    r"\bplease\s+(?:approve|confirm|decide|choose|pick|select|advise|specify|clarify|respond|reply|answer)\b",
    r"\b(?:choose|pick|select)\s+(?:one|between|either)\b",
    r"\b(?:would|do)\s+you\s+(?:like|want|prefer|wish|rather)\b",
    r"\bif\s+you(?:'d|\s+would|\s+do)?\s+(?:like|want|prefer|wish)\s+me\s+to\b",                                                                     # offers to act, not "if you want to change X"
    r"\b(?:should|shall)\s+(?:i|we)\b(?![\w/])",
    r"\bwhich\b[^.?!\n]{0,50}?\b(?:do|would|should|shall|can|could)\s+(?:you|we|i)\b(?![\w/])",
    r"\byou(?:'ll|\s+will|\s+must|\s+need\s+to|\s+have\s+to)\s+(?:need\s+to\s+|have\s+to\s+)?(?:decide|choose|pick|approve|confirm)\b",
    r"\byour\s+call\b|\bup\s+to\s+you\b|\bwaiting\s+for\s+you\s+to\b",
]
# what an agent's report says about the user: ask, escalate, items for the user's approval
_REPORT_CUES = [
    r"\buser decision",
    r"\b" + _NOT + r"needs? (?:user )?approval",
    r"\b" + _NOT + r"(?:ask(?:ing)?|(?:check|confirm)(?:ing)? with|consult(?:ing)?|escalat(?:e|ing) to)\s+(?:the\s+)?(?:user|human)\b",                  # ask the user
    r"\b(?:items?|points?|questions?|decisions?)\s+(?:for|requiring|needing|awaiting)\s+(?:the\s+)?(?:user|your|human)\b",                            # open questions for the user
    r"\b(?:approval|decision)\s+items?\b",
    r"\bfor\s+(?:the\s+)?(?:user'?s?|your|human)\s+(?:approval|decision|confirmation|sign-?off|input|call)\b",
    r"\bplease\s+(?:approve|confirm|decide|choose)\b",
    r"\b(?:needs?|requires?)\s+(?:a\s+)?(?:human|manual)\s+(?:approval|decision|confirmation|intervention)\b",
]
# DECIDE_RE runs on every line of every recent agent report at each poll, and the English patterns cost about 10 us a line: they are tried only on a line that holds
# one of their words (every word of the two lists above that must appear, in lower case). A cue that adds a new word must add it here too (tests/test_decide_detect.py checks each cue).
_DECIDE_GATE = re.compile(r"need|require|await|pending|blocked|waiting|necessary|approval|decision|confirm|check with|sign|go-?ahead|input|call|please|ask|consult|escalat|item|point|question")


class _Lines:
    """The Korean cues first, then the English ones behind the gate; search() answers like a compiled pattern (a match, or None)."""

    def __init__(self, korean, english, gate):
        self.korean, self.english, self.gate = re.compile(korean), re.compile('|'.join(english), re.I), gate

    def search(self, line):
        return self.korean.search(line) or (self.gate.search(line.lower()) and self.english.search(line)) or None


# whether the orchestrator ended its turn with a question (only the last few lines are checked)
QUESTION_RE = re.compile(r'\?|까요|할지|하시겠|골라\s*주|선택해\s*주|정해\s*주|알려\s*주|말씀해\s*주|답(을)?\s*주|'
                         r'원하시면|승인하시면|확정하시면|결정하시면|허락하시면|괜찮으시면|고르시면|정하시면|'
                         r'해\s*주실\s*일|주셔야|직접\s*하셔야|결정(이|을)?\s*(필요|내려)|승인(이|을)?\s*(필요|해\s*주)|확인해\s*주세요|사용자\s*결정'
                         '|' + '|'.join(_NEED_CUES + _ASK_CUES), re.I)
# decision items for the user inside an agent's report
DECIDE_RE = _Lines(r'사용자\s*(결정|확인|승인|판단)|사용자에게\s*(묻|확인|권고)|승인\s*항목|결정\s*(이\s*)?필요', _NEED_CUES + _REPORT_CUES, _DECIDE_GATE)

def spawn_map(s):
    """tool_use_id of an Agent tool call → agent id (Claude sub-agents only)."""
    return {a.tool_use_id: a.id for a in s.agents.values() if a.provider == 'claude'}


def resolve_spawn(e, ids):
    """The receiving side of a spawn event: the main record has only the tool_use_id, so once the agent exists it is resolved to that id (ids = spawn_map). Edits e."""
    if e['kind'] == 'spawn':
        e['agent'] = e.get('agent') or ids.get(e.get('tool_use_id'))
        e['to'] = e['agent'] or e['to']


UNKNOWN = object()         # "which node" is not known: the key is left out of the agent rather than saying "the main session"


def node_of(a):
    """The node that launched an agent, inside the tree that holds it: the id of the sub-agent whose own record made the call (or, for a sub-agent started by a
    sub-agent, its parent), None when the main session of the tree did. A Codex thread linked only by its process or environment has no call to read the node
    from: UNKNOWN (the page puts it under the main session, as for any unresolved node)."""
    if a.origin == 'cli':
        return (a.cli or {}).get('node')
    if a.provider == 'codex':
        link = a.link or {}
        return link.get('node') if link.get('node') is not None or link.get('call') else UNKNOWN
    return a.parent_agent


def by_of(a):
    """[tree session id, node] that handed the last run of a `claude -p` child its instruction (a resumed run), or None (not known / not a child)."""
    by = (a.cli or {}).get('by') if a.origin == 'cli' else None
    return list(by[-1]) if by and by[-1] else None


def runs_of(a):
    """The runs (process lifetimes) of an agent's record: the last 16 as {n, start, end, kind, by}. `by` is who handed that run its instruction (a resumed `claude -p` child)."""
    runs = a.runs.runs if a.provider == 'claude' else []
    by = (a.cli or {}).get('by') if a.origin == 'cli' else None
    aligned = bool(by) and len(by) == len(runs)
    return [{'n': r.epoch, 'start': r.start_ts, 'end': r.end_ts, 'kind': r.start_kind, 'by': list(by[i]) if aligned and by[i] else None}
            for i, r in list(enumerate(runs))[-16:]]


def link_of(s, a):
    """link_brief plus the class of the rule (certain | guess) and, for a `claude -p` child, why a `content_short` guess is one: the text comparison was cut short
    (`incomplete`: a fingerprint that could not be completed, not a short instruction) or the only call that could have launched it was a script nobody could read
    (`assumed`); and what the tree and the call are."""
    out = link_brief(a)
    out['rule_class'] = 'certain' if out['certain'] else 'guess'
    if a.origin == 'cli':
        dec = LINKS.decisions.get(a.id)
        out['incomplete'] = bool(dec is not None and dec.incomplete)
        out['assumed'] = bool(dec is not None and dec.assumed)
        out['tree'] = (a.cli or {}).get('sid')
    return out


def _with_orch_diag(entries, ov):
    """The diagnostics of the orchestrator's own judgment (OrchVerdict.diag: `proc_unknown` when its process cannot be told) added to the list as entries of the orchestrator,
    once per code, in the order and under the cap diag.collect keeps."""
    add = [diag._entry(code, 'orch', None, **params) for code, params in ov.diag if not any(e['code'] == code and e['scope'] == 'orch' for e in entries)]
    if not add:
        return entries
    rank = {'warn': 0, 'info': 1}
    return sorted(entries + add, key=lambda e: (rank[e['level']], e['code'], e['agent'] or '', e['unit'] or ''))[:diag.MAX_PER_SESSION]


def state(s):
    with s.lock:
        now = time.time()
        s._name_agents()
        s._attach_main_side()
        alive, pid, name = s.alive()
        has_cli = any(a.origin == 'cli' for a in s.agents.values())
        s._cli_procs = s.cli_procs() if has_cli else {}
        s._cli_alive = {sid for sid, p in s._cli_procs.items() if p.alive is not False}        # the ones that cannot be called finished: the same process check as the rest of the state
        verdicts = {aid: s.agent_verdict(a, alive, now) for aid, a in s.agents.items()}      # one `now` for the whole picture: a judgment never reads the clock itself
        statuses = {aid: v.status for aid, v in verdicts.items()}
        s._verdicts = verdicts
        jd = s.judged(statuses)
        debates, seated = jd.debates, s.seated_units(jd)
        orch_proc = s._orch_proc = RS.Proc(alive, (pid,) if pid else ())
        ov = RS.orch_state(s.runs, orch_proc, now) if s.provider == 'claude' else RS.OrchVerdict('idle')
        s._orch_verdict = ov
        entries = _with_orch_diag(diag.collect(s, now, verdicts, RS.group_limits(verdicts, ov if ov.state == 'limit_wait' else None)), ov)
        s._diag = entries
        agents = []
        for a in s.agents.values():
            cur = a.activity[-1] if a.activity else None
            last_tool = next((x for x in reversed(a.activity) if x['kind'] == 'tool'), None)
            spark = [0] * 30
            for ts, _ in a.ticks[-600:]:
                i = int((now - (ts or 0)) // 60)
                if 0 <= i < 30:
                    spark[29 - i] += 1
            agents.append({
                'id': a.id, 'tag': a.name_tag, 'title': a.title, 'description': a.description,
                'type': a.agent_type, 'model': a.model or a.model_hint, 'effort': a.effort,
                'status': statuses[a.id], 'reason': verdicts[a.id].reason, 'resets_at': verdicts[a.id].resets_at,
                'spawn_ts': a.spawn_ts or a.first_ts, 'first_ts': a.first_ts,
                'last_ts': a.last_ts, 'tool_count': a.tool_count, 'errors': a.errors,
                'top_tools': a.tool_counts.most_common(4), 'current': cur, 'last_tool': last_tool,
                'ctx': a.tokens.ctx, 'tokens': a.tokens.as_dict(), 'spark': spark,
                'pending': min(a.pending.values(), key=lambda p: p['ts'] or now) if a.pending and
                statuses[a.id] in ('running', 'stalled') else None,
                'handbacks': len(a.handbacks), 'received': len(a.received) + len(a.orch_msgs),
                'units': sorted(seated.get(a.id, [])), 'work_units': sorted(jd.agent_units.get(a.id, [])),
                'notification': a.notifications[-1] if a.notifications else None,
                'provider': a.provider, 'origin': a.origin, 'link': link_of(s, a),
                'by': by_of(a), 'parent': None if s.launcher_of(a) == 'orch' else s.launcher_of(a), 'runs': runs_of(a),
            })
            node = node_of(a)
            if node is not UNKNOWN:
                agents[-1]['node'] = node
        agents.sort(key=lambda x: -(x['spawn_ts'] or 0))
        o = dict(s.orch)
        o['tokens'] = s.orch_tokens.as_dict()
        o['ctx'] = s.orch_tokens.ctx
        o['state'] = ov.state if s.provider == 'claude' else ('working' if RS.turn_open(s.cx_turn, orch_proc) else 'idle')
        if ov.state == 'limit_wait':             # its last turn ended on a usage limit and nothing has come since: it waits for the reset
            o['resets_at'], o['auto'] = ov.resets_at, ov.auto
        o['provider'] = s.provider
        feed = []
        names = {a.id: (a.name_tag or a.title) for a in s.agents.values()}
        ids = spawn_map(s)
        for i, ev in enumerate(s.feed[-400:]):
            e = dict(ev)
            e['idx'] = len(s.feed) - len(s.feed[-400:]) + i
            resolve_spawn(e, ids)
            e['full_len'] = len(e.get('text') or '')
            e['text'] = trunc(e.get('text') or '', 1500)
            feed.append(e)
        return {
            'version': s.version, 'now': now,
            'session': {'id': s.id, 'title': s.title or name or s.slug, 'slug': s.slug,
                        'started': s.first_ts,
                        'cwd': s.cwd, 'alive': alive, 'pid': pid, 'name': name},
            'orch': o, 'agents': agents, 'names': names, 'debates': debates, 'feed': feed,
            'alerts': alerts(s, statuses, now), 'boot': BOOT, 'codex_limit': CODEX.limit(), 'unlinked': s.unlinked(), 'diag': diag.counts(entries),
        }


def _when(ts):
    """A time as month/day hour:minute for the old Korean fields, '' when there is none or it is not a time."""
    try:
        return datetime.fromtimestamp(ts).strftime('%m/%d %H:%M') if ts else ''
    except (OverflowError, OSError, ValueError, TypeError):
        return ''


LIMIT_TITLE_KO = {(True, False): '사용 한도 도달 — {}에 초기화', (True, True): '사용 한도 도달 — {}에 자동 재개',
                  (False, False): '사용 한도 도달 — 초기화 시각 모름', (False, True): '사용 한도 도달 — 자동 재개 (시각 모름)'}


def sys_line(code, status=None, at=None, auto=False):
    """(dictionary key, key parameters, old Korean title) of a system line of the feed (kind `sys`, made in sessions.py from an API error line or a limit notice).
    The page words the line from the key and its numbers: `status` (the HTTP status of the error) and `at` (the reset time, epoch seconds). The key says which sentence it is,
    since the dictionary cannot branch on a missing number. Codes: limit · reset · api_error (it passes: retry) · rejected (the server refuses the request) · auth."""
    when = _when(at)
    if code == 'limit':
        if when:
            return ('event.sys.limit.auto' if auto else 'event.sys.limit'), {'at': at}, '사용 한도 도달 · %s %s' % (when, '자동 재개' if auto else '초기화')
        return ('event.sys.limit.noTime.auto' if auto else 'event.sys.limit.noTime'), {}, '사용 한도 도달' + (' · 자동 재개' if auto else '')
    if code == 'reset':
        return 'event.sys.reset', {}, '사용 한도 초기화'
    if code == 'auth':
        return 'event.sys.auth', {}, '로그인 만료 · 다시 로그인 필요'
    if code == 'api_error':
        return (('event.sys.api_error', {'status': status}, 'API 오류 %d · 재시도 가능' % status) if status else
                ('event.sys.api_error.noStatus', {}, 'API 오류 · 재시도 가능'))
    return (('event.sys.rejected', {'status': status}, 'API 오류 %d · 요청 거부됨' % status) if status else
            ('event.sys.rejected.noStatus', {}, 'API 오류 · 요청 거부됨'))


def _alert(aid, level, title, text, ts, agent, key, params=None, text_i18n=None):
    """One alert. `title`/`text` are the old Korean fields (kept as they were); title_i18n {key, params} (and text_i18n where the server wrote the text)
    let the page word the same alert from the dictionary. Data inside the text (a question, an agent's words, a name) is not translated.
    key=None: no dictionary entry yet; the page shows `title` as it is (it falls back to it for a key it does not know) and `params` ride along as `title_params`."""
    a = {'id': aid, 'level': level, 'title': title, 'text': text, 'ts': ts, 'agent': agent}
    if key:
        a['title_i18n'] = {'key': key, 'params': params or {}}
    elif params:
        a['title_params'] = params
    if text_i18n:
        a['text_i18n'] = text_i18n
    return a


def alerts(s, statuses, now):
    """What the user needs to look at: a decision needed (decide) · something to check (check) · the user's turn (info) · news that needs nothing (note)."""
    out = []
    name = lambda aid: (s.agents[aid].name_tag or s.agents[aid].title) if aid in s.agents else aid
    for tid, q in s.pending_q.items():
        lines = []
        for x in q['questions']:
            opts = ' / '.join(op.get('label', '') for op in x.get('options') or [])
            lines.append(x.get('question', '') + (' — ' + opts if opts else ''))
        out.append(_alert('ask:' + str(tid), 'decide', '오케스트레이터가 선택지를 묻고 있습니다', '\n'.join(lines), q['ts'], None, 'alert.ask.title'))
    marks = s.runs.turn if s.provider == 'claude' else s.cx_turn
    waiting = not marks.open and s._orch_proc.alive is not False         # its turn is over and there is a session to answer in or to give the next instruction to
    last_user = max((e['ts'] or 0 for e in s.feed if e['kind'] == 'user_say'), default=0)
    last_say = next((e for e in reversed(s.feed) if e['kind'] == 'orch_say'), None)
    running = [aid for aid, st in statuses.items() if st in ('running', 'stalled')]
    groups = RS.group_limits({aid: v for aid, v in getattr(s, '_verdicts', {}).items() if aid in s.agents}, getattr(s, '_orch_verdict', None))
    for g in groups:                                   # one notice for everything that stopped on the same usage limit; no time is made up when the record has none
        names = [name(aid) for aid in g.agents]
        # the key says what the sentence is (a boolean cannot be branched on in the dictionary); the one parameter is the reset time as epoch seconds, which the page words in its own zone
        has_time = bool(_when(g.resets_at))
        key = {(True, False): 'alert.limit.title', (True, True): 'alert.limit.auto.title',
               (False, False): 'alert.limit.noTime.title', (False, True): 'alert.limit.noTime.auto.title'}[(has_time, bool(g.auto))]
        out.append(_alert('limit:%s' % (int(g.resets_at) if g.resets_at else 'unknown'), 'note' if g.auto else 'check',
                          LIMIT_TITLE_KO[(has_time, bool(g.auto))].format(_when(g.resets_at)),
                          ', '.join(names), s.orch.get('last_ts') if g.orch else max((s.agents[aid].last_ts or 0 for aid in g.agents), default=None), None,
                          key, {'at': g.resets_at} if has_time else {}))
        out[-1]['title_params'] = {'at': g.resets_at, 'auto': g.auto, 'agents': len(g.agents), 'orch': g.orch}        # what the group is made of, for whoever wants more than the sentence
    if waiting and last_say and (last_say['ts'] or 0) > last_user and not s.pending_q:
        tail = [ln for ln in last_say['text'].strip().splitlines() if ln.strip()][-2:]
        if any(QUESTION_RE.search(ln) for ln in tail):
            out.append(_alert('say:%d' % int(last_say['ts']), 'decide', '오케스트레이터가 답을 기다립니다', last_say['text'], last_say['ts'], None,
                              'alert.say.title'))
        elif not running and not any(g.auto for g in groups):         # it continues by itself after the reset: no "waiting for the next instruction"
            out.append(_alert('turn:%d' % int(last_say['ts']), 'info', '진행 중인 에이전트가 없고 오케스트레이터가 다음 지시를 기다립니다',
                              last_say['text'], last_say['ts'], None, 'alert.turn.title'))
    for aid, st in statuses.items():
        a = s.agents[aid]
        if st == 'stalled':
            minutes = int((now - (a.last_ts or now)) // 60)
            out.append(_alert('stall:%s:%d' % (aid, int(a.last_ts or 0)), 'check', '%s — %d분째 활동 없음' % (name(aid), minutes),
                              (a.activity[-1]['text'] if a.activity else ''), a.last_ts, aid, 'alert.stall.title', {'name': name(aid), 'minutes': minutes}))
        elif st in ('failed', 'killed') and now - (a.last_ts or 0) < 86400:
            out.append(_alert('fail:%s:%d' % (aid, int(a.last_ts or 0)), 'check', '%s — %s' % (name(aid), '실패' if st == 'failed' else '중지됨'),
                              a.notifications[-1]['summary'] if a.notifications else '', a.last_ts, aid, 'alert.fail.%s.title' % st, {'name': name(aid)}))
    seen = s.__dict__.setdefault('_decide_cache', {})        # a report does not change once it is in the feed: the lines asking for the user are found once
    for ev in s.feed[-400:]:
        if ev['kind'] != 'handback' or now - (ev['ts'] or 0) > 43200:
            continue
        key = (ev['agent'], ev['ts'], len(ev['text']))
        if key not in seen:
            if len(seen) > 2000:
                seen.clear()
            seen[key] = [ln.strip(' -*') for ln in ev['text'].splitlines() if DECIDE_RE.search(ln)][:5]
        lines = seen[key]
        if lines:
            out.append(_alert('hb:%s:%d' % (ev['agent'], int(ev['ts'])), 'check', '%s 보고에 사용자 결정·승인 항목' % name(ev['agent']),
                              '\n'.join(lines), ev['ts'], ev['agent'], 'alert.hb.title', {'name': name(ev['agent'])}))
    lim = CODEX.limit()
    if lim and not lim['stale'] and (lim['used_percent'] or 0) >= 90 and s._codex_busy(statuses, now):
        out.append(_alert('cxlimit:%s' % lim['resets_at'], 'check',
                          ('◆ Codex 주간 한도 도달' if lim['reached'] else
                           '◆ Codex 주간 한도 %d%%' % lim['used_percent']) + ' — 남은 Codex 작업이 멈출 수 있음',
                          '계정 전체 값 · 기록 %s · 재설정 %s' % (
                              datetime.fromtimestamp(lim['as_of']).strftime('%m/%d %H:%M') if lim['as_of'] else '-',
                              datetime.fromtimestamp(lim['resets_at']).strftime('%m/%d %H:%M') if lim['resets_at'] else '-'),
                          lim['as_of'], None,
                          'alert.cxlimit.reached.title' if lim['reached'] else 'alert.cxlimit.percent.title',
                          {} if lim['reached'] else {'percent': int(lim['used_percent'])},
                          # the dates as epoch seconds (None = unknown): the page formats them in its own language and zone
                          {'key': 'alert.cxlimit.text', 'params': {'as_of': lim['as_of'] or None, 'resets_at': lim['resets_at'] or None}}))
    rank = {'decide': 0, 'check': 1, 'info': 2, 'note': 3}
    out.sort(key=lambda x: (rank[x['level']], -(x['ts'] or 0)))
    return out


def agent_detail(s, aid):
    with s.lock:
        a = s.agents.get(aid)
        if not a:
            return None
        s._name_agents()
        s._attach_main_side()
        ticks = [[ts, c] for ts, c in a.ticks if ts]
        d = {
            'id': a.id, 'tag': a.name_tag, 'title': a.title, 'description': a.description,
            'spawn_prompt': a.spawn_prompt, 'spawn_ts': a.spawn_ts, 'activity': list(a.activity),
            'texts': list(a.texts), 'received': a.received, 'orch_msgs': a.orch_msgs,
            'handbacks': a.handbacks, 'notifications': a.notifications,
            'writes': [{'ts': w['ts'], 'path': w['path'], 'short': short_path(w['path'])} for w in a.writes],
            'reads': sorted(({'path': p, 'short': short_path(p), 'ts': t} for p, t in a.reads.items()),
                            key=lambda x: -(x['ts'] or 0))[:80],
            'tool_counts': a.tool_counts.most_common(), 'ticks': ticks,
            'provider': a.provider,
        }
        if a.provider == 'codex':
            d['link'] = dict(a.link, certain=certain((a.link or {}).get('rule'))) if a.link else a.link
            d['turns'] = [{k: t[k] for k in ('start', 'end', 'status', 'error', 'bash_ts', 'out', 'out_state')}
                          for t in a.turns]
            d['partial'] = {'skipped_bytes': a.partial} if a.partial else None
        return d


def timeline(s, since):
    with s.lock:
        lanes = []
        for a in s.agents.values():
            ticks = [[ts, c] for ts, c in a.ticks if ts and ts >= since]
            if ticks or (a.last_ts or 0) >= since:
                lanes.append({'id': a.id, 'ticks': ticks, 'spawn_ts': a.spawn_ts or a.first_ts,
                              'writes': [[w['ts'], short_path(w['path'])] for w in a.writes
                                         if w['ts'] and w['ts'] >= since],
                              'handbacks': [h['ts'] for h in a.handbacks if h['ts'] and h['ts'] >= since],
                              'received': [m['ts'] for m in a.received if m['ts'] and m['ts'] >= since]})
        orch = [[ev['ts'], ev['kind']] for ev in s.feed if ev['ts'] and ev['ts'] >= since and
                ev['kind'] in ('orch_msg', 'spawn', 'user_say', 'orch_say', 'handback', 'orch_ask', 'user_answer')]
        return {'since': since, 'now': time.time(), 'lanes': lanes, 'orch': orch}


def allowed_file(s, path):
    """The real path (realpath) that was checked if the file may be opened, else None. The caller opens that path."""
    real = os.path.realpath(path)
    if not re.search(r'\.(md|txt|ya?ml|json)$', real) or denied_file(real) or hidden_or_secret(real):
        return None                                   # the same holds for files an agent wrote: configs such as ~/.docker, .config/gh, .mcp.json are not opened
    with s.lock:                                   # safe even while poll is adding agents
        if any(os.path.realpath(w['path']) == real for a in s.agents.values() for w in a.writes if write_made(w)):
            return real                               # a file an agent wrote (and the write worked)
        debates, _ = s.debates({aid: v.status for aid, v in getattr(s, '_verdicts', {}).items() if aid in s.agents})        # the judgment the page shows: its statuses, not an invented one
    home = os.path.realpath(HOME)
    for d in debates:
        root = os.path.realpath(d['root'])
        if root == home or home.startswith(root + os.sep):
            continue                                  # HOME or an ancestor of it does not count as a root
        if not (os.path.exists(os.path.join(root, 'brief.md')) or glob.glob(os.path.join(root, 'r[0-9]*')) or any(t.get('room') for t in d.get('topics') or ())):
            continue                                  # only real debate folders (excludes fake roots that appear only in text); a room is a folder with a guide the records name
        if real.startswith(root + os.sep):
            return real
    return None
