"""The truth of a case: what a correct reader of the records may say, computed from the *meaning* of the axis values. It never imports board/.

Honest misses are the right answer: what the records cannot tell is `none` / `unknown` / held, and a false link is worse than a missed one.

Values that name a session or an agent are symbolic: '@orch' is the main session, '@sub' the sub-agent, '@mid' the `claude -p` child that launches the
target, '@child' the target itself ... run.py resolves them against the ids the builder made.

Evidence ranks and what each rank speaks to:

    rank 1  subagent meta / `out` (the launch call's redirect file holds the child's session id)      tree, node, call   certain
    rank 2  env / proc lineage / cache (links.json)                                                   tree only          certain
    rank 3  content: the instruction (>= 32 chars) is in the launcher's records AND a launch call ran at that moment   tree, node, call   certain
    rank 4  content_short: a shorter instruction equals the literal argument of a running launch call  tree, node, call   guess
    rank 5  time: the command reader sees a `claude -p` at a command position, same folder, one candidate  tree, node, call   guess

The relation is `certain` when its best tree evidence has rank <= 3. The node comes from the best node-capable evidence (ranks 1, 3, 4, 5); an environment or
a lineage alone names the tree, never the node. Ties are held, not broken by a lower rank.

Choices the truth makes beyond the ranks:
  1. A run closed by `cost-state` with no end turn and no explicit notice is `interrupted/exited`; a record that just stops with the process gone is
     `ended/crash`, with the process unknowable `unknown` - except a `claude -p` child whose launching background call has a notice (exit 137): the
     parent's record says the process ended, so `ended/crash` stands without `ps`.
  2. Refutation: a launch whose literal prompt argument differs from the child's first instruction cannot be its parent, and the time rule (rank 5) must
     not link them. The same goes for a competing launch (another orchestrator in the same folder) whose literal argument is another text: it drops out of the
     candidates, so the one that is left is the parent (a guess). Two launches with the same literal text stay a tie.
  3. Oracle choices behind the twins `cwd_mismatch` and `echo_only`: a launch call whose known folder differs
     from the child's recorded folder cannot be its parent even when the words match (the bait `cwd_mismatch` keeps the words and changes only the folder), and an
     `echo 'claude -p "..."'` is not a launch even though it names `claude` and ends within the 10 s slack (the bait `echo_only` changes only that).
  4. `by` of a resumed run follows the same ranks as the tree; a literal `--resume <id>` or `--session-id <child id>` in a running call is rank 3.
  5. A variable in a sub-agent's instruction text is not resolved; only a variable assigned in the launch command's own scope (`-o "$D/..."`, `> "$D/..."`) is resolved.
  6. A description tag alone seats nobody (a quoted, negated or failed-write path neither; they are `debate_in_misc`). A seat needs the agent's own write intent,
     its own marker, or a successful write of its own; with a successful write the spelled name of the written file is the seat (`B_gate`, not `B`, when both
     files exist). Two writers of one seat at the same rank leave it held (`seat_tie_held`); two debates that fit one path leave it held (`path_ambiguous`);
     a flat review whose guide declares no reviewers shows a title only (`declaration_missing`).
  7. A Codex overload is `interrupted/api_error`, like a Claude 529 (`apiErrorStatus` first); a 400 is `failed/api_error`. A sub-agent's limit is judged by its
     own 429 line, never by the words of the notice.
  8. `format_drift` is a known field gone inside the observed range, or a version outside it (older than 2.1.235, newer than 2.1.286); a line type the board
     does not read is ignored, no diagnostic.
  9. A `claude -p` child that launches a `claude -p` or `codex exec` is the tree of that grand-child; the screen puts it under that child on the top orchestrator's page.
 10. The environment of a macOS process is readable through `ps -E` (same user), the lineage through the `ps` parent table; neither when `ps` fails.
 11. An instruction file that somebody else wrote (`author`): the text is in the author's records, not in the launcher's, and the author's running call (a server,
     a build, a test run, or the launch of an unrelated child with other words) is no launch of this child. The launcher is the session whose launch call reads the
     file (time rule: a guess, or the process while it is alive); the author is not its parent and neither is the author's node. `content_author_differs` may be said.
 12. An output file proves nothing when a launch call of another session wrote the same path first and a later run wrote over it (`out=overwritten`): the launcher
     is the session whose call carries the instruction.
 13. A folder the command does not tell (`cd "$VAR"`) cannot be compared with the child's: the time rule does not link a stranger on that basis; `pushd X` is `cd X`.
 14. A Codex thread is a child of a `codex exec` call only when the words of the call (when it has a complete literal) and its folder (`-C`) fit; a stranger thread
     beside a real launch, other words, another folder, a call long gone or an echo link nothing.
 15. A description tag alone seats nobody and neither does a write path that cannot be resolved: held with `path_unresolved`. With a successful write of its
     own the spelled name of the written file is the seat.
 16. A reader that only names the report (`B's report: P`, `P의 주장을 검증해서 답으로만 알려줘`) is a reader; a marker that is only quoted seats nobody.
 17. The debate list is exactly the debate folders on disk (a folder that only holds the common brief of several topics is none); the seats of one agent are the
     whole set the board shows (an extra seat is wrong); a file `-o` writes beside the report is no second seat; `r1/` and `r01/` side by side are two files.
 18. A place is a file: the debate folder, the round, the seat and the path of the file below the debate folder, with the spelling of the round folder. A launch
     that writes the report's own name into the other spelling (`-o r01/B.md` while the instruction says `r1/B.md`) has written another file: the seat stays
     where the instruction put it and its cell follows that file only (no file there: not submitted, whatever sits in the other folder).
 19. A write counts when it succeeded, whether it was made by the Write tool, a patch, or a Bash command (redirect, `tee`, heredoc); a Bash command that fails, or
     one that only mentions the report and writes somewhere else (`wc -l REPORT > count.txt`), is not a write of the report.
 20. A session that wrote the instruction but started nothing (a person typed the launch in a terminal) is not the parent of the child: no link, whatever the author
     was running (a candidate or a diagnostic may say who wrote the words). An author running a script whose body cannot be read may have started it: an
     uncertain link is the most that can be said there, never a certain one.
 21. A guide declares a participant by a letter and a description (`**A — flow**`); a bold number (`**C-12**`, `**Codex X-2 · Claude C-36**`) names a finding, not a
     participant. A folder with a guide that declares no participant, no reviewer, no report path and has no round folder (an editing job beside the topics of a
     review, `edits=beside`) is listed as a title and has no row and no round.
"""

