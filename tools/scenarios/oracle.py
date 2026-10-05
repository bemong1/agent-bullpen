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
  8. `format_drift` is a known field gone inside the observed range (`field_gone`). A version outside the range (older than 2.1.235, newer than 2.1.286) is no drift
     by itself: every release would raise it for every user. A line type the board does not read is ignored, no diagnostic.
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
 22. A room is a folder, whatever its guide is called and whatever the instruction calls the work. Four things together make one, all facts of the records and the disk:
     (1) two or more agents of the orchestrator whose page is read have a first instruction that points at the same guide (a `.md` of any name) that is on disk,
     (2) those agents are told, or have, each a file of their own in the guide's folder or one folder below it,
     (3) their runs overlap in time (the latest start is before the earliest end; a run that has not ended counts as going on), and
     (4) most of them are not told to change, and have not changed, code or other files outside the guide's folder (rules 27 and 28).
     The room is the guide's folder, its title the guide's first heading, round 1 its only round (no round folder), the cell of a seat that participant's file. The words
     `debate`, `meeting`, `agenda` and their Korean are never read (`word` changes the text only).
 23. A guide that everybody reads is no room: the documents at the top of a repository (`README.md`, `CLAUDE.md`, `AGENTS.md`) and `docs/guide.md` are common
     documents. A participant that points at a guide of its own, at one that is not on disk, or at none in its first instruction (a later message names it), is not
     in a room with the others. One file all of them write is no per-participant file; code written in other repositories, files in another folder or two folders below
     the guide are outside the room: none of these is a room, whatever the participants say to each other.
 24. A meeting by message only: two or more agents share a guide of the room's own folder and none of them has a file of its own anywhere (`shape=none`), and
     the participants send each other `SendMessage` (a ring: each one to the next of its orchestrator). The room is the guide's folder with its participants and no
     cell. Messages from the orchestrator to each of them are no meeting, and neither are messages beside files that are outside the room.
 25. A folder with a round folder is a debate already: its participants are seated by the rules of the debate (the path they are told or write, the file's name)
     whatever the guide is called and however many they are, and the room rules add nothing. Whether a folder with a lone `brief.md` and no room is listed
     depends on who works there (the flat review): not asked of a room case.
 26. The seat of a room participant: the letter its first instruction names (`[ROOM-B]`, `B 담당`, `B(...) 담당`, `You are participant B`, `Work as B (...)`,
     `You hold seat B`, at the start of the instruction, and meant for the participant itself), else the one-letter tag of its description, else the stem of its file.
     A letter that is quoted as an example, negated (`You are not participant C`), or means something else (`as C (not C++)`, `seat C` on a train, `participant C` of a
     study, a team in charge of the C language) is no marker: the words `as C (`, `seat C` and `participant C` alone say nothing, only the sentence that addresses the
     participant does, so a reader that matches the bare words seats the lookalikes (the English markers are worth adding only in the framed forms). The file of a
     seat is the file the participant is told to write or wrote (`notes_B.md`, `out/B.md`); with the plain names (`B.md`) the stem and the letter are one.
 27. Runs that never overlap are no room: participants that are started one after the other, each only after the one before has finished (`rtime=sequential`), share a
     guide and write each a file of their own in its folder, and still they did not work together. Participants that are all still running, or whose runs share a moment,
     are together. Messages cannot be sent between runs that never coexist, so a meeting by message only needs overlapping runs as well.
 28. Code is no room: when more than half of the participants of a guide are told to change a file outside the guide's folder (code in the repository's source folder),
     or changed one, they are doing parallel work that happens to report beside a plan, whatever else sits in the folder (`code=majority|all`). Exactly half or fewer
     (`code=one`, or one of two) leave the folder a room: the notes still are a file of each one's own. The count is of the participants of the guide.
 29. A guide and a notes file that an instruction only quotes or reviews seat nobody and make no room: a quote in one line, a code fence, a Markdown block quote, or a
     read-only review of the earlier words, or the lines after a sentence that says they are a read-only quote of them (`cite`) are the words of somebody else's instruction,
     not the participant's own; nothing is written either way. A folder that has a round folder is still a debate on the list (rule 25), but nobody in it is seated by words that were only quoted. The same holds for a `quoter` of the debate
     bundle (`qform`): a report path in a fence, a block quote, a read-only review or the lines after a "read-only quote" sentence seats nobody. The same words in a code
     fence under a line that tells the participant to carry them out ("Execute these instructions:", `wrap=exec_fence`) are the participant's own instruction.
 30. A message that was answered with an error was not delivered, so it is no meeting: participants that share a guide and message each other with `delivery=failed`
     are no room by message (a room that is one by files stays one: it never needed the messages).
 31. The orchestrator page of a session is `working` only while a turn is going on: its last record is a tool call or a result, or a real prompt that nobody has answered
     yet (also when it came within a second of the end of the turn before: what opens a turn is the line, not the time since the last end), and the process of the session is there. A turn that ended (the last assistant line is `end_turn`, whether or not the record has a turn-duration line: a
     `claude -p` run has none), local slash commands typed after it (`/usage` and the like: they start no turn), a session that holds nothing but such commands, and a
     session whose process is gone are `idle`, however fresh the file is (`entry`, `tail`, `process`).
 32. A session whose process is gone has nobody to answer: the page does not say that the orchestrator waits for the answer (a turn that ended with a question) or for
     the next instruction (`process=gone`).
 34. The document view opens the guide of a room the page shows, whatever the states of its participants are: the room is the one the page has, not one worked out again as
     if everybody had finished (`rtime=quiet`: each participant started after the one before had gone quiet, and all of them are still running). Nothing under a dot folder
     (`site=dot`) is opened: that is the rule of the document view for hidden paths, not one about rooms.
 33. What an agent only saves is no change of the work (rule 28 counts the files of the work): the output of a command in a log (`scratch=log`) and the files it keeps in a
     folder for scratch work outside the repository (`scratch=tmp`), whoever of the participants does it. Parallel implementers whose instruction names no path and who write no
     code (`code=implied`) are indistinguishable from reviewers in the records: the truth is that there is no room, and the room the board shows is accepted (a known false room, left as it is).

