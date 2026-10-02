"""The link scan opens only regular record files: a FIFO (or a device, or a link to one) that carries a record's name is passed over, never waited on.

    python3 -m unittest discover -s tests
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_stage2 import CliFixture, T0, child_lines  # noqa: E402

SID = '22222222-2222-4222-8222-222222222222'
FIFO_SID = '33333333-3333-4333-8333-333333333333'
NODE = 'a1b2c3d4e5f6a7b8c'


class RecordFiles(CliFixture):
    def fifo(self, path):
        os.mkfifo(path)
        self.addCleanup(self.release, path)
        return path

    @staticmethod
    def release(path):
        try:                                                      # O_RDWR does not block on a FIFO: a thread stuck in open() gets its writer and ends
            os.close(os.open(path, os.O_RDWR | os.O_NONBLOCK))
        except OSError:
            pass

    def scan_within(self, seconds=10):
        t = threading.Thread(target=self.links.scan, daemon=True)
        t.start()
        t.join(seconds)
        return not t.is_alive()

    def test_a_fifo_with_a_record_name_does_not_stop_the_scan(self):
        self.write(SID, child_lines(T0))
        self.fifo(os.path.join(self.proj, FIFO_SID + '.jsonl'))
        self.assertTrue(self.scan_within(), 'a FIFO named like a record blocked the scan')
        self.assertTrue(self.links.ready.is_set())
        names = {os.path.basename(p) for p in self.links.files}
        self.assertIn(SID + '.jsonl', names)
        self.assertNotIn(FIFO_SID + '.jsonl', names)

    def test_a_link_to_a_fifo_does_not_stop_the_scan(self):
        self.write(SID, child_lines(T0))
        target = self.fifo(os.path.join(os.path.dirname(self.proj), 'pipe'))
        os.symlink(target, os.path.join(self.proj, FIFO_SID + '.jsonl'))
        self.assertTrue(self.scan_within(), 'a link to a FIFO blocked the scan')
        self.assertTrue(self.links.ready.is_set())

    def test_a_fifo_where_a_sub_agent_record_belongs_does_not_stop_the_scan(self):
        self.write(SID, child_lines(T0))
        sub = os.path.join(self.proj, SID, 'subagents')
        os.makedirs(sub)
        self.fifo(os.path.join(sub, 'agent-%s.jsonl' % NODE))
        self.assertTrue(self.scan_within(), 'a FIFO named like a sub-agent record blocked the scan')
        self.assertTrue(self.links.ready.is_set())
        self.assertEqual(self.links.sub, {})

    def test_a_link_to_a_regular_record_is_still_read(self):
        real = self.write(SID, child_lines(T0, cwd='/seen'))
        other = os.path.join(os.path.dirname(self.proj), 'elsewhere.jsonl')
        os.rename(real, other)
        os.symlink(other, real)
        self.assertTrue(self.scan_within())
        self.assertEqual(self.links._head(real)[1], '/seen')


if __name__ == '__main__':
    unittest.main()