from .axes import EDIT_DIR, FILE_WAYS, FLAW_VERSION, RESUMABLE, ROUND_DIR, WAY, aux_file_exists, launcher_writes


class Truth:
    def __init__(self):
        self.subjects = {}              # role -> {field: value}
        self.diag = []                  # (code, role)
        self.diag_params = {}           # (code, role) -> {param: value} the entry must carry (only the ones a case can know)
        self.allowed = []               # (code, role) the truth neither asks for nor rules out here: shown or not, it is not graded
        self.accepted = {}              # (role, field) -> values that are as right as the truth when the records leave the answer open
        self.forbid = []                # (role, field, value): the board must not show this

    def set(self, subject, /, **fields):
        self.subjects.setdefault(subject, {}).update(fields)

    def expect(self, code, subject='child', **params):
        if (code, subject) not in self.diag:
            self.diag.append((code, subject))
        if params:
            self.diag_params.setdefault((code, subject), {}).update(params)

    def accept(self, subject, field, *values):
        self.accepted.setdefault((subject, field), set()).update(values)

    def allow(self, code, subject='child'):
        self.allowed.append((code, subject))

    def deny(self, subject, field, value):
        self.forbid.append((subject, field, value))


def expect_proc_unknown(T):
    """`ps` unusable (`os=mac_nops`): no process can be told, so the board says so, in every case - a bait, a launch with no record and a child that is not
    linked to the page included. The board says it per agent (the judgment of each agent that has no process view), so the subject is the unit
    it is said about: the participant under test when it is an agent of the page (a subject of the truth that is linked to a tree, if the truth names one),
    otherwise the page itself - the orchestrator, whose own process is the one nobody can see."""
    f = T.subjects.get('child')
    on_page = f is not None and f.get('tree', True) is not None
    T.expect('proc_unknown', 'child' if on_page else 'orch')


