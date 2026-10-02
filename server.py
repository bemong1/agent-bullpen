#!/usr/bin/env python3
"""Agent Bullpen — a multi-agent dashboard for Claude Code and Codex.

Read-only: it reads the session transcripts Claude Code leaves (~/.claude/projects/<proj>/<session>.jsonl) and the
subagent transcripts (<session>/subagents/agent-*.jsonl, *.meta.json), and shows the progress of the orchestrator and
its subagents and the state of the debates (topic × round × participant) on a web page. Standard library only.
It writes to no transcript file.

    python3 server.py                      # the most recent session that launched subagents (else the most recent plain conversation)
    python3 server.py --session <id>       # pick a session
    python3 server.py --port 8790 --host 127.0.0.1
    python3 server.py --host 0.0.0.0       # reachable from other devices: a random access token is made and printed (--token to fix it, --no-auth to switch the check off)
    python3 server.py --claude-config-dir ~/.claude --codex-home ~/.codex     # transcript folders (default: the standard environment variables, else ~/.claude and ~/.codex)
    python3 server.py --lang ko            # language of the terminal output and --help (default auto: AGENT_BULLPEN_LANG → LC_ALL → LC_MESSAGES → LANG → en)
"""

import argparse
import errno
import gzip
import ipaddress
import json
import math
import os
import re
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import board
from board import access


def scan_path_flags(argv):
    """The values (the last one wins) of --claude-config-dir and --codex-home in argv. They are needed before board.util fixes the paths, so argv is scanned without argparse.
    A malformed one (no value, etc.) is skipped; main's argparse reports the error."""
    out = {}
    for key, flag in (('claude', '--claude-config-dir'), ('codex', '--codex-home')):
        for i, a in enumerate(argv):
            if a == '--':
                break
            if a == flag and i + 1 < len(argv):
                out[key] = argv[i + 1]
            elif a.startswith(flag + '='):
                out[key] = a[len(flag) + 1:]
    return out


def scan_flag(argv, flag):
    """The value (the last one) of flag (e.g. --lang) in argv, or None. Read before argparse runs: `--help` ends the parse, and the language must be known by then.
    A malformed one (no value) is skipped; argparse reports it."""
    out = None
    for i, a in enumerate(argv):
        if a == '--':
            break
        if a == flag and i + 1 < len(argv):
            out = argv[i + 1]
        elif a.startswith(flag + '='):
            out = a[len(flag) + 1:]
    return out


if __name__ == '__main__':      # only when run directly: this must be decided before the other board modules copy the paths by name (tests and the harness import the module, so they are not affected)
    board.PATH_FLAGS.update(scan_path_flags(sys.argv[1:]))

