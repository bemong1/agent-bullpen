"""Synthetic v2 rollouts: a missing history boundary needs the parent's matching no-fork call."""
import json
import os
import shutil
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from compat import cache_globals, patched, server  # noqa: E402
from test_codex_facts import ROOT, SUB1, SUB2, T0, Rollouts, activity, line, message, root_meta, sub_meta, user_message  # noqa: E402
from board import codex_facts as F, codex_index, diag, views  # noqa: E402


def v2_meta(tid=SUB1, **kw):
    m = sub_meta(tid, multi_agent_version='v2', history_mode='paginated', **kw)
    del m['subagent_history_start_ordinal']
    return m


def spawn(fork='none', cid='spawn-1', **kw):
    args = {'task_name': 's1', 'model': 'gpt-6.1-sol', 'fork_turns': fork, 'message': 'gAAAAABsynthetic-ciphertext'}
    p = {'type': 'function_call', 'call_id': cid, 'name': 'spawn_agent', 'namespace': 'collaboration', 'arguments': json.dumps(args)}
    p.update(kw)
    return p


def communication(name, cid):
    return {'type': 'function_call', 'call_id': cid, 'name': name, 'namespace': 'collaboration',
            'arguments': json.dumps({'target': '/root/s1', 'message': 'gAAAAABsynthetic-ciphertext'})}


