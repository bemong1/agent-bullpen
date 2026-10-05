"""A writer of synthetic Codex 0.160 rollout files: a root thread (TUI or exec), a native sub-agent thread with the forked front part of its parent, a guardian
(approval review) thread. The one place the shape of those lines lives; the scenario builder and tools/synth_home.py both write through it.

Shapes after the records of Codex 0.153-0.160 (every id, path and word here is made up):
  - every line is `{"timestamp", "ordinal", "type", "payload"}` (compact, in that key order); `ordinal` is the line number minus one
  - a shell command is a `custom_tool_call` named `exec` (the input is JavaScript: `tools.exec_command({cmd: ...})`) and its `custom_tool_call_output`; the command
    itself is told once, when its process ends, by an `event_msg` `item_completed` whose item is a `CommandExecution` (`command` = [shell, "-lc", text], `cwd` a
    `file://` URL, `process_id` a string, a `duration`); a command still running, or one that outlived its turn, has no such line
  - a native sub-agent is made by a `function_call` `spawn_agent` (the message argument is ciphertext) followed by `SubAgentActivity` `started` (its id is the call id);
    the end of the sub-agent is a `completed` (id `subagent-completed-<turn id>`), `interrupt_agent` makes an `interrupted` (also after the sub-agent has finished)
  - the rollout of a sub-agent starts with its own `session_meta`, then `subagent_history_start_ordinal` lines that are the parent's front part (its meta, its first
    turn, its first user message), all stamped with the moment of the spawn and with no token count; the sub-agent's own first turn starts right behind them, and the
    first `agent_message` from the parent's path is a forwarding notice (an `input_text` part, about 60 characters, that names the path) beside the instruction
    itself, an `encrypted_content` part: the instruction cannot be read
  - in the parent's rollout the end of a sub-agent is `SubAgentActivity completed` first and the sub-agent's last `agent_message` (the same id as in its own rollout) after it
"""

import json
import os

from .axes import digest
from .build import dump, iso, put

CLI_VERSION = '0.160.0'
MODEL = 'gpt-6.1-sol'
FORK_LINES = 9                      # the parent's front part in a sub-agent's rollout, as the records show it
CIPHER = 'gAAAAA' + 'B' * 58        # what the message argument of spawn_agent looks like: ciphertext, never text


def tid_of(cid, role):
    """A thread id (the shape of Codex's time-ordered ids) made from the case id and a role name."""
    h = digest(cid, 'thread', role, n=32)
    return '019a%s-%s-7%s-8%s-%s' % (h[:4], h[4:8], h[9:12], h[13:16], h[16:28])


def rollout_path(codex_home, tid, t, day='2026/10/01'):
    d = os.path.join(codex_home, 'sessions', *day.split('/'))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'rollout-%s-%s.jsonl' % (iso(t)[:19].replace(':', '-'), tid))


def usage_dict(inp, out, cached=0, reasoning=0):
    return {'input_tokens': inp, 'cached_input_tokens': cached, 'cache_write_input_tokens': 0, 'output_tokens': out, 'reasoning_output_tokens': reasoning, 'total_tokens': inp + out}