# Used directly here, among others: REG, scan_sessions, session_projects, default_session, sources, LINKS, plan_status, SID_RE, short_path, trunc,
# views, i18n, procs, the file guards (FILE_MAX, open_safe, open_nofollow, Denied) and the path settings (CLAUDE_HOME, CODEX_HOME, PATH_FROM, LINK_CACHE).
# The rest re-exports names that the harness (tools/regress) and the tests (tests) use as `server.<name>`. They are listed one by one (no `import *`).
from board.util import (  # noqa: F401
    BOOT, CLAUDE_HOME, CLAUDE_JSON, CODEX_HOME, CODEX_NAMES, CODEX_SESSIONS, DENY_FILES, FILE_MAX, HOME, PATH_FROM, PROJECTS, SID_RE, STALL_SEC,
    TOOL_STALL_SEC, Denied, Tail, as_text, denied_file, hidden_or_secret, load_json_line, open_nofollow, open_safe, parse_ts,
    short_path, stat_plain, stat_regular, strip_reminders, trunc,
)
from board.tokens import TokenMeter, call_cost, claude_model_short, cx_model_short  # noqa: F401
from board.codex_parse import (  # noqa: F401
    CX_LINE_MAX, CX_WINDOW, codex_call, codex_say_text, codex_tool, codex_user_text, cx_decode, cx_procs,
)
from board.codex_index import CODEX, CodexIndex  # noqa: F401
from board.lineage import LINK_CACHE, claude_alive_ids  # noqa: F401
from board.link import (  # noqa: F401
    CLAUDE_LAUNCH_RE, CLI_WINDOW, LAUNCH_RE, LINKS, LinkIndex, cx_launches, cx_link, cx_parse_call, cx_prompt_rx,
    shell_code,
)
from board.agents import Agent, CodexAgent, model_numbers, tool_brief, tool_category  # noqa: F401
from board.debates import REL_REPORT_RE, REPORT_RE, brief_table, read_head, write_intent, writer_table  # noqa: F401
from board.sessions import STATUS_LABEL, CodexLinker, CodexSession, Session  # noqa: F401
from board.catalog import CX_LIST_DAYS, REG, Registry, default_session, list_sessions, scan_sessions, session_projects, sources  # noqa: F401
from board import procs  # noqa: F401
from board.plans import plan_status  # noqa: F401
from board import diag, i18n, views
from board import sessions as board_sessions

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/game': ('game.html', 'text/html; charset=utf-8'),
    '/game.js': ('game.js', 'text/javascript; charset=utf-8'),
    '/game-art.js': ('game-art.js', 'text/javascript; charset=utf-8'),
    '/game-demo.js': ('game-demo.js', 'text/javascript; charset=utf-8'),
    '/board.css': ('board.css', 'text/css; charset=utf-8'),
    '/board.js': ('board.js', 'text/javascript; charset=utf-8'),
    '/common.js': ('common.js', 'text/javascript; charset=utf-8'),
    '/i18n.js': ('i18n.js', 'text/javascript; charset=utf-8'),
}
FONT_RE = re.compile(r'[A-Za-z0-9._-]+\.(woff2|woff|ttf|css|txt)')       # names accepted as /fonts/<file>
FONT_TYPES = {'woff2': 'font/woff2', 'woff': 'font/woff', 'ttf': 'font/ttf', 'css': 'text/css; charset=utf-8', 'txt': 'text/plain; charset=utf-8'}
DEFAULT_SESSION = None
FIXED_DEFAULT = None       # the default session set with --session
CLI_LANG = i18n.DEFAULT    # language of the terminal output and --help (main sets it from --lang and the environment); texts are printed with cli_t(key, name=value ...)
cli_t = i18n.translator(CLI_LANG)
TALK_KINDS = {             # /api/talk?scope=: the events each card shows. user is the default (no scope)
    'user': ('user_say', 'orch_say', 'orch_ask', 'user_answer', 'sys'),                 # user ↔ orchestrator (+ the board's system lines: usage limit, API error)
    'agents': ('spawn', 'orch_msg', 'handback', 'peer', 'agent_msg', 'xread'),          # orchestrator ↔ agents, and agents among themselves
}


# Allow-list for the Host header (defence against DNS rebinding). Only the name is compared, not the port (ssh -L and an editor's port forwarding arrive as localhost:<another port>).
# main adds the addresses it binds and --allow-host. There is no built-in suffix: a VPN name (*.ts.net, *.netbird.cloud …) is accepted with --allow-host .suffix.
ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1'}
ALLOWED_SUFFIXES = set()        # '.example.net': names ending in this suffix (the leading dot means example.net itself does not match)


HOST_RE = re.compile(r'(?:\[([^\]\s]+)\]|([^:\[\]@\s/?#]+))(?::\d+)?')      # a name (or [IPv6]) plus a numeric port only


def norm_host(name):
    """An IP address in the canonical text of ipaddress (IPv6 compressed), a name as it is. 'fd7a:115c:a1e0:0:0:0:0:1' and '[fd7a:115c:a1e0::1]' become equal."""
    try:
        return str(ipaddress.ip_address(name))
    except ValueError:
        return name


def host_name(header):
    """The name of a Host header without the port (lower case, no trailing dot, an IP in canonical text). '[::1]:8790' → '::1'. '' when the shape is odd (letters where the port should be, @, etc.)."""
    m = HOST_RE.fullmatch((header or '').strip().lower())
    return norm_host((m.group(1) or m.group(2)).rstrip('.')) if m else ''


