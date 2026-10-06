"""Axes and values of the scenario generator, the real cases (A1-A21, B1-B10, C1-C16) as axis-value dicts, and what each value means for the evidence.

A case is a bundle plus a value for every axis of that bundle (the other axes sit at their baseline). Its id is the bundle and its axis values:

    aff:target=cli;spawner=main;way=direct;via=arg;...

Bundles:
  aff  affiliation: who launched the child session (tree, node, rule class, `by`)
  deb  debate: which debate, round and seat a participant holds, and the state of its cell
  sta  state: the status of an agent (or the orchestrator) after limits, errors, kills, record flaws
  cpl  coupling: one `claude -p` debate participant crossing launcher x life x report path x file state x OS (the integrated scene)
  room a room: several sub-agents of one orchestrator that share a guide file of any name and each hold a file of their own (a meeting, an agenda ...),
       or only message each other; and the lookalikes that are no room (a common document read, one shared file, code scattered over repositories ...)
  rer  a rerun: an orchestrator starts the participants of a debate with one script that redirects every run's result to the same file, they stop without a report, the orchestrator
       fixes the brief and runs the same script again (new sessions, the file now holds the new run's id)
  cxo  a Codex orchestrator: a page whose top is a Codex thread (TUI or `exec`) and the team around it: native sub-agent threads, `claude -p` and `codex exec` runs started
       from its shell (alone, or one level down a chain such as Claude > codex exec > claude -p), a guardian thread, a debate folder; and the lookalikes that are no team
       (a session that only relays the instruction by `tmux send-keys`, two orchestrators of one folder with the same words, a script that runs `codex exec`)

The tables WAY, PROMPT and the helpers at the bottom say what each value does to the *evidence* (does the environment survive, does the process lineage survive,
can a redirect be read ...). build.py realises them as files and processes; oracle.py reads the same tables to say what a correct reader can know.
"""

import hashlib
from collections import OrderedDict

# ---------------------------------------------------------------------------------------------------------------------
# Axes
# ---------------------------------------------------------------------------------------------------------------------
AXES = OrderedDict([
    # --- affiliation ---
    ('target', ('cli', 'cx_exec', 'cx_tui', 'cx_desktop', 'cx_guardian')),
    ('spawner', ('main', 'sub', 'grand')),
    ('way', ('direct', 'bg', 'detach', 'script', 'loop', 'xargs', 'tmux', 'pysub', 'envi', 'timeout', 'later')),
    ('via', ('arg', 'heredoc', 'subst', 'stdin', 'pipe')),
    ('src', ('call', 'prior', 'parts', 'sed', 'posarg', 'write', 'absent')),
    ('form', ('new', 'resume', 'session_id', 'fork', 'nopersist', 'deleted')),
    ('cwd', ('same', 'cd', 'other', 'worktree', 'symlink', 'pushd', 'var')),
    ('out', ('none', 'json_file', 'json_var', 'json_var_ext', 'log_only', 'reused', 'removed', 'overwritten')),
    ('timing', ('normal', 'seq_late', 'call_first', 'bg_no_notif')),
    ('seen', ('live', 'ended_seen', 'ended_unseen', 'restart_seen', 'restart_unseen')),
    ('decoy', ('none', 'sibling', 'xmsg', 'watcher', 'concurrent', 'twin_text', 'same_n', 'short', 'paste', 'sidmention', 'switch', 'subnoise')),
    ('bait', ('none', 'cwd_mismatch', 'text_mismatch', 'too_old', 'echo_only', 'other_user', 'pid_reuse', 'foreign')),
    ('author', ('self', 'peer', 'main', 'sub')),              # who wrote the instruction file the launch reads: the launcher itself, another session, the main session or a sub-agent
    ('busy', ('py', 'sh', 'unittest', 'child', 'child_file', 'idle', 'script_unread', 'script_var')),    # what the author is running then: a script that is no launch, the launch of an unrelated child (words in the call, or read from a file it wrote), nothing at all, or a script whose body cannot be read (no file at the path, or a path made of a variable)
    ('starter', ('call', 'person')),                          # who started the child: a launch call in some session's record, or a person in a terminal (no call anywhere)
    # --- debate ---
    ('structure', ('single', 'topics', 'deep3', 'readme', 'dot', 'flat')),
    ('kind', ('sub', 'cli', 'codex')),
    ('rpath', ('abs', 'tilde', 'short', 'folder', 'dotdot', 'var', 'var_ext', 'instr_only', 'dash_o', 'dash_o_last', 'redirect', 'dash_o_aux')),
    ('nstyle', ('plain', 'named', 'lower', 'numbered', 'collide')),
    ('rdir', ('r1', 'r01', 'round1', 'both')),
    ('role', ('writer', 'reader', 'quoter', 'negator', 'tag_only', 'failed_write', 'ghost', 'rival', 'absent', 'ref_reader')),
    ('marker', ('none', 'own', 'cross', 'quoted')),
    ('lang', ('en', 'ko')),                                   # the language the participant's instruction is written in
    ('aux', ('none', 'alias')),                               # a file the launch (`-o`, `>`) writes besides the report: none, or the report's own name in the other spelling of the round folder
    ('wmode', ('tool', 'redirect', 'tee', 'heredoc')),        # how the participant itself writes (or fails to write, or only reads beside) its file: the Write tool, or a Bash command
    ('decl', ('yes', 'no')),
    ('homonym', ('none', 'two')),
    ('fstate', ('none', 'written', 'empty')),
    ('edits', ('none', 'beside')),                            # an editing job beside the topics of a review: a guide that lists findings by bold numbers, with no round folder
    ('qform', ('inline', 'fence', 'blockquote', 'readonly', 'sentence')),    # how a `quoter` shows the earlier instruction it only talks about: a short quote in one line, a code fence, a Markdown block quote, a read-only review of it, or lines that follow a sentence that says they are a read-only quote of it
    # --- room ---
    ('guide', ('agenda', 'brief', 'readme', 'plan', 'top_readme', 'top_claude', 'top_agents', 'docs_guide', 'own', 'missing', 'late')),    # what the first instruction points at: a guide of the room's folder (by its name), a common document of the repository, a guide of the participant's own, a guide that is not on disk, or nothing (the guide comes in a later message)
    ('shape', ('beside', 'below', 'r1', 'mixed', 'deep', 'far', 'same', 'scatter', 'none')),    # where each participant's own file is: beside the guide, one folder below it, under a round folder, each one's own way (`A.md`, `notes_B.md`, `out/C.md` ...), two below, in another folder, one file for all, code in other repositories, or no file at all
    ('talk', ('none', 'peer', 'orch')),                       # the messages (`SendMessage`): none, the participants to each other, or only from the orchestrator to each of them
    ('trees', ('one', 'two')),                                # all participants belong to the orchestrator whose page is read, or every other one belongs to another orchestrator
    ('people', ('1', '2', '3', '5')),
    ('word', ('debate', 'meeting', 'mtg', 'agenda', 'none')),    # the noun the instruction uses for the work (in the language of `lang`): only wording, never evidence
    ('seatmark', ('none', 'tag', 'bracket', 'dam', 'dam_paren', 'en_participant', 'en_as', 'en_seat', 'quoted', 'negated', 'other')),    # how the instruction names the participant's seat letter: a one-letter description tag, `[ROOM-B]`, `B 담당`, `B(...) 담당`, `You are participant B`, `Work as B (...)`, `You hold seat B`; or a lookalike that is no marker: another letter quoted as an example, negated, or a letter of another meaning (`as C (not C++)`)
    ('fname', ('plain', 'prefix')),                           # the file of letter B is `B.md` or `notes_B.md`
    ('proof', ('told', 'wrote', 'both')),                     # what shows the participant's own file: the instruction names it, the participant wrote it, or both
    ('phase', ('working', 'done')),                           # the participants are still working, or have finished
    ('site', ('docs', 'top', 'dot')),                         # the room's folder: docs/meeting, meeting, .records/meeting
    ('ref', ('abs', 'rel', 'tilde')),                         # how the instruction writes a path: absolute, relative to the folder the participant works in (the repository top), or with `~`
    ('rtime', ('overlap', 'sequential', 'quiet')),            # when the participants run: at the same time, each one starts after the one before has finished, or each one starts after the one before has gone quiet and none has finished (all still running)
    ('code', ('none', 'one', 'majority', 'all', 'implied')),    # who is also told to change (or changes) a code file outside the guide's folder, beside the notes in it: nobody, the first participant only, most of them, all of them; or all of them told to implement their step with no path and no code written
    ('cite', ('none', 'inline', 'fence', 'blockquote', 'readonly', 'sentence')),    # the guide and the notes file are only the words of an earlier instruction the participant is asked to review (quoted in one line, in a code fence, in a block quote, as a read-only review, or in the lines after a sentence that says they are a read-only quote)
    ('delivery', ('ok', 'failed')),                           # what the messages between the participants come to: delivered, or answered with an error
    ('wrap', ('plain', 'exec_fence')),                        # how the instruction that is meant for the participant is written: in plain sentences, or in a code fence after "Execute these instructions:"
    ('scratch', ('none', 'tmp', 'log')),                      # what each participant also writes that is no change of the work: files in a scratch folder outside the repository (the Write tool), or the output of a command saved in a log by a redirect
    ('bundle', ('none', 'root')),                             # the room's folder alone, or one topic of a bundle: the folder above it holds the bundle's own brief (docs/records/bundle/meeting)
    ('above', ('none', 'closing', 'unnamed', 'early', 'plain', 'open')),    # a document in the bundle's folder beside the room's: none; a conclusion written after the files that names the room's folder; one that names nobody (the room is the bundle's only topic); a conclusion written before the files; a document that is no conclusion's name; a conclusion written after the files that names nobody and says some is not done
    ('copy', ('none', 'link', 'worktree')),                   # the bundle's folder reached by a link the participants write their paths with, or copied into a linked worktree of the repository where another agent works
    # --- state (life is shared with deb and cpl) ---
    ('skind', ('main', 'sub', 'grandsub', 'cli', 'codex')),
    ('life', ('running', 'normal_end', 'limit_exit', 'limit_auto', 'limit_repeat', 'sub_limit_resume', 'sub_limit_dead', 'time_limit_kill',
              'time_limit_silent', 'taskstop_kill', 'kill_resume', 'api_529', 'api_400', 'crash', 'weekly', 'no_reset', 'stalled_silent', 'codex_error')),
    ('flaw', ('none', 'torn', 'multi_proc', 'unknown_type', 'old_format', 'child_bg', 'field_gone', 'future_version')),
    ('at', ('live', 'just_ended', 'after_resume', 'after_restart', 'resume_stopped')),
    ('os', ('linux', 'mac', 'mac_nops')),
    ('entry', ('cli', 'sdk')),                                # the session the orchestrator page opens: a typed one, or the record of a `claude -p` run (`sdk-cli`: no turn-duration lines)
    ('tail', ('mid', 'end', 'prompt', 'commands', 'commands_only', 'next', 'asked')),    # what the records of that session end with: a turn in progress, a turn that ended, a new prompt nobody answered, local slash commands after the end, nothing but local slash commands, a new prompt and a call without a result that came within a second of the end of a turn, a turn that ended with a question
    ('process', ('there', 'gone')),                           # the process of that session: still running, or gone
    # --- Codex orchestrator ---
    ('top', ('claude', 'cx_tui', 'cx_exec')),                 # the provider of the orchestrator whose page is read: a Claude session, a Codex TUI thread (source "cli"), a `codex exec` thread (source "exec")
    ('chain', ('one', 'cl>cx>cl', 'cx>cl>sub', 'cx>cx')),     # how far down the subject is: started by the page's own orchestrator, or Claude > codex exec > claude -p, Codex > claude -p > its sub-agent, Codex > codex exec > codex exec
    ('subj', ('cl', 'cx', 'cx_sub', 'cl_sub')),               # the one under test: a `claude -p` run, a `codex exec` run, a native sub-agent thread, a sub-agent of a `claude -p` run
    ('host', ('main', 'sub')),                                # who acts first below the page: the orchestrator thread itself, or a native sub-agent thread of it (starts the run, or spawns a sub-agent of its own)
    ('env', ('codex', 'both', 'none', 'stale')),              # the names in the environment of the started process: those of Codex only, those of both providers, none, or those of a Codex thread that has been over for hours (a tmux server that a Codex shell started earlier passes them on)
    ('how', ('fg', 'bg', 'detach')),                          # the shell call: runs until the child ends, `&` (the child dies with the call and leaves no record), `setsid nohup ... &`
    ('look', ('live', 'ended')),                              # when the board looks: the child is still running, or everything is over
    ('lure', ('none', 'relay', 'relay_py', 'relay_script', 'relay_pyfile', 'relay_xargs', 'relay_ssh', 'relay_kube', 'relay_curl', 'twin_orch', 'twin_out', 'user_script', 'gap', 'launch_tmux', 'launch_xargs', 'launch_pyfile', 'pin_unknown', 'pin_stale_claude', 'stale_turn')),    # a lookalike: another session only relays the instruction (`tmux send-keys`, a plain call or inside `python3 -c`; a shell script or a Python file that types it; the same through `xargs`, `ssh`, `kubectl exec`; words that are an argument of `curl`), a Claude and a Codex orchestrator of one folder say the same words (the Codex one's command also redirects the child's output to a file that names it: `twin_out`), a script (no orchestrator) runs `codex exec`, a Claude session starts the child while a Codex thread of the folder has a command whose record has not come yet (`gap`; with `env=none`, the child is the Codex one's, started by `tmux new-window`, and still running); a Claude session that really starts the child itself through a wrapper that is a launch (`launch_tmux`: `tmux new-session -d '...'`, `launch_xargs`: `xargs -I{} claude -p "{}"`, `launch_pyfile`: a Python file that runs `subprocess.run(["claude", "-p", ...])`); names of a thread nobody can check in the environment of a child (`pin_unknown`: a Codex thread the index does not have, beside a running Claude call with the same words; `pin_stale_claude`: a Claude session's names that a tmux server keeps, while a Codex thread starts the child; `stale_turn`: a Codex thread's names from an earlier turn of the thread, whose present turn has only `ls`)
    ('substate', ('running', 'done', 'int_mid', 'int_after', 'parent_gone')),    # a native sub-agent: working, finished, interrupted while it worked, interrupted after it finished, its parent's process gone while it was open
    ('guard', ('none', 'one')),                               # an approval-review thread beside the team
    ('rec', ('end', 'lost', 'late')),                         # the record of the command the child was started by: written when its process ended, never (the turn ended first), or 135 s after the child started (a long run)
    ('topic', ('none', 'talk', 'talk_rel', 'talk_aux', 'talk_redir')),    # the child is a participant of a debate folder (talk/r1/<letter>.md) of the repository: `talk` (a `claude -p` run is told its report path, a `codex exec` run has it only in `-o`), `talk_rel` (`-o` is a relative path), `talk_aux` (the instruction names r1/B.md and `-o` is another file of the round), `talk_redir` (a Codex shell starts `claude -p ... > r1/A.md` and the instruction has no path)
    ('parts', ('ab', 'a', 'b')),                              # which participants are run twice: the `claude -p` one (A) and the `codex exec` one (B), or one of them
    ('stop', ('cost', 'cut')),                                # how the first runs stop: `claude -p` closes its record with `cost-state` and no end of turn, `codex exec` writes `turn_aborted`; or the record is just cut off (the process was killed)
    ('edge', ('sure', 'guess')),                              # below Claude > codex exec: the link of the codex exec run to the Claude session is proven (its text is in the call), or only guessed (the call reads its text from a file)
    # --- the orchestrator's own writes (`owr`; `top` and `copy` are the axes above) ---
    ('ow', ('tool', 'patch', 'redirect', 'mkdir_only', 'failed', 'words')),      # what the orchestrator did to make the folder: the Write tool (Claude), a patch (Codex), a shell redirect with a heredoc (and the `mkdir` of the round folder), only the `mkdir` of the round folder (the guide is a person's), an attempt that failed (the files are somebody else's), words that only say it (`echo "mkdir -p ..."`; the files are somebody else's)
    ('dshape', ('brief_r1', 'readme_r1', 'brief_only', 'declared2', 'declared1', 'notes')),    # what the folder holds: a brief.md and a round folder, a README.md and a round folder, a lone brief.md, a brief.md that declares two result files, one result file, a README.md of notes and nothing else
    ('dsite', ('repo', 'plain', 'scratch', 'state', 'top', 'docs')),             # where the folder is: below a repository's docs, in a plain folder outside any repository, in a scratch folder, under the agent's own state folder (~/.claude), at the top of the repository, at its docs folder
    ('kid', ('none', 'died', 'unlinked', 'seated')),          # a `claude -p` run beside the folder: none; started with `&` (it left no record); a record nobody started (no link to the page); started by the orchestrator, told its report path and written it (a participant of the page)
])