The Codex orchestrator (`cxo`: a page whose top is a Codex thread, or a Claude session with a Codex run below it). Words: the *top* is the orchestrator whose page is read,
the *launcher* of a run is the thread (or session) whose shell call started it, the *tree* of a run is the session or root thread of its launcher (`owner_of(id).parent`), the
*node* is the sub-agent inside that tree whose own shell made the call (None: the orchestrator itself, or not known), the *page* is the top of the chain of trees
(`page_of`), the *screen parent* is the agent a card hangs under on the page of the top (None: the orchestrator). Evidence, strongest first, and what each says:
 35. The records of a native sub-agent (its own meta, naming its parent, and the `SubAgentActivity started` the parent wrote for it) are certain for tree, node and call. A
     sub-agent below the top's own sub-agent has that sub-agent as its parent (tree); the page is still the top.
 36. The environment of a started process names the thread whose shell ran the call (`CODEX_THREAD_ID`) and the root of its tree (`CODEX_SESSION_ID`): certain, for the tree
     and, when that thread is a native sub-agent, for the node as well (the one place where an environment names a node). It exists only while the process runs, and only when
     no `env -i` or the like cleared it.
 37. The process lineage (the child is still a descendant of the Codex process that holds the top's rollout) is certain for the tree and says nothing of the node. A `setsid`
     call re-parents the child: no lineage.
 38. The `CommandExecution` record of the launching thread (written when the process of the command ends: at once for a `&` call, at the end of the child for a foreground
     call, never for a command that outlived its turn) holds the command text and its folder: with the instruction as a literal argument of a `claude -p` / `codex exec` at
     the command position, it is certain for tree and node (the thread whose record it is), also when another thread of the same tree has a turn that is still going and a command
     the board cannot read yet: the node is the sub-agent whose record shows the launch. A command that only quotes such a command (`tmux send-keys`, `echo`) is no launch.
 39. Both providers' names in one environment (a Claude session started a Codex run whose shell started the child): the run that is the nearest agent decides; when the
     lineage is cut, the known owner chain does (the Codex run belongs to the Claude session, so it is the direct parent); the top Claude session is never the parent of the
     grandchild. Nothing is read from which names a provider is known to clear.
 40. A session that only relays the instruction (a `tmux send-keys -l '<instruction>'`, or the same inside `python3 -c`) started nothing: no link to the run, whatever the
     run's own evidence is, and none to the sessions it relays through. Two orchestrators of one folder (one Claude, one Codex) that say the same words and start a child
     each: the environment or the lineage of the child names its own, and so does a valid output file (`twin_out`: the command redirects the child's output to a file that
     holds its session id, the run is over): they rank above the words; only when none of them exists do the words tie and the child is held. A script that runs `codex exec` with no orchestrator behind it links nothing.
 40a. A command the board cannot read yet is a competitor it cannot rule out: when a Codex thread of the same folder has a command whose record has not come (an exec cell that
     returned "Script running" and whose `CommandExecution` is not there), a Claude session's launch that matches the child by its words (or by time) is still the
     answer, but only a guess; the environment, a valid output file or the sub-agent's meta stay certain. Two providers' names in one environment are told apart only
     along an owner chain whose every edge is proven: a chain with one edge that is a guess holds the child (`evidence_conflict`) and the middle run keeps its guess.
 40b. A record that comes late (a command that ran for minutes: its `CommandExecution` is written when it ends, 135 s after the child started) links the child in the end, even
     when the board had looked before it came and found nothing.
 39a. On macOS `ps` does not say which file a process holds open, so a Codex process cannot be told from any other program by what it has open. A grandchild that carries both
     providers' names (Claude > codex exec > claude -p) is then not given to the top Claude session as if the environment proved it: it belongs to the codex exec run (the owner chain
     says so, the Codex names say so) or is held; the one answer that is wrong is the top Claude session as its certain parent.
 40c. What only passes the words on starts nothing, however it is written: a script that types them into a window (`tmux send-keys -t w "claude -p \"$1\"" Enter`), the same
     through `xargs -I{} tmux send-keys ...`, `ssh h echo '...'`, `kubectl exec pod -- tmux send-keys ...`, or the words as an argument of a program that is no launcher
     (`curl --data claude -p --data "<instruction>" https://...`). What starts a run is a launch as before: `tmux new-session -d 'claude -p "..."'`, `xargs -I{} claude -p "{}"`.
 40d. An environment names the thread that ran the call only while that thread's turn is going on: the names a tmux server keeps from a Codex thread that has been over for hours are
     stale. A child that carries them and was started by another session (`tmux new-window -t w 'claude -p "..."'`) belongs to that session, by its words; never to the old thread.
 40e. A Codex thread with a command whose record has not come may be the one that started a child whose words match a Claude call that is still running, when the child's environment
     names nobody: the Claude link is not certain (a guess, or held).
 40f. A Python file that only types the words into a window (`subprocess.run(["tmux", "send-keys", "-t", "w", "claude -p \"%s\"" % sys.argv[1], "Enter"])`, run as
     `python3 relay.py "<instruction>"`) starts nothing, the same as the shell script of 40c; a Python file that really runs `subprocess.run(["claude", "-p", ...])` is a launch.
 40g. A name in the environment that nobody can check is no proof: the id of a Codex thread the index does not have (a new thread not listed yet, or none), and a Claude session whose
     names a tmux server kept and that had no running call when the child started. The first leaves a Claude call with the same words uncertain (a guess, or held: the child may
     be the unknown thread's); the second is never the parent, the Codex thread whose record shows the launch is.
 40h. The names of a Codex thread also say nothing for a turn other than the one that is going on: a thread that started a tmux server in an earlier turn and now only runs
     `ls` did not start a child that another session started through that server; the session whose call carries the words is its parent.
 41. A `&` call of a Codex shell takes the child down with the call: no record of the run exists, so there is no card, and the launch that has none behind it is an
     `orphan_launch`.
 42. A native sub-agent's state is its own record's (a turn started and not ended: working; `task_complete`: done) as long as its parent's process is there; an
     `interrupted` the parent wrote after the sub-agent finished changes nothing, one that came while it worked makes it interrupted; a sub-agent whose parent's process is gone
     with its turn open has ended. The front part of a sub-agent's rollout (the parent's meta, first turn and first user message, up to `subagent_history_start_ordinal`) is
     the parent's, not the sub-agent's. The first message from the parent's path is a plain forwarding notice (it names the path) beside the instruction itself, which is
     ciphertext: the instruction is not known, nothing reads the notice as one, and the card's name is the end of the sub-agent's path.
 42a. In the parent's record the end of a sub-agent is `SubAgentActivity completed` first and its message to the parent after it: that first message after `completed` is the
     handback; a report in the middle of the work is a message between agents (`agent_msg`); the same message in the sub-agent's record and in the parent's is one event.
 43. A guardian (approval review) thread is no team member: no card. Its tokens are the page's approval-review tokens, and only its: a native sub-agent's are its own card's.
 44. A rerun: when the same script is run again with the same command text (the first runs stopped with no report, the brief was fixed, the second runs are new sessions with
     the same instruction and the same output file, which now names the new `claude -p` run only), the second run is the child of the second command (its call, its start
     and its title are that command's) and holds the seat of its participant: the period of the seat of the first run is closed (no `seat_tie_held`). The first run's call is the
     first command or none, never the second; its seat is gone. The output file proves nothing for the first run (it was overwritten). A first run that was closed with
     `cost-state` and no end of turn, or whose record was cut off, is over either way (`stop`): it does not keep the seat.
"""

import collections
import os

from .axes import (ABOVE_DOC, COMMON_DOCS, EDIT_DIR, FILE_WAYS, FLAW_VERSION, RESUMABLE, ROUND_DIR, SEAT_LETTERS, WAY, CXO_OWN_LAUNCHES, CXO_RELAYS, CXO_TWINS, aux_file_exists, cxo_alive, cxo_hop, cxo_proof,
                   launcher_writes, room_coders,
                   room_folder, room_guide, room_out, room_stem, room_title, room_tree)


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
CXO_CODES = frozenset(('orphan_launch', 'format_drift', 'evidence_conflict'))      # a `&` launch that left nothing; a drift that the shapes of this bundle (all valid) never call for; a hold between two providers' names
BUNDLE_CODES = {'aff': AFF_CODES, 'sta': STA_CODES, 'deb': DEB_CODES, 'room': DEB_CODES, 'cxo': CXO_CODES, 'rer': DEB_CODES}


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
        return main_truth(T, life, at, v)
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
    elif flaw == 'field_gone':
        T.expect('format_drift', version=FLAW_VERSION[flaw])   # a known field gone inside the observed range; a version outside it says nothing (old_format, future_version)
    elif flaw == 'child_bg':
        T.expect('stray_notice', count=1)
        T.deny('orch', 'event', 'notify_stray')


def main_working(v):
    """Whether the orchestrator page of a session that was not stopped by a limit is working (rule 31): a turn is going on and the process is there."""
    return v['tail'] in ('mid', 'prompt', 'next') and v['process'] == 'there'


def main_truth(T, life, at, v):
    """The orchestrator that hit the usage limit: waiting for the reset (with or without an automatic continue), and one of its agents stopped by the same limit."""
    if life == 'running':
        T.set('orch', orch_state='working' if main_working(v) else 'idle')
        if v['process'] == 'gone':                    # nobody is there to answer, or to give the next instruction
            T.deny('orch', 'alert', 'say')
            T.deny('orch', 'alert', 'turn')
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
        if v['qform'] == 'inline':
            T.expect('debate_in_misc')
        else:
            T.allow('debate_in_misc')                  # a path in a fence, a block quote or a read-only review: whether the board says so is not asked, only that nobody is seated
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


# ---------------------------------------------------------------------------------------------------------------------
# room
# ---------------------------------------------------------------------------------------------------------------------
SEAT_MARKS = ('bracket', 'dam', 'dam_paren', 'en_participant', 'en_as', 'en_seat')      # the ways the first instruction names the participant's own seat letter


def room_seat(v, i):
    """The seat of participant i of a room: the letter its instruction names, else the one-letter tag of its description, else the stem of its file."""
    if v['seatmark'] in SEAT_MARKS or v['seatmark'] == 'tag':
        return SEAT_LETTERS[i]
    return room_stem(v, i)


def below_folder(path, folder):
    """The file below the room's folder without the extension (`out/B`)."""
    return os.path.splitext(os.path.relpath(path, folder))[0]


def in_reach(path, folder):
    """A file is in the guide's folder or one folder below it."""
    return os.path.dirname(path) == folder or os.path.dirname(os.path.dirname(path)) == folder


def room_veto(v, mine):
    """Why a folder that has a shared guide and a file of each participant's own is no room anyway: 'timing' (the runs never overlap, rule 27) or 'code' (most of the
    participants change files outside the folder, rule 28); None when nothing speaks against it."""
    if v['rtime'] == 'sequential' and len(mine) >= 2:
        return 'timing'
    if 2 * len(room_coders(v) & set(mine)) > len(mine):
        return 'code'
    return None


def room_trace(v):
    """(kind, participants of the page that are in it, folder, why) of the guide's folder, where kind is 'cells' (a room whose participants have a file each), 'members'
    (a meeting by message only), 'debate' (it has a round folder: the rules of the debate) or None; `why` names what spoke against a room that the files and the guide
    alone would make ('cite', 'timing', 'code', 'delivery'), else None."""
    n = int(v['people'])
    page = [i for i in range(n) if room_tree(v, i) == 1]
    cited = 'cite' if v['cite'] != 'none' else None
    if v['shape'] == 'r1':
        return 'debate', page, room_folder(v), cited
    if cited:
        return None, [], None, cited                                                               # the guide and the files are the words of an earlier instruction (rule 29)
    points = collections.Counter(room_guide(v, i)[0] for i in page if room_guide(v, i)[1])         # the guide the first instruction points at, when it is on disk
    shared = [g for g, k in points.items() if k >= 2]
    if not shared:
        return None, [], None, None
    guide = shared[0]
    folder = os.path.dirname(guide)
    mine = [i for i in page if room_guide(v, i)[0] == guide]
    files = {room_out(v, i) for i in mine if room_out(v, i) is not None}
    if len({f for f in files if in_reach(f, folder)}) >= 2:
        why = room_veto(v, mine)
        return (None, [], None, why) if why else ('cells', mine, folder, None)
    if v['shape'] == 'none' and v['talk'] == 'peer' and guide not in COMMON_DOCS.values():
        if v['delivery'] == 'failed':
            return None, [], None, 'delivery'                                                      # nothing was delivered (rule 30)
        return 'members', mine, folder, None
    return None, [], None, None


def room_kind(v):
    """What the evidence of the scene makes of the guide's folder (see room_trace): the kind, the participants of the page that are in it and the folder."""
    return room_trace(v)[:3]


def room_truth(case):
    v = case.v
    kind, mine, folder = room_kind(v)
    T = _room_truth(v, kind, mine)
    if v['code'] == 'implied' and kind == 'cells':
        # Known false room, left as it is: the participants are told to implement their step with no path and write no code, so the records give no sign of parallel work, and
        # the same words in a meeting that is about code would be a room. The truth is that there is none; the room the board shows is accepted (a board that reads the path of
        # the code gets it right either way).
        none = _room_truth(v, None, [])
        for role, fields in T.subjects.items():
            for field, value in fields.items():
                T.accept(role, field, value)
        T.subjects = none.subjects
    return T


def _room_truth(v, kind, mine):
    T = Truth()
    unit = room_folder(v)
    life = 'running' if v['phase'] == 'working' else 'normal_end'
    file_ok = v['proof'] in ('wrote', 'both')
    listed = kind is not None
    if v['guide'] == 'brief':
        T.allow('declaration_missing', 'child')                            # the flat review that declares nobody: whether it says so is not asked of a room
    if kind is not None or v['guide'] != 'brief':
        T.set('listing', units=frozenset([unit]) if listed else frozenset())       # whether the folder of a lone `brief.md` is listed depends on who works there: not asked
    if kind in ('cells', 'members'):
        T.set('listing', titles=frozenset(['%s|%s' % (unit, room_title(v))]), guide_opens=frozenset([room_guide(v, mine[0])[0]] if v['site'] != 'dot' else []))
    if v['bundle'] == 'root':
        # the bundle's conclusion closes the room when it was written after the files (and names the room's folder, or names nobody and says nothing is left open), the files are all in
        # and nobody is still working; its name is the one `auto_final` reads for a conclusion
        closed = kind == 'cells' and v['phase'] == 'done' and file_ok and v['above'] in ('closing', 'unnamed')
        T.set('listing', finals=frozenset(['%s|../%s' % (unit, ABOVE_DOC[v['above']])]) if closed else frozenset())
    for i in range(int(v['people'])):
        if room_tree(v, i) != 1:
            continue                                                       # another orchestrator's agent is not on this page
        role = 'p%d' % (i + 1)
        if v['cite'] != 'none':
            T.set(role, seat=None, cell=None, role='none', placements=frozenset())           # words that were only quoted seat nobody (a debate folder is listed all the same)
        elif i in mine and kind in ('cells', 'debate'):
            seat = room_seat(v, i)
            below = below_folder(room_out(v, i), unit)
            T.set(role, unit=unit, seat=seat, cell=cell_state(life, file_ok), role='writer', placements=frozenset(['%s|1|%s|%s' % (unit, seat, below)]))
        elif i in mine and kind == 'members':
            T.set(role, unit=unit, seat=None, cell=None, role='none', placements=frozenset())
        else:
            T.set(role, seat=None, cell=None, role='none', placements=frozenset())
            if v['guide'] != 'brief':
                T.set(role, unit=None)                                     # no debate folder is listed for it: it works in no debate
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


# ---------------------------------------------------------------------------------------------------------------------
# Codex orchestrator
# ---------------------------------------------------------------------------------------------------------------------
CXO_GUARD_CALLS = 3                  # the token records of the guardian thread of the scene
CXO_SEAT = {'cl': 'A', 'cx': 'B'}    # the debate letter of a participant (`topic=talk`): the `claude -p` run is A, the `codex exec` run B


def cxo_sub_state(substate):
    """(status of the sub-agent, status of its parent sub-agent when it has one) for a native sub-agent after `substate` (rule 42)."""
    own = {'running': 'running', 'done': 'done', 'int_mid': 'interrupted', 'int_after': 'done', 'parent_gone': 'ended'}[substate]
    return own, ('running' if substate == 'running' else ('ended' if substate == 'parent_gone' else 'done'))


def cxo_sub_events(substate):
    """The kinds of event the page shows about a native sub-agent: it was spawned, it reported in the middle of its work (a message between agents), and a sub-agent that
    finished handed its last message back (rules 42, 42a)."""
    return {'spawn', 'agent_msg', 'handback'} if substate in ('done', 'int_after') else {'spawn', 'agent_msg'}


def cxo_truth(case):
    v = case.v
    T = Truth()
    T.set('orch', guardian_calls=CXO_GUARD_CALLS if v['guard'] == 'one' else 0)
    if v['guard'] == 'one':
        T.set('guardian', listed='none')                            # rule 43
    if v['subj'] == 'cx_sub':
        return _cxo_sub_truth(v, T)
    if v['chain'] == 'one' and v['how'] == 'bg':
        T.expect('orphan_launch', 'orch', n=1)                       # rule 41: the call is there, the child is not
        return T
    if v['lure'] in CXO_OWN_LAUNCHES or v['env'] == 'stale':
        # a Claude session starts the child itself (a wrapper that is a launch; the stale names of an old Codex thread come with the tmux server): its words are the proof (rule 38 / 40d)
        T.set('child', tree='@orchc', node=None, rule_class='certain', page='@orchc', listed='none')
        T.deny('child', 'tree', '@top')
        return T
    if v['lure'] == 'pin_unknown':
        # the child has the names of a thread the index does not know and a Claude call with the same words is running: that call may be a guess, never the proof (rule 40g)
        T.deny('child', 'rule_class', 'certain')
        return T
    if v['lure'] == 'pin_stale_claude':
        # a Codex thread's command record shows the launch; the names of the Claude session are what a tmux server kept, and that session had no call running (rule 40g)
        T.set('child', tree='@top', node=None, rule_class='certain', page='@top', listed='yes', parent=None, status='running')
        T.deny('child', 'tree', '@orchc')
        T.expect('evidence_conflict', 'child')                           # the names are dropped because they are stale, and the board says there was a conflict
        return T
    if v['lure'] == 'gap' and v['env'] == 'none':
        # the child is the Codex thread's (started by `tmux new-window`, no names in its environment) and its command has no record yet; a Claude call with the same words is still
        # running. The board cannot know whose it is: the Claude link may be a guess or held, never certain (rule 40e)
        T.deny('child', 'rule_class', 'certain')
        return T
    if v['lure'] == 'gap':
        # the child is a Claude session's (its call carries the words) and the page's Codex thread has a command nobody can read yet: the words are a guess now
        T.set('child', tree='@orchc', node=None, rule_class='guess', page='@orchc', listed='none')
        T.deny('child', 'tree', '@top')
        return T
    hop = cxo_hop(v)
    certain_hop = cxo_proof(hop)
    if v['chain'] == 'cl>cx>cl' and v['os'] != 'linux':
        # rule 39a: the process table cannot say which of its processes is a Codex agent, so the names of both providers prove nothing about the top Claude session
        T.set('mid', tree='@top', node=None, rule_class='certain', page='@top', listed='yes', parent=None)
        T.set('child', tree='@mid', rule_class='certain')
        T.accept('child', 'tree', None)                                  # held is as honest as the codex exec run
        T.accept('child', 'rule_class', 'none')
        T.deny('child', 'tree', '@top')
        return T
    if v['chain'] == 'cl>cx>cl' and v['edge'] == 'guess':
        T.set('mid', tree='@top', node=None, rule_class='guess', page='@top', listed='yes', parent=None)      # the run's link to the Claude session is a guess (time and folder)
        T.set('child', tree=None, rule_class='none', page='@child', listed='none')
        T.deny('child', 'tree', '@top')
        T.deny('child', 'tree', '@mid')
        T.expect('evidence_conflict', 'child')                       # rule 40a: held
        return T
    if v['chain'] == 'one':
        _cxo_first_hop(v, T, 'child', hop)
        if v['lure'] in CXO_RELAYS:
            T.deny('child', 'tree', '@relayer')                     # rule 40
        if v['lure'] in CXO_TWINS:
            T.deny('child', 'tree', '@orchc')
        if v['lure'] == 'user_script':
            T.deny('child', 'tree', '@top')
            T.allow('orphan_launch', 'orch')                          # the orchestrator's own `codex exec` of the folder, run just before, has no thread behind it: whether it counts is not asked
        if v['topic'] == 'talk' and certain_hop:
            _cxo_seat(v, T)
        return T
    # a chain: the first hop is a plain foreground call with the names of its provider in the environment (certain); the last hop varies
    first = {'env': True, 'proc': True, 'content': True}
    if v['chain'] == 'cx>cl>sub':
        _cxo_first_hop(v, T, 'mid', first)
        T.set('child', listed='yes', parent='@mid')                 # the sub-agent of a `claude -p` run hangs under that run on the page of the top (C14)
        return T
    _cxo_first_hop(v, T, 'mid', first)
    # the run below the middle one: its launcher is the middle run, a root thread (no node)
    if certain_hop:
        T.set('child', tree='@mid', node=None, rule_class='certain', page='@top', listed='yes', parent='@mid', status='running' if cxo_alive(v) else 'done')
    else:
        T.set('child', tree=None, rule_class='none', page='@child', listed='none')
    T.deny('child', 'tree', '@top')                                 # rule 39: never the top (the names of both providers are in the environment)
    return T


def _cxo_first_hop(v, T, role, hop):
    """The truth of a run started by a shell call of the top's thread or of a native sub-agent of it (`host`): `role` is the run (a `child`, or the `mid` of a chain)."""
    on_sub = v['host'] == 'sub'
    if not cxo_proof(hop):
        T.set(role, tree=None, rule_class='none', page='@' + role, listed='none')       # nothing says who started it: it stands alone, on a page of its own
        T.deny(role, 'tree', '@top')
        return
    node = '@host' if on_sub and (hop['env'] or hop['out'] or hop['content']) else None       # rule 36 / 38: the environment and the record name the sub-agent; the lineage names the tree only
    T.set(role, tree='@top', node=node, rule_class='certain', page='@top', listed='yes')
    if not on_sub:
        T.set(role, parent=None)
    elif node:
        T.set(role, parent='@host')                                              # the card hangs under the sub-agent that started it; unknown node: not asked
    if role == 'child':
        T.set(role, status='running' if cxo_alive(v) else 'done')


def _cxo_seat(v, T):
    """The child is a participant of the debate folder `talk` (rule 17-19): the folder is listed, the child sits at its letter of round 1, its cell follows its life."""
    seat = CXO_SEAT[v['subj']]
    T.set('listing', units=frozenset(['talk']))
    T.set('child', unit='talk', round=1, seat=seat, role='writer', placements=place('talk', 1, seat, 'r1'), cell=cell_state('running' if cxo_alive(v) else 'normal_end', v['look'] == 'ended'))


def _cxo_sub_truth(v, T):
    on_sub = v['host'] == 'sub'
    own, host = cxo_sub_state(v['substate'])
    sub_tree = '@host' if on_sub else '@top'                       # rule 35
    T.set('child', tree=sub_tree, node=None, rule_class='certain', page='@top', listed='yes', parent='@host' if on_sub else None, status=own,
          events=frozenset(cxo_sub_events(v['substate'])), label='ss' if on_sub else 's1', first_user=None)
    T.deny('child', 'spawn_text', '@root_text')                    # rule 42: the front part is the parent's, the notice is no instruction
    T.deny('child', 'spawn_text', '@notice')
    if on_sub:
        T.set('host', tree='@top', node=None, rule_class='certain', page='@top', listed='yes', parent=None, status=host, label='s1')
    return T


# ---------------------------------------------------------------------------------------------------------------------
# a rerun
# ---------------------------------------------------------------------------------------------------------------------
RER_UNIT = 'docs/rev'
RER_SEAT = {'a': 'A', 'b': 'B'}
RER_SECOND = {'a': 330.0, 'b': 331.0}                  # when the second commands were called (offsets of the case)
RER_TITLE = {'a': '@title'}                            # the description of the second command of the `claude -p` participant (a codex exec thread has no title of its own to ask for)


def rer_truth(case):
    """A rerun (rule 44): the same script is run again with the same command text; the first runs stopped without a report, the second runs (new sessions, the same
    instruction, the same output file, which now names the new `claude -p` run only) wrote theirs.
      - a second run is the child of the second command: its call is that command, its start and its title are the command's;
      - it holds the seat of its participant (A, B): the first run's period of the seat closed when the second run took it over, and the seat is not held (`seat_tie_held`);
      - a first run has no seat now; its call is the first command, or none (the output file proves nothing for it any more: it was overwritten), never the second one.
    The orchestrator's tree is the tree of all four."""
    v = case.v
    T = Truth()
    T.set('listing', units=frozenset([RER_UNIT]))
    for x in ('a', 'b'):
        if x not in v['parts']:
            continue
        T.set(x + '2', tree='@orch', call='@call_%s2' % x, start=('T', RER_SECOND[x]), seat=RER_SEAT[x], unit=RER_UNIT, round=1, role='writer',
              placements=place(RER_UNIT, 1, RER_SEAT[x], 'r1'), cell='done')
        if x in RER_TITLE:
            T.set(x + '2', title=RER_TITLE[x])
        T.set(x + '1', tree='@orch', call='@call_%s1' % x, seat=None, placements=frozenset())
        T.accept(x + '1', 'call', None)                                      # held is as honest as the first command
    T.allow('orphan_launch', 'orch')
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
    if case.bundle == 'room':
        return room_truth(case)
    if case.bundle == 'cxo':
        return cxo_truth(case)
    if case.bundle == 'rer':
        return rer_truth(case)
    return cpl_truth(case)
