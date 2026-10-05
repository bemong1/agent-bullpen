"""State of a single agent (Agent: Claude sub-agent or claude -p; CodexAgent: Codex thread) and the helper functions both share (tool categories, stall judgment, name numbering, Codex token sync)."""

import collections
import hashlib
import json
import os
import re

from . import runstate as RS
from .util import as_text, norm_key, parse_ts, short_path, strip_reminders, trunc
from .tokens import TokenMeter
from .codex_parse import CX_CALL_ID_RE, codex_call, codex_say_text, codex_user_text, cx_decode, cx_usage_add
from .codex_index import CODEX


COORD_PREFIX = 'The coordinator sent a message while you were working:'
TURNS_KEEP = 200     # number of turns CodexAgent keeps for the detail view


def tool_category(name):
    # Codex is classified by the inner tool name inside exec (JS), decided at call time. The same rule as a Claude Bash `cat` counting as bash
    if name in ('Read', 'Grep', 'Glob', 'view_image'):
        return 'read'
    if name in ('Write', 'Edit', 'NotebookEdit', 'apply_patch'):
        return 'write'
    if name in ('Bash', 'exec_command', 'write_stdin'):
        return 'bash'
    if name in ('WebFetch', 'WebSearch', 'web__run'):
        return 'web'
    if name in ('SendMessage', 'SubagentHandback', 'Agent'):
        return 'msg'
    return 'other'


TOOL_TEXT_KEY = {'SubagentHandback': 'event.tool.handback'}     # tools whose summary is the board's own wording, not the agent's data: the page words them from the dictionary


