"""Claude · Codex plans and usage: the usage Claude Code hands to its status line (statusline.py leaves it in a small file), the ~/.claude.json cache,
records of reaching a limit, and the values of /api/plans. Nothing here makes a network request or opens a credential file."""

import json
import math
import os
import re
import stat
import time

from . import lineage
from .util import CLAUDE_HOME, CLAUDE_JSON, HOME, parse_ts
from .codex_index import CODEX
from .link import LINKS


_CL_CONF = {'key': None, 'v': None}      # key = (file read, modification time)
# what statusline.py (the status line command of Claude Code) leaves: the rate limits Claude Code passed in, with the time they were received. Only read here, never written
STATUSLINE_STATE = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.join(HOME, '.cache'), 'agent-bullpen', 'statusline.json')
STATUSLINE_MAX = 64 << 10      # cap on the file read
STATUSLINE_LIVE = 600          # seconds: a status line reading this recent is used even if another source has a newer one (so the bar does not flip between sources)


def _obj(x):
    """x when it is an object, else an empty one: a file of valid JSON in another shape is read as having none of the field."""
    return x if isinstance(x, dict) else {}


def usage_fields(u):
    """Only the values to use on the page, from the utilization in the ~/.claude.json cache. A field of another shape counts as missing."""
    iso = lambda x: parse_ts(x) if isinstance(x, str) else None
    win = lambda w: {'percent': w.get('utilization'), 'resets_at': iso(w.get('resets_at'))} if isinstance(w, dict) else None
    u = _obj(u)
    limits = u.get('limits') if isinstance(u.get('limits'), list) else []
    scoped = [{'name': (_obj(_obj(x.get('scope')).get('model')).get('display_name') or ''), 'percent': x.get('percent'),
               'resets_at': iso(x.get('resets_at'))} for x in limits if isinstance(x, dict) and x.get('kind') == 'weekly_scoped']
    ex = _obj(u.get('extra_usage'))
    return {'five_hour': win(u.get('five_hour')), 'seven_day': win(u.get('seven_day')), 'scoped': scoped,
            'extra': {'enabled': ex.get('is_enabled'), 'reason': ex.get('disabled_reason'),
                      'used': ex.get('used_credits'), 'limit': ex.get('monthly_limit')} if ex else None}


def _epoch(x):
    """A reset time as epoch seconds: a number in seconds or milliseconds, or an ISO date-time text. None for anything else (a bool, NaN, a shape that is not a time)."""
    if isinstance(x, str):
        return parse_ts(x[:40])
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0:
        return None
    return x / 1000 if x > 1e11 else float(x)


def _status_window(w, now):
    """A window of the status line file as the page reads it ({percent, resets_at}). One whose reset time has passed has no known usage any more
    (Claude Code reports a window only while it is running), so its percent is None and only the reset time is kept. None if the shape is wrong."""
    if not isinstance(w, dict):
        return None
    p, r = w.get('used_percentage'), _epoch(w.get('resets_at'))
    if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or r is None:
        return None
    return {'percent': min(100.0, max(0.0, float(p))) if r > now else None, 'resets_at': r}


def statusline_usage(path=None, now=None):
    """The reading statusline.py left, as {five_hour, seven_day, as_of}, or None: no file, not a file of mine that only I can write (the same rule as the link
    cache), not a regular file, too big, not the expected shape, from the future, or with no window that is still running."""
    path = path or STATUSLINE_STATE
    now = time.time() if now is None else now
    try:
        if not lineage._cache_trusted(path):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > STATUSLINE_MAX:
            return None
        with os.fdopen(fd, 'rb') as fh:
            fd = -1
            d = json.loads(fh.read(STATUSLINE_MAX + 1))
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(d, dict):
        return None
    at, rl = d.get('at'), d.get('rate_limits')
    if d.get('v') != 1 or isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at) or at > now + 300 or not isinstance(rl, dict):
        return None
    out = {k: _status_window(rl.get(k), now) for k in ('five_hour', 'seven_day')}
    if not any(w and w['percent'] is not None for w in out.values()):
        return None
    return dict(out, as_of=float(at))


def _claude_cache():
    """The plan tier and the usage cache in ~/.claude.json (read again only when the file changes), or None without a readable file."""
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
            return dict(_CL_CONF['v']) if _CL_CONF['v'] else None
        if not isinstance(d, dict):                        # valid JSON of another shape: like a file that cannot be read
            return dict(_CL_CONF['v']) if _CL_CONF['v'] else None
        acc, cu = _obj(d.get('oauthAccount')), _obj(d.get('cachedUsageUtilization'))
        tier = acc.get('userRateLimitTier') or acc.get('organizationRateLimitTier') or ''
        name = re.sub(r'^default_claude_', '', tier).replace('_', ' ').strip() if isinstance(tier, str) else ''
        fetched = cu.get('fetchedAtMs')
        ok = isinstance(fetched, (int, float)) and not isinstance(fetched, bool) and math.isfinite(fetched) and fetched > 0
        _CL_CONF['v'] = dict(usage_fields(cu.get('utilization')), plan=(name[:1].upper() + name[1:]) if name else '',
                             billing=acc.get('billingType'), as_of=fetched / 1000 if ok else None, source='cache')
        _CL_CONF['key'] = (p, mt)
    return dict(_CL_CONF['v'] or {})


def claude_plan(now=None):
    """Claude Code plan and usage. The plan tier comes from ~/.claude.json. The 5-hour and weekly usage comes from the first of these that has one: the status
    line file (statusline.py; no request, no token) or the ~/.claude.json cache (refreshed when /usage is opened, and a limit hit found in the records). Which one is
    in `source` ('statusline' | 'cache') with its time in `as_of`. A status line reading is used while it is under STATUSLINE_LIVE seconds old; after that the newest
    of the two is used, so a status line command that stopped (Claude Code closed) does not hold back a newer cache. Account identifiers and email are not sent out."""
    now = time.time() if now is None else now
    v = _claude_cache()
    sl = statusline_usage(now=now)
    if v is None and not sl:
        return None
    v = v or {}
    v['cache_as_of'] = v.get('as_of')                  # when the ~/.claude.json cache was written (the values below it, such as the scoped weekly ones, are from then)
    if sl and (now - sl['as_of'] <= STATUSLINE_LIVE or sl['as_of'] >= (v.get('as_of') or 0)):
        v.update(five_hour=sl['five_hour'], seven_day=sl['seven_day'], as_of=sl['as_of'], source='statusline')   # the plan, scoped and extra usage stay from the cache
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
