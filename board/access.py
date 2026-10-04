"""Who may open the pages: the access token (random at each start unless it is fixed), the cookie it earns, the 401 answer, and the addresses of this machine to print.

An address that is not loopback gets a token check by default. The token is compared in constant time and appears only in the address the server prints and in the
`?token=` the user types; the cookie the browser keeps holds a value derived from it and from a salt of this start (HMAC), so no response ever repeats the token. Standard library only."""

import hashlib
import hmac
import html
import ipaddress
import re
import secrets
import socket
import struct
import sys
import threading
from urllib.parse import parse_qsl, unquote_plus, urlencode

from . import i18n

TOKEN_MIN = 16                                   # characters of a token given with --token or AGENT_BULLPEN_TOKEN
TOKEN_RE = re.compile(r'[A-Za-z0-9._~-]{%d,200}' % TOKEN_MIN)         # only characters that go into an address as they are
COOKIE_PREFIX = 'agent_bullpen_'                 # + the port: two dashboards on one host name do not overwrite each other's cookie
COOKIE_LABEL = b'agent-bullpen cookie v1'
VIRTUAL_NICS = ('docker', 'br-', 'veth', 'virbr', 'vmnet', 'vboxnet')         # bridges of containers and virtual machines: not where another device would reach this one
OK, LOGIN, DENY = 'ok', 'login', 'deny'          # what Gate.judge answers


def new_token():
    return secrets.token_urlsafe(32)


def token_ok(value):
    return bool(TOKEN_RE.fullmatch(value or ''))


def is_loopback(name):
    """Is an address or a host name loopback: localhost, 127.0.0.0/8, ::1 (also written as an IPv4-mapped address). A wildcard (0.0.0.0, ::) and any other name is not."""
    name = (name or '').strip().strip('[]').lower().rstrip('.')
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return name == 'localhost'
    mapped = getattr(ip, 'ipv4_mapped', None)
    return (mapped or ip).is_loopback


def is_wildcard(name):
    try:
        return ipaddress.ip_address((name or '').strip().strip('[]')).is_unspecified
    except ValueError:
        return False


_PARAM = re.compile(r'([?&;])([^=&;\s"]*)=([^&;\s"]*)')


def redact(text):
    """A log line without the value of the `token` parameter (the request line of a first visit carries it). The key is judged after decoding, as the check does it
    (`%74oken` is `token`), and every repeat of it loses its value."""
    return _PARAM.sub(lambda m: m.group(0) if unquote_plus(m.group(2)).lower() != 'token' else '%s%s=…' % (m.group(1), m.group(2)), text)


def _bytes(value):
    return (value or '').encode('utf-8', 'replace')


class Gate:
    """The check one listener makes before it answers. Holds the token, and from it the value of the cookie: derived from the token and from a salt made at each start,
    so a cookie that was copied stops working when the server is started again, even with the same fixed token."""

    def __init__(self, token):
        self._token = _bytes(token)
        self._cookie = hmac.new(self._token, COOKIE_LABEL + secrets.token_bytes(16), hashlib.sha256).hexdigest().encode()

    @staticmethod
    def cookie_name(port):
        return '%s%d' % (COOKIE_PREFIX, port)

    def cookie_header(self, port):
        """The Set-Cookie value: a session cookie the page script cannot read and other sites never get sent."""
        return '%s=%s; Path=/; HttpOnly; SameSite=Strict' % (self.cookie_name(port), self._cookie.decode())

    def _is_token(self, value):
        return hmac.compare_digest(self._token, _bytes(value))

    def _from_cookie(self, header, port):
        want, found = self.cookie_name(port), False
        for part in (header or '').split(';'):
            name, _, value = part.strip().partition('=')
            if name == want and hmac.compare_digest(self._cookie, _bytes(value)):
                found = True
        return found

    def _from_bearer(self, header):
        scheme, _, value = (header or '').strip().partition(' ')
        return scheme.lower() == 'bearer' and self._is_token(value.strip())

    def judge(self, cookie, authorization, query, port):
        """(OK, None): serve the request. (LOGIN, rest): the address carried the token, so set the cookie and send the browser to the same address without it;
        rest is the query that remains (a list of pairs). (DENY, None): 401."""
        pairs = parse_qsl(query or '', keep_blank_values=True)
        if any(k == 'token' and self._is_token(v) for k, v in pairs):
            return LOGIN, [(k, v) for k, v in pairs if k != 'token']
        if self._from_cookie(cookie, port) or self._from_bearer(authorization):
            return OK, None
        return DENY, None


def clean_location(path, rest):
    """Where to send the browser once the token is out of the address: the same path and the other query pairs. Only ever a path on this host."""
    if not path.startswith('/') or path.startswith('//') or '\\' in path:
        path = '/'
    return path + ('?' + urlencode(rest) if rest else '')


def denied_page():
    """The 401 body for a page: a short note in English and in Korean (the reader's language is not known here). It repeats nothing from the request."""
    parts = []
    for lang in ('en', 'ko'):
        t = i18n.translator(lang)
        parts.append('<section lang="%s"><h1>%s</h1><p>%s</p></section>' % (lang, html.escape(t('cli.auth.denied.title')), html.escape(t('cli.auth.denied.body'))))
    head = '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent Bullpen</title></head>'
    style = 'font:16px/1.6 system-ui,sans-serif;max-width:40em;margin:3em auto;padding:0 1em'
    return ('%s<body style="%s">%s</body></html>\n' % (head, style, ''.join(dict.fromkeys(parts)))).encode()


NAME_LOOKUP_WAIT = 1.0          # seconds the host-name fallback of local_addresses() may take


def local_addresses():
    """IPv4 addresses of this machine that another device could use (not loopback, not link-local, not a container or virtual machine bridge), asked of the system's
    interfaces. No connection is made and nothing is sent. Every step may fail on some systems and then adds nothing."""
    found = []

    def add(text):
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            return
        if ip.version == 4 and not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast) and str(ip) not in found:
            found.append(str(ip))
    try:
        import fcntl
        req = 0x8915 if sys.platform.startswith('linux') else 0xc0206921            # SIOCGIFADDR
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _, name in socket.if_nameindex():
                if name.startswith(VIRTUAL_NICS):
                    continue
                try:
                    out = fcntl.ioctl(s.fileno(), req, struct.pack('256s', name.encode()[:15]))
                    add(socket.inet_ntoa(out[20:24]))
                except OSError:
                    pass
    except (ImportError, OSError, AttributeError, ValueError):
        pass
    if not found:
        # Resolving this machine's own name can wait on DNS for many seconds (macOS with a .local name): ask in a side thread and give up
        # after a second, so the start-up output is never held back by it.
        box = []

        def resolve():
            try:
                box.extend(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
            except (OSError, UnicodeError):
                pass
        t = threading.Thread(target=resolve, daemon=True)
        t.start()
        t.join(NAME_LOOKUP_WAIT)
        for text in list(box) if not t.is_alive() else ():
            add(text)
    return found


def shown(host):
    """An address as it goes into a URL: localhost for 127.0.0.1, brackets around an IPv6 address."""
    return 'localhost' if host == '127.0.0.1' else '[%s]' % host if ':' in host else host


def shown_addresses(host, limit=4):
    """The hosts to put in the printed addresses for one listener: for a wildcard, localhost and this machine's addresses; otherwise the address itself."""
    return ['localhost'] + local_addresses()[:limit] if is_wildcard(host) else [shown(host)]