def tool_brief(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name == 'Bash':
        d = inp.get('description') or ''
        cmd = (inp.get('command') or '').strip().splitlines()
        return d or (cmd[0] if cmd else '')
    if name in ('Read', 'Write', 'Edit', 'NotebookEdit'):
        return short_path(inp.get('file_path') or inp.get('notebook_path'))
    if name in ('Grep', 'Glob'):
        return inp.get('pattern') or ''
    if name == 'WebFetch':
        return inp.get('url') or ''
    if name == 'WebSearch':
        return inp.get('query') or ''
    if name == 'SendMessage':
        return '→ %s: %s' % (inp.get('to', ''), inp.get('summary') or trunc(inp.get('message'), 60))
    if name == 'Agent':
        return inp.get('description') or ''
    if name == 'SubagentHandback':
        return '최종 보고 전달'
    if name == 'TaskStop':
        return inp.get('task_id') or inp.get('shell_id') or ''
    if name == 'ToolSearch':
        return inp.get('query') or ''
    return trunc(json.dumps(inp, ensure_ascii=False), 80)


CD_WORD_RE = re.compile(r'\b(?:cd|pushd|popd)\b')
ASSIGN_WORD_RE = re.compile(r'[A-Za-z_]\w*=')
SHELL_MOVES_MAX = 64


def shell_writes(cmd, cwd):
    """The markdown files a Bash command writes with the shell itself: a redirect of standard output (`> f`, `>> f`, `&> f`, `cat > f <<EOF`) or `tee f`, as absolute normalised
    paths in the order they come. The command is read the way board/link.py reads a launching command: text in quotes, in heredoc bodies and in comments does nothing, a `cd`
    before the redirect moves the folder, and a variable counts only when the command itself gives it one value. What cannot be told (a path with a variable or a substitution
    in it, a relative path with no known folder, a file with two possible names) is left out. `cwd` is the folder the call ran in."""
    if '.md' not in cmd or ('>' not in cmd and 'tee' not in cmd):
        return []
    from . import link as L
    code = L.shell_code(cmd)
    base = os.path.normpath(cwd) if cwd and os.path.isabs(cwd) else None
    env = assigns = None
    moves, budget = bool(CD_WORD_RE.search(cmd)), SHELL_MOVES_MAX      # a `cd` in the command moves the folder; following it is a pass over the text before the redirect, so only so many
    out = []
    n, i = len(code), 0
    while i < n:
        line_end = code.find('\n', i)
        line_end = n if line_end < 0 else line_end
        if i < line_end and not code[i:line_end].strip('x'):                # a line the reader masked: the body of a heredoc
            i = line_end + 1
            continue
        toks, end = L._simple_tokens(code, i)
        i = end + 1
        if not toks:
            continue
        shape = [code[a:b] for a, b in toks]
        if not any('>' in t or t.rsplit('/', 1)[-1] == 'tee' for t in shape):        # words are read only in a command that has a redirect or a tee (a heredoc body is hundreds of lines)
            continue
        words, reds, _stdin = L._classify(cmd, code, toks)
        k = 0
        while k < len(words) and words[k] and (ASSIGN_WORD_RE.match(words[k]) or words[k] in L.CMD_PREFIXES):
            k += 1
        tee = k < len(words) and bool(words[k]) and words[k].rsplit('/', 1)[-1] == 'tee'
        if not reds and not tee:
            continue
        if env is None:
            env, assigns = L.literal_env(cmd, code), L._cmd_assigns(cmd)
        here = base
        if moves:
            budget -= 1
            here = L.shell_cwd(cmd, code, toks[0][0], assigns, base) if budget >= 0 else None
        got = collections.OrderedDict()                               # the word as written -> the paths it can mean
        for r in L.build_redirects(reds, env, here):
            if r.fd == 1 and r.op in ('>', '>>'):
                got.setdefault(r.path_raw, []).append(r.path_resolved)
        if tee:
            options = True
            for w in words[k + 1:]:
                if w is None:
                    continue
                if options and w == '--':
                    options = False
                elif not (options and w.startswith('-') and len(w) > 1):
                    got.setdefault(w, []).extend(p for p, un in L.resolve_path(w, env, here, quoted=False) if not un)
        for paths in got.values():
            found = {os.path.normpath(p) for p in paths if p}
            if len(found) == 1 and None not in paths:
                path = found.pop()
                if path.endswith('.md') and path not in out:
                    out.append(path)
    return out


class Agent:
    def describe(self, description):
        """Sets the description; the short role marker at its head (T1-A, A, opus-1) is the tag, the rest the title."""
        self.description = description
        m = re.match(r'^(T\d+-[A-Z]|[A-Z]|[a-z]+-\d+)\s+(.*)$', description)
        self.tag, self.title = (m.group(1), m.group(2)) if m else ('', description)

    def __init__(self, agent_id, meta):
        self.id = agent_id
        self.description = meta.get('description') or agent_id
        self.agent_type = meta.get('agentType') or ''
        self.model_hint = meta.get('model') or ''
        self.tool_use_id = meta.get('toolUseId')
        # a sub-agent started by a sub-agent (a grandchild): meta carries the parent agent id and the depth. Its completion notice is left in the parent agent's record, not in the main record
        self.parent_agent = meta.get('parentAgentId') if isinstance(meta.get('parentAgentId'), str) else None
        self.depth = meta.get('spawnDepth')
        self.host = None                                 # a sub-agent that a `claude -p` child of the page started: the session id of that child (the page of the top orchestrator shows it under the child)
        self.relay = None                                # a descendant Claude of a page: what its record tells the page's Codex linker (Bash calls, results, TaskStop, notices of background jobs)
        self.child_notes = []                            # completion notices, left in this agent's own record, for the agents it started {ts, task, status, summary, usage}
        self.child_results = []                          # results that this agent's Agent tool calls got back (completions, not ones still being waited for) {ts, tool_use_id, agent_id, status}
        self.describe(self.description)
        self.model = ''
        self.effort = ''
        self.spawn_prompt = None
        self.spawn_ts = None
        self.first_ts = None
        self.last_ts = None
        self.tool_count = 0
        self.tool_counts = collections.Counter()
        self.activity = collections.deque(maxlen=600)   # {ts, kind, name, text}
        self.ticks = []                                  # (ts, category)
        self.texts = collections.deque(maxlen=40)
        self.cwd = ''                                    # working folder from the first record that carries one (Codex: set from its index entry)
        self.received = []                               # {ts, text}
        self.writes = []                                 # {ts, path, id, ok}: ok (True/False) appears when the tool result of that Write/Edit is known, not before. An entry kept at completion (Codex) has neither
        self._by_call = {}                               # Write/Edit/Bash/SendMessage tool_use id -> its entry of `writes`, `shell_writes` or `sent`, until the result comes
        self.shell_writes = []                           # {ts, paths, id, ok}: the markdown files a Bash call writes by a shell redirect or tee (shell_writes()); ok as in `writes`. Kept apart: `writes` is what the tools wrote
        self.redirects = []                              # cli: the output redirects of the launching command (facts.Redirect) that name this child's report
        self.reads = {}                                  # path -> ts
        self.read_log = []                               # (ts, path) in the order read
        self.sent = []                                   # {ts, to, summary, text, id, ok}: the SendMessage calls this agent made; ok as in `writes` (a message that was not delivered is no talk)
        self.pending = {}                                # tool calls with no result yet: id -> {ts, name, text}
        self.tokens = TokenMeter()
        self.last_stop = None
        self.errors = 0
        # filled in from the main record
        self.notifications = []                          # {ts, status, summary, usage}
        self.handbacks = []                              # {ts, text}
        self.orch_msgs = []                              # {ts, summary, text}
        self.stopped_ts = None
        self.provider = 'claude'
        self.origin = 'subagent'                         # subagent | exec (Codex) | cli (claude -p started from Bash)
        self.cli = None                                  # cli: ownership info (an item of LINKS.cli_owners)
        self.talk = []                                   # cli: the conversation exchanged with the orchestrator {ts, kind: in (instruction)|end (last text of a turn), text}
        self._talk_keys = set()                          # so the same line is not counted twice even when the record is read again (Tail from the start)
        self.out_paths = []                              # Codex: planned -o paths {ts, path}
        self.auto_tag = ''                               # model name to show on the page when there is no role name (opus5.5, opus5.5-2)
        self.runs = RS.RunTracker(agent_id)              # the process lifetimes (runs) of this record file, in the order the lines came (board/runstate.py)
        self.ledger = RS.Ledger()                        # what this record knows about the children it launched: completion notices, TaskStop calls, background task ids

    def reset_runs(self):
        """The record is read again from its start (the file shrank): the trackers are rebuilt from the same lines, so nothing is counted twice."""
        self.runs, self.ledger = RS.RunTracker(self.id), RS.Ledger()

    @property
    def name_tag(self):
        """The name to show on the page: the role or report name, else the model name. Matching to debate cells (key) uses only tag."""
        return self.tag or self.auto_tag

    @property
    def key(self):
        """Participant key to match against report file names (T1-C → c, opus-1 → opus1, A → a)."""
        t = self.tag
        m = re.match(r'^T\d+-([A-Za-z])$', t)
        if m:
            return m.group(1).lower()
        return norm_key(t)

    def feed(self, d):
        typ = d.get('type')
        rc = RS.rec(d)                    # the time and the content blocks are read once for the tracker, the ledger and this
        ts = rc.ts
        if isinstance(self.runs, RS.RunTracker):
            self.runs.feed(d, rc)
        else:                                 # a Codex thread's tracker takes the raw rollout line
            self.runs.feed(d)
        self.ledger.feed(d, rc)
        if not self.cwd and isinstance(d.get('cwd'), str):
            self.cwd = d['cwd']
        if typ == 'assistant':
            self._touch(ts)
            if d.get('effort'):
                self.effort = d['effort']
            m = d.get('message') or {}
            if m.get('model') and not m['model'].startswith('<'):
                self.model = m['model']
            self.tokens.add(m)
            if m.get('stop_reason'):
                self.last_stop = m['stop_reason']
            api_error = bool(d.get('isApiErrorMessage')) and RS.error_of(d) is not None      # the line of an API error ("You've hit your limit ..") is the board's news (the state says it), not something the agent said
            for b in rc.blocks:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'text' and b.get('text', '').strip() and not api_error:
                    self._say(ts, b['text'].strip())
                elif b.get('type') == 'tool_use':
                    name, inp = b.get('name', ''), b.get('input') or {}
                    self._tool(ts, name, trunc(tool_brief(name, inp), 240), b.get('id'))
                    if name in ('Write', 'Edit') and inp.get('file_path'):
                        w = {'ts': ts, 'path': os.path.normpath(inp['file_path']), 'id': b.get('id')}
                        self.writes.append(w)
                        if b.get('id'):
                            self._by_call[b['id']] = w
                    elif name == 'Read' and inp.get('file_path'):
                        self.reads[os.path.normpath(inp['file_path'])] = ts
                        self.read_log.append((ts, os.path.normpath(inp['file_path'])))
                    elif name == 'Bash' and isinstance(inp.get('command'), str):
                        if self.relay is not None:
                            self.relay.bash(d, ts, b)
                        paths = shell_writes(inp['command'], d.get('cwd') if isinstance(d.get('cwd'), str) and d.get('cwd') else self.cwd)
                        if paths:
                            w = {'ts': ts, 'paths': paths, 'id': b.get('id')}
                            self.shell_writes.append(w)
                            if b.get('id'):
                                self._by_call[b['id']] = w
                    if name == 'TaskStop' and self.relay is not None:
                        self.relay.stop(inp.get('task_id') or '', ts)
                    if name == 'SendMessage':
                        msg = inp.get('message')
                        m = {'ts': ts, 'to': str(inp.get('to', '')), 'summary': inp.get('summary') or '', 'id': b.get('id'),
                             'text': msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)}
                        self.sent.append(m)
                        if b.get('id'):
                            self._by_call[b['id']] = m
            if self.origin == 'cli' and m.get('stop_reason') == 'end_turn':
                self._cli_end(ts, d, m)
        elif typ == 'attachment':
            a = d.get('attachment') or {}
            if a.get('type') == 'model':
                self.tokens.model((a.get('identity') or {}).get('modelId'))
            elif a.get('type') == 'queued_command' and a.get('commandMode') == 'task-notification':
                self._child_note(parse_ts(a.get('timestamp')) or ts, as_text(a.get('prompt')), a.get('usage'))
                if self.relay is not None:
                    self.relay.notice(as_text(a.get('prompt')), parse_ts(a.get('timestamp')) or ts)
        elif typ == 'user':
            self._touch(ts)
            c = (d.get('message') or {}).get('content')
            if self.origin == 'cli' and not d.get('isMeta'):
                self._cli_prompt(ts, d, c)
            if isinstance(c, str):
                self._user_text(ts, c)
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get('type') == 'tool_result':
                        if self.relay is not None:
                            self.relay.result(b.get('tool_use_id'), d, ts)
                        called = self.pending.pop(b.get('tool_use_id'), None)
                        if called and called['name'] == 'Agent':
                            self._child_result(ts, b, d.get('toolUseResult'))
                        w = self._by_call.pop(b.get('tool_use_id'), None)
                        if w is not None:
                            w['ok'] = not b.get('is_error')         # a failed write is no report and no seat; a message that was not delivered is no talk
                    if b.get('type') == 'tool_result' and b.get('is_error'):
                        self.errors += 1
                        self.ticks.append((ts, 'error'))
                    elif b.get('type') == 'text':
                        self._user_text(ts, b.get('text', ''))

    def _child_note(self, ts, text, usage):
        """The completion notice (task-notification) of an agent it started. Only the tags at the front of the notice text are read (the <result> after them is long)."""
        head = text[:4000]
        task, status, summary = (re.search(r'<%s>([^<]+)</%s>' % (t, t), head) for t in ('task-id', 'status', 'summary'))
        if not task or not RS.is_agent_id(task.group(1)):
            return                                       # a notice of the agent's own Bash background task: no agent finished, so no "work finished" card
        u = usage if isinstance(usage, dict) else {}
        self.child_notes.append({'ts': ts, 'task': task.group(1), 'status': status.group(1) if status else '', 'summary': summary.group(1) if summary else '',
                                 'usage': (u.get('totalTokens'), u.get('toolUses'), u.get('durationMs')) if u else None})

    def _child_result(self, ts, block, result):
        """Its own Agent tool call got a result back. The confirmation that it started in the background (async_launched) is not a completion. Only a status in the closed list counts as complete."""
        r = result if isinstance(result, dict) else {}
        if r.get('isAsync') or r.get('status') == 'async_launched':
            return
        status = 'failed' if block.get('is_error') else r.get('status') if r.get('status') in ('completed', 'failed', 'killed') else None
        if status:
            self.child_results.append({'ts': ts, 'tool_use_id': block.get('tool_use_id'), 'agent_id': r.get('agentId'), 'status': status})

    def _tool(self, ts, name, text, call_id=None):
        """One tool call: count, tick and activity; if there is a call_id it is also recorded as a call waiting for its result. text is already within 240 characters."""
        cat = tool_category(name)
        self.tool_count += 1
        self.tool_counts[name] += 1
        self.ticks.append((ts, cat))
        entry, wait = {'ts': ts, 'kind': 'tool', 'name': name, 'text': text, 'cat': cat}, {'ts': ts, 'name': name, 'text': trunc(text, 160)}
        if name in TOOL_TEXT_KEY:
            entry['text_i18n'] = wait['text_i18n'] = {'key': TOOL_TEXT_KEY[name], 'params': {}}
        self.activity.append(entry)
        if call_id:
            self.pending[call_id] = wait

    def _say(self, ts, text):
        """What the agent said (text with leading and trailing whitespace stripped)."""
        self.texts.append({'ts': ts, 'text': text})
        self.activity.append({'ts': ts, 'kind': 'text', 'name': '', 'text': trunc(text, 400), 'cat': 'other'})

    def _user_text(self, ts, text):
        if self.spawn_prompt is None:
            self.spawn_prompt = text
            self.spawn_ts = self.spawn_ts or ts
            return
        text = strip_reminders(text)
        if text.startswith('<task-notification>'):
            if self.relay is not None:
                self.relay.notice(text, ts)
            if self.origin == 'cli':                     # a `claude -p` child is a session of its own: the notices of the agents it started are in its record as user lines
                u = re.search(r'<subagent_tokens>(\d+)</subagent_tokens>.*?<tool_uses>(\d+)</tool_uses>.*?<duration_ms>(\d+)</duration_ms>', text, re.S)
                self._child_note(ts, text, {'totalTokens': int(u.group(1)), 'toolUses': int(u.group(2)), 'durationMs': int(u.group(3))} if u else None)
            return
        if not text:
            return
        if text.startswith(COORD_PREFIX):
            text = text[len(COORD_PREFIX):].strip()
        self.received.append({'ts': ts, 'text': text})
        self.activity.append({'ts': ts, 'kind': 'msg', 'name': '', 'text': trunc(text, 400), 'cat': 'msg'})
        self.ticks.append((ts, 'recv'))

    def _cli_end(self, ts, d, m):
        """The last text of a turn that ended with end_turn. A record holding only thinking has no text, so it is skipped and the text is caught from the text record of the same message (stop_sequence etc. is not the end of a turn)."""
        said = [b['text'].strip() for b in m.get('content') or []
                if isinstance(b, dict) and b.get('type') == 'text' and (b.get('text') or '').strip()]
        if said:
            self._talk(ts, 'end', m.get('id') or d.get('uuid') or d.get('timestamp'), said[-1])

    def _cli_prompt(self, ts, d, c):
        """The user instruction a claude -p child session received (the first instruction, and one given by continuing with --resume). Tool results, notices and isMeta (image notes) are not instructions."""
        text = c if isinstance(c, str) else '\n'.join(
            b.get('text') or '' for b in (c or []) if isinstance(b, dict) and b.get('type') == 'text')
        text = strip_reminders(text)
        if not text or text.startswith('<task-notification>'):
            return
        if text.startswith(COORD_PREFIX):
            text = text[len(COORD_PREFIX):].strip()
        self._talk(ts, 'in', d.get('uuid') or '%s|%s' % (d.get('timestamp'), text), text)

    def _talk(self, ts, kind, key, text):
        """One cli conversation item. If (kind, key) is the same it is not added again: key is the uuid of the record line or the message id."""
        if (kind, key) not in self._talk_keys:
            self._talk_keys.add((kind, key))
            self.talk.append({'ts': ts, 'kind': kind, 'text': text})

    def _touch(self, ts):
        if ts is None:
            return
        self.first_ts = self.first_ts or ts
        self.last_ts = max(self.last_ts or 0, ts)