# ---------------------------------------------------------------------------------------------------------------------
# which diagnostics a truth speaks to
# ---------------------------------------------------------------------------------------------------------------------
# A bundle varies the axes of its own domain only: an affiliation case does not say what the state judgment ought to notice, a state case does not say how its
# agent is linked, a debate case says neither. The board does emit the other domains' diagnostics there (a content link in a state case is a `content_only`
# too); the truth just does not assert them, so an observed code outside the bundle's scope is neither right nor wrong - it is not graded. Without this the
# state and debate bundles would show 63 `wrong` cells that are no mistake (measured: `content_only` 22 + `orphan_launch` 25 in `sta`, `content_only` 16 in `deb`). The integrated bundle (`cpl`) varies all three domains, so every code is in scope.
# This is a decision about what the answer covers, so it lives here and not in the adapter that reads the board (observe.py reports every entry it sees).
AFF_CODES = frozenset(('evidence_conflict', 'content_author_differs', 'content_only', 'ambiguous_content', 'node_unresolved', 'orphan_launch', 'fingerprint_incomplete',
                       'path_unresolved', 'invisible_child', 'proc_unknown'))
STA_CODES = frozenset(('limit_group', 'not_resumed', 'silent_live', 'torn_lines', 'multi_process', 'format_drift', 'parse_errors', 'stray_notice', 'proc_unknown',
                       'cache_error', 'orphan_launch'))
DEB_CODES = frozenset(('path_unresolved', 'path_ambiguous', 'alias_collision', 'seat_tie_held', 'debate_in_misc', 'declaration_missing', 'listing_capped', 'orphan_launch'))
BUNDLE_CODES = {'aff': AFF_CODES, 'sta': STA_CODES, 'deb': DEB_CODES}


def diag_scope(case):
    """The diagnostic codes the truth of `case` asserts (an observed code outside it is not graded), or None for every code."""
    return BUNDLE_CODES.get(case.bundle)


# ---------------------------------------------------------------------------------------------------------------------
# affiliation
# ---------------------------------------------------------------------------------------------------------------------
def _out_var_outside_script(v):
    """`OUT=...; bash run.sh ...` where the script writes `> \"$OUT\"`: the variable is set by the caller and not exported, so it is not in the script's own scope."""
    return v['src'] == 'posarg' and v['out'] == 'json_var'


def aff_evidence(v):
    """The evidence that exists for a launch, by kind: {out, env, proc, content, short, time, id_literal} -> bool."""
    way = WAY[v['way']]
    seen = v['seen'] not in ('ended_unseen', 'restart_unseen')            # the board looked while the process was alive
    ev = {}
    ev['out'] = v['out'] in ('json_file', 'json_var') and way['redirect'] and not _out_var_outside_script(v)
    readable = v['os'] != 'mac_nops'                                       # `ps` unusable: neither the environment nor the lineage can be read
    ev['env'] = way['env'] and seen and readable
    ev['proc'] = way['lineage'] and seen and readable
    in_records = (v['src'] != 'absent' or v['decoy'] == 'same_n') and v['author'] == 'self'      # the repeated launches of the same text are recorded too; the text an author wrote is in the author's records
    literal_arg = v['via'] == 'arg' and v['src'] in ('call', 'posarg') and v['way'] != 'pysub'
    long_text = v['decoy'] != 'short'
    tie = v['decoy'] == 'twin_text'                                         # two launches with the same literal text: a tie, held
    ev['content'] = in_records and long_text and not tie
    ev['short'] = (not long_text) and literal_arg
    # a competing launch with another literal text is refuted and drops out; a folder the command does not tell (`cd "$VAR"`) or a link cannot be compared with the child's
    ev['time'] = way['time'] and v['cwd'] not in ('symlink', 'var') and not tie
    ev['id_literal'] = v['form'] in ('resume', 'session_id') and v['way'] not in FILE_WAYS and v['src'] != 'posarg'    # `--resume <id>` / `--session-id <id>` in the call
    if v['decoy'] == 'switch':
        ev['env'] = False                          # the board refuses an environment whose Claude process now runs another session
    return ev


def best_rank(ev):
    ranks = []
    if ev['out']:
        ranks.append(1)
    if ev['env'] or ev['proc']:
        ranks.append(2)
    if ev['content'] or ev['id_literal']:
        ranks.append(3)
    if ev['short']:
        ranks.append(4)
    if ev['time']:
        ranks.append(5)
    return min(ranks) if ranks else None


def node_capable(ev):
    return ev['out'] or ev['content'] or ev['short'] or ev['time'] or ev['id_literal']


