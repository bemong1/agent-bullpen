"""The content fingerprint (board/fingerprint.py): normalisation, anchors, coverage, the pick rule, the bounded text cache.

    python3 -m unittest discover -s tests
"""
import ast
import os
import random
import sys
import unicodedata
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from board import fingerprint as fp  # noqa: E402

LONG = ('Please review the amber basin cedar delta ember fjord grove harbor island juniper kelp lagoon meadow nectar orchard prairie quartz ridge summit '
        'tundra umber valley willow xenon yarrow zephyr and report in plain words.')


class Normalize(unittest.TestCase):
    def test_quotes_backslashes_backticks_and_whitespace(self):
        self.assertEqual(fp.normalize('  a  "b"\n\t\'c\' \\d `e` '), 'a b c d e')
        self.assertEqual(fp.normalize(''), '')
        self.assertEqual(fp.normalize(None), '')
        self.assertEqual(fp.normalize('a b　c'), 'a b c')                    # any whitespace

    def test_nfc(self):
        composed, decomposed = 'é', 'é'
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(fp.normalize(decomposed), fp.normalize(composed))
        ko = unicodedata.normalize('NFD', '한글 지시문')
        self.assertEqual(fp.normalize(ko), '한글 지시문')

    def test_command_line_and_record_agree(self):
        """The shell removes the quoting; the record keeps the dequoted words: both normalise to the same text."""
        self.assertEqual(fp.normalize('claude -p "Please \\"review\\" it"'), fp.normalize('claude -p Please review it'))