def model_numbers(items, base_of):
    """Gives names to agents laid out in order: if several have the same model, base, base-2, base-3, … → {id: name}. Sorting and filtering the targets is up to the caller."""
    seen, out = collections.Counter(), {}
    for a in items:
        base = base_of(a)
        seen[base] += 1
        out[a.id] = base if seen[base] == 1 else '%s-%d' % (base, seen[base])
    return out


def cx_sync_base(meter, partial, thread_total, seen, model):
    """A big rollout whose front part was not read: the tokens of the unread span are got by subtracting what was seen from the last thread_token_usage, and added as one lump."""
    if partial and thread_total:
        base = {k: max(0, (thread_total.get(k) or 0) - seen.get(k, 0)) for k in
                ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens',
                 'reasoning_output_tokens')}
        meter.add_codex('__base__', base, model, ctx=False, calls=0)


def cx_record_usage(h, meter, p, model):
    """One token_usage_record line: add it to the meter, put it on the list to recount if the model is unknown, and update the total of the span read (seen) and the thread's running total.
    h is a CodexAgent or CodexSession that has seen, nomodel and thread_total."""
    u = p.get('usage') or {}
    rid = p.get('response_id') or 'r%d' % len(meter.by_msg)
    meter.add_codex(rid, u, model)
    if not model:
        h.nomodel.append((rid, u))
    cx_usage_add(h.seen, u)
    h.thread_total = p.get('thread_token_usage') or h.thread_total