def aff_truth(case):
    T = _aff_truth(case)
    if case.v['os'] == 'mac_nops':
        expect_proc_unknown(T)
    elif case.v['os'] == 'mac' and case.v['target'] != 'cli':
        T.allow('proc_unknown')                        # `ps` lists a Codex process but not the thread's file: a board that cannot tie them says so, one that can does not
    return T


def _aff_truth(case):
    v = case.v
    T = Truth()
    sp = v['spawner'] if v['bait'] in ('none', 'foreign') else 'main'      # the builder launches most baits from the main session (scene_aff.make_launcher)
    tree = '@mid' if sp == 'grand' else '@orch'
    node = '@sub' if sp == 'sub' else None
    if v['out'] == 'overwritten':
        tree, node = '@orch2', None                                          # the launcher is a peer session; the page's main session only has the same output path
        T.allow('content_only')                                              # the child is on the peer's page, not on this one: no entry of this page is about it
    if v['target'] != 'cli':
        return codex_truth(case, T, tree, node)
    if v['bait'] != 'none':
        T.set('child', tree=None, rule_class='none')
        T.deny('child', 'tree', tree)
        # A sequential call (or a loop) that is still running and already has a child for its first launch cannot tell a late launch from one that never comes
        still_running_seq = v['seen'] == 'live' and (v['timing'] == 'seq_late' or v['way'] == 'loop')
        if v['bait'] in ('cwd_mismatch', 'text_mismatch', 'too_old') and not still_running_seq:
            T.expect('orphan_launch', 'orch', n=1)     # a real `claude -p` launch with no child of its own behind it; echo is no launch
        if v['bait'] == 'foreign' and v['out'] in ('json_var_ext', 'json_var') and v['cwd'] == 'var':
            T.allow('path_unresolved')                 # the launch is not refuted by its words here, so what it redirects to may be said
        if v['bait'] == 'foreign' and not still_running_seq:
            if v['cwd'] == 'var':
                T.allow('orphan_launch', 'orch')       # a launch whose folder and words cannot be compared with the stranger's: whether it is counted as childless is not asked
            else:
                T.expect('orphan_launch', 'orch')      # the launch of the positive stays; its way decides how many launches there are
        return T
    if v['form'] in ('nopersist', 'deleted'):
        T.expect('orphan_launch', 'orch', n=1)
        if v['form'] == 'nopersist' and v['seen'] == 'live':
            T.expect('invisible_child', 'orch')
        return T                                   # no record, no card, no seat: nothing else to say
    if v['starter'] == 'person':
        return _person_truth(v, T)
    ev = aff_evidence(v)
    rank = best_rank(ev)
    multi_node = v['spawner'] == 'sub' or v['decoy'] == 'subnoise' or v['author'] == 'sub'
    if v['form'] == 'resume':
        _resume_truth(v, T, ev, rank, tree, node, multi_node)
    elif rank is None:
        T.set('child', tree=None, rule_class='none')
        if v['decoy'] == 'twin_text':
            T.set('child', unlinked='ambiguous')
    else:
        T.set('child', tree=tree, rule_class='certain' if rank <= 3 else 'guess')
        if node_capable(ev):
            T.set('child', node=node)
        else:
            T.set('child', node=None)
            if multi_node:
                T.expect('node_unresolved')
    if rank == 3 and not ev['out'] and not ev['id_literal'] and v['form'] != 'resume' and v['out'] != 'overwritten':
        T.expect('content_only')
    if v['decoy'] == 'twin_text' and v['src'] != 'absent' and not (ev['out'] or ev['env'] or ev['proc']):
        T.expect('ambiguous_content')                  # a content tie that had to decide; with the tree already named by ranks 1-2 nothing was left to compare
    if (v['out'] == 'json_var_ext' and WAY[v['way']]['redirect']) or _out_var_outside_script(v):
        T.expect('path_unresolved')
    if v['out'] == 'overwritten':
        # an output file that holds the child's id proves nothing when another launch call wrote the same path first (a path shared by two calls, and a later
        # run that wrote over it): the launcher is the peer that started the child (its instruction is in that call), `by` of the later run is the peer as well
        T.deny('child', 'tree', '@orch')
        T.subjects['child'].pop('node', None)          # the page lists no card for it: its node and `by` are not shown here
    if v['author'] != 'self':
        # the author's running call is no launch of this child: the text it wrote proves nothing about who launched it (the launch only reads a file)
        if v['author'] == 'peer':
            T.deny('child', 'tree', '@author')
        T.allow('content_author_differs')
    if v['decoy'] == 'switch':
        T.expect('evidence_conflict')
        T.deny('child', 'tree', '@orch_new')
    if v['decoy'] == 'paste':
        T.deny('paste', 'tree', '@orch')
    if v['decoy'] == 'sidmention':
        T.deny('child', 'tree', '@mention')
    return T