class Rollout:
    """One rollout file. `kind` is 'root' (source "cli" for the TUI, "exec" for `codex exec`), 'sub' (a native sub-agent: `depth` 1 under the root, 2 under a sub-agent)
    or 'guardian' (an approval review thread). Lines are collected with a time and written in time order; the ordinal is given when the file is saved."""

    def __init__(self, path, tid, cwd, cid, kind='root', origin='codex-tui', parent=None, agent_path='/root', nick=None, depth=0, root_id=None, version=CLI_VERSION):
        self.path, self.tid, self.cwd, self.cid, self.kind, self.origin = path, tid, cwd, cid, kind, origin
        self.version = version
        self.parent, self.agent_path, self.nick, self.depth = parent, agent_path, nick, depth
        self.root_id = root_id or tid
        self.rows = []                      # (time, order, line dict without ordinal)
        self.fork_lines = 0                 # filled by fork()
        self.t_meta = None
        self.total = usage_dict(0, 0)       # the thread's running token total
        self.n_usage = 0
        self.last_t = 0.0

    # ---- lines ----
    def _add(self, t, typ, payload=None, metadata=None, **extra):
        d = {'timestamp': iso(t), 'type': typ}
        if payload is not None:
            d['payload'] = payload
        if metadata is not None:
            d['metadata'] = metadata
        d.update(extra)
        self.rows.append((t, len(self.rows), d))
        self.last_t = max(self.last_t, t)
        return d

    def _ev(self, t, payload):
        return self._add(t, 'event_msg', payload)

    def _item(self, t, turn, item, started=None):
        ms = int(t * 1000)
        return self._ev(t, {'type': 'item_completed', 'thread_id': self.tid, 'turn_id': turn, 'item': item, 'started_at_ms': int((started if started is not None else t) * 1000),
                            'completed_at_ms': ms})

    def _meta_payload(self, t, tid=None, origin=None, cwd=None):
        src = {'subagent': {'thread_spawn': {'parent_thread_id': self.parent, 'depth': self.depth, 'agent_path': self.agent_path, 'agent_nickname': self.nick, 'agent_role': None}}} \
            if self.kind == 'sub' else ({'subagent': {'other': 'guardian'}} if self.kind == 'guardian' else ('exec' if self.origin == 'codex_exec' else 'cli'))
        tid = tid or self.tid
        p = {'creator_user_id': 'user-' + digest(self.cid, 'user', n=8), 'creator_account_id': 'acct-' + digest(self.cid, 'acct', n=8), 'session_id': self.root_id if self.kind == 'sub' else tid, 'id': tid}
        if self.kind != 'root':
            p.update(forked_from_id=self.parent, parent_thread_id=self.parent)
        p.update(timestamp=iso(t), cwd=cwd or self.cwd, runtime_workspace_roots=[cwd or self.cwd], originator=origin or self.origin, cli_version=self.version, source=src,
                 thread_source={'root': 'user', 'sub': 'subagent', 'guardian': 'guardian_review'}[self.kind], model_provider='openai',
                 base_instructions={'text': 'Synthetic base instructions.', 'provenance': {'type': 'model', 'model': MODEL}}, history_mode='paginated')
        if self.kind == 'sub':
            p.update(agent_nickname=self.nick, agent_path=self.agent_path, multi_agent_version='v2', subagent_history_start_ordinal=self.fork_lines)
        return p

    def meta(self, t):
        """The first line. For a sub-agent it is written by fork() (the ordinal of the end of the front part is not known before)."""
        self.t_meta = t
        if self.kind != 'sub':
            self._add(t, 'session_meta', self._meta_payload(t))

    def fork(self, t, parent, parent_turn, parent_text, parent_started):
        """A sub-agent's own meta and the front part it inherits from `parent` (a Rollout): the parent's meta, its first turn and its first user message, all stamped `t`,
        the moment of the spawn. FORK_LINES lines in all; own `task_started` follows at t + 0.009."""
        self.t_meta = t
        self.fork_lines = FORK_LINES
        self._add(t, 'session_meta', self._meta_payload(t))
        pm = dict(parent._meta_payload(parent.t_meta))
        lines = [('session_meta', pm, None),
                 ('event_msg', {'type': 'task_started', 'turn_id': parent_turn, 'root_turn_id': parent_turn, 'started_at': int(parent_started), 'model_context_window': 258400,
                                'collaboration_mode_kind': 'default'}, None),
                 ('response_item', {'type': 'message', 'id': 'msg_' + digest(self.cid, 'dev1', n=16), 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'Synthetic developer notes.'}]}, None),
                 ('world_state', {'full': True, 'state': {'model': MODEL, 'multi_agent_mode': {'mode': 'collaborative'}}}, None),
                 ('turn_context', {'turn_id': parent_turn, 'root_turn_id': parent_turn, 'cwd': self.cwd, 'model': MODEL, 'effort': 'medium', 'multi_agent_version': 'v2'}, None),
                 ('response_item', {'type': 'message', 'id': 'msg_' + digest(self.cid, 'inh', n=16), 'role': 'user', 'content': [{'type': 'input_text', 'text': parent_text}]},
                  {'client_authored': False, 'user_input_order': 0, 'inherited_user_message': True}),
                 ('response_item', {'type': 'message', 'id': 'msg_' + digest(self.cid, 'dev2', n=16), 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'Synthetic role notes.'}]}, None),
                 ('event_msg', {'type': 'thread_settings_applied', 'thread_id': self.tid, 'thread_settings': {'model': MODEL, 'cwd': self.cwd}}, None),
                 ('response_item', {'type': 'message', 'id': 'msg_' + digest(self.cid, 'dev3', n=16), 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'Synthetic fork notes.'}]}, None)]
        assert len(lines) == FORK_LINES
        for typ, payload, metadata in lines:
            self._add(t, typ, payload, metadata)

    # ---- a turn ----
    def task_started(self, t, turn):
        return self._ev(t, {'type': 'task_started', 'turn_id': turn, 'root_turn_id': turn, 'started_at': int(t), 'model_context_window': 258400, 'collaboration_mode_kind': 'default'})

    def turn_context(self, t, turn):
        return self._add(t, 'turn_context', {'turn_id': turn, 'root_turn_id': turn, 'cwd': self.cwd, 'model': MODEL, 'effort': 'medium', 'multi_agent_version': 'v2'})

    def user(self, t, text, turn):
        """The user's message that opens a turn (a root thread): the response item and the finished `UserMessage` item."""
        self._add(t, 'response_item', {'type': 'message', 'id': 'msg_' + digest(self.cid, self.tid, 'user', t, n=16), 'role': 'user', 'content': [{'type': 'input_text', 'text': text}]},
                  {'client_authored': False, 'user_input_order': 0})
        self._item(t + 0.001, turn, {'type': 'UserMessage', 'id': 'um_' + digest(self.cid, self.tid, 'um', t, n=16), 'content': [{'type': 'text', 'text': text, 'text_elements': []}]})

    def say(self, t, text, turn, final=False):
        self._add(t, 'response_item', {'type': 'message', 'id': 'msg_' + digest(self.cid, self.tid, 'say', t, n=16), 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]})
        self._item(t + 0.001, turn, {'type': 'AgentMessage', 'id': 'am_' + digest(self.cid, self.tid, 'am', t, n=16), 'content': [{'type': 'Text', 'text': text}],
                                     'phase': 'final_answer' if final else 'commentary'})

    def complete(self, t, turn, last=None, started=None):
        return self._ev(t, {'type': 'task_complete', 'turn_id': turn, 'last_agent_message': last, 'started_at': int(started if started is not None else t - 5), 'completed_at': int(t),
                            'duration_ms': 5000})

    def aborted(self, t, turn, reason='interrupted'):
        return self._ev(t, {'type': 'turn_aborted', 'turn_id': turn, 'reason': reason})

    def usage(self, t, turn, inp, out, cached=0, reasoning=0, rates=None, response_id=None):
        """A token_usage_record (and the `token_count` line that follows it, with the account's `rate_limits` when `rates` is given)."""
        u = usage_dict(inp, out, cached, reasoning)
        for k in self.total:
            self.total[k] += u[k]
        self.n_usage += 1
        self._add(t, 'token_usage_record', {'thread_id': self.tid, 'turn_id': turn, 'session_id': self.root_id, 'root_turn_id': turn,
                                           'response_id': response_id or 'resp_' + digest(self.cid, self.tid, 'usage', self.n_usage, n=16),
                                           'usage': u, 'turn_token_usage': dict(u), 'thread_token_usage': dict(self.total)})
        count = {'type': 'token_count', 'info': {'total_token_usage': dict(self.total), 'last_token_usage': dict(u), 'model_context_window': 258400}}
        if rates is not None:
            count['rate_limits'] = rates
        self._ev(t + 0.001, count)

    # ---- a shell command ----
    def shell(self, t, cmd, cwd, call_id, turn, end=None, out_at=None, pid=None, exit_code=0, text='ok', running=False, parsed=None):
        """A command through the exec tool. The call is written at `t`; its output comes back at `out_at` (None: the cell has not returned yet); the CommandExecution
        line comes at `end`, when the process ends (None: no such line, the process is still running, or it outlived the turn). `pid` is the process number the
        records call `process_id` (a string). `running`: the cell's output is the one of a command that is still running (`Script running with cell ID`): its record is to come.
        Returns the item id."""
        js = 'const r = await tools.exec_command({cmd: %s, workdir: %s, yield_time_ms: 1000});\ntext(r.output);' % (json.dumps(cmd), json.dumps(cwd))
        self._add(t, 'response_item', {'type': 'custom_tool_call', 'id': 'ctc_' + digest(self.cid, call_id, n=16), 'status': 'completed', 'call_id': call_id, 'name': 'exec', 'input': js})
        item = 'item_' + digest(self.cid, call_id, 'cmd', n=16)
        if end is not None:
            self.shell_done(cmd, cwd, call_id, turn, t, end, pid=pid, exit_code=exit_code, text=text, parsed=parsed)
        if out_at is not None:
            self._add(out_at, 'response_item', {'type': 'custom_tool_call_output', 'id': 'cto_' + digest(self.cid, call_id, n=16), 'call_id': call_id,
                                                'output': [{'type': 'input_text', 'text': 'Script running with cell ID 7' if running else 'Script completed'},
                                                           {'type': 'input_text', 'text': '' if running else text}]})
        return item

    def shell_done(self, cmd, cwd, call_id, turn, t, end, pid=None, exit_code=0, text='ok', parsed=None):
        """The `CommandExecution` line of a command that was called at `t`: written at `end`, when its process ended. `parsed`: Codex's own reading of the command
        (`parsed_cmd`), for instance a file it read: [{'type': 'read', 'cmd': ..., 'name': ..., 'path': ...}]."""
        dur = max(0.0, end - (t + 0.05))
        self._item(end, turn, {'type': 'CommandExecution', 'id': 'item_' + digest(self.cid, call_id, 'cmd', n=16),
                               'process_id': str(pid if pid is not None else 40000 + int(digest(call_id, n=3), 16) % 9000), 'command': ['/bin/bash', '-lc', cmd],
                               'cwd': 'file://' + cwd, 'parsed_cmd': parsed or [{'type': 'unknown', 'cmd': cmd}], 'source': 'unified_exec_startup',
                               'status': 'completed' if exit_code == 0 else 'failed', 'stdout': text, 'stderr': '', 'aggregated_output': text, 'exit_code': exit_code,
                               'duration': {'secs': int(dur), 'nanos': int((dur % 1) * 1e9)}, 'formatted_output': text}, started=t + 0.05)

    def file_change(self, t, path, content, turn):
        """A file the thread wrote with a patch: a `FileChange` item (what a Codex thread leaves when it writes a report)."""
        self._item(t, turn, {'type': 'FileChange', 'id': 'fc_' + digest(self.cid, self.tid, 'fc', path, n=16), 'changes': {path: {'type': 'add', 'content': content}},
                             'status': 'completed', 'stdout': 'Success. Updated the following files:\nA %s' % os.path.basename(path), 'stderr': ''})

    # ---- native sub-agents ----
    def spawn(self, t, call_id, turn, task_name, agent_thread_id, agent_path, ok=True):
        """`spawn_agent` and, when it worked, the `SubAgentActivity started` right behind it (its id is the call id) and the call's output. A failed spawn (a name the
        rules refuse) has the call and a plain-text output and no activity line."""
        self._add(t, 'response_item', {'type': 'function_call', 'id': 'fc_' + digest(self.cid, call_id, n=16), 'name': 'spawn_agent', 'namespace': 'collaboration',
                                       'arguments': json.dumps({'task_name': task_name, 'message': CIPHER}), 'call_id': call_id})
        if ok:
            self._item(t + 0.05, turn, {'type': 'SubAgentActivity', 'id': call_id, 'kind': 'started', 'agent_thread_id': agent_thread_id, 'agent_path': agent_path})
            out = json.dumps({'task_name': agent_path})
        else:
            out = 'invalid task_name: %s' % task_name
        self._add(t + 0.07, 'response_item', {'type': 'function_call_output', 'id': 'fco_' + digest(self.cid, call_id, n=16), 'call_id': call_id, 'output': out})

    def sub_completed(self, t, turn, sub_turn, agent_thread_id, agent_path):
        self._item(t, turn, {'type': 'SubAgentActivity', 'id': 'subagent-completed-' + sub_turn, 'kind': 'completed', 'agent_thread_id': agent_thread_id, 'agent_path': agent_path})

    def interrupt(self, t, call_id, turn, agent_thread_id, agent_path):
        """`interrupt_agent` and its `SubAgentActivity interrupted` (its id is the call id); it also comes after the sub-agent has finished."""
        self._add(t, 'response_item', {'type': 'function_call', 'id': 'fc_' + digest(self.cid, call_id, n=16), 'name': 'interrupt_agent', 'namespace': 'collaboration',
                                       'arguments': json.dumps({'target': agent_path}), 'call_id': call_id})
        self._item(t + 0.004, turn, {'type': 'SubAgentActivity', 'id': call_id, 'kind': 'interrupted', 'agent_thread_id': agent_thread_id, 'agent_path': agent_path})
        self._add(t + 0.02, 'response_item', {'type': 'function_call_output', 'id': 'fco_' + digest(self.cid, call_id, n=16), 'call_id': call_id, 'output': '{"ok":true}'})

    def wait_agent(self, t, t_end, call_id, turn, receivers):
        """`wait_agent` and the `CollabAgentToolCall` item it ends with: not an event of the team."""
        self._add(t, 'response_item', {'type': 'function_call', 'id': 'fc_' + digest(self.cid, call_id, n=16), 'name': 'wait_agent', 'namespace': 'collaboration',
                                       'arguments': '{"timeout_ms":60000}', 'call_id': call_id})
        self._item(t_end, turn, {'type': 'CollabAgentToolCall', 'id': call_id, 'tool': 'wait', 'status': 'completed', 'sender_thread_id': self.tid, 'receiver_thread_ids': list(receivers),
                                 'receiver_agents': [], 'agents_states': {}}, started=t)
        self._add(t_end + 0.003, 'response_item', {'type': 'function_call_output', 'id': 'fco_' + digest(self.cid, call_id, n=16), 'call_id': call_id, 'output': '{"status":"done"}'})

    def agent_message(self, t, author, recipient, text, cipher=False, turn=None, msg_id=None):
        """A message between agents (a response item). The first message a sub-agent gets from its parent has a plain forwarding notice as its text (it names the path) and the
        instruction itself as an `encrypted_content` part (`cipher`). A message a sub-agent hands to its parent is in the sub-agent's rollout and, with the same id, in the
        parent's; `turn` is the turn of the side that receives it."""
        content = [{'type': 'input_text', 'text': text}]
        if cipher:
            content.append({'type': 'encrypted_content', 'encrypted_content': CIPHER})
        payload = {'type': 'agent_message', 'id': msg_id or 'ag_' + digest(self.cid, self.tid, 'msg', t, n=16), 'author': author, 'recipient': recipient, 'content': content}
        if turn:
            payload['internal_chat_message_metadata_passthrough'] = {'turn_id': turn, 'create_time': t}
        self._add(t, 'response_item', payload, {'client_authored': False, 'user_input_order': 0})

    # ---- file ----
    def lines(self, before=None):
        out = []
        for i, (_, _, d) in enumerate(sorted((r for r in self.rows if before is None or r[0] <= before), key=lambda r: r[:2])):
            line = {'timestamp': d['timestamp'], 'ordinal': i}
            line.update({k: v for k, v in d.items() if k != 'timestamp'})
            out.append(line)
        return out

    def save(self, before=None):
        """Writes the rollout. `before`: only the lines up to that time (the file as it was when the board first looked, the rest still to be written)."""
        ls = self.lines(before)
        put(self.path, ''.join(dump(d) + '\n' for d in ls), max((r[0] for r in self.rows if before is None or r[0] <= before), default=self.last_t))
        return self.path
