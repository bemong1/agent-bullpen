#!/usr/bin/env python3
"""Status line command for Claude Code: keeps the plan usage Claude Code hands to its status line, so the dashboard can show it with no request and no token.

Claude Code runs this command and pipes a JSON document to its stdin. Only `rate_limits` is taken from it (the 5-hour and weekly percentages and
their reset times) and written to $XDG_CACHE_HOME/agent-bullpen/statusline.json (default ~/.cache/agent-bullpen/, folder 0700, file 0600). Nothing else
in the input is kept: no path, folder, session id, model, cost or instruction. Standard library only, and it does not import the board package, so it starts fast.

    "statusLine": {"type": "command", "command": "python3 /path/to/statusline.py"}

    --chain CMD         also run CMD (your existing status line) with the same stdin and print its output as is.
                        The environment variable AGENT_BULLPEN_STATUSLINE_CHAIN works the same way
    --chain-timeout S   give CMD this many seconds (default 5); on a timeout CMD is killed and nothing is printed
    --show              with no --chain, print a short usage text ("5h 42% · 7d 18%") as the status line
    --print-config      print the settings.json piece that sets this up (keeping your current statusLine as the chain); nothing is modified
    --help              this text

It never fails: whatever the input, the exit code is 0 and an error prints nothing."""

import json
import math
import os
import stat
import sys
import time

VERSION = 1
MAX_STDIN = 1 << 20              # the most of stdin that is looked at (a real input is a few KiB)
SPILL_MAX = 64 << 20             # with --chain, the most of an oversized input that is still handed on to the command
MAX_OUT = 1 << 20                # the most of the chained command's output that is printed
WRITE_GAP = 30                   # seconds: an unchanged reading is not written again within this time
CHAIN_TIMEOUT = 5.0
STATE_MAX = 64 << 10             # the most of the saved file that is read back
CHAIN_ENV = 'AGENT_BULLPEN_STATUSLINE_CHAIN'
NESTED_ENV = 'AGENT_BULLPEN_STATUSLINE_ACTIVE'     # set for the chained command, so this script run from inside it does not chain again
WINDOWS = ('five_hour', 'seven_day', 'spend_limit')
ISO_CHARS = frozenset('0123456789-:.TZ+ ')


def state_path():
    return os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache'), 'agent-bullpen', 'statusline.json')


def _number(x, lo, hi):
    """x as a float if it is a finite number in [lo, hi] (a bool is not a number here), else None."""
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not lo <= x <= hi:
        return None
    return float(x)


def _reset_time(x):
    """A reset time as it is kept: seconds or milliseconds as a number, or an ISO date-time text. Anything else is None."""
    if isinstance(x, str):
        return x if 8 <= len(x) <= 40 and x[0].isdigit() and set(x) <= ISO_CHARS else None
    n = _number(x, 1, 4e12)
    return int(n) if n is not None and n == int(n) else n


def sanitize(rl):
    """The part of `rate_limits` that is kept: for each of five_hour, seven_day and spend_limit, a percentage and a reset time (and for a gateway the
    amounts in dollars). Every value is checked for its shape and nothing else is copied, so no text from the input can reach the file. {} if nothing is usable."""
    out = {}
    if not isinstance(rl, dict):
        return out
    for k in WINDOWS:
        w = rl.get(k)
        if not isinstance(w, dict):
            continue
        p, r = _number(w.get('used_percentage'), 0, 1000), _reset_time(w.get('resets_at'))
        if p is None or r is None:
            continue
        o = {'used_percentage': round(p, 1), 'resets_at': r}
        if k == 'spend_limit':
            for f in ('used_usd', 'limit_usd'):
                n = _number(w.get(f), 0, 1e12)
                if n is not None:
                    o[f] = round(n, 2)
            per = w.get('period')
            if isinstance(per, str) and 0 < len(per) <= 16 and all('a' <= c <= 'z' or c == '_' for c in per):
                o['period'] = per
        out[k] = o
    return out


def parse(data):
    """The kept rate limits of one input (bytes), or {} when the input is not a JSON object, is too big, or has no usable rate_limits."""
    if not data or len(data) > MAX_STDIN:
        return {}
    try:
        d = json.loads(data)
    except (ValueError, RecursionError):
        return {}
    return sanitize(d.get('rate_limits')) if isinstance(d, dict) else {}


def _trusted(path):
    """Whether the folder and the file are mine and no other user can write to them (the same rule as the dashboard's other cache files)."""
    uid = os.geteuid() if hasattr(os, 'geteuid') else None
    for p in (os.path.dirname(path), path):
        try:
            st = os.stat(p)
        except FileNotFoundError:
            continue
        if uid is not None and (st.st_uid != uid or st.st_mode & 0o022):
            return False
    return True