def _person_truth(v, T):
    """A person started the child in a terminal and some session only wrote the instruction file: no call in any record launched it, so the author (or the page's
    main session, which has nothing to do with it at all) is not its parent. The words in the author's records tell who wrote them, not who started the child:
    the board may list the author as a candidate or say `content_author_differs`, but must not link. When the author was running a script whose body cannot be
    read, that script may have started the child: no link, or an uncertain one (never a certain one), and the call may count as a launch with no child."""
    T.allow('content_author_differs')
    peer = v['author'] == 'peer'
    if v['busy'] in ('script_unread', 'script_var'):
        T.deny('child', 'rule_class', 'certain')
        T.allow('orphan_launch', 'orch')
        if peer:
            T.deny('child', 'tree', '@orch')               # another session's script, not the page's
        return T
    T.set('child', tree=None, rule_class='none')
    T.deny('child', 'tree', '@orch')                       # the page's main session, or the main session of the sub-agent that wrote the words
    if peer:
        T.deny('child', 'tree', '@author')
    return T


def _resume_truth(v, T, ev, rank, tree, node, multi_node):
    """A resumed child: its parent is whoever launched the first run (the main session, by a plain call), and the resume only adds a `by` to the run."""
    T.set('child', tree='@orch', rule_class='certain', node=None)
    if rank is None:
        T.set('child', by=None)
    else:
        T.set('child', by=(tree, node if node_capable(ev) else None))
        if not node_capable(ev) and multi_node:
            T.expect('node_unresolved')


def codex_truth(case, T, tree, node):
    v = case.v
    if v['target'] != 'cx_exec':
        T.set('child', tree=None, rule_class='none')
        T.deny('child', 'tree', '@orch')
        return T
    if v['bait'] != 'none':
        # a thread that merely looks like the child of a `codex exec` call: other words in the call, another folder (`-C`), a call long gone, an echo, or a stranger
        # thread beside the call: nothing links them. Whether the call is then counted as one that left no child is not asked.
        T.set('child', tree=None, rule_class='none')
        T.deny('child', 'tree', tree)
        T.allow('orphan_launch', 'orch')
        return T
    T.set('child', tree=tree, rule_class='certain', node=node)      # the prompt rule (rank 3) always holds: the literal instruction is in the launching call
    return T


# ---------------------------------------------------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------------------------------------------------
# (status, reason, seconds until resets_at or None) of a life that ended with a record the reader can classify
RESET_SECS = {'limit_exit': 7200, 'sub_limit_resume': 7200, 'sub_limit_dead': 1200, 'weekly': 4 * 86400}
LIMIT_LIVES = ('limit_exit', 'sub_limit_resume', 'sub_limit_dead', 'weekly', 'no_reset')


def life_state(kind, life, at, proc_unknown):
    """(status, reason, resets_at) the records and the process table support for an agent of `kind` after `life`.

    Error classification comes before the process: a limit line is `interrupted/limit` whether or not the process is gone. A run closed without
    an end turn and without any evidence of why is `interrupted/exited`; a record that just stops (no closing marker) with the process gone is `ended/crash`.
    A process the board cannot see is `unknown`, never `ended`."""
    resettable = life in RESUMABLE and life not in ('running', 'stalled_silent')
    if at == 'resume_stopped' and resettable:
        return 'killed', 'stopped', None                   # the resumed run was stopped by a TaskStop of its own call: the earlier error says nothing about it
    if at == 'after_resume' and resettable:
        return 'running', None, None
    if life == 'running':
        return 'running', None, None
    if life == 'stalled_silent':
        return ('unknown' if proc_unknown and kind != 'sub' else 'stalled'), None, None
    if life == 'normal_end':
        return 'done', None, None
    if life in LIMIT_LIVES:
        return 'interrupted', 'limit', (('T', RESET_SECS[life]) if life in RESET_SECS else None)
    if life == 'api_529' or life == 'codex_error':
        return 'interrupted', 'api_error', None
    if life == 'api_400':
        return 'failed', 'api_error', None
    if life == 'time_limit_kill':
        return 'interrupted', 'time_limit', None
    if life == 'time_limit_silent':
        return 'interrupted', 'exited', None            # no explicit notice: the run only ended (no guess from the run length)
    if life in ('taskstop_kill', 'kill_resume'):
        return 'killed', 'stopped', None
    if life == 'crash':
        if kind == 'cli':
            return 'ended', 'crash', None                  # the parent's record holds the background task's notice (exit 137): the process is known to be over
        return ('unknown' if proc_unknown else 'ended'), (None if proc_unknown else 'crash'), None
    raise ValueError(life)