class V2History(Rollouts):
    def parent(self, call=None, started=None):
        self.put(ROOT, root_meta(source='vscode', originator='Codex Desktop', thread_source='user', history_mode='paginated'),
                 [(T0 + 1, 'event_msg', {'type': 'task_started'}),
                  (T0 + 2, 'response_item', spawn() if call is None else call),
                  (T0 + 3, 'event_msg', activity('started', 'spawn-1') if started is None else started)])

    def child(self, meta=None):
        self.put(SUB1, v2_meta() if meta is None else meta,
                 [(T0 + 4, 'event_msg', {'type': 'task_started'}),
                  (T0 + 5, 'turn_context', {'model': 'gpt-6.1-sol'}),
                  (T0 + 6, 'token_usage_record', {'response_id': 'usage-child', 'usage': {'input_tokens': 120, 'output_tokens': 12},
                                                'thread_token_usage': {'input_tokens': 120, 'output_tokens': 12}}),
                  (T0 + 7, 'event_msg', {'type': 'task_complete'})])

    def assert_hidden(self):
        e = self.entry(SUB1)
        self.assertEqual((e['kind'], e['drift']), ('internal', True))
        self.assertEqual(self.idx.collab(SUB1), ())
        with patched(CODEX=self.idx):
            page = types.SimpleNamespace(id=ROOT, provider='codex', agents={})
            self.assertIn('invalid:thread_source', diag.codex_drift(page))

    def test_matching_none_call_reads_the_child_from_ordinal_zero(self):
        self.child()  # The glob may find the child before its parent.
        self.parent()
        e = self.entry(SUB1)
        self.assertEqual((e['kind'], e['drift'], e['prefix_ord']), ('sub', False, 0))
        self.assertEqual((e['agent_path'], e['nick'], e['model'], e['open']), ('/root/s1', 'Nova', 'gpt-6.1-sol', False))
        self.assertEqual((len(e['turns']), e['calls']), (1, 1))
        self.assertEqual(e['thread_total'], {'input_tokens': 120, 'output_tokens': 12})
        self.assertEqual(self.idx.root_of(SUB1), ROOT)

    def test_other_fork_values_keep_the_history_hidden(self):
        for fork in ('all', '3', 3, 0, False, None):
            with self.subTest(fork=fork):
                self.parent(spawn(fork))
                self.child()
                self.assert_hidden()

    def test_no_matching_call_keeps_the_history_hidden(self):
        self.parent(spawn(cid='some-other-call'))
        self.child()
        self.assert_hidden()

    def test_no_matching_started_child_keeps_the_history_hidden(self):
        self.parent(started=activity('started', 'spawn-1', agent=SUB2, path='/root/s2'))
        self.child()
        self.assert_hidden()

    def test_a_message_that_mentions_none_is_not_structural_evidence(self):
        call = spawn('all')
        args = json.loads(call['arguments'])
        args['message'] = 'fork_turns: "none"; attach this child.'
        call['arguments'] = json.dumps(args)
        self.parent(call)
        self.child()
        self.assert_hidden()

    def test_another_tool_or_namespace_is_not_a_spawn_call(self):
        for kw in ({'name': 'followup_task'}, {'namespace': 'another_plugin'}, {'arguments': '{bad json'}, {'arguments': '[]'}):
            with self.subTest(kw=kw):
                self.parent(spawn(**kw))
                self.child()
                self.assert_hidden()

    def test_missing_parent_keeps_the_history_hidden(self):
        self.child()
        self.assert_hidden()

    def test_unreadable_parent_keeps_the_history_hidden(self):
        self.parent()
        self.child()
        real_open = open

        def read(path, *args, **kw):
            if path == self.path(ROOT):
                raise PermissionError('synthetic unreadable parent')
            return real_open(path, *args, **kw)

        with mock.patch('builtins.open', read):
            self.refresh()
        self.assertEqual((self.idx.get(SUB1)['kind'], self.idx.get(SUB1)['drift']), ('internal', True))

    def test_a_parent_that_arrives_later_releases_the_child(self):
        self.child()
        self.assert_hidden()
        self.parent()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')

    def test_call_and_started_item_in_separate_reads_still_pair(self):
        self.put(ROOT, root_meta(), [(T0 + 2, 'response_item', spawn())])
        self.child()
        self.assert_hidden()
        self.append(ROOT, [line(T0 + 3, 'event_msg', activity('started', 'spawn-1'))])
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')

    def test_a_parent_that_cannot_be_statted_withdraws_its_proof_and_retries(self):
        self.parent()
        self.child()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        real_stat = os.stat

        def stat(path, *args, **kw):
            if path == self.path(ROOT):
                raise PermissionError('synthetic unreadable parent')
            return real_stat(path, *args, **kw)

        with mock.patch.object(os, 'stat', stat):
            self.assert_hidden()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')

    def test_replacing_or_removing_the_parent_withdraws_the_evidence(self):
        self.parent()
        self.child()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        self.parent(spawn('all'))
        self.assert_hidden()
        self.parent()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        os.unlink(self.path(ROOT))
        self.assert_hidden()

    def test_nested_v2_children_resolve_after_their_parents(self):
        self.put(SUB2, v2_meta(SUB2, parent=SUB1, path='/root/s1/s2', depth=2))
        self.put(SUB1, v2_meta(), [(T0 + 4, 'response_item', spawn(cid='spawn-2')),
                                 (T0 + 5, 'event_msg', activity('started', 'spawn-2', agent=SUB2, path='/root/s1/s2'))])
        self.parent()
        self.assertEqual(self.entry(SUB2)['kind'], 'sub')
        self.assertEqual(self.idx.root_of(SUB2), ROOT)

    def test_existing_v1_boundary_still_skips_parent_history(self):
        self.parent(spawn('all'))
        self.put(SUB1, sub_meta(SUB1, start=2), [(T0 + 1, 'event_msg', {'type': 'task_started'}),
                                              (T0 + 2, 'response_item', user_message('Copied parent instruction.')),
                                              (T0 + 4, 'event_msg', {'type': 'task_started'}),
                                              (T0 + 7, 'event_msg', {'type': 'task_complete'})])
        e = self.entry(SUB1)
        self.assertEqual((e['kind'], e['prefix_ord'], len(e['turns']), e['first_user']), ('sub', 2, 1, None))

    def test_a_spawn_in_the_parents_copied_history_is_no_proof(self):
        self.put(ROOT, root_meta())
        self.put(SUB2, sub_meta(SUB2, path='/root/s2', start=2),
                 [(T0 + 2, 'response_item', spawn()),
                  (T0 + 3, 'event_msg', activity('started', 'spawn-1', path='/root/s2/s1')),
                  (T0 + 4, 'event_msg', {'type': 'task_started'})])
        self.child(v2_meta(parent=SUB2, path='/root/s2/s1', depth=2))
        e = self.entry(SUB1)
        self.assertEqual((e['kind'], e['drift']), ('internal', True))
        self.assertEqual(self.idx.collab(SUB2), ())

    def test_unknown_metadata_values_stay_hidden(self):
        for kw in ({'thread_source': 'future_source'}, {'multi_agent_version': 'v3'}, {'history_mode': 'future_history'}):
            with self.subTest(kw=kw):
                m = v2_meta()
                m.update(kw)
                self.parent()
                self.child(m)
                self.assert_hidden()

    def test_missing_v2_markers_bad_boundary_or_disagreeing_parents_stay_hidden(self):
        metas = []
        for key in ('thread_source', 'multi_agent_version', 'history_mode'):
            m = v2_meta()
            del m[key]
            metas.append(m)
        for boundary in (None, False, -1, '0'):
            m = v2_meta()
            m['subagent_history_start_ordinal'] = boundary
            metas.append(m)
        metas.append(v2_meta(parent_thread_id=SUB2))
        for m in metas:
            with self.subTest(meta=m):
                self.parent()
                self.child(m)
                e = self.entry(SUB1)
                self.assertEqual((e['kind'], e['drift']), ('internal', True))

    def test_known_metadata_values_are_accepted_and_unknown_values_drift(self):
        self.assertEqual(F.classify(root_meta(thread_source='user', history_mode='paginated'))['kind'], 'root')
        for kw in ({'thread_source': 'future_source'}, {'multi_agent_version': 'v3'}, {'history_mode': 'future_history'}):
            with self.subTest(kw=kw):
                self.assertEqual(F.classify(root_meta(**kw))['kind'], 'internal')

    def test_legacy_root_and_v1_child_keep_their_existing_kinds(self):
        self.put(ROOT, root_meta(thread_source='user', history_mode='legacy'))
        self.child(sub_meta(SUB1, start=2, history_mode='legacy', multi_agent_version='v1'))
        for tid, kind, prefix in ((ROOT, 'root', 0), (SUB1, 'sub', 2)):
            with self.subTest(tid=tid):
                e = self.entry(tid)
                self.assertEqual((e['kind'], e['prefix_ord'], e['drift']), (kind, prefix, False))

    def test_reused_call_ids_never_authorize_copied_child_history(self):
        conflicts = [communication('followup_task', 'spawn-1'), communication('send_message', 'spawn-1'),
                     spawn(arguments='{bad json'), spawn(), spawn('all'),
                     {'type': 'custom_tool_call', 'call_id': 'spawn-1', 'name': 'exec', 'input': 'text(1)'}]
        for second in conflicts:
            for after_started in (False, True):
                with self.subTest(second=second, after_started=after_started):
                    rest = [(T0 + 2, 'response_item', spawn())]
                    if after_started:
                        rest.append((T0 + 3, 'event_msg', activity('started', 'spawn-1')))
                    rest.append((T0 + 4, 'response_item', second))
                    if not after_started:
                        rest.append((T0 + 5, 'event_msg', activity('started', 'spawn-1')))
                    self.put(ROOT, root_meta(), rest)
                    self.put(SUB1, v2_meta(), [(T0 + 1, 'event_msg', {'type': 'task_started'}),
                                             (T0 + 2, 'response_item', user_message('Synthetic copied parent instruction.')),
                                             (T0 + 7, 'event_msg', {'type': 'task_started'})])
                    self.assert_hidden()
                    self.assertEqual(self.idx.get(SUB1)['turns'], [])
                    self.assertIsNone(self.idx.get(SUB1)['first_user'])

    def test_later_call_collision_withdraws_an_already_confirmed_child(self):
        self.parent()
        self.child()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        self.append(ROOT, [line(T0 + 8, 'response_item', communication('followup_task', 'spawn-1'))])
        self.assert_hidden()

    def test_a_prior_call_with_the_same_id_also_invalidates_a_spawn(self):
        for prior in (communication('followup_task', 'spawn-1'), spawn(arguments='{bad json'), spawn('all')):
            with self.subTest(prior=prior):
                self.put(ROOT, root_meta(), [(T0 + 1, 'response_item', prior), (T0 + 2, 'response_item', spawn()),
                                            (T0 + 3, 'event_msg', activity('started', 'spawn-1'))])
                self.child()
                self.assert_hidden()

    def test_the_separate_proof_limit_retains_old_facts_and_refuses_untracked_ids(self):
        with mock.patch.object(F, 'CX_SPAWNS_KEEP', 2):
            self.parent()
            self.child()
            self.assertEqual(self.entry(SUB1)['kind'], 'sub')
            self.append(ROOT, [line(T0 + 8, 'response_item', communication('send_message', 'other-1')),
                               line(T0 + 9, 'response_item', communication('followup_task', 'untracked'))])
            self.assertEqual(self.entry(SUB1)['kind'], 'sub')
            self.append(ROOT, [line(T0 + 10, 'response_item', spawn(cid='untracked')),
                               line(T0 + 11, 'event_msg', activity('started', 'untracked', agent=SUB2, path='/root/s2'))])
            self.put(SUB2, v2_meta(SUB2, path='/root/s2'))
            self.assertEqual(self.entry(SUB2)['kind'], 'internal')
            self.assertEqual(self.idx.get(SUB1)['kind'], 'sub')
            self.append(ROOT, [line(T0 + 12, 'response_item', spawn())])
            self.assert_hidden()

    def test_conflicting_started_items_withdraw_the_proof(self):
        for claim in (activity('started', 'spawn-1', agent=SUB2, path='/root/s2'),
                      activity('started', 'spawn-1', path='/root/other')):
            with self.subTest(claim=claim):
                self.parent()
                self.child()
                self.assertEqual(self.entry(SUB1)['kind'], 'sub')
                self.append(ROOT, [line(T0 + 8, 'event_msg', claim)])
                self.assert_hidden()

    def test_another_parents_call_id_neither_confirms_nor_invalidates_the_child(self):
        self.parent()
        self.child()
        self.put(SUB2, root_meta(SUB2), [(T0 + 2, 'response_item', spawn('all')),
                                       (T0 + 3, 'event_msg', activity('started', 'spawn-1'))])
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        self.parent(spawn('all'))
        self.put(SUB2, root_meta(SUB2), [(T0 + 2, 'response_item', spawn()),
                                       (T0 + 3, 'event_msg', activity('started', 'spawn-1'))])
        self.assert_hidden()

    def test_confirmed_proof_outlives_the_conversation_card_limit(self):
        self.parent()
        self.child()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        generation = self.idx.get(ROOT)['gen']
        self.append(ROOT, [line(T0 + 8 + i, 'response_item', message('/root', '/root/s1', 'Synthetic message.', mid='msg-%d' % i))
                           for i in range(F.CX_COLLAB_KEEP + 1)])
        e = self.entry(SUB1)
        self.assertEqual((e['kind'], e['drift']), ('sub', False))
        self.assertEqual(self.idx.get(ROOT)['gen'], generation)
        self.assertEqual(len(self.idx.collab(ROOT)), F.CX_COLLAB_KEEP)
        self.assertFalse(any(c['kind'] == 'started' for c in self.idx.collab(ROOT)))
        self.append(ROOT, [line(T0 + 3000, 'response_item', spawn('all'))])
        self.assert_hidden()

    def test_tail_rereads_do_not_lose_or_duplicate_spawn_evidence(self):
        self.parent()
        self.child()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        with mock.patch.object(codex_index, 'CX_BIG', 512), mock.patch.object(codex_index, 'CX_TAIL', 4096):
            for i in range(2):
                self.append(ROOT, [line(T0 + 8 + i, 'response_item', message('/root', '/root/s1', 'Synthetic message.', mid='msg-%d' % i))])
                self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        with mock.patch.object(codex_index, 'CX_BIG', 512), mock.patch.object(codex_index, 'CX_TAIL', 1024):
            self.append(ROOT, [line(T0 + 10 + i, 'response_item', message('/root', '/root/s1', 'Synthetic message.', mid='tail-%d' % i))
                               for i in range(20)])
            self.assertEqual(self.entry(SUB1)['kind'], 'sub')

    def large_parent(self):
        """Keep the real 64 MiB threshold: the spawn is outside the tail that the index reads."""
        self.parent()
        self.child()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        with open(self.path(ROOT), 'ab') as f:
            f.write(b'{"timestamp":"2026-10-04T00:00:00Z","ordinal":4,"type":"response_item","payload":{"type":"message","role":"assistant","content":"')
            for _ in range(65):
                f.write(b'x' * (1 << 20))
            f.write(b'"}}\n')
        self.append(ROOT, [line(T0 + 90, 'token_usage_record', {'thread_token_usage': {'input_tokens': 1, 'output_tokens': 1}})])
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        self.assertTrue(self.idx.get(ROOT)['partial'])
        self.assertIsNone(self.idx.by_id[ROOT]['_anchor'])

    def overwrite_parent_line(self, ordinal, change):
        """An equal-length rewrite on the same inode, with the cached size and mtime unchanged."""
        path = self.path(ROOT)
        before = os.stat(path)
        with open(path, 'r+b') as f:
            for _ in range(ordinal):
                f.readline()
            off = f.tell()
            old = f.readline()
            d = json.loads(old)
            change(d['payload'])
            new = (json.dumps(d, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
            self.assertEqual(len(new), len(old))
            f.seek(off)
            f.write(new)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(os.stat(path).st_ino, before.st_ino)

    def test_large_parent_replacement_with_the_same_meta_withdraws_old_proof(self):
        self.large_parent()
        path = self.path(ROOT)
        before = os.stat(path)
        replacement = path + '.replacement'
        with open(path, 'rb') as src, open(replacement, 'wb') as dst:
            for _ in range(4):
                d = json.loads(src.readline())
                p = d['payload']
                if p.get('name') == 'spawn_agent':
                    args = json.loads(p['arguments'])
                    args['fork_turns'] = 'all'
                    args['extra'] = 'Synthetic padding makes the replacement larger.'
                    p['arguments'] = json.dumps(args)
                dst.write((json.dumps(d, separators=(',', ':'), ensure_ascii=False) + '\n').encode())
            shutil.copyfileobj(src, dst, length=1 << 20)
        os.replace(replacement, path)
        self.assertNotEqual(os.stat(path).st_ino, before.st_ino)
        self.put(SUB1, v2_meta(), [(T0 + 1, 'event_msg', {'type': 'task_started'}),
                                 (T0 + 2, 'response_item', user_message('Synthetic copied parent instruction.')),
                                 (T0 + 7, 'event_msg', {'type': 'task_started'})])
        self.assert_hidden()
        self.assertIsNone(self.idx.get(SUB1)['first_user'])
        self.assertEqual(self.idx.get(SUB1)['turns'], [])

    def test_large_parent_spawn_rewrite_on_the_same_inode_withdraws_old_proof(self):
        self.large_parent()

        def change(p):
            args = json.loads(p['arguments'])
            args['fork_turns'] = 'all'
            args['message'] += 'x'
            p['arguments'] = json.dumps(args)

        self.overwrite_parent_line(2, change)
        self.assert_hidden()

    def test_changed_started_line_cannot_keep_the_original_child_claim(self):
        self.large_parent()

        def change(p):
            p['item']['agent_thread_id'] = SUB2
            p['item']['agent_path'] = '/root/s2'

        self.overwrite_parent_line(3, change)
        self.assert_hidden()

    def test_unreadable_cached_spawn_proof_hides_then_recovers_without_parent_changes(self):
        self.parent()
        self.child()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        generation = self.idx.get(ROOT)['gen']
        real_open = open

        def read(path, *args, **kw):
            if path == self.path(ROOT):
                raise PermissionError('synthetic proof cannot be verified')
            return real_open(path, *args, **kw)

        with mock.patch('builtins.open', read):
            self.assert_hidden()
        for _ in range(3):
            e = self.entry(SUB1)
            self.assertEqual((e['kind'], e['drift']), ('sub', False))
            self.assertEqual(e['thread_total'], {'input_tokens': 120, 'output_tokens': 12})
        self.assertEqual(self.idx.get(ROOT)['gen'], generation)

    def test_one_verification_failure_hides_until_the_next_refresh(self):
        self.parent()
        self.child()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        real_open = open
        failed = False

        def read(path, *args, **kw):
            nonlocal failed
            if path == self.path(ROOT) and not failed:
                failed = True
                raise PermissionError('synthetic one-time verification failure')
            return real_open(path, *args, **kw)

        with mock.patch('builtins.open', read):
            self.assert_hidden()
            self.assertTrue(failed)
            e = self.entry(SUB1)
            self.assertEqual((e['kind'], e['drift']), ('sub', False))

    def test_parent_proof_changed_during_read_failure_stays_hidden_after_recovery(self):
        self.parent()
        self.child()
        self.assertEqual(self.entry(SUB1)['kind'], 'sub')
        generation = self.idx.get(ROOT)['gen']
        real_open = open

        def read(path, *args, **kw):
            if path == self.path(ROOT) and args and args[0] == 'rb':
                raise PermissionError('synthetic proof cannot be verified')
            return real_open(path, *args, **kw)

        def change(p):
            args = json.loads(p['arguments'])
            args['fork_turns'] = 'all'
            args['message'] += 'x'
            p['arguments'] = json.dumps(args)

        with mock.patch('builtins.open', read):
            self.assert_hidden()
            self.overwrite_parent_line(2, change)
            self.put(SUB1, v2_meta(), [(T0 + 1, 'event_msg', {'type': 'task_started'}),
                                     (T0 + 2, 'response_item', user_message('Synthetic copied parent instruction.')),
                                     (T0 + 7, 'event_msg', {'type': 'task_started'})])
            self.assert_hidden()
        for _ in range(3):
            self.assert_hidden()
            self.assertIsNone(self.idx.get(SUB1)['first_user'])
            self.assertEqual(self.idx.get(SUB1)['turns'], [])
        self.assertEqual(self.idx.get(ROOT)['gen'], generation)

    def test_a_byte_identical_large_replacement_cannot_reuse_another_inodes_proof(self):
        self.large_parent()
        path = self.path(ROOT)
        before = os.stat(path)
        replacement = path + '.replacement'
        shutil.copyfile(path, replacement)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)
        self.assert_hidden()

    def test_v2_name_status_tokens_and_conversation_cards(self):
        self.parent()
        self.child()
        first = message('/root', '/root/s1', 'An encrypted instruction note.', encrypted=True, mid='msg-spawn')
        follow = message('/root', '/root/s1', 'An encrypted follow-up note.', encrypted=True, mid='msg-followup')
        self.append(SUB1, [line(T0 + 4.1, 'inter_agent_communication_metadata', {'trigger_turn': 'turn-child'}),
                           line(T0 + 4.2, 'response_item', first), line(T0 + 9, 'response_item', follow)])
        self.append(ROOT, [line(T0 + 7.1, 'event_msg', activity('completed', 'subagent-completed-turn-child')),
                           line(T0 + 7.2, 'inter_agent_communication_metadata', {'trigger_turn': 'turn-parent'}),
                           line(T0 + 7.3, 'response_item', message('/root/s1', '/root', 'Synthetic report.', mid='msg-report')),
                           line(T0 + 8, 'response_item', communication('send_message', 'send-1')),
                           line(T0 + 9, 'response_item', follow),
                           line(T0 + 10, 'response_item', communication('followup_task', 'follow-1')),
                           line(T0 + 10.1, 'event_msg', activity('started', 'follow-1')),
                           line(T0 + 11, 'response_item', message('/root', '/root/s1', 'Resume note.', encrypted=True, mid='msg-resume'))])
        links = server.LinkIndex()
        with patched(CODEX=self.idx, LINKS=links, HOME=self.tmp.name, CLAUDE_HOME=os.path.join(self.tmp.name, 'claude'),
                     PROJECTS=os.path.join(self.tmp.name, 'claude', 'projects'), **cache_globals(self.tmp.name)):
            self.refresh()
            links.scan()
            page = server.CodexSession(self.idx.get(ROOT))
            page.poll()
            page.poll()
            state = views.state(page)
            child = next(a for a in state['agents'] if a['id'] == SUB1)
            self.assertEqual((child['tag'], child['title'], child['status']), ('s1', 'Nova', 'done'))
            self.assertEqual((child['tokens']['input'], child['tokens']['output'], child['tokens']['calls']), (120, 12, 1))
            self.assertEqual(state['orch']['tokens']['calls'], 0)
            self.assertIsNone(page.agents[SUB1].spawn_prompt)
            cards = [e for e in page.feed if e.get('agent') == SUB1 and e['kind'] in ('spawn', 'handback', 'orch_msg', 'agent_msg')]
            self.assertEqual([e['kind'] for e in cards], ['spawn', 'handback', 'orch_msg', 'orch_msg'])
            self.assertEqual([e['text_i18n']['key'] for e in cards if e['kind'] != 'handback'], ['event.encrypted.text'] * 3)
            self.assertEqual(next(e['text'] for e in cards if e['kind'] == 'handback'), 'Synthetic report.')
            self.assertEqual(diag.codex_drift(page), set())


if __name__ == '__main__':
    unittest.main()