def host_allowed(header):
    name = host_name(header)
    return bool(name) and (name in ALLOWED_HOSTS or any(name.endswith(sfx) for sfx in ALLOWED_SUFFIXES))


def is_ip(name):
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def host_hint(header):
    """403 body: one line of how to fix it in English, then the same in Korean. Not the terminal language: the reader is a browser or curl, whose
    language is unknown. The name is echoed only when it is made of safe characters. A suffix is offered only for a name that has one, never for an IP address."""
    name = host_name(header)
    safe = bool(re.fullmatch(r'[a-z0-9.:-]+', name))
    labels = name.split('.')
    lines = []
    for lang in ('en', 'ko'):
        t = i18n.translator(lang)
        shown = name if safe else t('cli.host.name_unknown')
        if is_ip(name):
            line = t('cli.host.denied_ip', name=shown)
        else:
            sfx = '.' + '.'.join(labels[1:]) if safe and len(labels) > 2 else t('cli.host.suffix_unknown')
            line = t('cli.host.denied', name=shown, suffix=sfx)
        if line not in lines:                          # no Korean dictionary: the English line alone
            lines.append(line)
    return '\n'.join(lines) + '\n'


class BoardServer(ThreadingHTTPServer):
    """http.server's HTTPServer.server_bind sets server_name with socket.getfqdn(host). When a non-loopback address (a VPN address) is opened, that
    does a reverse DNS (PTR) lookup through the system resolver. This server does not use server_name, so it skips the lookup and keeps the address as it is.
    `gate` is the access.Gate this listener checks every request against (None: no token check here; main sets it)."""
    gate = None

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class HTTPServer6(BoardServer):
    """For `--host ::1` and `--host ::`. The default server class opens IPv4 (AF_INET) only."""
    address_family = socket.AF_INET6

    def server_bind(self):
        if self.server_address[0] == '::':             # the wildcard answers IPv4 clients too, whatever the system default is
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (OSError, AttributeError):
                pass
        super().server_bind()


def _nums(q, spec):
    """The numeric arguments of a query: spec = {key: (value when absent, int|float)}. ValueError when one is not a number or not finite (nan, inf)."""
    out = {}
    for key, (default, cast) in spec.items():
        v = q.get(key)
        n = default if v is None else cast(v)
        if isinstance(n, float) and not math.isfinite(n):
            raise ValueError(key)
        out[key] = n
    return out