def sta_truth(case):
    T = _sta_truth(case)
    if case.v['os'] == 'mac_nops':
        expect_proc_unknown(T)
    return T


def _sta_truth(case):
    v = case.v
    sk, life, at, flaw = v['skind'], v['life'], v['at'], v['flaw']
    T = Truth()
    if sk == 'main':
        return main_truth(T, life, at)
    status, reason, resets = life_state(sk, life, at, v['os'] == 'mac_nops')
    T.set('child', status=status, reason=reason, resets_at=resets)
    if sk == 'cli' and at in ('after_resume', 'resume_stopped') and life in RESUMABLE:
        T.set('child', by=('@orch', None))             # the main session handed the resumed run its instruction: the tree stays the one of the first run
    if status == 'interrupted' and reason == 'limit' or (status == 'interrupted' and reason == 'api_error'):
        T.deny('child', 'alert', 'fail')
    if life == 'sub_limit_dead' and at != 'after_resume':
        T.expect('not_resumed', reason='limit')
    if sk == 'codex' and v['os'] == 'mac':
        # `ps` lists the thread's process (with the words it was started with) but not the file it has open. A board that ties the two says what a quiet run is
        # (`stalled`); one that cannot says it does not know (`unknown`, with `proc_unknown`). Both are honest; nothing else is.
        T.allow('proc_unknown')
        if status == 'stalled':
            T.accept('child', 'status', 'unknown')
    _flaw_diag(T, flaw)
    return T


def _flaw_diag(T, flaw):
    if flaw == 'torn':
        T.expect('torn_lines')
    elif flaw == 'multi_proc':
        T.expect('multi_process', pids=2)
    elif flaw in ('old_format', 'future_version', 'field_gone'):
        T.expect('format_drift', version=FLAW_VERSION[flaw])   # a version outside the observed range, or a known field gone inside it
    elif flaw == 'child_bg':
        T.expect('stray_notice', count=1)
        T.deny('orch', 'event', 'notify_stray')


def main_truth(T, life, at):
    """The orchestrator that hit the usage limit: waiting for the reset (with or without an automatic continue), and one of its agents stopped by the same limit."""
    if life == 'running':
        T.set('orch', orch_state='working')
        return T
    T.set('child', status='interrupted', reason='limit', resets_at=('T', 7200))
    T.deny('child', 'alert', 'fail')
    T.deny('orch', 'event', 'orch_say_limit')
    if at == 'after_resume':
        T.set('orch', orch_state='working')            # resumed automatically: only the sub-agent is still stopped by the limit, one member is no group
    else:
        T.set('orch', orch_state='limit_wait', resets_at=('T', 7200))
        if life in ('limit_auto', 'limit_repeat'):
            T.deny('orch', 'alert', 'turn')
        T.expect('limit_group', 'orch', members=2, resets_at=('T', 7200))      # the orchestrator waiting for the reset and its agent share one resets_at
    return T


# ---------------------------------------------------------------------------------------------------------------------
# debate
# ---------------------------------------------------------------------------------------------------------------------
UNIT_REL = {'single': 'docs/rev', 'topics': 'docs/rev/t1', 'deep3': 'docs/records/2026/rev', 'readme': 'docs/rev', 'dot': '.records/rev', 'flat': 'docs/rev'}
PATH_FORMS = ('abs', 'tilde', 'folder', 'short', 'dotdot', 'var', 'dash_o_last', 'dash_o_aux')       # the instruction names the report path and it can be resolved
INTERRUPTED_LIVES = ('limit_exit', 'time_limit_kill', 'api_529')


