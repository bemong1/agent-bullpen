"""Claude · Codex plans and usage: the account usage query (ClaudeUsage, singleton CL_USAGE; only with --claude-usage-api), the ~/.claude.json cache, records of reaching a limit, and the values of /api/plans."""

import json
import os
import re
import time
import urllib.error
import urllib.request

from .util import CLAUDE_HOME, CLAUDE_JSON, HOME, parse_ts
from .codex_index import CODEX
from .link import LINKS


_CL_CONF = {'key': None, 'v': None}      # key = (file read, modification time)
USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
USAGE_EVERY = 60           # seconds. With --claude-usage-api on, the same account usage API as Claude Code's /usage is called once a minute
# the last queried value and its query time (usage rates only, no token or account info). The value is shown even right after a restart, and the first query is
# put off to 1 minute after the previous one, which prevents the two queries within a minute that used to draw a 429
USAGE_STATE = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.join(HOME, '.cache'), 'agent-bullpen', 'usage.json')


def _usage_request(tok):
    """The usage query request. The token goes only in a header that is not passed on to another address even if a redirect is followed."""
    req = urllib.request.Request(USAGE_URL, headers={'anthropic-beta': 'oauth-2025-04-20',
                                                     'Content-Type': 'application/json', 'User-Agent': 'agent-bullpen'})
    req.add_unredirected_header('Authorization', 'Bearer ' + tok)
    return req


class ClaudeUsage:
    """Queries the Claude account usage directly (only when turned on with --claude-usage-api. Off by default: touches neither the auth file nor usage.json).
    Uses only the current access token in ~/.claude/.credentials.json: it does not issue a new token (the CLI login could be dropped),
    and never puts the token on the screen, in a response or in a log. On failure it keeps only the reason and falls back to the ~/.claude.json cache."""

    def __init__(self, state=None):
        self.enabled = False     # main turns this on after seeing --claude-usage-api. While it is off, loop does nothing
        self.v = None            # the last value that succeeded
        self.error = None        # the last failure reason (short text)
        self.tried = None
        self.retry_after = None  # Retry-After of a 429 (seconds): wait at least that long until the next query
        self.code = None         # HTTP status of the last failure (429 etc.)
        self.error_info = None   # the same failure as a code, {code, params}: the page words it from the dictionary (plan.error.<code>)
        self.state = state or USAGE_STATE

    def poll(self):
        """Queries once. The failure reason and status code are changed together after the result is in (so the page never reads a half-changed value during the request)."""
        self.tried = time.time()
        err, code, ra, info = self._fetch()
        self.error, self.code, self.retry_after, self.error_info = err, code, ra, info

    def _fetch(self):
        """(failure text, HTTP status, Retry-After seconds, failure code {code, params}). On success it sets self.v and returns (None, None, None, None)."""
        try:
            with open(os.path.join(CLAUDE_HOME, '.credentials.json')) as f:
                o = json.load(f).get('claudeAiOauth') or {}
        except (OSError, ValueError, AttributeError):
            return '로그인 정보 없음', None, None, {'code': 'login_missing', 'params': {}}
        tok = o.get('accessToken')
        if not tok or (o.get('expiresAt') or 0) / 1000 <= time.time():
            return '토큰 만료 — Claude Code를 쓰면 갱신', None, None, {'code': 'token_expired', 'params': {}}
        try:
            with urllib.request.urlopen(_usage_request(tok), timeout=15) as r:
                self.v = dict(usage_fields(json.load(r)), as_of=time.time())
            return None, None, None, None
        except urllib.error.HTTPError as e:
            try:
                ra = float(e.headers.get('Retry-After')) if e.code == 429 and e.headers else None
            except (TypeError, ValueError):
                ra = None
            return '조회 실패(HTTP %d)' % e.code, e.code, ra, {'code': 'http_error', 'params': {'status': e.code}}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return '조회 실패(%s)' % type(e).__name__, None, None, {'code': 'fetch_failed', 'params': {'error': type(e).__name__}}

    def shown_error(self, now=None):
        """The failure reason to show on the page. This API gives a 429 on every other call at 1-minute spacing (observed 2026-09-30): even with a 429, if the last value
        is within 3 minutes the value is still right, so the reason is hidden, and it is shown only when the value is older. Other failures are always shown."""
        now = time.time() if now is None else now
        if self.code == 429 and self.v and now - (self.v.get('as_of') or 0) < 3 * USAGE_EVERY:
            return None
        return self.error

    def shown_error_info(self, now=None):
        """shown_error() as a code: hidden when it is hidden, shown when it is shown."""
        return self.error_info if self.shown_error(now) else None

    def load(self):
        """The last value and query time left by the previous server. If missing or broken, just move on."""
        try:
            with open(self.state) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(d, dict):
            if isinstance(d.get('v'), dict) and d['v'].get('as_of'):
                self.v = d['v']
            if isinstance(d.get('tried'), (int, float)):
                self.tried = d['tried']

    def save(self):
        """Writes only the usage rates and the query time (mode 0600, swapped in with os.replace). A failure does not stop the querying."""
        try:
            os.makedirs(os.path.dirname(self.state), mode=0o700, exist_ok=True)
            tmp = self.state + '.tmp'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump({'v': self.v, 'tried': self.tried}, f)
            os.replace(tmp, self.state)
        except OSError:
            pass

    def wait(self, now=None):
        """Seconds to wait until the next query: 1 minute after the previous query (including one from before a restart); if a 429 gives a Retry-After, no sooner than that."""
        now = time.time() if now is None else now
        gap = max(USAGE_EVERY, self.retry_after or 0)
        return max(0.0, (self.tried or 0) + gap - now)

    def loop(self):
        if not self.enabled:
            return
        self.load()
        while True:
            time.sleep(self.wait())
            try:
                self.poll()
            except Exception as e:   # noqa: BLE001 — the board must stay up
                self.error = '조회 실패(%s)' % type(e).__name__
                self.error_info = {'code': 'fetch_failed', 'params': {'error': type(e).__name__}}
            if self.v is not None or self.code is not None:   # keep it only when the API was actually called (with no credentials, no file is created)
                self.save()