BUNDLES = OrderedDict([
    ('aff', ('target', 'spawner', 'way', 'via', 'src', 'form', 'cwd', 'out', 'timing', 'seen', 'os', 'decoy', 'bait', 'author', 'busy', 'starter')),
    ('deb', ('structure', 'kind', 'rpath', 'nstyle', 'rdir', 'role', 'marker', 'decl', 'homonym', 'fstate', 'life', 'os', 'lang', 'aux', 'wmode', 'edits', 'qform')),
    ('sta', ('skind', 'life', 'flaw', 'at', 'os', 'entry', 'tail', 'process')),
    ('cpl', ('spawner', 'life', 'rpath', 'os', 'fstate')),
    ('room', ('guide', 'shape', 'talk', 'trees', 'people', 'word', 'lang', 'seatmark', 'fname', 'proof', 'phase', 'site', 'ref', 'rtime', 'code', 'cite', 'delivery', 'wrap', 'scratch', 'bundle', 'above', 'copy')),
    ('cxo', ('top', 'chain', 'subj', 'host', 'env', 'how', 'look', 'lure', 'substate', 'guard', 'rec', 'topic', 'edge', 'os')),
    ('rer', ('parts', 'stop')),
    ('owr', ('top', 'ow', 'dshape', 'dsite', 'kid', 'copy')),
])

# Axes that were added after the first case ids were fixed. A case id names them only when they differ from the baseline, so the ids of every earlier case
# are the same as before (and the times, ids and files made from them). They stay out of the pairwise cover: each has its own product of cases (run.select).
OPTIONAL = {'aff': ('author', 'busy', 'starter'), 'deb': ('os', 'lang', 'aux', 'wmode', 'edits', 'qform'), 'sta': ('entry', 'tail', 'process'),
            'room': ('rtime', 'code', 'cite', 'delivery', 'wrap', 'scratch', 'bundle', 'above', 'copy'),
            'cxo': ('lure', 'substate', 'guard', 'rec', 'topic', 'edge', 'os')}