def cell_state(life, file_nonempty):
    """The cell of a seat whose agent lives `life`: only `interrupted` is new (paused without a file, draft with one); an agent that is over
    for any other reason keeps today's reading (a file means done, no file means missing); a working agent has a draft or is writing."""
    if life in ('running', 'stalled_silent'):
        return 'draft' if file_nonempty else 'writing'
    if life in INTERRUPTED_LIVES:
        return 'draft' if file_nonempty else 'paused'
    return 'done' if file_nonempty else 'missing'


def seat_of(v, kind):
    """(seat stem or None, why) for a writer: path intent > own marker > successful write > tag; a tie or an unresolvable path is held."""
    rp, nstyle = v['rpath'], v['nstyle']
    stem = {'plain': 'B', 'named': 'B_gate', 'lower': 'b', 'numbered': 'opus1', 'collide': 'B_gate'}[nstyle]
    flat = v['structure'] == 'flat'
    if flat:
        stem = 'sol' if nstyle == 'plain' else 'sol-final3'
    if v['homonym'] == 'two':
        # the instruction's relative path fits two debates and is held; a write that succeeded names its folder by itself (a real write path resolves
        # against the cwd of that event only; a successful write of its own report seats it)
        return (stem if flat else 'B', 'write') if wrote_ok(v, kind) else (None, 'ambiguous')
    if flat:
        return (stem, 'path') if v['decl'] == 'yes' else (None, 'undeclared')
    path_ok = rp in PATH_FORMS or (rp == 'dash_o' and kind == 'codex') or (rp == 'redirect' and kind == 'cli')
    if path_ok:
        return stem, 'path'
    # the letter alone names the seat; the spelled name of a file only when the guide's folder already has it: the participant's own file once it wrote it
    # (with collide, B.md, b.md and B_gate.md all exist: only a successful write of its own says which one is its seat)
    if wrote_ok(v, kind):
        return stem, 'write'                           # the spelled name of the file the participant wrote is its seat (B_gate, opus1 ...), whatever else sits in the folder
    letter_stem = 'B_gate' if (nstyle == 'named' and v['fstate'] in ('written', 'empty')) else 'B'
    if v['marker'] in ('own', 'cross'):
        if v['rdir'] == 'both':
            return None, 'held_round'                  # a marker names a seat, not which of the two round folders (r1/, r01/) holds its file: held (alias_collision says why)
        return letter_stem, 'marker'
    # a description tag alone seats nobody, and a write path that cannot be resolved is no write intent of this debate: held (`path_unresolved`)
    return None, 'no_evidence'


def wrote_ok(v, kind):
    """The agent itself wrote its report successfully (a Write call / a patch that succeeded and left a file)."""
    return v['role'] == 'writer' and v['fstate'] == 'written' and not launcher_writes(kind, v['rpath'])


def listed_units(v):
    """The debate folders of the scene, as the board's list must show them: every folder of a debate that is on disk, and nothing above them (the folder that
    holds the common brief of several topics is no debate of its own)."""
    if v['homonym'] == 'two':
        return {UNIT_REL['single'], 'docs/rev2'}
    if v['structure'] == 'topics':
        return {'docs/rev/t1', 'docs/rev/t2'} | ({'docs/rev/' + EDIT_DIR} if v['edits'] == 'beside' else set())      # the editing job has a guide, so it is listed (as a title)
    return {UNIT_REL[v['structure']]}


def place(unit, rnd, seat, folder=None):
    """The one place a seat is shown at: debate folder | round | seat | the path of the file below the debate folder (round folder and name, no extension).
    The spelling of the round folder is part of the place: `r1/B` and `r01/B` are two files."""
    if seat is None:
        return frozenset()
    return frozenset(['%s|%s|%s|%s' % (unit, rnd, seat, '%s/%s' % (folder, seat) if folder else seat)])