def usage_fields(u):
    """Only the values to use on the page, from a usage response (or from the utilization in the ~/.claude.json cache)."""
    win = lambda w: {'percent': w.get('utilization'), 'resets_at': parse_ts(w.get('resets_at'))} if isinstance(w, dict) else None
    scoped = [{'name': ((x.get('scope') or {}).get('model') or {}).get('display_name') or '', 'percent': x.get('percent'),
               'resets_at': parse_ts(x.get('resets_at'))} for x in u.get('limits') or [] if x.get('kind') == 'weekly_scoped']
    ex = u.get('extra_usage') or {}
    return {'five_hour': win(u.get('five_hour')), 'seven_day': win(u.get('seven_day')), 'scoped': scoped,
            'extra': {'enabled': ex.get('is_enabled'), 'reason': ex.get('disabled_reason'),
                      'used': ex.get('used_credits'), 'limit': ex.get('monthly_limit')} if ex else None}


CL_USAGE = ClaudeUsage()


def claude_plan():
    """Claude Code plan and usage: the plan tier in ~/.claude.json and the usage cache (refreshed when /usage is opened), and limit hits found in the records.
    With --claude-usage-api on, a directly queried value is used when it is newer. If usage_api is false only the cache is used and error is always empty.
    Account identifiers and email are not sent out."""
    p = next((c for c in CLAUDE_JSON if os.path.exists(c)), None)
    if p is None:
        return None
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    if (p, mt) != _CL_CONF['key']:
        try:
            with open(p) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return dict(_CL_CONF['v'], usage_api=CL_USAGE.enabled) if _CL_CONF['v'] else None
        acc, cu = d.get('oauthAccount') or {}, d.get('cachedUsageUtilization') or {}
        tier = acc.get('userRateLimitTier') or acc.get('organizationRateLimitTier') or ''
        name = re.sub(r'^default_claude_', '', tier).replace('_', ' ').strip()
        _CL_CONF['v'] = dict(usage_fields(cu.get('utilization') or {}), plan=(name[:1].upper() + name[1:]) if name else '',
                             billing=acc.get('billingType'), as_of=(cu.get('fetchedAtMs') or 0) / 1000 or None, source='cache')
        _CL_CONF['key'] = (p, mt)
    v = dict(_CL_CONF['v'] or {})
    live = CL_USAGE.v
    if live and (not v.get('as_of') or live['as_of'] >= v['as_of']):   # a directly queried value wins if it is newer than the cache
        v.update(live, source='api')
    v['usage_api'] = CL_USAGE.enabled
    v['error'] = CL_USAGE.shown_error()
    v['error_info'] = CL_USAGE.shown_error_info()
    v['hits'] = live_hits(LINKS.quota, v)
    return v


def live_hits(quota, v):
    """Limit hits (rejections) in the records that are still in effect. The records keep only the rejection, not the release, so if the usage read after a hit is
    under 100% in that window, it is treated as a past record (the case where a reset ticket took usage to 0% but the scheduled reset time remained, so it still showed "limit reached")."""
    out = {}
    for k, h in quota.items():
        w = v.get(k)
        if isinstance(w, dict) and w.get('percent') is not None and w['percent'] < 100 \
                and (v.get('as_of') or 0) > (h.get('ts') or 0):
            continue
        out[k] = dict(h)
    return out


def plan_status():
    return {'now': time.time(), 'claude': claude_plan(), 'codex': CODEX.limit()}