BASE = {
    'target': 'cli', 'spawner': 'main', 'way': 'direct', 'via': 'arg', 'src': 'call', 'form': 'new', 'cwd': 'same', 'out': 'none',
    'timing': 'normal', 'seen': 'live', 'decoy': 'none', 'bait': 'none', 'author': 'self', 'busy': 'py', 'starter': 'call', 'lang': 'en', 'aux': 'none', 'wmode': 'tool', 'edits': 'none',
    'qform': 'inline',
    'structure': 'single', 'kind': 'sub', 'rpath': 'abs', 'nstyle': 'plain', 'rdir': 'r1', 'role': 'writer', 'marker': 'none', 'decl': 'yes',
    'homonym': 'none', 'fstate': 'none',
    'guide': 'agenda', 'shape': 'beside', 'talk': 'none', 'trees': 'one', 'people': '3', 'word': 'meeting', 'seatmark': 'none', 'fname': 'plain', 'proof': 'both',
    'phase': 'working', 'site': 'docs', 'ref': 'abs', 'rtime': 'overlap', 'code': 'none', 'cite': 'none', 'delivery': 'ok', 'wrap': 'plain', 'scratch': 'none',
    'bundle': 'none', 'above': 'none', 'copy': 'none',
    'skind': 'cli', 'life': 'running', 'flaw': 'none', 'at': 'live', 'os': 'linux', 'entry': 'cli', 'tail': 'mid', 'process': 'there',
    'top': 'cx_tui', 'chain': 'one', 'subj': 'cl', 'host': 'main', 'env': 'codex', 'how': 'fg', 'look': 'live', 'lure': 'none', 'substate': 'running', 'guard': 'none', 'rec': 'end',
    'topic': 'none', 'edge': 'sure', 'parts': 'ab', 'stop': 'cost',
    'ow': 'tool', 'dshape': 'brief_r1', 'dsite': 'plain', 'kid': 'none',
}
# a bundle may sit on a different baseline than the global one (the debate bundle's agent is not yet running unless asked)
BUNDLE_BASE = {
    'owr': {'top': 'claude'},
    'deb': {'life': 'running'},
    'cpl': {'life': 'running', 'rpath': 'abs', 'fstate': 'none'},
}

# ---------------------------------------------------------------------------------------------------------------------
# What a launch way does to the evidence (the one place the oracle and the builder agree on)
# ---------------------------------------------------------------------------------------------------------------------
#   env       the child's environment keeps CLAUDE_CODE_SESSION_ID / CLAUDE_PID (False: `env -i`, a tmux server started elsewhere)
#   lineage   the child's process is still a descendant of the launching Claude process (False: setsid/nohup re-parent it, tmux server)
#   redirect  an output redirect of the launch can be read from the command or the script text (False: it hides inside python or a quoted tmux string)
#   time      the launch is a `claude -p` at a command position the command reader understands (the "time" rule, a guess)
WAY = {
    'direct':  {'env': True,  'lineage': True,  'redirect': True,  'time': True},
    'bg':      {'env': True,  'lineage': True,  'redirect': True,  'time': True},
    'detach':  {'env': True,  'lineage': False, 'redirect': True,  'time': True},
    'script':  {'env': True,  'lineage': True,  'redirect': True,  'time': True},
    'loop':    {'env': True,  'lineage': True,  'redirect': True,  'time': True},
    'xargs':   {'env': True,  'lineage': True,  'redirect': False, 'time': False},
    'tmux':    {'env': False, 'lineage': False, 'redirect': False, 'time': False},
    'pysub':   {'env': True,  'lineage': True,  'redirect': False, 'time': False},
    'envi':    {'env': False, 'lineage': True,  'redirect': True,  'time': True},
    'timeout': {'env': True,  'lineage': True,  'redirect': True,  'time': True},
    'later':   {'env': True,  'lineage': True,  'redirect': True,  'time': True},
}
# ways whose launch text lives in a file the launching call runs (script body, python source): it is in the records only when a prior call wrote it
FILE_WAYS = ('script', 'pysub')
# ways whose launch call carries the instruction as a complete literal argument (a refutation by text needs one)
LITERAL_WAYS = ('direct', 'bg', 'detach', 'timeout', 'envi', 'later', 'tmux', 'xargs', 'loop')
SHORT_PROMPT_CHARS = 32          # below this a content match is only a guess
# the Claude Code version the records of a state case carry (one inside every observed range of the private formats; a flaw changes it)
VERSION = '2.1.284'
FLAW_VERSION = {'old_format': '2.1.234',       # older than the first `cost-state` (2.1.235): the legitimate old format
                'future_version': '2.1.290',   # newer than the last observed one (2.1.286)
                'field_gone': '2.1.285'}       # inside the observed range of `cost-state`, where a missing known field is a format change

# the round folder of the report by `rdir` (printf pattern of the round number): the builder makes the folders from it and the oracle names the file by it; `both`
# keeps the participant's own folder `r1` and puts somebody else's `r01` beside it
ROUND_DIR = {'r1': 'r%d', 'r01': 'r%02d', 'round1': 'round%d', 'both': 'r%d'}
EDIT_DIR = 'edit1'                   # the editing job beside the topics (`edits=beside`): docs/rev/edit1, a guide and no round folder
ALIAS_DIR = 'r01'                    # the other spelling of round 1 when both are on disk


def launcher_writes(kind, rp):
    """The report file is written by the launching shell or by `-o`, not by the agent itself."""
    return rp == 'redirect' or (kind == 'cli' and rp in ('var', 'var_ext')) or (kind == 'codex' and rp in ('dash_o', 'var', 'var_ext'))


def aux_file_exists(v):
    """The file the launch writes besides the report (`aux=alias`) is on disk when the board looks: a redirect creates it at the launch, `-o` writes it when the run ends."""
    return v['aux'] == 'alias' and (v['kind'] == 'cli' or v['life'] not in ('running', 'stalled_silent'))


def wrote_itself(v):
    """The participant wrote its own report file successfully (a Write call, a patch, or a Bash command) and nobody else wrote it for it."""
    return v['role'] == 'writer' and v['fstate'] == 'written' and not launcher_writes(v['kind'], v['rpath'])


LIFE_BY_KIND = {
    'main': ('running', 'limit_auto', 'limit_repeat', 'limit_exit'),
    'sub': ('running', 'normal_end', 'sub_limit_resume', 'sub_limit_dead', 'api_529', 'api_400', 'taskstop_kill', 'kill_resume', 'stalled_silent'),
    'grandsub': ('running', 'normal_end', 'sub_limit_resume', 'sub_limit_dead'),
    'cli': ('running', 'normal_end', 'limit_exit', 'time_limit_kill', 'time_limit_silent', 'taskstop_kill', 'kill_resume', 'api_529', 'api_400',
            'crash', 'weekly', 'no_reset', 'stalled_silent'),
    'codex': ('running', 'normal_end', 'codex_error', 'crash', 'taskstop_kill', 'stalled_silent'),
}
RESUMABLE = ('limit_exit', 'sub_limit_resume', 'kill_resume', 'time_limit_kill', 'time_limit_silent', 'api_529', 'weekly', 'no_reset', 'crash',
             'limit_auto', 'limit_repeat', 'limit_exit')
DEB_LIFE = {'sub': ('running', 'normal_end', 'limit_exit', 'taskstop_kill', 'stalled_silent'), 'cli': ('running', 'normal_end', 'limit_exit', 'taskstop_kill', 'stalled_silent'),
            'codex': ('running', 'normal_end', 'taskstop_kill', 'stalled_silent')}
CPL_LIFE = ('running', 'normal_end', 'limit_exit', 'time_limit_kill', 'taskstop_kill', 'api_529', 'api_400', 'crash')


# ---------------------------------------------------------------------------------------------------------------------
# The Codex orchestrator scene (the one place the oracle and the builder agree on)
# ---------------------------------------------------------------------------------------------------------------------
CXO_RELAYS = ('relay', 'relay_py', 'relay_script', 'relay_pyfile', 'relay_xargs', 'relay_ssh', 'relay_kube', 'relay_curl')     # sessions that only pass the instruction on
CXO_TWINS = ('twin_orch', 'twin_out')
CXO_TALKS = ('talk', 'talk_rel', 'talk_aux', 'talk_redir')               # the child is a participant of a debate folder
CXO_OWN_LAUNCHES = ('launch_tmux', 'launch_xargs', 'launch_pyfile')      # a Claude session that starts the child itself, through a wrapper that is a launch
CXO_PINS = ('pin_unknown', 'pin_stale_claude', 'stale_turn')            # the names in the environment of the child cannot be checked: the thread is unknown, was a Claude session that is no launcher now, or was a turn ago
CXO_SUBSTATES_END = ('parent_gone',)


def cxo_alive(v):
    """The process of the child is there when the board looks: it is still running, and a `&` call did not take it down with the call."""
    return v['look'] == 'live' and v['how'] != 'bg' and v['subj'] in ('cl', 'cx', 'cl_sub', 'cx_sub')


def cxo_record(v):
    """The command that started the child has its `CommandExecution` record when the board looks (the last look): it is written when the process of the command ends (at
    once for a `setsid nohup ... &` call, when the child ends for a foreground call, a long while after the child started when the run is long: `rec=late`), and never when
    the turn ended first (`rec=lost`)."""
    return v['rec'] in ('end', 'late') and (v['how'] in ('detach', 'bg') or v['look'] == 'ended')