def deb_truth(case, T=None, kind=None):
    v = case.v
    T = T or Truth()
    kind = kind or v['kind']
    role = v['role']
    unit = UNIT_REL[v['structure']]
    T.set('listing', units=frozenset(listed_units(v)))
    if v['edits'] == 'beside':
        # an editing job lists findings by bold numbers (`**C-12**`); nothing in it declares a participant, a report or a round, and it has no round folder: no row, no round
        T.set('listing', edit_rows=frozenset(), edit_rounds=frozenset())
    if role == 'absent':
        T.set('listing', unit=unit)
        return T
    if role == 'ghost':
        T.set('child', unit=None, seat=None, cell=None, role='none', placements=place(None, None, None))
        return T
    if role in ('reader', 'ref_reader'):
        # a reader is shown on the cell of the report it read; a flat review that declares no reviewers has no cell to read (title and diagnostic only).
        # A reader that only names the report (`B's report: P`, `P의 주장을 검증해서 답으로만 알려줘`) is a reader as well: no verb of writing, no seat
        T.set('child', unit=unit, seat=None, cell=None, role='none' if (v['structure'] == 'flat' and v['decl'] == 'no') else 'reader', placements=place(None, None, None))
        return T
    if role in ('quoter', 'negator', 'tag_only', 'failed_write'):
        T.set('child', seat=None, cell=None, role='none', placements=place(None, None, None))
        T.expect('debate_in_misc')
        return T
    if role == 'rival':
        T.set('child', unit=unit, seat=None, cell=None, role='none', placements=place(None, None, None))
        T.expect('seat_tie_held')
        return T
    seat, why = seat_of(v, kind)
    if v['structure'] == 'flat' and why == 'undeclared':
        T.set('child', unit=unit, round=None, seat=None, cell=None, role='none', placements=place(None, None, None))
        T.expect('declaration_missing')
        return T
    if why == 'ambiguous':
        T.set('child', unit=None, seat=None, cell=None, role='none', placements=place(None, None, None))
        T.expect('path_ambiguous')
        return T
    if v['homonym'] == 'two':
        T.expect('path_ambiguous')                     # the instruction is still ambiguous even though the write settled the seat
    if v['rpath'] == 'var_ext':
        T.expect('path_unresolved')
    if v['nstyle'] == 'collide' or (v['rdir'] == 'both' and (v['aux'] != 'alias' or aux_file_exists(v))):
        T.expect('alias_collision')                    # two different files with the same round and name (B.md and b.md; r1/B.md and r01/B.md): never merged
    elif v['rdir'] == 'both':
        T.allow('alias_collision')                     # the launch has not written the other spelling's file yet: two folders, one file of that name
    elif v['rpath'] == 'dash_o_aux':
        T.allow('alias_collision')                     # the file `-o` writes is named like a variant of the seat (B_last.md): whether that counts as a second file of the seat is not asked
    if seat is None:
        T.set('child', unit=unit, seat=None, cell=None, role='none', placements=place(None, None, None))
        return T
    life = v['life']
    rnd = None if v['structure'] == 'flat' else 1
    # the file of the seat is there when the participant wrote it, or when the seat is the plain letter and the folder holds files of that letter that others wrote
    # (nstyle=collide: B.md and b.md are in the round folder with content)
    file_ok = v['fstate'] == 'written' or (v['nstyle'] == 'collide' and seat == 'B')
    folder = None if rnd is None else ROUND_DIR[v['rdir']] % 1
    T.set('child', unit=unit, round=rnd, seat=seat, role='writer', placements=place(unit, rnd, seat, folder), cell=cell_state(life, file_ok))
    return T


def cpl_truth(case):
    """The integrated scene: a `claude -p` participant launched by `spawner`, with a life, a report path and a file state. The launch is the plain one
    (a background call with the literal instruction), so its affiliation follows the rank 3 content rule, or the process while it is alive."""
    v = case.v
    T = Truth()
    tree = {'main': '@orch', 'sub': '@orch', 'grand': '@mid'}[v['spawner']]
    node = '@launcher' if v['spawner'] == 'sub' else None
    T.set('child', tree=tree, rule_class='certain', node=node)
    if v['life'] != 'running':
        T.expect('content_only')
    dv = dict(v)
    dv.update(kind='cli', role='writer', nstyle='plain', structure='single', rdir='r1', homonym='none', decl='yes', marker='own' if v['rpath'] == 'instr_only' else 'none')
    deb = Case_like(dv)
    deb_truth(deb, T, kind='cli')
    status, reason, resets = life_state('cli', v['life'], 'just_ended', False)
    T.set('child', status=status, reason=reason, resets_at=resets)
    if status == 'interrupted':
        T.deny('child', 'alert', 'fail')
    return T


class Case_like:
    """Just enough of a Case (a `.v`) for the truth functions."""

    def __init__(self, v):
        self.v = v


def truth(case):
    """The Truth of any case."""
    if case.bundle == 'aff':
        return aff_truth(case)
    if case.bundle == 'sta':
        return sta_truth(case)
    if case.bundle == 'deb':
        return deb_truth(case)
    return cpl_truth(case)