def cx_reprice(meter, nomodel, model):
    """Recounts calls that were counted without knowing the model, once the model is known (same id, so it is swapped in)."""
    if model and nomodel:
        for rid, u in nomodel:
            meter.add_codex(rid, u, model, ctx=False)
        nomodel.clear()


def cx_sync_guardians(meter, parent_id):
    """Puts the cumulative tokens of guardian (approval review) child threads into the parent total (the subset tokens.guardian). Activity is not read.
    A native sub-agent is a child thread too, but its tokens are its own card's (CodexAgent counts them): only `kind == 'guardian'` comes in here."""
    for g in CODEX.children(parent_id):
        if g.get('kind') == 'guardian' and g['thread_total']:
            meter.add_codex('guardian:' + g['id'], g['thread_total'], g['model'] or 'codex-auto-review',
                            ctx=False, guardian=True, calls=g['calls'])


class ForkSkip:
    """The front of a native sub-agent's rollout is its parent's history, copied when it was spawned: the lines whose ordinal is at most `prefix_ord` (a line with no ordinal
    counts by its place in the file: the meta line is 0, the next 1 ...). None of it is the sub-agent's own turn, activity, tool call, instruction or tokens, so the reader leaves
    it out before anything is fed. Read from the start of the file, the first line behind it ends the copied part for good. A read that starts inside the file (`partial`, the tail
    of a big rollout) may still hold some of it, and nothing says where it began: each line is told by its own ordinal, and a line with none is not known to be the thread's
    own, so it is left out too. `prefix_ord` None: a thread that is no sub-agent has no copy."""

    def __init__(self, prefix_ord=None, partial=False):
        self.prefix = prefix_ord or 0
        self.partial = bool(partial) and prefix_ord is not None
        self.sub = prefix_ord is not None
        self.restart()

    def restart(self):
        """The rollout is read again from its start."""
        self.line = 0
        self.over = not self.sub or (not self.partial and not self.prefix)


