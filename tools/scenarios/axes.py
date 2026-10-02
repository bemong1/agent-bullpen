"""Axes and values of the scenario generator, the real cases (A1-A21, B1-B10, C1-C16) as axis-value dicts, and what each value means for the evidence.

A case is a bundle plus a value for every axis of that bundle (the other axes sit at their baseline). Its id is the bundle and its axis values:

    aff:target=cli;spawner=main;way=direct;via=arg;...

Bundles:
  aff  affiliation: who launched the child session (tree, node, rule class, `by`)
  deb  debate: which debate, round and seat a participant holds, and the state of its cell
  sta  state: the status of an agent (or the orchestrator) after limits, errors, kills, record flaws
  cpl  coupling: one `claude -p` debate participant crossing launcher x life x report path x file state x OS (the integrated scene)

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
    # --- state (life is shared with deb and cpl) ---
    ('skind', ('main', 'sub', 'grandsub', 'cli', 'codex')),
    ('life', ('running', 'normal_end', 'limit_exit', 'limit_auto', 'limit_repeat', 'sub_limit_resume', 'sub_limit_dead', 'time_limit_kill',
              'time_limit_silent', 'taskstop_kill', 'kill_resume', 'api_529', 'api_400', 'crash', 'weekly', 'no_reset', 'stalled_silent', 'codex_error')),
    ('flaw', ('none', 'torn', 'multi_proc', 'unknown_type', 'old_format', 'child_bg', 'field_gone', 'future_version')),
    ('at', ('live', 'just_ended', 'after_resume', 'after_restart', 'resume_stopped')),
    ('os', ('linux', 'mac', 'mac_nops')),
])

BUNDLES = OrderedDict([
    ('aff', ('target', 'spawner', 'way', 'via', 'src', 'form', 'cwd', 'out', 'timing', 'seen', 'os', 'decoy', 'bait', 'author', 'busy', 'starter')),
    ('deb', ('structure', 'kind', 'rpath', 'nstyle', 'rdir', 'role', 'marker', 'decl', 'homonym', 'fstate', 'life', 'os', 'lang', 'aux', 'wmode', 'edits')),
    ('sta', ('skind', 'life', 'flaw', 'at', 'os')),
    ('cpl', ('spawner', 'life', 'rpath', 'os', 'fstate')),
])

# Axes that were added after the first case ids were fixed. A case id names them only when they differ from the baseline, so the ids of every earlier case
# are the same as before (and the times, ids and files made from them). They stay out of the pairwise cover: each has its own product of cases (run.select).
OPTIONAL = {'aff': ('author', 'busy', 'starter'), 'deb': ('os', 'lang', 'aux', 'wmode', 'edits')}

BASE = {
    'target': 'cli', 'spawner': 'main', 'way': 'direct', 'via': 'arg', 'src': 'call', 'form': 'new', 'cwd': 'same', 'out': 'none',
    'timing': 'normal', 'seen': 'live', 'decoy': 'none', 'bait': 'none', 'author': 'self', 'busy': 'py', 'starter': 'call', 'lang': 'en', 'aux': 'none', 'wmode': 'tool', 'edits': 'none',
    'structure': 'single', 'kind': 'sub', 'rpath': 'abs', 'nstyle': 'plain', 'rdir': 'r1', 'role': 'writer', 'marker': 'none', 'decl': 'yes',
    'homonym': 'none', 'fstate': 'none',
    'skind': 'cli', 'life': 'running', 'flaw': 'none', 'at': 'live', 'os': 'linux',
}
# a bundle may sit on a different baseline than the global one (the debate bundle's agent is not yet running unless asked)
BUNDLE_BASE = {
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
    fold = {'aff': _aff, 'deb': _deb, 'sta': _sta, 'cpl': _cpl}[b]
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
