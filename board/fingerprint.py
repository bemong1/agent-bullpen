"""Content fingerprint of a child's instruction.

A `claude -p` child's first instruction is usually *assembled* by the launching shell from several pieces (a common file + a role file, a heredoc
variable, a positional argument), so a hash of the whole text almost never appears in the launcher's record. What does appear are pieces of it. The
fingerprint therefore cuts the normalised instruction into up to 16 anchors of 32 characters and asks which owner's recent text (Bash commands, Write
contents, Edit replacements) contains how many of them.

Everything here is pure: the caller supplies the texts. The one stateful thing is `TextCache`, a bounded memory of normalised line texts (never written to
a file). Standard library only; Python 3.9 compatible.
"""

import collections
import sys
import unicodedata

ANCHOR_LEN = 32                  # characters in one anchor; an instruction shorter than this is "short"
ANCHOR_MAX = 16                  # anchors per instruction
SHORT_MIN = ANCHOR_LEN
COVER_MIN = 0.4                  # an owner must contain at least this share of the anchors ...
COVER_RATIO = 2.0                # ... and at least this many times the second owner's share
WINDOW_BEFORE = 7200.0           # the owner's text window is [t0 - 2 h, t0 + 2 s]
WINDOW_AFTER = 2.0
HEAD_BYTES = 16 << 10            # only the head of a child's instruction is read; counted in bytes held, an ASCII head is this many characters
HEAD_CHARS = HEAD_BYTES
CACHE_BYTES = 32 << 20           # all cached text together (LRU), counted in bytes held (a character takes 1, 2 or 4 bytes in memory)
OWNER_BYTES = 2 << 20            # one owner's window, in bytes held; a bigger window is cut and the comparison is marked incomplete
LINE_BYTES = 256 << 10           # one record line is cut here (bytes held) before it is normalised; a cut line marks the comparison incomplete
ENTRY_BYTES = 256                # what one cache entry costs besides the bytes of its text: the object header, the key tuple, the mapping slot and the list links
CUT = '\x01'                     # ends a text that was cut at LINE_BYTES (no normalised text holds it: a pool containing it is incomplete)

_DROP = str.maketrans('', '', '\\\'"`')
_EMPTY = sys.getsizeof('')


def nbytes(s):
    """The bytes a text holds in memory (CPython keeps 1, 2 or 4 bytes a character by the widest character in it), less the header every string has."""
    return max(0, sys.getsizeof(s) - _EMPTY)


def char_width(s):
    """The bytes one character of s takes in memory: CPython keeps every character of a string at the width of the widest one in it (1, 2 or 4)."""
    if s.isascii():
        return 1
    top = max(s)
    return 1 if top < '\u0100' else 2 if top < '\U00010000' else 4