def cx_rows(lines, fork=None):
    """The decoded rollout lines of one read (cx_decode), without the lines `fork` (a ForkSkip) says are the parent's copy. A line that cannot be decoded is no row, and
    still has its place in the count."""
    for raw in lines:
        r = cx_decode(raw)
        if fork is not None and fork.partial:
            if r and (r['ord'] is None or r['ord'] <= fork.prefix):
                continue
        elif fork is not None and not fork.over:
            n, fork.line = fork.line, fork.line + 1
            if (r['ord'] if r and r['ord'] is not None else n) <= fork.prefix:
                continue
            fork.over = True
        if r:
            yield r


class CodexAgent(Agent):
    """A Codex thread that a Claude session started with Bash `codex exec` = one agent. A turn = an instruction received, the turn's final answer = the final report."""

    def __init__(self, e, link):
        Agent.__init__(self, e['id'], {})
        self.kind = e.get('kind') or 'root'
        self.provider, self.origin = 'codex', 'subagent' if self.kind == 'sub' else 'exec'      # a native sub-agent thread of Codex, or a `codex exec` thread started from a shell
        self.tag = self.title = self.description = ''
        self.agent_path, self.nick = e.get('agent_path'), e.get('nick')
        self.fork = ForkSkip(e.get('prefix_ord') if self.kind == 'sub' else None)      # the parent's copied history at the front of a sub-agent's rollout is none of its own (set again once `partial` is known)
        self.cuts = []             # sub-agent: the times its parent wrote `SubAgentActivity interrupted` for it
        self.runtime = None        # sub-agent: (id, rollout path) of the root thread, the process it lives in has that rollout open
        self.report_tag = ''       # report name (r<N>/<name>.md). If none, tag = the model name (sol6.1, sol6.1-2)
        self.meta_ts = e['meta_ts']
        self.link = link
        self.path = e['path']
        self.cwd = e['cwd']
        self.turns = []            # {n, start, end, status, error, sha, msg, user, call, bash_ts, out, out_state}. After the events are produced, trim_turns keeps only 200
        self.turn_seq = 0          # monotonically increasing number given to turns (n). Used for event dedup keys and first-turn detection, and unrelated to trimming the kept list
        self.partial = 0
        self.seen = {}
        self.thread_total = None
        self.tokens.fixed_limit = True
        self.model = e['model']
        self.nomodel = []          # calls that came before the model was known (when reading from the end): recounted once the model is known
        self.runs = RS.CodexTracker(e['id'])      # a Codex process lives for one `exec`: a run is a turn (board/runstate.py)

    def reset_runs(self):
        self.runs = RS.CodexTracker(self.id)

    def trim_turns(self):
        """Trims the kept turns to the last TURNS_KEEP. Call it **after** the events have been derived (CodexLinker._derive):
        so that the events of earlier turns are not cut off and lost when a lot was read at once (the initial read)."""
        if len(self.turns) > TURNS_KEEP:
            del self.turns[:-TURNS_KEEP]

    def feed_cx(self, r):
        ts, typ, pt, p = r['ts'], r['type'], r['pt'], r['p']
        self.runs.feed_cx(ts, typ, pt, p)
        if typ == 'session_meta':
            return
        if p is None:                       # a line over 1 MB: only close the tool call that was waiting
            if pt in ('custom_tool_call_output', 'function_call_output'):
                m = CX_CALL_ID_RE.search(r['head'])
                if m:
                    self.pending.pop(m.group(1).decode(), None)
            self._touch(ts)
            return
        if typ == 'turn_context':
            self.model = p.get('model') or self.model
            self.effort = p.get('effort') or self.effort
            cx_reprice(self.tokens, self.nomodel, self.model)
        elif typ == 'token_usage_record':
            cx_record_usage(self, self.tokens, p, self.model)
        elif typ == 'event_msg':
            self._feed_event(ts, pt, p)
        elif typ == 'response_item':
            self._feed_item(ts, pt, p)

    def _feed_event(self, ts, pt, p):
        if pt == 'task_started':
            self._touch(ts)
            if p.get('model_context_window'):
                self.tokens.ctx_limit = p['model_context_window']
            if self.turns and self.turns[-1]['end'] is None and self.turns[-1]['status'] == 'running':
                self.turns[-1]['status'] = 'killed'     # the next turn came without an end record (the process was stopped)
            self.turns.append({'n': self.turn_seq, 'start': ts, 'end': None, 'status': 'running', 'error': None, 'sha': None,
                               'msg': None, 'user': None, 'call': None, 'bash_ts': None, 'out': None,
                               'out_state': None})
            self.turn_seq += 1
        elif pt in ('task_complete', 'turn_aborted') and self.turns and self.turns[-1]['end'] is None:
            t = self.turns[-1]
            self._touch(ts)
            t['end'] = ts
            self.last_stop = 'end_turn'
            if pt == 'turn_aborted':
                t['status'], t['error'] = 'killed', p.get('reason') or 'aborted'
                return
            msg = p.get('last_agent_message') or ''
            t['msg'] = msg
            t['sha'] = hashlib.sha1(msg.encode()).hexdigest() if msg else None
            err = p.get('error')
            if err:
                t['status'] = 'failed'
                t['error'] = trunc('%s %s' % (err.get('codex_error_info') or '', err.get('message') or ''), 300).strip()
                self.errors += 1
                self.ticks.append((ts, 'error'))
            else:
                t['status'] = 'done'
        elif pt == 'token_count':
            w = (p.get('info') or {}).get('model_context_window')
            if w:
                self.tokens.ctx_limit = w
        elif pt == 'item_completed':
            it = p.get('item') or {}
            if it.get('type') == 'CommandExecution':      # Codex's own parsing of the command: the files read (cross-review "read", xread)
                cwd = (it.get('cwd') or '').replace('file://', '') or self.cwd or '/'
                for pc in it.get('parsed_cmd') or []:
                    if pc.get('type') == 'read' and pc.get('path'):
                        path = os.path.normpath(os.path.join(cwd, pc['path']))
                        self.reads[path] = ts
                        self.read_log.append((ts, path))
            elif it.get('type') == 'FileChange':
                for path in it.get('changes') or {}:
                    self.writes.append({'ts': ts, 'path': os.path.normpath(path)})

    def _feed_item(self, ts, pt, p):
        if pt == 'message':
            role = p.get('role')
            if role == 'user':
                text = codex_user_text(p)
                if not text:
                    return
                self._touch(ts)
                if self.turns and self.turns[-1]['user'] is None:
                    self.turns[-1]['user'] = text
                if not self.title:
                    self.title = self.description = trunc(text.strip().splitlines()[0], 80)
                self._user_text(ts, text)
            elif role == 'assistant':
                text = codex_say_text(p)
                if text:
                    self._touch(ts)
                    self._say(ts, text)
        elif pt in ('custom_tool_call', 'function_call'):
            self._touch(ts)
            name, text = codex_call(pt, p)
            self._tool(ts, name, text, p.get('call_id'))
        elif pt in ('custom_tool_call_output', 'function_call_output'):
            self._touch(ts)
            self.pending.pop(p.get('call_id'), None)