def cxo_hop(v):
    """The evidence for the child of the last hop of a case (a shell call of a Codex thread starts it): {env, proc, out, content} -> bool.
      env      the environment of the child process names the Codex thread that ran the call (alive, and the names were not cleared)
      proc     the child is still a descendant of the Codex process of the page (a foreground call; `setsid` re-parents it)
      out      the command redirects the child's output to a file, and the file holds the child's session id (the run is over): the strongest proof, it ranks above the environment
      content  the command text, with the instruction as a literal argument, is in the launching thread's `CommandExecution` record, and nobody else's launch fits it as well"""
    if v['lure'] == 'user_script':
        return {'env': False, 'proc': False, 'out': False, 'content': False}       # started in a terminal by a script: no thread's shell, no thread's record
    alive = cxo_alive(v)
    twin = v['lure'] in CXO_TWINS
    return {'env': alive and v['env'] in ('codex', 'both'), 'proc': alive and v['how'] == 'fg', 'out': v['lure'] == 'twin_out' and cxo_record(v) and v['look'] == 'ended',
            'content': cxo_record(v) and not twin}                         # two launches with the same words tie: only the environment, the lineage or an output file can tell them apart


def cxo_proof(hop):
    """Whether any of the evidence of `cxo_hop` exists."""
    return bool(hop['env'] or hop['proc'] or hop['out'] or hop['content'])


def cxo_linked(v):
    """Whether the board can say who started the subject of a shell hop at all: some evidence exists. A subject with none stands alone."""
    return cxo_proof(cxo_hop(v))


# ---------------------------------------------------------------------------------------------------------------------
# The room scene: where every file is (the one place the oracle and the builder agree on). Paths are relative to the repository top.
# ---------------------------------------------------------------------------------------------------------------------
ROOM_GUIDES = {'agenda': 'agenda.md', 'brief': 'brief.md', 'readme': 'README.md', 'plan': 'plan.md'}          # the guide of the room's own folder, by `guide`
COMMON_DOCS = {'top_readme': 'README.md', 'top_claude': 'CLAUDE.md', 'top_agents': 'AGENTS.md', 'docs_guide': 'docs/guide.md'}    # documents everybody reads: no room has one
ROOM_SITE = {'docs': 'docs/meeting', 'top': 'meeting', 'dot': '.records/meeting'}
SEAT_LETTERS = 'ABCDE'
MIXED_FILES = ('A', 'notes_B', 'out/C', 'out/notes_D', 'E')              # `shape=mixed`: the file of participant i below the room's folder, without `.md`
ROOM_TITLE = {'en': {'agenda': 'Meeting agenda: release readiness', 'brief': 'Brief: release readiness meeting', 'readme': 'Release readiness meeting', 'plan': 'Plan: release readiness'},
              'ko': {'agenda': '릴리스 준비 회의 아젠다', 'brief': '릴리스 준비 회의 지침', 'readme': '릴리스 준비 회의', 'plan': '릴리스 준비 계획'}}
COMMON_TITLE = {'top_readme': 'Project overview', 'top_claude': 'Project rules', 'top_agents': 'Agent rules', 'docs_guide': 'Developer guide'}


ROOM_BUNDLE = 'docs/records/bundle'                       # `bundle=root`: the folder above the room's, with the bundle's own brief and, beside the room's folder, the documents of `above`
ROOM_ALIAS = 'view'                                       # `copy=link`: a link to the bundle's folder; the participants write their paths with it
ABOVE_DOC = {'closing': 'CLOSING.md', 'unnamed': 'CLOSING.md', 'early': 'CLOSING.md', 'plain': 'notes.md', 'open': 'CLOSING.md'}
ABOVE_TEXT = {'closing': 'Everything is in. The meeting/ folder is closed.\n', 'unnamed': 'Everything is in; nothing is left open.\n', 'early': 'The meeting/ folder is closed.\n',
              'plain': 'The meeting/ folder is closed.\n', 'open': 'Part of it is in; the rest is not done yet.\n'}


def room_folder(v):
    """The room's folder as the participants write it (relative to the repository top): beside the others, or below the bundle's folder; with `copy=link` through the link."""
    if v['bundle'] == 'root':
        return '%s/meeting' % (ROOM_ALIAS if v['copy'] == 'link' else ROOM_BUNDLE)
    return ROOM_SITE[v['site']]


def room_disk(v, rel):
    """Where a path the participants write is on disk: the same, or below the real folder when it is written with the link."""
    return ROOM_BUNDLE + rel[len(ROOM_ALIAS):] if v['copy'] == 'link' and rel.startswith(ROOM_ALIAS + '/') else rel


def room_stem(v, i):
    """The file name (without `.md`) of participant i (0 for A)."""
    if v['shape'] == 'mixed':
        return MIXED_FILES[i].rsplit('/', 1)[-1]
    return ('notes_' if v['fname'] == 'prefix' else '') + SEAT_LETTERS[i]


def room_tree(v, i):
    """The orchestrator participant i belongs to: 1 is the one whose page is read; `trees=two` gives every second participant to another one."""
    return 2 if v['trees'] == 'two' and i % 2 == 1 else 1


def room_guide(v, i):
    """(path relative to the repository top, on disk) of the guide the first instruction of participant i points at; (None, False) when it points at none."""
    g, folder = v['guide'], room_folder(v)
    if g in ROOM_GUIDES:
        return '%s/%s' % (folder, ROOM_GUIDES[g]), True
    if g in COMMON_DOCS:
        return COMMON_DOCS[g], True
    if g == 'own':
        return '%s/agenda_%s.md' % (folder, SEAT_LETTERS[i]), True
    if g == 'missing':
        return '%s/agenda.md' % folder, False
    return None, False                                   # late: the guide comes in a later message


def room_late_guide(v):
    """The guide of `guide=late`: on disk in the room's folder, named only in a message that comes after the first instruction."""
    return '%s/agenda.md' % room_folder(v)


def room_out(v, i):
    """The file participant i is told to write, or writes (relative to the repository top); None when it has none."""
    shape, folder, stem = v['shape'], room_folder(v), room_stem(v, i)
    return {'beside': '%s/%s.md' % (folder, stem), 'below': '%s/out/%s.md' % (folder, stem), 'r1': '%s/r1/%s.md' % (folder, stem), 'mixed': '%s/%s.md' % (folder, MIXED_FILES[i]),
            'deep': '%s/out/x/%s.md' % (folder, stem), 'far': 'work/reports/%s.md' % stem, 'same': '%s/minutes.md' % folder,
            'scatter': 'repos/svc_%s/src/part.py' % SEAT_LETTERS[i].lower(), 'none': None}[shape]