class Handler(BaseHTTPRequestHandler):
    server = None            # BaseRequestHandler.__init__ sets the real one; a Handler made without it has no token check

    def log_message(self, fmt, *args):
        if os.environ.get('AGENT_BULLPEN_LOG'):
            print(access.redact('%s %s' % (self.address_string(), fmt % args)), flush=True)      # a first visit carries the token in its request line

    def _send(self, code, body, ctype='application/json; charset=utf-8', cache='no-store', headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        if len(data) > 2048 and not ctype.startswith('font/') and 'gzip' in (self.headers.get('Accept-Encoding') or ''):
            data = gzip.compress(data, 5)                  # fonts are already compressed
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', cache)
        self.send_header('Content-Length', str(len(data)))
        for name, value in headers:
            self.send_header(name, value)
        # The headers and the body leave in ONE write. Two writes (end_headers() flushes the headers alone) make a body over about 64 KB wait ~200 ms for the
        # client's delayed ACK of the small first segment; one write does not (219 -> 15 ms). So end_headers() is done by hand, without its flush.
        if self.request_version == 'HTTP/0.9':
            self.wfile.write(data)                         # a 0.9 request has no headers at all (send_response made no buffer)
            return
        self._headers_buffer.append(b'\r\n')
        self._headers_buffer.append(data)
        self.flush_headers()

    def _fail(self, code, error, error_code, **extra):
        """A JSON error answer: the old `error` text (and any extra fields) as they were, then `error_code`, the code a page words in its own language."""
        return self._send(code, dict({'error': error}, **dict(extra, error_code=error_code)))

    def _font(self, name):
        """/fonts/<file>: only a file directly under static/fonts/ (a subfolder, .. or a link is refused). Only a regular file is opened."""
        m = FONT_RE.fullmatch(name)
        if not m or '..' in name:
            return self._fail(404, 'not found', 'not_found')
        try:
            with open_nofollow(os.path.join(os.path.realpath(STATIC), 'fonts', name), binary=True) as f:
                data = f.read()
        except OSError:
            return self._fail(404, 'not found', 'not_found')
        return self._send(200, data, FONT_TYPES[m.group(1)], cache='public, max-age=86400')

    def _locale(self, name):
        """/locales/<code>.json: only a registered dictionary (a well-formed JSON file directly under static/locales/). A subpath, .., a link, a name starting with _ or an unregistered name is a 404."""
        m = i18n.FILE_RE.fullmatch(name)
        entry = i18n.registry().get(m.group(1)) if m else None
        if not entry:
            return self._fail(404, 'not found', 'not_found')
        return self._send(200, entry['data'])

    def _unauthorized(self, path):
        """401 with a short note: a page gets it in English and Korean, an API path the same JSON error as the other failures. It repeats nothing from the request."""
        headers = [('WWW-Authenticate', 'Bearer realm="agent-bullpen"')]
        if path.startswith('/api/'):
            return self._send(401, {'error': 'unauthorized', 'error_code': 'unauthorized'}, headers=headers)
        return self._send(401, access.denied_page(), 'text/html; charset=utf-8', headers=headers)

    def _admit(self, u):
        """True when the request may be answered. With a token check on this listener: a valid cookie or `Authorization: Bearer`, or the token in the address (the cookie
        is set and the browser is sent to the same address without it). Without: only the Host header is checked (defence against DNS rebinding)."""
        gate = getattr(self.server, 'gate', None)
        if gate is None:
            if host_allowed(self.headers.get('Host')):
                return True
            self._send(403, host_hint(self.headers.get('Host')).encode(), 'text/plain; charset=utf-8')
            return False
        port = self.server.server_address[1]
        verdict, rest = gate.judge(self.headers.get('Cookie'), self.headers.get('Authorization'), u.query, port)
        if verdict == access.LOGIN:
            self._send(302, b'', 'text/plain; charset=utf-8', headers=[('Location', access.clean_location(u.path, rest)), ('Set-Cookie', gate.cookie_header(port))])
        elif verdict == access.DENY:
            self._unauthorized(u.path)
        return verdict == access.OK

    def do_GET(self):
        """Answers one GET. Anything the answer code does not handle is a 500 JSON (the connection is not dropped); a client that has gone away is left to the server."""
        try:
            self._get()
        except ConnectionError:
            raise
        except Exception as e:   # noqa: BLE001 — a request must be answered whatever fails inside it
            print('request error', type(e).__name__, urlparse(self.path).path[:80], flush=True)
            self._headers_buffer = []                  # a half-built answer is dropped
            try:
                self._fail(500, 'internal error', 'internal_error')
            except OSError:
                pass

    def _get(self):
        try:
            u = urlparse(self.path)
        except ValueError:
            return self._send(400, b'bad request\n', 'text/plain; charset=utf-8')
        if not self._admit(u):
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        page = STATIC_FILES.get(u.path)
        if page:
            try:
                with open(os.path.join(STATIC, page[0]), 'rb') as f:
                    return self._send(200, f.read(), page[1])
            except OSError:                                # a 404 when the file is not there yet (the connection is not dropped)
                return self._fail(404, 'not found', 'not_found')
        if u.path.startswith('/fonts/'):
            return self._font(u.path[len('/fonts/'):])
        if u.path.startswith('/locales/'):
            return self._locale(u.path[len('/locales/'):])
        if u.path == '/api/i18n':                      # the first thing a page asks: answered without a session too (the first-screen diagnosis needs it)
            return self._send(200, {'default': i18n.default_code(), 'languages': i18n.languages()})
        if u.path == '/api/plans':
            return self._send(200, plan_status())
        if u.path == '/api/sessions':
            ss, counts = scan_sessions()               # Claude with agents: 30 + Codex: 7 days + solo (Claude without agents): 30
            projs = session_projects(ss)
            # What to open when the address has no session: the one set with --session, else the representative of the most recent project whose representative is an orchestration (the most recent solo if there is none)
            dflt = FIXED_DEFAULT or default_session(projs, ss) or DEFAULT_SESSION
            return self._send(200, {'default': dflt, 'sessions': ss, 'projects': projs, 'sources': sources(counts)})
        sid = q.get('session') or DEFAULT_SESSION
        try:
            s = REG.get(sid) if sid and u.path.startswith('/api/') else None
        except PermissionError:
            return self._fail(403, 'unreadable', 'unreadable', session=sid)
        except OSError:
            return self._fail(404, 'unreadable', 'unreadable', session=sid)
        if u.path.startswith('/api/') and not s:
            return self._fail(404, 'session not found', 'session_not_found', session=sid)
        try:                                           # numeric arguments: a bad one is a 400 (it used to drop the connection with no response)
            if u.path == '/api/state':
                t = _nums(q, {'t': (None, float)})['t']
            elif u.path == '/api/timeline':
                since = _nums(q, {'since': (time.time() - 3600, float)})['since']
            elif u.path == '/api/talk':
                a = _nums(q, {'before': (10 ** 12, int), 'limit': (80, int)})
                before, limit = a['before'], a['limit']
                if not 1 <= limit <= 300:                  # only 1–300 is accepted; anything else is a 400
                    raise ValueError('limit')
                scope = q.get('scope', 'user')             # an empty value (scope=) is the same as none (parse_qs drops it)
                if scope not in TALK_KINDS:
                    raise ValueError('scope')
            elif u.path == '/api/event':
                idx = _nums(q, {'idx': (None, int)})['idx']
                if idx is None or idx < 0:
                    raise ValueError('idx')
        except ValueError:
            return self._fail(400, 'bad parameter', 'bad_parameter')
        if u.path == '/api/state':
            if q.get('v') and q['v'] == str(s.version) and t is not None and time.time() - t < 10:
                return self._send(200, {'unchanged': True, 'version': s.version})
            return self._send(200, s.state())
        if u.path == '/api/agent':
            d = s.agent_detail(q.get('id', ''))
            return self._send(200, d) if d else self._fail(404, 'no agent', 'no_agent')
        if u.path == '/api/diag':                      # what the judgments noticed and could not settle (board/diag.py): codes and small values, never record text
            s.state()                                  # the page of this moment: the entries are made by the same pass as the state
            with s.lock:
                items = list(getattr(s, '_diag', None) or [])
            return self._send(200, dict(diag.counts(items), session=s.id, now=time.time(), items=items))
        if u.path == '/api/timeline':
            return self._send(200, s.timeline(since))
        if u.path == '/api/talk':
            # scope=user (default): the user ↔ orchestrator conversation (instructions, reports, multiple-choice questions, answers). scope=agents: the agent talk card.
            # the newest `limit` items that come before `before`
            kinds = TALK_KINDS[scope]
            with s.lock:
                items = [dict(e, idx=i) for i, e in enumerate(s.feed) if e['kind'] in kinds and i < before]
                ids = views.spawn_map(s) if scope == 'agents' else {}
            more = len(items) > limit
            items = items[-limit:]
            for e in items:
                views.resolve_spawn(e, ids)                # as in the feed of state(), the receiving side of a spawn becomes the agent id (a user conversation has no spawn)
                e['full_len'] = len(e.get('text') or '')
                e['text'] = trunc(e.get('text') or '', 2000)
            return self._send(200, {'items': items, 'more': more})
        if u.path == '/api/event':
            try:
                ev = s.feed[idx]
            except IndexError:
                return self._fail(404, 'no event', 'no_event')
            return self._send(200, ev)
        if u.path == '/api/file':
            path = q.get('path', '')
            real = s.allowed_file(path) if path else None
            if not real:
                return self._fail(403, 'not allowed', 'not_allowed')
            try:                                       # open the path that was approved, as it is: the deny decision is made afresh right before opening, and nothing is opened if any component has turned into a link
                with open_safe(real, approved=True) as f:
                    st = os.fstat(f.fileno())
                    if st.st_size > FILE_MAX:
                        return self._fail(413, 'too large', 'too_large', limit=FILE_MAX)
                    text = f.read(FILE_MAX + 1)[:FILE_MAX]
                    mtime = st.st_mtime
            except Denied:
                return self._fail(403, 'not allowed', 'not_allowed')
            except OSError as e:
                return self._fail(404, str(e), 'open_failed')
            return self._send(200, {'path': path, 'short': short_path(path), 'text': text, 'mtime': mtime})
        return self._fail(404, 'not found', 'not_found')


HOST_NAME_RE = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?')


def _host_arg(h):
    """--host: where to listen. Any IP address (127.0.0.1, 0.0.0.0, ::, a LAN or VPN address; [::1] too) or a host name; not empty, not with a port or a path.
    Whether that address needs a token is main's business (access.is_loopback)."""
    name = h.strip()
    if name.startswith('[') and name.endswith(']'):
        name = name[1:-1]
    try:
        ipaddress.ip_address(name)
        return name
    except ValueError:
        pass
    if HOST_NAME_RE.fullmatch(name) and '..' not in name:
        return name
    raise argparse.ArgumentTypeError(cli_t('cli.arg.host', value=repr(h)))


def _token_arg(v):
    if not access.token_ok(v):
        raise argparse.ArgumentTypeError(cli_t('cli.arg.token', min=access.TOKEN_MIN))
    return v


def _allow_host_arg(v):
    """--allow-host: one name (my.box) or a .suffix (.example.net). Returned in lower case. `*.example.net` is taken as `.example.net`."""
    name = v.strip().lower().rstrip('.')
    if name.startswith('*.'):
        name = name[1:]
    suffix = name.startswith('.')
    body = name[1:] if suffix else (host_name(name) or norm_host(name.strip('[]')))
    ok = re.fullmatch(r'[a-z0-9][a-z0-9.-]*' if suffix else r'[a-z0-9:][a-z0-9.:-]*', body)
    if not ok or '..' in body:
        raise argparse.ArgumentTypeError(cli_t('cli.arg.allow_host', value=repr(v)))
    return '.' + body if suffix else body


def _session_arg(v):
    if not SID_RE.fullmatch(v):
        raise argparse.ArgumentTypeError(cli_t('cli.arg.session', value=repr(v)))
    return v


def _dir_arg(v):
    if not v.strip():
        raise argparse.ArgumentTypeError(cli_t('cli.arg.dir'))
    return v


def check_path_flags(ap, args):
    """Checks that the path flags took effect in board's paths. Run directly, server.py applies them before the import, but a harness that calls main()
    after `import server` skips that step, so the flags would be accepted and do nothing: a mismatch ends with an error."""
    for key, val, flag, cur in (('claude', args.claude_config_dir, '--claude-config-dir', CLAUDE_HOME),
                                ('codex', args.codex_home, '--codex-home', CODEX_HOME)):
        if val and (PATH_FROM[key] != 'flag' or os.path.abspath(os.path.expanduser(val)) != cur):
            ap.error(cli_t('cli.arg.path_mismatch', flag=flag, value=val, current=cur, source=PATH_FROM[key]))


def bind_servers(hosts, port):
    """Creates (binds) a server for each address to open. If any cannot be opened, it closes the ones already opened, prints a one-line notice (address, port, a --port alternative) and ends with exit code 1.
    The scan and the background threads start after this, so nothing has been done yet at this point."""
    servers = []
    for h in hosts:
        try:
            servers.append((HTTPServer6 if ':' in h else BoardServer)((h, port), Handler))
        except OSError as e:
            for srv in servers:
                srv.server_close()
            addr = '%s:%d' % ('[%s]' % h if ':' in h else h, port)
            if isinstance(e, socket.gaierror):
                why = cli_t('cli.bind.no_name')
            elif e.errno == errno.EADDRINUSE:
                why = cli_t('cli.bind.in_use', port=port, next=port + 1)
            elif e.errno == errno.EACCES:
                why = cli_t('cli.bind.denied')
            elif e.errno == errno.EADDRNOTAVAIL:
                why = cli_t('cli.bind.no_address')
            else:
                why = cli_t('cli.bind.other', reason=e.strerror or type(e).__name__, next=port + 1)
            print(cli_t('cli.bind.failed', addr=addr, why=why), file=sys.stderr, flush=True)
            raise SystemExit(1)
    return servers


FROM_LABEL = {'flag': {'claude': '--claude-config-dir', 'codex': '--codex-home'}, 'env': {'claude': 'CLAUDE_CONFIG_DIR', 'codex': 'CODEX_HOME'}}


def startup_lines(srcs):
    """The start output (the lines after the address): per Claude and Codex the folder read, session counts and where the folder came from, and the process
    detection method. The words come from the dictionary (cli.start.*), the layout of the columns stays here."""
    out = []
    for s in srcs:
        cl = s['provider'] == 'claude'
        where = s['dir'] + ('/projects' if cl else '/sessions')
        n = (cli_t('cli.start.no_folder') if not s['exists'] else
             cli_t('cli.start.claude_counts', sessions=s['sessions'], agents=s['agent_sessions']) if cl else
             cli_t('cli.start.codex_counts', days=CX_LIST_DAYS, sessions=s['sessions']))
        label = cli_t('cli.start.default') if s['from'] == 'default' else FROM_LABEL[s['from']][s['provider']]
        out.append('  %-13s %-28s %s  (%s)' % ('Claude Code' if cl else '◆ Codex', where, n, label))
    out.append(cli_t('cli.start.status', how=cli_t('cli.start.method.' + procs.method())))
    return out


def _help(key, **params):
    """A help text for argparse, which %-formats it: a literal % is doubled."""
    return cli_t(key, **params).replace('%', '%%')


def set_cli_lang(lang):
    """Sets the terminal language everywhere (this module, and board.i18n for the modules below it)."""
    global CLI_LANG, cli_t
    CLI_LANG = lang
    cli_t = i18n.translator(lang)
    i18n.set_cli_lang(lang)


def main():
    global DEFAULT_SESSION, FIXED_DEFAULT
    set_cli_lang(i18n.cli_lang(scan_flag(sys.argv[1:], '--lang')))     # the language first: the help can be built in it before argparse ends at `--help --lang ko`
    ap = argparse.ArgumentParser(description=cli_t('cli.help.description'), formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    ap.add_argument('--version', action='version', version='Agent Bullpen ' + board.__version__, help=_help('cli.help.version'))
    ap.add_argument('--session', type=_session_arg, help=_help('cli.help.session'))
    ap.add_argument('--host', action='append', type=_host_arg, help=_help('cli.help.host'))
    ap.add_argument('--port', type=int, default=8790)
    ap.add_argument('--token', type=_token_arg, metavar='VALUE', help=_help('cli.help.token', min=access.TOKEN_MIN))
    ap.add_argument('--no-auth', action='store_true', help=_help('cli.help.no_auth'))
    ap.add_argument('--allow-host', action='append', default=[], metavar='NAME', type=_allow_host_arg, help=_help('cli.help.allow_host'))
    ap.add_argument('--claude-config-dir', type=_dir_arg, metavar='DIR', help=_help('cli.help.claude_config_dir'))
    ap.add_argument('--codex-home', type=_dir_arg, metavar='DIR', help=_help('cli.help.codex_home'))
    ap.add_argument('--lang', default='auto', metavar='CODE', help=_help('cli.help.lang', langs=', '.join(l['code'] for l in i18n.languages())))
    ap.add_argument('--claude-usage-api', action='store_true', help=argparse.SUPPRESS)        # gone: still read so that it can end with a pointer to the status line command
    ap.add_argument('--no-link-cache', action='store_true', help=_help('cli.help.no_link_cache'))
    args = ap.parse_args()
    set_cli_lang(i18n.cli_lang(args.lang))
    if args.claude_usage_api:
        ap.error(cli_t('cli.arg.usage_api_removed'))
    if args.no_auth and args.token:
        ap.error(cli_t('cli.arg.no_auth_token'))
    token = args.token or os.environ.get('AGENT_BULLPEN_TOKEN') or None
    if token and not access.token_ok(token):
        ap.error('AGENT_BULLPEN_TOKEN: ' + cli_t('cli.arg.token', min=access.TOKEN_MIN))
    for name, why in i18n.problems().items():                      # a dictionary that could not be registered is skipped: say why it is missing
        print(cli_t('cli.warn.dictionary', name=name, why=why), file=sys.stderr, flush=True)
    check_path_flags(ap, args)
    hosts = args.host or ['127.0.0.1']
    guarded = [not args.no_auth and (bool(token) or not access.is_loopback(h)) for h in hosts]     # a token check on every address that is not loopback, and wherever a token was given
    token = (token or access.new_token()) if any(guarded) else None
    gate = access.Gate(token) if token else None
    ALLOWED_HOSTS.update(host_name(h) or norm_host(h.strip().strip('[]').lower().rstrip('.')) for h in hosts)
    if any(access.is_wildcard(h) for h in hosts):                  # the addresses printed for a wildcard are this machine's own: without a token they must be let in, or the printed address would be refused
        ALLOWED_HOSTS.update(access.local_addresses())
    ALLOWED_HOSTS.update(a for a in args.allow_host if not a.startswith('.'))
    ALLOWED_SUFFIXES.update(a for a in args.allow_host if a.startswith('.'))
    servers = bind_servers(hosts, args.port)                       # the ports are opened first; on a failure the program ends here
    for srv, on in zip(servers, guarded):
        srv.gate = gate if on else None
    for h, srv, on in zip(hosts, servers, guarded):
        port = srv.server_address[1]
        if on:
            print(cli_t('cli.start.auth'), flush=True)
            for name in access.shown_addresses(h):
                print('  http://%s:%d/?token=%s' % (name, port, token), flush=True)
            print(cli_t('cli.start.auth_note'), flush=True)
        else:
            for name in access.shown_addresses(h):
                print(cli_t('cli.start.url', url='http://%s:%d/' % (name, port)), flush=True)
    open_hosts = [h for h, on in zip(hosts, guarded) if not on]
    exposed = [('cli.warn.no_auth.hosts', [access.shown(h) for h in open_hosts if not access.is_loopback(h)]),
               ('cli.warn.no_auth.allowed', [a for a in args.allow_host if open_hosts and not access.is_loopback(a)])]
    if any(names for _, names in exposed):
        bar = '!' * 72
        print('%s\n%s\n%s\n%s' % (bar, cli_t('cli.warn.no_auth.title'), cli_t('cli.warn.no_auth', where=' · '.join(cli_t(key, names=', '.join(names)) for key, names in exposed if names)), bar),
              file=sys.stderr, flush=True)
    board_sessions.WALK_ENABLED = True                              # the background walk for debate folders that no record names (every 60 s, off the request thread)
    board_sessions.LATER.arm()                                      # the second link pass and the walk wait for the first picture, or this many seconds
    if not args.no_link_cache:
        LINKS.lineage.enable_cache(LINK_CACHE)                  # keep the certain links in a file and read them at start (the option to turn it off: --no-link-cache)
    threading.Thread(target=LINKS.scan, daemon=True).start()     # the first Claude ↔ Codex link scan (in the background)
    if args.session:
        DEFAULT_SESSION = FIXED_DEFAULT = args.session
    LINKS.ready.wait(30)
    ss, counts = scan_sessions()
    for line in startup_lines(sources(counts)):
        print(line, flush=True)
    if not args.session:
        DEFAULT_SESSION = default_session(session_projects(ss), ss)
    if DEFAULT_SESSION:
        t0 = time.time()
        try:
            s = REG.get(DEFAULT_SESSION)
        except OSError:                                            # a record that cannot be opened: the page says so when it asks for it
            s = None
        if s is None and args.session:
            for srv in servers:
                srv.server_close()
            ap.error(cli_t('cli.arg.session_not_found', session=args.session))          # a well-formed id with no transcript
        if s is not None:
            print(cli_t('cli.start.session', id=DEFAULT_SESSION, agents=len(s.agents), sec='%.1f' % (time.time() - t0)), flush=True)
    threading.Thread(target=REG.loop, daemon=True).start()
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0].serve_forever()


if __name__ == '__main__':
    main()