def clip(s, limit):
    """The longest prefix of s that holds at most `limit` bytes (nbytes). The limits of this module count what the text takes, not how many characters it has."""
    if len(s) > limit:
        s = s[:limit]                                    # a character takes at least one byte: no more than `limit` of them can fit
    if nbytes(s) <= limit:
        return s
    width = char_width(s)
    s = s[:max(0, limit // width)]
    while s and nbytes(s) > limit:
        s = s[:-max(1, (nbytes(s) - limit + width - 1) // width)]
    return s


def normalize(s):
    """NFC, the shell-quoting characters ``\\ ' " ` `` removed, whitespace runs folded to one space, ends trimmed. The same function is applied to the
    child's instruction and to the launcher's texts, so a quoting difference between the command line and the record does not matter."""
    if not s:
        return ''
    return ' '.join(unicodedata.normalize('NFC', s).translate(_DROP).split())


def anchors(text_n):
    """Up to ANCHOR_MAX anchors of ANCHOR_LEN characters, spread evenly over an already normalised text. () when the text is shorter than one anchor."""
    n = len(text_n)
    if n < ANCHOR_LEN:
        return ()
    k = min(ANCHOR_MAX, 1 + (n - ANCHOR_LEN) // 16)
    if k == 1:
        offs = [0]
    else:
        step = (n - ANCHOR_LEN) / float(k - 1)
        offs = [int(round(i * step)) for i in range(k)]
    out = []
    for o in offs:
        a = text_n[o:o + ANCHOR_LEN]
        if a not in out:
            out.append(a)
    return tuple(out)


def coverage(anchor_list, pool):
    """The share of the anchors that occur in `pool` (a normalised text). 0.0 for no anchors."""
    if not anchor_list or not pool:
        return 0.0
    return sum(1 for a in anchor_list if a in pool) / float(len(anchor_list))


def pick(scores):
    """Which key of {key: coverage} the content points to: ('ok', key) when the best is >= COVER_MIN and at least COVER_RATIO times the second best,
    ('tie', [keys]) when two or more owners reach COVER_MIN and none stands out (the instruction fits several launchers; held, not broken by a lower rank),
    ('weak', None) when only one reaches COVER_MIN but another is too close behind it for the ratio, ('none', None) when nobody reaches COVER_MIN."""
    ranked = sorted(((v, k) for k, v in scores.items() if v >= COVER_MIN), key=lambda x: -x[0])
    if not ranked:
        return 'none', None
    best = ranked[0][0]
    second = scores_second(scores, ranked[0][1])
    if best >= COVER_RATIO * second:
        return 'ok', ranked[0][1]
    near = [k for v, k in ranked if v * COVER_RATIO > best]
    return ('tie', near) if len(near) > 1 else ('weak', None)


def scores_second(scores, best_key):
    rest = [v for k, v in scores.items() if k != best_key]
    return max(rest) if rest else 0.0


class TextCache:
    """A bounded memory of normalised line texts: key -> text, least recently used first out, `cap` bytes in all (the bytes the texts hold in memory plus
    ENTRY_BYTES an entry, not their number of characters). A text that is missing is simply read again by the caller, so the cap changes speed, never an
    answer. It is never written to a file."""

    def __init__(self, cap=CACHE_BYTES):
        self.cap = cap
        self.size = 0
        self._d = collections.OrderedDict()

    def get(self, key):
        v = self._d.get(key)
        if v is not None:
            self._d.move_to_end(key)
        return v

    @staticmethod
    def _cost(text):
        return nbytes(text) + ENTRY_BYTES

    def put(self, key, text):
        if key in self._d:
            self.size -= self._cost(self._d.pop(key))
        n = self._cost(text)
        if n > self.cap:
            return
        self._d[key] = text
        self.size += n
        while self.size > self.cap and self._d:
            _, old = self._d.popitem(last=False)
            self.size -= self._cost(old)

    def drop_prefix(self, owner):
        """Forgets every text of one owner (its record was replaced or removed). Keys are (owner, offset)."""
        for k in [k for k in self._d if k[0] == owner]:
            self.size -= self._cost(self._d.pop(k))

    def __len__(self):
        return len(self._d)


def join_pool(texts, limit=OWNER_BYTES):
    """One '\\0'-joined pool of normalised texts, newest first until `limit` bytes (nbytes of the joined string). (pool, complete): complete is False when older
    texts were left out because the window is bigger than one owner may hold, or when a text in it was cut at LINE_BYTES (marked with CUT): no certain link may
    come from that comparison. The CUT mark itself is not part of the pool. A joined string is as wide as its widest character, so one emoji makes every
    character of the pool take 4 bytes: the size is the characters kept times that width, not the sum of what each text held on its own."""
    out, chars, wide, complete = [], 0, 1, True
    for t in texts:
        cut = CUT in t
        if cut:
            t = t.replace(CUT, '')
        w = max(wide, char_width(t))
        n = chars + len(t) + (1 if out else 0)
        if n * w > limit:
            complete = False
            break
        if cut:
            complete = False
        out.append(t)
        chars, wide = n, w
    pool = '\0'.join(out)
    while out and nbytes(pool) > limit:                  # the header of a string with wide characters is a few bytes bigger: the exact size has the last word
        out.pop()
        complete = False
        pool = '\0'.join(out)
    return pool, complete