class Anchors(unittest.TestCase):
    def test_short_text_has_none(self):
        self.assertEqual(fp.anchors('x' * 31), ())
        self.assertEqual(len(fp.anchors('x' * 32)), 1)

    def test_count_and_spread(self):
        t = fp.normalize(LONG)
        a = fp.anchors(t)
        self.assertEqual(len(a), min(fp.ANCHOR_MAX, 1 + (len(t) - 32) // 16))
        self.assertTrue(all(len(x) == fp.ANCHOR_LEN for x in a))
        self.assertTrue(t.startswith(a[0]) and t.endswith(a[-1]))                  # the first and the last piece are both used
        self.assertLessEqual(len(fp.anchors(t * 20)), fp.ANCHOR_MAX)

    def test_no_duplicates(self):
        a = fp.anchors('ab' * 200)
        self.assertEqual(len(a), len(set(a)))


class Coverage(unittest.TestCase):
    def test_assembled_instruction_is_found_in_pieces(self):
        """An instruction made of a common file and a role file is in no single text, yet most anchors are in the two pieces (why a whole-text hash fails)."""
        t = fp.normalize(LONG)
        cut = t.rfind(' ', 0, int(len(t) * 0.55)) + 1
        pool = '\0'.join([t[:cut], 'something else', t[cut:]])
        self.assertNotIn(t, pool)
        self.assertGreaterEqual(fp.coverage(fp.anchors(t), pool), fp.COVER_MIN)

    def test_unrelated_text_scores_zero(self):
        self.assertEqual(fp.coverage(fp.anchors(fp.normalize(LONG)), 'nothing to see here ' * 50), 0.0)
        self.assertEqual(fp.coverage((), 'x'), 0.0)
        self.assertEqual(fp.coverage(('a',), ''), 0.0)


class Pick(unittest.TestCase):
    def test_threshold_and_ratio(self):
        self.assertEqual(fp.pick({'a': 0.9, 'b': 0.1}), ('ok', 'a'))
        self.assertEqual(fp.pick({'a': 0.39, 'b': 0.0}), ('none', None))               # below 0.4
        self.assertEqual(fp.pick({'a': 0.8, 'b': 0.5}), ('tie', ['a', 'b']))           # not twice the second
        self.assertEqual(fp.pick({'a': 0.8, 'b': 0.4}), ('ok', 'a'))                   # exactly twice
        self.assertEqual(fp.pick({'a': 1.0, 'b': 1.0}), ('tie', ['a', 'b']))
        self.assertEqual(fp.pick({}), ('none', None))
        self.assertEqual(fp.pick({'a': 0.5}), ('ok', 'a'))

    def test_a_second_that_is_almost_there_still_blocks_the_leader(self):
        self.assertEqual(fp.pick({'a': 0.6, 'b': 0.35}), ('weak', None))             # 0.6 < 2 * 0.35: not clearly ahead of something that is almost there


class Cache(unittest.TestCase):
    def test_lru_by_bytes(self):
        one = 40 + fp.ENTRY_BYTES
        c = fp.TextCache(cap=2 * one + 10)
        c.put(('p', 1), 'a' * 40)
        c.put(('p', 2), 'b' * 40)
        self.assertEqual(c.get(('p', 1)), 'a' * 40)                                      # refreshes 1
        c.put(('p', 3), 'c' * 40)                                                        # evicts 2, the least recently used
        self.assertIsNone(c.get(('p', 2)))
        self.assertIsNotNone(c.get(('p', 1)))
        self.assertLessEqual(c.size, 2 * one + 10)

    def test_oversize_and_replace(self):
        c = fp.TextCache(cap=fp.ENTRY_BYTES + 10)
        c.put('k', 'x' * 11)
        self.assertEqual(len(c), 0)
        c.put('k', 'abc')
        c.put('k', 'de')
        self.assertEqual((c.get('k'), c.size), ('de', 2 + fp.ENTRY_BYTES))

    def test_drop_prefix(self):
        c = fp.TextCache()
        c.put(('p', 1), 'a')
        c.put(('q', 1), 'b')
        c.drop_prefix('p')
        self.assertEqual((c.get(('p', 1)), c.get(('q', 1))), (None, 'b'))

    def test_the_limits_are_the_approved_ones(self):
        self.assertEqual((fp.CACHE_BYTES, fp.OWNER_BYTES, fp.WINDOW_BEFORE, fp.WINDOW_AFTER, fp.HEAD_CHARS), (32 << 20, 2 << 20, 7200.0, 2.0, 16 << 10))
        self.assertEqual((fp.ANCHOR_LEN, fp.ANCHOR_MAX, fp.COVER_MIN, fp.COVER_RATIO), (32, 16, 0.4, 2.0))

    def test_pool_is_newest_first_and_reports_what_it_left_out(self):
        self.assertEqual(fp.join_pool(['new', 'old']), ('new\0old', True))
        pool, complete = fp.join_pool(['x' * 60, 'y' * 60], limit=100)
        self.assertEqual((pool, complete), ('x' * 60, False))                            # the older text did not fit: the comparison is incomplete


class ByteCounting(unittest.TestCase):
    """The limits count what a text holds in memory, not how many characters it has (a character takes 1, 2 or 4 bytes)."""

    def test_nbytes_and_clip(self):
        self.assertEqual(fp.nbytes('a' * 100), 100)
        self.assertEqual(fp.nbytes('한' * 100) // 100, 2)                                  # the width of a character: 1, 2 or 4 bytes
        self.assertEqual(fp.nbytes('\U0001f600' * 100) // 100, 4)
        self.assertEqual(fp.clip('a' * 100, 40), 'a' * 40)
        for text, width in (('한' * 100, 2), ('\U0001f600' * 100, 4)):
            cut = fp.clip(text, 90)
            self.assertLessEqual(fp.nbytes(cut), 90)
            self.assertGreater(fp.nbytes(cut), 90 - 2 * width)                           # as long as it can be
        self.assertEqual(fp.clip('short', 40), 'short')
        self.assertEqual(fp.clip('', 0), '')
        for text in ('a' * 50 + '\U0001f600', '한' * 33 + 'z', 'é' * 70):
            for limit in (0, 1, 7, 64, 101):
                self.assertLessEqual(fp.nbytes(fp.clip(text, limit)), limit)

    def test_cache_counts_wide_characters_by_the_bytes_they_hold(self):
        """1 024 emoji are 1 024 characters and 4 096 bytes: a 1 KiB cache must not keep them."""
        c = fp.TextCache(cap=1024)
        c.put('k', '\U0001f600' * 1024)
        self.assertEqual(len(c), 0)
        c.put('k', 'a' * 200)
        self.assertEqual(len(c), 1)

    def test_cache_never_holds_more_than_its_cap(self):
        rnd = random.Random(7)
        cap = 64 << 10
        c = fp.TextCache(cap=cap)
        for i in range(4000):
            ch = rnd.choice(('a', 'é', '한', '\U0001f600'))
            c.put(('p', i), ch * rnd.randint(1, 900))
            held = sum(sys.getsizeof(v) + 192 for v in c._d.values())                   # the object itself and about 190 bytes of key and links
            self.assertLessEqual(held, cap)
            self.assertEqual(c.size, sum(fp.nbytes(v) + fp.ENTRY_BYTES for v in c._d.values()))

    def test_the_pool_of_one_owner_is_limited_by_bytes_too(self):
        pool, complete = fp.join_pool(['\U0001f600' * 600, '\U0001f600' * 600], limit=4096)
        self.assertEqual((len(pool), complete), (600, False))                          # 2 400 bytes each: the second one does not fit
        pool, complete = fp.join_pool(['a' * 600, 'b' * 600], limit=4096)
        self.assertEqual((len(pool), complete), (1201, True))

    def test_the_pool_is_limited_by_the_width_of_the_text_it_is_joined_into(self):
        """A pool is one string: a single emoji makes every ASCII character in it take 4 bytes. 20 x 100 000 ASCII characters are 2 MB, with an emoji 8 MB."""
        ascii_only = ['a' * 100000] * 20
        pool, complete = fp.join_pool(ascii_only)
        self.assertEqual((complete, fp.nbytes(pool) <= fp.OWNER_BYTES), (True, True))              # the control: all of it fits
        for at in (0, 10, 20):                                                                       # the newest, one in the middle, the oldest
            with self.subTest(emoji_at=at):
                texts = list(ascii_only)
                texts.insert(at, '\U0001f600')
                pool, complete = fp.join_pool(texts)
                self.assertLessEqual(fp.nbytes(pool), fp.OWNER_BYTES)
                self.assertFalse(complete)                                                           # text was left out: the comparison is incomplete

    def test_the_pool_of_wide_text_is_not_cut_early_and_not_over_the_limit(self):
        """For any mix of widths: the pool fits the limit when it is called complete, and what was left out would not have fitted."""
        rnd = random.Random(11)
        for trial in range(300):
            texts = [rnd.choice(('a', 'é', '한', '\U0001f600')) * rnd.randint(1, 60) for _ in range(rnd.randint(1, 9))]
            limit = rnd.randint(8, 900)
            pool, complete = fp.join_pool(texts, limit=limit)
            kept = pool.split('\0') if pool else []
            self.assertEqual(kept, texts[:len(kept)])                                                # newest first, no gaps
            self.assertLessEqual(fp.nbytes(pool), limit, (texts, limit))
            self.assertEqual(complete, len(kept) == len(texts), (texts, limit))
            if not complete:
                self.assertGreater(fp.nbytes('\0'.join(texts[:len(kept) + 1])), limit, (texts, limit))      # the next text really did not fit

    def test_a_text_cut_at_the_line_limit_makes_the_pool_incomplete(self):
        pool, complete = fp.join_pool(['new text', 'old' + fp.CUT])
        self.assertFalse(complete)
        self.assertNotIn(fp.CUT, pool)
        self.assertEqual(fp.join_pool(['new text', 'old']), ('new text\0old', True))


class NothingIsWritten(unittest.TestCase):
    def test_module_never_opens_a_file(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'board', 'fingerprint.py')) as f:
            tree = ast.parse(f.read())
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertNotIn('open', calls)                                                   # the texts live in memory only


if __name__ == '__main__':
    unittest.main()