def read_state(path):
    """The saved document, or None if it is absent, not a regular file, too big or broken."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > STATE_MAX:
            return None
        with os.fdopen(fd, 'rb') as fh:
            fd = -1
            d = json.loads(fh.read(STATE_MAX + 1))
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    return d if isinstance(d, dict) else None


def save(rl, now=None, path=None):
    """Writes {"v": 1, "at": <now>, "rate_limits": rl} by replacement. Not written when rl is empty, when the folder is not safe, or when the same
    reading was written less than WRITE_GAP seconds ago. True if a file was written."""
    if not rl:
        return False
    now = time.time() if now is None else now
    path = path or state_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if not _trusted(path):
        return False
    old = read_state(path)
    if old and old.get('rate_limits') == rl and isinstance(old.get('at'), (int, float)) and 0 <= now - old['at'] < WRITE_GAP:
        return False
    tmp = '%s.%d.tmp' % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump({'v': VERSION, 'at': round(now, 3), 'rate_limits': rl}, fh, separators=(',', ':'))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def short_text(rl):
    """'5h 42% · 7d 18%' for --show ('' if there is neither window)."""
    parts = []
    for k, label in (('five_hour', '5h'), ('seven_day', '7d')):
        if k in rl:
            parts.append('%s %d%%' % (label, round(rl[k]['used_percentage'])))
    return ' · '.join(parts)


def read_input(chain):
    """(bytes, spool): the input up to MAX_STDIN, or for an oversized input with a chain, (None, a temporary file holding it from the start). Never raises."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return b'', None
        head = sys.stdin.buffer.read(MAX_STDIN + 1)
        if len(head) <= MAX_STDIN:
            return head, None
        if not chain:
            return None, None
        import tempfile
        spool = tempfile.TemporaryFile()
        spool.write(head)
        total = len(head)
        while total < SPILL_MAX:
            chunk = sys.stdin.buffer.read(min(1 << 16, SPILL_MAX - total))
            if not chunk:
                break
            spool.write(chunk)
            total += len(chunk)
        spool.seek(0)
        return None, spool
    except (OSError, ValueError, AttributeError):
        return b'', None


def run_chain(cmd, data, spool=None, timeout=None):
    """Runs cmd through the shell with the same stdin and returns its stdout as bytes (b'' on a timeout or when it cannot start).
    A timeout kills the whole process group, so a command that started others does not keep this one waiting."""
    import signal
    import subprocess
    timeout = CHAIN_TIMEOUT if timeout is None else timeout
    env = dict(os.environ, **{NESTED_ENV: '1'})
    try:
        p = subprocess.Popen(cmd, shell=True, stdin=spool if spool is not None else subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    except (OSError, ValueError):
        return b''
    try:
        out, _ = p.communicate(input=None if spool is not None else data, timeout=timeout)
        return out[:MAX_OUT]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()
        try:
            p.communicate(timeout=1)
        except Exception:      # noqa: BLE001 — a stuck pipe must not hold this up
            pass
        return b''


def runs_this(command, me):
    """Whether a shell command line already runs this script (one of its words is this file, with `~`, `$VAR` and links resolved)."""
    import shlex
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    real = os.path.realpath(me)
    return any(os.path.realpath(os.path.expandvars(os.path.expanduser(w))) == real for w in words[:50])


def print_config(settings_dir=None, out=None, err=None):
    """Prints the settings.json piece that registers this command, keeping the current statusLine as the chain. Only reads the file."""
    import shlex
    out, err = out or sys.stdout, err or sys.stderr
    home = settings_dir or os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.path.expanduser('~'), '.claude')
    path = os.path.join(home, 'settings.json')
    cur = None
    try:
        with open(path, 'rb') as fh:
            d = json.loads(fh.read(MAX_STDIN))
        cur = d.get('statusLine') if isinstance(d, dict) else None
    except (OSError, ValueError, RecursionError):
        pass
    me = os.path.abspath(__file__)
    mine = 'python3 ' + shlex.quote(me)
    if isinstance(cur, dict) and cur.get('type') == 'command' and isinstance(cur.get('command'), str) and cur['command'].strip():
        if runs_this(cur['command'], me):
            new = cur
            print('The statusLine in %s already runs this command; nothing to change.' % path, file=err)
        else:
            new = dict(cur, command='%s --chain %s' % (mine, shlex.quote(cur['command'])))
            print('Your current statusLine in %s is kept as the chain.' % path, file=err)
    else:
        new = {'type': 'command', 'command': mine}
        if cur is not None:
            print('The statusLine in %s is not a command, so it cannot be chained; this piece would replace it.' % path, file=err)
        else:
            print('There is no statusLine in %s. Add --show to the command to also draw a short usage text.' % path, file=err)
    print('Merge this into %s (this command does not edit it):' % path, file=err)
    print(json.dumps({'statusLine': new}, indent=2, ensure_ascii=False), file=out)


def parse_args(argv):
    o = {'chain': None, 'timeout': None, 'show': False, 'config': False, 'help': False}
    i = 0
    while i < len(argv):
        a = argv[i]
        name, eq, val = a.partition('=')
        if name in ('--chain', '--chain-timeout'):
            if not eq:
                i += 1
                val = argv[i] if i < len(argv) else ''
            if name == '--chain':
                o['chain'] = val or None
            else:
                try:
                    o['timeout'] = float(val) if float(val) > 0 else None
                except ValueError:
                    pass
        elif a == '--show':
            o['show'] = True
        elif a == '--print-config':
            o['config'] = True
        elif a in ('-h', '--help'):
            o['help'] = True
        i += 1                  # anything else is ignored: a status line command must not fail on an option it does not know
    return o


def run(argv):
    o = parse_args(argv)
    if o['help']:
        print(__doc__)
        return
    if o['config']:
        print_config()
        return
    chain = None if os.environ.get(NESTED_ENV) else (o['chain'] or os.environ.get(CHAIN_ENV) or None)
    data, spool = read_input(chain)
    rl = {}
    try:
        rl = parse(data)
        save(rl)
    except Exception:          # noqa: BLE001 — keeping the reading is a side effect: the status line itself still gets drawn
        pass
    if chain:
        out = run_chain(chain, data, spool, o['timeout'])
    else:
        out = (short_text(rl) + '\n').encode() if o['show'] and rl else b''
    if out:
        sys.stdout.buffer.write(out)
        sys.stdout.buffer.flush()


def main(argv=None):
    try:
        run(sys.argv[1:] if argv is None else argv)
    except BaseException:      # noqa: BLE001 — whatever happens the exit code stays 0 and nothing else is printed
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