def room_coders(v):
    """The participants (by index) that are also told to change a code file, or change one, besides their notes: counted over the participants of the page, in order."""
    page = [i for i in range(int(v['people'])) if room_tree(v, i) == 1]
    return set(page[:{'none': 0, 'one': 1, 'majority': len(page) // 2 + 1, 'all': len(page), 'implied': 0}[v['code']]])


def room_code(v, i):
    """The code file participant i is also told to change, or changes (relative to the repository top); None when it has none. It sits in the repository's source
    folder, outside the guide's folder."""
    return 'src/task_%s/part.py' % SEAT_LETTERS[i].lower() if i in room_coders(v) else None


def room_title(v):
    """The first heading of the guide of the room's own folder."""
    return ROOM_TITLE[v['lang']][v['guide']] if v['guide'] in ROOM_GUIDES else None


class Case:
    """A bundle and the full axis values of that bundle. `real` names the real case (A1 ...) it stands for, if any; `twin` is the id of the positive it shadows."""

    def __init__(self, bundle, values, real=None):
        self.bundle = bundle
        base = dict(BASE)
        base.update(BUNDLE_BASE.get(bundle, {}))
        self.v = dict(base)
        self.v.update(values)
        self.real = real
        self.twin_of = None
        self.core = False                      # a real case or part of the pairwise cover (they get decoy twins)

    @property
    def axes(self):
        return BUNDLES[self.bundle]

    @property
    def id(self):
        optional = OPTIONAL.get(self.bundle, ())
        return self.bundle + ':' + ';'.join('%s=%s' % (a, self.v[a]) for a in self.axes if a not in optional or self.v[a] != BASE[a])

    def key(self):
        return tuple(self.v[a] for a in self.axes)

    def __repr__(self):
        return 'Case(%s)' % self.id

    @classmethod
    def from_id(cls, cid):
        bundle, _, rest = cid.partition(':')
        return cls(bundle, dict(kv.split('=', 1) for kv in rest.split(';') if kv))


def digest(*parts, n=16):
    """The one place ids and times come from: sha1 of the parts (never hash() or random)."""
    return hashlib.sha1('\x1f'.join(str(p) for p in parts).encode()).hexdigest()[:n]


# ---------------------------------------------------------------------------------------------------------------------
# Normalisation: a combination that cannot happen is rewritten to the nearest one that can, so the generator can reject duplicates
# ---------------------------------------------------------------------------------------------------------------------
def normalize(case):
    """The case with impossible combinations folded (to a fixed point: a fold may enable another). The folded case has the same id as an existing one."""
    v = dict(case.v)
    b = case.bundle
    fold = {'aff': _aff, 'deb': _deb, 'sta': _sta, 'cpl': _cpl, 'room': _room, 'cxo': _cxo, 'rer': _rer, 'owr': _owr}[b]
    for _ in range(6):
        before = dict(v)
        fold(v)
        if v == before:
            break
    out = Case(b, v, case.real)
    out.twin_of = case.twin_of
    out.core = case.core
    return out


def is_valid(case):
    n = normalize(case)
    return n.key() == case.key()


CODEX_BAITS = ('text_mismatch', 'cwd_mismatch', 'too_old', 'echo_only', 'foreign')       # the lookalikes a Codex exec thread can have (the others need a Claude record)
AUTHOR_WAYS = ('direct', 'bg', 'detach', 'timeout', 'envi', 'later')                      # launches that read an instruction file written by somebody else
PLAIN_CWD_WAYS = ('direct', 'bg', 'detach', 'timeout', 'envi', 'later')                   # launches whose folder is told in the command (`pushd X &&`, `cd "$VAR" &&`)


def _aff(v):
    tgt = v['target']
    if tgt != 'cli':
        # a Codex thread: only a plain launch, a new session, no output file, no record tricks
        if tgt != 'cx_exec' or v['bait'] not in CODEX_BAITS:
            v['bait'] = 'none'
        v.update(via='arg', src='call', form='new', cwd='same', out='none', timing='normal', decoy='none', author='self', busy='py', starter='call')
        if v['way'] not in ('direct', 'bg', 'later'):
            v['way'] = 'direct'
        if v['spawner'] == 'grand':
            v['spawner'] = 'main'
        if v['seen'] not in ('live', 'ended_unseen'):
            v['seen'] = 'ended_unseen'
        if v['bait'] not in ('none', 'foreign'):
            v.update(spawner='main', way='direct')       # a lookalike replaces the launch: one plain call of the main session
        return
    if v['timing'] in ('call_first', 'bg_no_notif') and v['way'] not in ('bg', 'detach'):
        v['way'] = 'bg'                          # only a call that returns at once can end before the child starts
    if v['timing'] == 'seq_late' and v['way'] not in ('direct', 'loop', 'script', 'timeout', 'envi'):
        v['way'] = 'direct'                      # a sequential launch inside one blocking call
    if v['way'] == 'later':
        v['timing'] = 'normal'                   # the delay already is the lateness
    way, via, src = v['way'], v['via'], v['src']
    if via in ('arg', 'heredoc'):
        if src not in ('call', 'posarg') and not (src == 'absent' and way in FILE_WAYS):
            v['src'] = 'call'
    else:
        if src in ('call', 'posarg'):
            v['src'] = 'prior'
    if v['src'] == 'posarg':
        if way not in ('script', 'envi'):
            v['way'] = 'script'
        v['via'] = 'arg'
    if v['via'] == 'heredoc' and v['way'] in ('script', 'loop', 'xargs', 'tmux', 'pysub', 'detach'):
        v['way'] = 'direct'
    if v['way'] == 'loop':
        v['via'] = 'arg'                         # a loop walks a literal list of instructions
        if v['src'] not in ('call', 'absent'):
            v['src'] = 'call'
        v['src'] = 'call'
    if v['way'] in ('xargs', 'tmux', 'pysub'):
        v['via'] = 'arg'
        if v['src'] not in ('call', 'absent') or (v['src'] == 'absent' and v['way'] not in FILE_WAYS):
            v['src'] = 'call'
    if v['form'] in ('nopersist', 'deleted'):
        v.update(via='arg', src='call', cwd='same', out='none', timing='normal', decoy='none')
        if v['way'] in ('xargs', 'tmux', 'pysub', 'loop'):
            v['way'] = 'direct'
    if v['form'] == 'deleted' and v['seen'] == 'live':
        v['seen'] = 'ended_unseen'               # the launcher removes the records after the run, so the run is over when the board looks
    if v['decoy'] == 'switch':
        v.update(out='json_file', seen='live', os='linux', way='direct', spawner='main', form='new', via='arg', src='call')
    if v['out'] == 'overwritten':
        # the output path of an earlier launch of another session, now holding this child's id because a later run wrote over it: a plain launch of a peer session
        if v['bait'] != 'none' or v['form'] in ('nopersist', 'deleted') or v['decoy'] != 'none':
            v['out'] = 'none'
        else:
            v.update(spawner='main', via='arg', src='call', form='new', timing='normal', cwd='same', author='self')
            if v['way'] not in ('direct', 'bg'):
                v['way'] = 'direct'
    if v['starter'] == 'person' and v['author'] == 'self':
        v['author'] = 'peer'                             # somebody wrote the words; nobody's call launched the child, so there is no launcher to have written them
    if v['author'] != 'self':
        # the instruction travels in a file another session (or the other node of the tree) wrote while it was busy with something that is no launch of this child
        if v['bait'] != 'none' or v['form'] != 'new' or v['out'] != 'none' or v['decoy'] != 'none' or v['timing'] != 'normal' or v['cwd'] in ('symlink', 'var'):
            v['author'] = 'self'
        else:
            if v['via'] in ('arg', 'heredoc'):
                v['via'] = 'subst'
            if v['src'] not in ('prior', 'write'):
                v['src'] = 'prior'
            if v['way'] not in AUTHOR_WAYS:
                v['way'] = 'direct'
            if v['author'] == 'main':
                v['spawner'] = 'sub'                     # the main session writes, a sub-agent launches
            elif v['author'] == 'sub':
                v['spawner'] = 'main'                    # a sub-agent writes, the main session launches
            elif v['spawner'] == 'grand':
                v['spawner'] = 'main'
    if v['author'] == 'self':
        v['busy'] = 'py'
        v['starter'] = 'call'
    elif v['starter'] == 'person':
        # a person typed the command in a terminal: there is no launch call in any record, so the way, the spawner, the command text and the folder of a launch mean nothing
        v.update(spawner='main', way='direct', via='subst', cwd='same')
        if v['busy'] in ('child', 'child_file'):
            v['busy'] = 'py'                             # a call that launches a child is a launch call: not this scene
    elif v['busy'] in ('idle', 'script_unread', 'script_var'):
        v['busy'] = 'py'                                 # beside a real launch call, only the five busy values with a known meaning
    if v['cwd'] in ('pushd', 'var'):
        if v['way'] not in PLAIN_CWD_WAYS or v['src'] == 'posarg' or v['decoy'] in ('switch', 'short', 'concurrent', 'twin_text'):
            v['cwd'] = 'cd'
        elif v['cwd'] == 'var' and v['bait'] == 'none' and v['src'] == 'absent':
            v['src'] = 'prior'                           # a folder the command does not tell needs another proof (the text): never the time rule alone
        if v['cwd'] == 'var' and v['via'] not in ('arg', 'heredoc') and v['src'] in ('call', 'posarg'):
            v['src'] = 'prior'
    if v['decoy'] in ('concurrent', 'twin_text', 'subnoise'):
        v['spawner'] = 'main'
    if v['decoy'] == 'short' and v['via'] == 'arg' and v['src'] == 'absent':
        v['src'] = 'call'
    if v['spawner'] == 'grand' and v['decoy'] in ('concurrent', 'twin_text', 'subnoise'):
        v['decoy'] = 'none'
    if v['bait'] == 'foreign':
        # the launch stays exactly as the positive's; only the child is a stranger (other text, no Claude above it, another id in the output file).
        # Only a complete literal argument can refute it, so the launch has to be one. The exception is a launch whose folder nothing tells (`cd "$VAR"`): there
        # the stranger sits in another project and the launch reads its instruction from a file, so neither the words nor the folder can be compared.
        if v['cwd'] == 'var' and v['via'] not in ('arg', 'heredoc'):
            v.update(src='prior' if v['src'] not in ('prior', 'write') else v['src'], form='new')
        else:
            v.update(via='arg', src='call', form='new')
        if v['way'] not in LITERAL_WAYS:
            v['way'] = 'direct'
        if v['decoy'] == 'switch':
            v['decoy'] = 'none'
        return
    if v['bait'] != 'none':
        # a twin: the real launch is gone, only the bait is left; nothing else about the launch matters, so the launch axes fall to the plain ones
        v.update(form='new', out='none', timing='normal', decoy='none', spawner='main', author='self', busy='py', starter='call')
        if v['cwd'] in ('pushd', 'var'):
            v['cwd'] = 'same'
        if v['way'] in ('xargs', 'tmux', 'pysub', 'loop', 'script', 'envi'):
            v['way'] = 'direct'
        v.update(via='arg', src='call')
        if v['bait'] in ('other_user', 'pid_reuse'):
            v['seen'] = 'live'
        elif v['seen'] == 'live':
            v['seen'] = 'ended_unseen'


def _deb(v):
    kind, rp, role = v['kind'], v['rpath'], v['role']
    if kind == 'sub' and rp in ('dash_o', 'dash_o_last', 'redirect', 'dash_o_aux'):
        v['rpath'] = 'abs'
    if kind == 'sub' and rp == 'var':
        v['rpath'] = 'var_ext'                   # a sub-agent has no shell scope to define the variable in
    if kind == 'cli' and rp in ('dash_o', 'dash_o_last'):
        v['rpath'] = 'redirect'
    if kind == 'codex' and rp == 'redirect':
        v['rpath'] = 'dash_o'
    if v['structure'] == 'flat':
        v.update(rdir='r1', marker='none')
        if v['homonym'] == 'two':
            v.update(decl='yes', rpath='short')          # two flat reviews that declare the same result file, and a path that names only the file
        else:
            v['homonym'] = 'none'
        if v['nstyle'] not in ('plain', 'named'):
            v['nstyle'] = 'plain'
        if v['rpath'] not in ('abs', 'folder', 'short'):
            v['rpath'] = 'abs'
        if role not in ('writer', 'reader', 'ref_reader'):
            v['role'] = 'writer'
        if role == 'ref_reader':
            v['rpath'] = 'abs'
    else:
        v['decl'] = 'yes'
    if v['rdir'] != 'r1' and v['structure'] not in ('single', 'topics'):
        v['rdir'] = 'r1'
    if v['rdir'] == 'both' and (v['role'] != 'writer' or v['homonym'] == 'two' or v['nstyle'] not in ('plain', 'named')):
        v['rdir'] = 'r1'                                 # the folder of the other spelling holds somebody else's file of the same name: a writer's scene
    if v['marker'] == 'cross' and v['structure'] != 'topics':
        v['marker'] = 'own'
    if v['homonym'] == 'two':
        if v['structure'] != 'flat':
            v.update(structure='single', rpath='short', marker='none', nstyle='plain', rdir='r1', role='writer')
        else:
            v.update(marker='none', nstyle='plain', rdir='r1', role='writer')
        if v['kind'] == 'sub':
            v['kind'] = 'cli'
    if v['nstyle'] == 'collide':
        v['role'] = 'writer'
    role = v['role']
    if role in ('reader', 'ref_reader', 'quoter', 'negator', 'ghost', 'tag_only', 'failed_write', 'rival', 'absent'):
        if not (role == 'quoter' and v['marker'] == 'quoted'):
            v['marker'] = 'none'                 # the instruction of these roles never opens with a marker (a quoter may quote one)
    if v['marker'] == 'quoted' and role != 'quoter':
        v['marker'] = 'none'
    if role in ('reader', 'ref_reader', 'quoter', 'negator', 'ghost'):
        if v['rpath'] not in ('abs', 'folder', 'tilde'):
            v['rpath'] = 'abs'
        v['life'] = 'running'
        v['fstate'] = 'written' if role in ('reader', 'ref_reader') else 'none'
        if role == 'ghost':
            v.update(structure='single', marker='none', nstyle='plain', rdir='r1')
    elif role == 'rival':
        v.update(kind='sub', rpath='abs', life='running', fstate='none', marker='none', structure='single', nstyle='plain', rdir='r1', homonym='none')
    elif role in ('tag_only', 'failed_write'):
        v.update(rpath='instr_only', life='running', fstate='none', marker='none', kind='sub')
        if v['structure'] == 'flat':
            v['structure'] = 'single'
            v['decl'] = 'yes'
    elif role == 'absent':
        v.update(kind='sub', rpath='abs', life='running', fstate='written', marker='none', homonym='none', nstyle='plain', rdir='r1', structure='single')
    else:
        if v['rpath'] == 'instr_only':
            v['marker'] = 'own'
            if v['nstyle'] not in ('plain', 'named'):
                v['nstyle'] = 'plain'
        if v['life'] not in DEB_LIFE[v['kind']]:
            v['life'] = 'running'
        if v['rpath'] in ('var_ext', 'dash_o_last') and v['marker'] == 'cross':
            v['marker'] = 'own'
        if v['rpath'] == 'dash_o_last':
            v['marker'] = 'none'
    if v['structure'] != 'flat':
        v['decl'] = 'yes'
    if v['role'] != 'writer' or v['kind'] == 'sub':
        v['os'] = 'linux'                        # the process table only matters for a writer whose process may still be there (a sub-agent has none)
    elif v['os'] == 'mac_nops' and v['life'] not in ('running', 'stalled_silent'):
        v['os'] = 'mac'
    if role not in ('writer', 'reader', 'ref_reader', 'quoter', 'negator', 'ghost', 'tag_only', 'failed_write', 'rival'):
        v['lang'] = 'en'                         # no instruction text of its own
    if role != 'quoter':
        v['qform'] = 'inline'
    elif v['qform'] != 'inline':
        v.update(marker='none', wmode='tool')    # the earlier instruction is the only thing quoted
    _report_file(v, v['kind'])
    _aux(v)
    _wmode(v)
    if v['edits'] == 'beside' and v['structure'] != 'topics':
        v['edits'] = 'none'                      # the job sits beside the topics of a review, below the common guide that groups them


def _aux(v):
    """A launch that also writes the report's own name into the other spelling of the round folder: a writer that is launched (a sub-agent has no launch), in a
    debate with round folders, with a path in its instruction that names the report (so the launch's own output flag is the other one)."""
    if v['aux'] != 'alias':
        return
    if v['kind'] == 'sub' or v['role'] != 'writer' or v['structure'] not in ('single', 'topics') or v['homonym'] == 'two' or v['nstyle'] not in ('plain', 'named'):
        v['aux'] = 'none'
        return
    v['rdir'] = 'both'
    if v['rpath'] not in ('abs', 'short'):
        v['rpath'] = 'abs'
    _report_file(v, v['kind'])


def _wmode(v):
    """A Bash command in place of the Write tool: for a writer that writes its own file, a reader, a quoter or a participant known by its tag alone that writes
    something else beside the report, a participant whose write fails. A Codex participant writes by patch, and every other role does not write at all."""
    if v['wmode'] == 'tool':
        return
    role = v['role']
    if v['kind'] == 'codex' or role not in ('writer', 'reader', 'failed_write', 'quoter', 'tag_only') or (role == 'writer' and not wrote_itself(v)):
        v['wmode'] = 'tool'


def _report_file(v, kind):
    """A file the launching shell writes exists from the moment of the launch (a redirect creates it empty and the output lands when the run ends); the file of `-o` is
    written at the end of the run. A combination that contradicts this cannot happen."""
    if v['role'] != 'writer':
        return
    rp = v['rpath']
    if kind == 'cli' and rp in ('redirect', 'var', 'var_ext'):
        if v['fstate'] == 'none' or (v['life'] in ('running', 'stalled_silent') and v['fstate'] == 'written'):
            v['fstate'] = 'empty'
    elif kind == 'codex' and rp in ('dash_o', 'var', 'var_ext') and v['life'] in ('running', 'stalled_silent') and v['fstate'] == 'written':
        v['fstate'] = 'none'


def _room(v):
    n = int(v['people'])
    if v['guide'] in COMMON_DOCS and v['shape'] in ('beside', 'below', 'r1', 'deep'):
        v['shape'] = 'far'                       # a common document has no folder of its own to write beside
    if v['shape'] in ('r1', 'mixed'):
        v['fname'] = 'plain'                     # the reports under a round folder are named by their letter; each participant of a mixed room has a name of its own
        v['talk'] = 'none'                       # a folder with a round folder is a debate whatever else is said
        if v['guide'] not in ROOM_GUIDES:
            v['guide'] = 'agenda'
    if n == 1:
        v.update(talk='none', trees='one')       # nobody to message, nobody beside it
        if v['shape'] == 'same':
            v['shape'] = 'beside'
    if v['shape'] == 'none':
        v['proof'] = 'told'                      # no file: nothing is told and nothing is written
    if v['seatmark'] in ('dam', 'dam_paren'):
        v['lang'] = 'ko'
    elif v['seatmark'] in ('en_participant', 'en_as', 'en_seat'):
        v['lang'] = 'en'
    files = v['shape'] in ('beside', 'below', 'mixed') and v['guide'] in ROOM_GUIDES and n > 1
    if v['cite'] != 'none':
        if n == 1 or v['guide'] not in ROOM_GUIDES or v['shape'] not in ('beside', 'below', 'mixed', 'r1'):
            v['cite'] = 'none'
        else:
            v.update(proof='told', seatmark='none', talk='none', trees='one', code='none', rtime='overlap')      # the scene is the words of an earlier instruction and nothing done
    if v['code'] != 'none':
        if not files or v['cite'] != 'none':
            v['code'] = 'none'                   # code is a second file beside the notes in the guide's folder: it needs a room that could be one
        else:
            v['trees'] = 'one'
            if v['code'] == 'majority' and n == 2:
                v['code'] = 'all'                # two of two
    for name in ('wrap', 'scratch'):
        if v[name] != BASE[name] and (not files or v['cite'] != 'none'):
            v[name] = BASE[name]                 # the words of the instruction and the files beside the notes need a room that could be one, and an instruction that is meant for the participant
        elif v[name] != BASE[name]:
            v['trees'] = 'one'
    if v['rtime'] in ('sequential', 'quiet'):
        if not files or v['cite'] != 'none':
            v['rtime'] = 'overlap'
        else:
            v['trees'] = 'one'
            if v['rtime'] == 'quiet':
                v['phase'] = 'working'           # nobody has finished
            if v['talk'] == 'peer':
                v['talk'] = 'none'               # runs that never coexist send each other nothing (the orchestrator may still message each one)
    if v['talk'] != 'peer' or n == 1:
        v['delivery'] = 'ok'
    if v['bundle'] == 'root':
        if n == 1 or v['guide'] not in ROOM_GUIDES or v['shape'] not in ('beside', 'below') or v['cite'] != 'none' or v['trees'] != 'one':
            v['bundle'] = 'none'                 # a bundle's topic is a room that could be one (a guide of its own folder, files beside or below it, all of them of this page)
        else:
            v.update(site='docs', code='none', rtime='overlap', wrap='plain', scratch='none', talk='none', delivery='ok', seatmark='none', fname='plain')
    if v['bundle'] != 'root':
        v.update(above='none', copy='none')


def _sta(v):
    sk, life, flaw, at = v['skind'], v['life'], v['flaw'], v['at']
    if life not in LIFE_BY_KIND[sk]:
        v['life'] = life = LIFE_BY_KIND[sk][0]
    ongoing = life in ('running', 'stalled_silent')
    if ongoing:
        if at not in ('live', 'after_restart'):
            v['at'] = 'live'
    elif life == 'normal_end':
        if at not in ('just_ended', 'after_restart'):
            v['at'] = 'just_ended'
    else:
        if at == 'live':
            v['at'] = 'just_ended'
        if v['at'] == 'resume_stopped' and (sk != 'cli' or life not in RESUMABLE or flaw != 'none'):
            v['at'] = 'after_resume'                 # only a `claude -p` run is resumed by a call the main session can stop again
        if v['at'] == 'after_resume' and life not in RESUMABLE:
            v['at'] = 'just_ended'
    if flaw == 'torn' and (sk not in ('cli', 'sub') or life not in ('normal_end', 'limit_exit', 'sub_limit_resume')):
        v['flaw'] = 'none'
    if flaw == 'multi_proc' and (sk not in ('cli', 'main') or life not in ('running', 'normal_end')):
        v['flaw'] = 'none'
    if flaw in ('old_format', 'future_version') and (sk not in ('cli', 'sub') or life not in ('normal_end', 'limit_exit', 'time_limit_kill', 'crash', 'sub_limit_resume')):
        v['flaw'] = 'none'
    if flaw == 'field_gone' and (sk != 'cli' or life not in ('normal_end', 'limit_exit', 'time_limit_kill', 'api_529')):
        v['flaw'] = 'none'                       # the lost field is in the `cost-state` line a finished `claude -p` run writes
    if flaw == 'child_bg' and (sk != 'cli' or life not in ('running', 'normal_end')):
        v['flaw'] = 'none'
    if v['os'] == 'mac_nops' and life not in ('running', 'stalled_silent', 'crash'):
        v['os'] = 'mac'
    if sk != 'main' or life != 'running':
        v.update(entry='cli', tail='mid', process='there')       # only an orchestrator that was not stopped by a limit has the shapes of a session's end
    else:
        if v['entry'] == 'sdk' and v['tail'] in ('commands', 'commands_only'):
            v['tail'] = 'end'                    # a `claude -p` run has no local slash commands
        if (v['entry'], v['tail'], v['process']) != ('cli', 'mid', 'there'):
            v.update(at='live', flaw='none', os='linux')


def _cpl(v):
    if v['life'] not in CPL_LIFE:
        v['life'] = 'running'
    if v['rpath'] in ('dash_o', 'dash_o_last', 'dash_o_aux'):
        v['rpath'] = 'abs'
    if v['os'] == 'mac_nops':
        v['os'] = 'mac'
    if v['rpath'] == 'instr_only':
        v['marker'] = 'own'
    else:
        v['marker'] = 'none'
    v['role'] = 'writer'
    _report_file(v, 'cli')


def _rer(v):
    pass                                         # every combination of the one axis can happen


def _cxo(v):
    chain, lure = v['chain'], v['lure']
    # the chain names the top and the subject; a Claude top exists only in the chain that starts with Claude (a Claude-only team is the affiliation bundle's business)
    if chain == 'cl>cx>cl':
        v.update(top='claude', subj='cl', host='main', lure='none', guard='none', topic='none', substate='running')
    else:
        v['edge'] = 'sure'
        if v['top'] == 'claude':
            v['top'] = 'cx_tui'
        if chain == 'cx>cl>sub':
            v['subj'] = 'cl_sub'
        elif chain == 'cx>cx':
            v['subj'] = 'cx'
        elif v['subj'] == 'cl_sub':
            v['subj'] = 'cl'
        if chain != 'one':
            v.update(lure='none', topic='none', substate='running')
            if v['rec'] == 'late':
                v['rec'] = 'end'
        if chain == 'cx>cl>sub':
            v.update(env='codex', how='fg', rec='end')   # the sub-agent lives inside the `claude -p` run the plain first call started: nothing of the last hop is left to vary
        if v['env'] == 'both':
            v['env'] = 'codex'                       # both providers' names are in the environment only below a Claude orchestrator
    if chain != 'one' and v['how'] == 'bg':
        v['how'] = 'detach'                          # in every chain: a Codex `&` call takes the child down with the call (nohup does not help, only `setsid` does), so there is no run to put in a chain
    if chain == 'cl>cx>cl':
        if v['edge'] == 'guess':
            # the codex exec run is linked to the Claude session by a guess only when nothing proves it any more: it ended (`setsid` child alive after it), the call reads its text from a file
            if not (v['env'] == 'both' and v['how'] == 'detach' and v['look'] == 'live'):
                v['edge'] = 'sure'
        if v['rec'] == 'late':
            v['rec'] = 'end'
    if v['subj'] != 'cx_sub':
        v['substate'] = 'running'
    if lure != 'none':
        if chain != 'one':
            v['lure'] = lure = 'none'
        else:
            v.update(host='main', topic='none', guard='none', substate='running')
            v['subj'] = 'cx' if lure == 'user_script' else 'cl'
            if lure in CXO_RELAYS:
                v['top'] = 'cx_tui'                  # the instruction goes into a terminal: the TUI
            if lure == 'user_script':
                v.update(env='none', how='fg', rec='end')       # a script in a terminal: no Codex above it, nothing to leave in a shell, no record of a call anywhere
            elif lure == 'gap':
                if v['env'] == 'none':
                    v.update(how='detach', rec='end', look='live')   # the child is the Codex thread's: its shell started it with `tmux new-window` (nothing of Codex or Claude in its environment, the command's record has not come), and a Claude call with the same words is still running
                else:
                    v.update(env='codex', how='fg', rec='end', look='ended')    # the Claude session's own foreground call; the child is over, so only the words can tell whose it is
            elif lure == 'launch_tmux':
                v.update(env='none', how='detach', rec='end')        # `tmux new-session -d`: the child is the tmux server's, with none of the names
            elif lure in ('launch_xargs', 'launch_pyfile'):
                v.update(env='codex', how='fg', rec='end')
            elif lure == 'pin_unknown':
                v.update(env='none', how='fg', rec='end', look='live')   # the child has the names of a thread the index does not know, and a Claude call with the same words is running
            elif lure == 'pin_stale_claude':
                v.update(env='none', how='detach', rec='end', look='live')   # a Codex thread starts the child with `tmux new-window`; the tmux server keeps the names of a Claude session
            elif lure == 'stale_turn':
                v.update(env='stale', how='detach', rec='end', look='live')   # the names are the Codex thread's own, from an earlier turn
            elif v['how'] == 'bg':
                v['how'] = 'detach'
            if lure in CXO_TWINS:
                v['rec'] = 'end'                     # the tie between the two launches does not depend on the record of one of them
    if v['env'] == 'stale':
        if chain == 'one' and v['lure'] in ('none', 'stale_turn') and v['subj'] == 'cl':
            # a Claude session starts the child with `tmux new-window` and the names of a Codex thread that is long over come with the tmux server
            v.update(host='main', how='detach', look='live', rec='end', topic='none', guard='none', substate='running')
        else:
            v['env'] = 'codex'
    if v['subj'] == 'cx_sub':
        # a native sub-agent has no shell call, no environment, no record to lose: its life is the sub-agent's own, and the page's process
        v.update(how='fg', env='codex', rec='end', topic='none', lure='none')
        v['look'] = 'ended' if v['substate'] in CXO_SUBSTATES_END else 'live'
    elif v['how'] == 'bg':
        v.update(env='codex', rec='end', topic='none', look='live')      # the child died with the call: there is nothing to look at later
    if v['rec'] == 'lost' and not (v['how'] == 'detach' or v['look'] == 'ended'):
        v['rec'] = 'end'                         # a foreground call that is still running has no record yet anyway
    if v['rec'] == 'late' and not (chain == 'one' and v['how'] == 'fg' and v['look'] == 'ended' and v['env'] == 'none' and v['subj'] in ('cl', 'cx') and v['lure'] == 'none'
                                    and v['topic'] == 'none'):
        v['rec'] = 'end'                         # a record that comes late matters where nothing else linked the child before it: no environment, a foreground call, a run that is over
    if v['topic'] != 'none' and (chain != 'one' or v['subj'] not in ('cl', 'cx') or v['host'] != 'main' or v['lure'] != 'none' or v['how'] == 'bg'):
        v['topic'] = 'none'
    if v['topic'] in ('talk_rel', 'talk_aux') and v['subj'] != 'cx':
        v['topic'] = 'talk'                      # `-o` is a `codex exec` option
    if v['topic'] == 'talk_redir' and v['subj'] != 'cl':
        v['topic'] = 'talk'                      # the redirect is of a `claude -p` run's output
    if v['top'] == 'claude':
        v['guard'] = 'none'
    if v['os'] != 'linux':
        # macOS: `ps` does not say which file a process holds open, so a Codex process cannot be told from another program. It matters where both providers' names are in
        # the environment of a grandchild and the chain is read from the process table, and where a running participant's report is known from the arguments of its process
        both = chain == 'cl>cx>cl' and v['env'] == 'both' and v['look'] == 'live' and v['edge'] == 'sure'
        talk = chain == 'one' and v['topic'] in CXO_TALKS and v['look'] == 'live' and v['how'] == 'fg' and v['env'] == 'codex' and v['lure'] == 'none'      # a running participant: its argv would say its report
        v['os'] = 'mac' if (both or talk) else 'linux'


# ---------------------------------------------------------------------------------------------------------------------
# The orchestrator's own writes: which folder the page lists
# ---------------------------------------------------------------------------------------------------------------------
OWR_ROUNDED = ('brief_r1', 'readme_r1')                  # shapes that have a round folder (r1/) on disk
OWR_FLAT = ('brief_only', 'declared2', 'declared1')      # shapes that are a lone brief.md
OWR_DEBATE = ('brief_r1', 'readme_r1', 'declared2')      # shapes that are a debate folder: a guide and a round folder, or a brief.md that declares two result files
OWR_GUIDE = {'brief_r1': 'brief.md', 'readme_r1': 'README.md', 'brief_only': 'brief.md', 'declared2': 'brief.md', 'declared1': 'brief.md', 'notes': 'README.md'}
OWR_REPO_SITES = ('repo', 'top', 'docs')                 # sites inside the repository the page works in
OWR_SITE_REL = {'repo': 'work/repo/docs/talk', 'plain': 'work/plain/talk', 'scratch': 'scratch/talk', 'state': '.claude/plans/talk', 'top': 'work/repo', 'docs': 'work/repo/docs'}   # the folder, below the case's HOME
OWR_WRITERS = ('tool', 'patch', 'redirect', 'mkdir_only')    # what the orchestrator does that can name the folder


def owr_rounds_on_disk(v):
    """Whether the folder has a round folder when the board looks: the shape's own, or the one the orchestrator makes with `mkdir` of the round folder of a debate shape."""
    return v['dshape'] in OWR_ROUNDED or (v['ow'] == 'mkdir_only' and v['dshape'] in OWR_DEBATE)


def owr_is_debate(v):
    """The folder is a debate folder on disk: a guide and a round folder, or a brief.md that declares two result files."""
    return owr_rounds_on_disk(v) or v['dshape'] == 'declared2'


def owr_named_by_write(v):
    """The orchestrator's successful write names the folder: a guide it wrote, or the round folder it made (the `mkdir` of any other folder names nothing)."""
    if v['ow'] not in OWR_WRITERS:
        return False
    return v['dshape'] in OWR_DEBATE if v['ow'] == 'mkdir_only' else True


def owr_walked(v):
    """The folder is one a walk of the repository lists: a debate folder, or a lone brief.md (a title). A README.md alone confirms nothing."""
    return v['dsite'] in OWR_REPO_SITES and (owr_rounds_on_disk(v) or v['dshape'] in OWR_FLAT)


def owr_listed(v):
    """Whether the page lists the folder: the walk of the repository, the child that is seated in it, or the orchestrator's own write when the folder is a debate on disk
    and is not under the agent's state folder."""
    return owr_walked(v) or v['kid'] == 'seated' or (owr_named_by_write(v) and owr_is_debate(v) and v['dsite'] != 'state')


def _owr(v):
    if v['top'] == 'claude' and v['ow'] == 'patch':
        v['ow'] = 'tool'                         # a patch is Codex's tool, the Write tool Claude's
    elif v['top'] != 'claude' and v['ow'] == 'tool':
        v['ow'] = 'patch'
    if v['copy'] != 'worktree' or v['dsite'] != 'repo':
        v['copy'] = 'none'                       # the copy is of a folder of the repository, in a worktree where another agent works
    if v['kid'] == 'seated' and (v['dshape'] not in OWR_ROUNDED or v['dsite'] in ('state', 'top', 'docs')):
        v['kid'] = 'none'                        # a participant is told a report path in a round folder, below a folder that is no top or docs of the repository and no state folder
    if v['kid'] == 'unlinked' and v['dsite'] == 'state':
        v['kid'] = 'none'


# ---------------------------------------------------------------------------------------------------------------------
# The real cases, as axis values. A letter and a number name each one (A affiliation, B debate, C state); the value dicts say what makes each of them tick.
# ---------------------------------------------------------------------------------------------------------------------
REAL = OrderedDict([
    # affiliation
    ('A1', ('aff', dict(way='loop', timing='seq_late', decoy='short', cwd='other', seen='ended_unseen'))),         # sequential /init children, 90-160 s late, outside the parent's folder
    ('A2', ('aff', dict(way='bg', via='subst', src='parts', seen='ended_unseen'))),                                  # prompt = common file + role file, joined at launch
    ('A3', ('aff', dict(way='script', via='subst', src='prior', seen='ended_unseen'))),                              # prompt written in an earlier call, launched by a script
    ('A4', ('aff', dict(decoy='short', seen='ended_unseen'))),                                                       # a short slash command
    ('A5', ('aff', dict(decoy='same_n', way='bg', seen='ended_unseen'))),                                            # the same prompt launched many times
    ('A6', ('aff', dict(decoy='sibling', way='bg', via='subst', src='parts', seen='ended_unseen'))),                 # siblings share boilerplate
    ('A7', ('aff', dict(decoy='xmsg', way='bg', seen='ended_unseen'))),                                              # siblings message each other
    ('A8', ('aff', dict(form='resume', seen='ended_unseen'))),                                                       # the parent resumes the same child
    ('A9', ('aff', dict(out='json_file', way='bg', seen='ended_unseen'))),                                           # the child's session id sits in the output file
    ('A10', ('aff', dict(out='reused', way='bg', seen='ended_unseen'))),                                             # the output path was reused: the earlier child's proof is overwritten
    ('A11', ('aff', dict(spawner='sub', way='envi', via='arg', src='posarg', seen='ended_unseen'))),                 # sub-agent + `env -i` script + positional prompt
    ('A12', ('aff', dict(form='deleted', seen='ended_unseen'))),                                                     # the launcher removed the child's records
    ('A13', ('aff', dict(form='nopersist', seen='live', way='bg'))),                                                 # --no-session-persistence, alive
    ('A14', ('aff', dict(spawner='sub', src='absent', via='subst', seen='live'))),                                   # the environment names the tree, not the node
    ('A15', ('aff', dict(decoy='switch'))),                                                                          # the parent switched sessions (/resume)
    ('A16', ('aff', dict(decoy='concurrent', src='call', seen='ended_unseen'))),                                     # two orchestrators launch at the same time
    ('A17', ('aff', dict(decoy='watcher', way='bg', seen='ended_unseen'))),                                          # long watcher calls overlap the launch
    ('A18', ('aff', dict(decoy='same_n', cwd='cd', seen='ended_unseen'))),                                           # wrong folder first, then the same prompt again
    ('A19', ('aff', dict(decoy='sidmention', seen='ended_unseen'))),                                                 # a session only mentions the child's id
    ('A20', ('aff', dict(timing='call_first', way='bg', seen='ended_unseen'))),                                      # the launching call ended before the child's first record
    ('A21', ('aff', dict(target='cx_desktop', seen='live'))),                                                        # Codex Desktop / TUI / guardian threads are not children
    # debate
    ('B1', ('deb', dict(kind='cli', nstyle='named', rpath='folder'))),                                               # A_flow / B_gate / C_github
    ('B2', ('deb', dict(kind='codex', nstyle='named', rpath='dash_o_last'))),                                        # the report differs from the -o last-message file
    ('B3', ('deb', dict(kind='codex', nstyle='numbered', rpath='var'))),                                             # `-o $D/r1/astra$n.md` from another folder
    ('B4', ('deb', dict(kind='codex', rpath='dash_o', nstyle='named'))),                                             # -o is the report; stdout/stderr logs are not
    ('B5', ('deb', dict(structure='flat', decl='no', rpath='folder'))),                                              # flat review without declared reviewers
    ('B6', ('deb', dict(structure='deep3', kind='cli', rpath='folder', nstyle='named'))),                            # deep review folders mixed with edit briefs
    ('B7', ('deb', dict(role='reader', structure='topics'))),                                          # a reader tagged B reads B's report
    ('B8', ('deb', dict(role='negator'))),                                                                           # "no need to write B.md"; a ghost absolute path is the ghost role
    ('B9', ('deb', dict(nstyle='collide', rdir='r01'))),                                                             # r01/ round1/ and A.md next to A_flow.md
    ('B10', ('deb', dict(role='absent'))),                                                                           # the report is on disk, no session names it
    # state
    ('C1', ('sta', dict(skind='cli', life='limit_exit', at='just_ended'))),
    ('C2', ('sta', dict(skind='sub', life='sub_limit_resume', at='just_ended'))),
    ('C3', ('sta', dict(skind='main', life='limit_auto', at='just_ended'))),
    ('C4', ('sta', dict(skind='main', life='limit_repeat', at='just_ended'))),
    ('C5', ('sta', dict(skind='cli', life='weekly', at='just_ended'))),
    ('C6', ('sta', dict(skind='cli', life='api_529', at='just_ended'))),
    ('C7', ('sta', dict(skind='cli', life='time_limit_kill', at='just_ended'))),
    ('C8', ('sta', dict(skind='cli', life='kill_resume', at='after_resume'))),
    ('C9', ('sta', dict(skind='cli', life='running', flaw='child_bg', at='live'))),
    ('C10', ('sta', dict(skind='cli', life='normal_end', flaw='torn', at='just_ended'))),
    ('C11', ('sta', dict(skind='cli', life='running', flaw='multi_proc', at='live'))),
    ('C12', ('sta', dict(skind='cli', life='stalled_silent', at='live'))),
    ('C13', ('aff', dict(form='nopersist', seen='live', way='bg'))),                                                 # an invisible live child: the same scene as A13
    ('C14', ('sta', dict(skind='cli', life='limit_exit', at='after_restart'))),
    ('C15', ('sta', dict(skind='codex', life='codex_error', at='just_ended'))),
    ('C16', ('sta', dict(skind='grandsub', life='sub_limit_resume', at='just_ended'))),
])


def real_cases():
    """[(name, Case)] for every real case, normalised (a real case is never dropped; if the normaliser folds it, the folded case stands for it)."""
    out = []
    for name, (bundle, vals) in REAL.items():
        c = normalize(Case(bundle, vals, real=name))
        out.append((name, c))
    return out
