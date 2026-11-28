import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from quickpool import session_cache
from quickpool.native_upstream import NativeSession, Upstream, UpstreamError
from quickpool.session_cache import SessionCache, hash_message


def context(*messages):
    return {'messages': [{'role': role, 'content': text} for role, text in messages]}


FIRST = context(('user', 'first question'))


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(session_cache, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def test_match_exact_prefix():
    cache = SessionCache()
    cache.save('a', 'thread', FIRST, 'smart', 'off')
    new = context(('user', 'first question'), ('user', 'next question'))
    entry, offset = cache.match(new, 'smart', 'off')
    assert (entry.account_id, entry.thread_id, offset) == ('a', 'thread', 1)
    assert entry.msg_hashes == (hash_message(FIRST['messages'][0]),)
    assert entry.turns == 1


@pytest.mark.parametrize('role,offset', [('user', 2), ('tool', 2), (None, 1)])
def test_match_prefix_with_assistant(role, offset):
    cache = SessionCache()
    cache.save('a', 'thread', FIRST, 'smart', 'off',
               assistant_reply={'role': 'assistant', 'content': 'server reply'})
    tail = [(role, 'next message')] if role else []
    new = context(('user', 'first question'), ('assistant', 'client reply'), *tail)
    assert cache.match(new, 'smart', 'off')[1] == offset


@pytest.mark.parametrize('new', [context(), FIRST, context(('user', 'other'), ('user', 'next'))],
                         ids=['shorter', 'equal', 'different-prefix'])
def test_no_match_without_a_longer_identical_prefix(new):
    cache = SessionCache()
    cache.save('a', 'thread', FIRST, 'smart', 'off')
    assert cache.match(new, 'smart', 'off') is None


@pytest.mark.parametrize('key,value', [
    ('agent_mode', 'fast'), ('thinking_effort', 'high'), ('tools', [{'name': 'new'}]),
    ('response_format', {'type': 'json_object'}), ('tool_choice', 'required'), ('parallel_tool_calls', False),
], ids=['mode', 'effort', 'tools', 'format', 'choice', 'parallel'])
def test_config_changes_prevent_reuse(key, value):
    cache = SessionCache()
    config = {'agent_mode': 'smart', 'thinking_effort': 'off'}
    cache.save('a', 'thread', FIRST, **config)
    new = context(('user', 'first question'), ('user', 'next question'))
    (config if key in config else new)[key] = value
    assert cache.match(new, **config) is None


def test_hashes_ignore_json_object_key_order():
    cache = SessionCache()
    first = {'messages': [{'role': 'user', 'content': '你好'}], 'tools': [{'name': '查找', 'args': {}}]}
    cache.save('a', 'thread', first, 'smart', 'off')
    new = {'messages': [{'content': '你好', 'role': 'user'}, {'role': 'user', 'content': '继续'}],
           'tools': [{'args': {}, 'name': '查找'}]}
    assert cache.match(new, 'smart', 'off')[0].thread_id == 'thread'


def test_expiration_filters_matches_and_stats(clock):
    cache = SessionCache(ttl=5.0)
    cache.save('a', 'thread', FIRST, 'smart', 'off')
    new = context(('user', 'first question'), ('user', 'next question'))
    clock.now += 4
    assert cache.match(new, 'smart', 'off') is not None
    clock.now += 5.01
    assert cache.match(new, 'smart', 'off') is None
    assert cache.stats()['active_sessions'] == 0


def test_lru_evicts_oldest_last_used_and_save_updates_thread(clock):
    cache = SessionCache(max_entries=2)
    cache.save('a', 'first', FIRST, 'smart', 'off')
    clock.now += 1
    cache.save('a', 'second', FIRST, 'smart', 'off')
    clock.now += 1
    updated = context(('user', 'first question'), ('user', 'updated prefix'))
    cache.save('a', 'first', updated, 'smart', 'off')
    clock.now += 1
    cache.save('b', 'third', FIRST, 'smart', 'off')
    new = context(('user', 'first question'), ('user', 'updated prefix'), ('user', 'next'))
    entry, offset = cache.match(new, 'smart', 'off', {'a'})
    assert (entry.thread_id, entry.turns, entry.last_used, offset) == ('first', 2, clock.now, 2)
    assert cache.match(updated, 'smart', 'off', {'a'}) is None
    assert cache.stats()['active_sessions'] == 2


def test_available_accounts_filter_matches():
    cache = SessionCache()
    cache.save('a', 'long', context(('user', 'first question'), ('user', 'mid')), 'smart', 'off')
    cache.save('b', 'short', FIRST, 'smart', 'off')
    new = context(('user', 'first question'), ('user', 'mid'), ('user', 'next'))
    assert cache.match(new, 'smart', 'off', {'b'})[0].thread_id == 'short'
    assert cache.match(new, 'smart', 'off', set()) is None


def test_longest_prefix_then_most_recent(clock):
    cache = SessionCache()
    long = context(('user', 'first question'), ('user', 'mid'))
    cache.save('a', 'old-long', long, 'smart', 'off')
    clock.now += 1
    cache.save('a', 'new-long', long, 'smart', 'off')
    clock.now += 1
    cache.save('a', 'newest-short', FIRST, 'smart', 'off')
    new = context(('user', 'first question'), ('user', 'mid'), ('user', 'next'))
    assert cache.match(new, 'smart', 'off')[0].thread_id == 'new-long'


def test_invalidate_thread_and_account():
    cache = SessionCache()
    for account, thread in [('a', 'a1'), ('a', 'a2'), ('a', 'a3'), ('b', 'b1')]:
        cache.save(account, thread, FIRST, 'smart', 'off')
    cache.invalidate('a1')
    assert cache.stats()['active_sessions'] == 3
    cache.invalidate_account('a')
    new = context(('user', 'first question'), ('user', 'next'))
    assert cache.match(new, 'smart', 'off')[0].thread_id == 'b1'
    assert cache.stats()['active_sessions'] == 1


def test_stats_count_hits_and_lookups():
    cache = SessionCache()
    assert cache.stats() == {'hits': 0, 'misses': 0, 'hit_rate': 0.0, 'active_sessions': 0,
                             'saved_tokens': 0, 'evictions': 0}
    assert cache.match(FIRST, 'smart', 'off') is None
    cache.save('a', 'thread', FIRST, 'smart', 'off')
    assert cache.match(context(('user', 'first question'), ('user', 'next')), 'smart', 'off')
    cache.record_hit()
    cache.record_miss()
    assert cache.stats() == {'hits': 2, 'misses': 2, 'hit_rate': 0.5, 'active_sessions': 1,
                             'saved_tokens': 0, 'evictions': 0}


def test_match_records_misses_without_entries(clock):
    cache = SessionCache()
    assert cache.match(FIRST, 'smart', 'off') is None
    stats = cache.stats()
    assert (stats['hits'], stats['misses'], stats['hit_rate']) == (0, 1, 0.0)


def test_hit_refreshes_ttl(clock):
    cache = SessionCache(ttl=100)
    cache.save('a', 'thread', FIRST, 'smart', 'off')
    clock.now += 90
    assert cache.match(context(('user', 'first question'), ('user', 'next')), 'smart', 'off') is not None
    clock.now += 90
    assert cache.match(context(('user', 'first question'), ('user', 'again')), 'smart', 'off') is not None
    assert cache.stats()['active_sessions'] == 1


def test_per_account_limit_evicts_oldest(clock):
    cache = SessionCache(max_entries=10, max_per_account=2)
    for index in range(3):
        cache.save('a', f'a{index}', FIRST, 'smart', 'off')
        clock.now += 1
    cache.save('b', 'b0', FIRST, 'smart', 'off')
    ids = [entry['thread_id'] for entry in cache.list_sessions()]
    assert 'a0' not in ids and 'a2' in ids and 'b0' in ids
    assert cache.stats()['evictions'] == 1


def test_saved_tokens_are_tracked():
    cache = SessionCache()
    cache.record_saved(120)
    cache.record_saved(0)
    cache.record_saved(-5)
    cache.record_saved('30')
    assert cache.stats()['saved_tokens'] == 150


@pytest.fixture
def conversation():
    return {
        'messages': [
            {'role': 'system', 'content': 'OLD_SYSTEM_RULE'},
            {'role': 'user', 'content': 'OLD_USER_QUESTION [图片附件 1]'},
            {'role': 'assistant', 'content': 'OLD_ASSISTANT_REPLY'},
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'FIRST_TOOL_RESULT'},
            {'role': 'assistant', 'tool_calls': [{'id': 'call_2', 'type': 'function',
                'function': {'name': 'check', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call_2', 'content': 'SECOND_TOOL_RESULT'},
            {'role': 'system', 'content': 'DELTA_SYSTEM_RULE'},
            {'role': 'user', 'content': 'NEW_USER_QUESTION'},
        ],
        'tools': [{'type': 'function', 'function': {'name': 'TOOL_SCHEMA_MARKER'}}],
    }


@pytest.fixture
def native():
    session = NativeSession('social', 'FIXTURE', 'fixture.quick.aws.com', '', '', 'outpost')
    connections, outcomes, uploads = [], [], []

    class Socket:
        def __init__(self, outcome):
            self.sent, self.responses = [], []
            self.outcome = outcome

        async def send(self, raw):
            event = json.loads(base64.b64decode(json.loads(raw)['payload']['payload']))
            self.sent.append(event)
            if event['type'] != 'user_message':
                return
            thread = event['payload']['thread_id']
            self.wire_bytes = len(raw)
            if self.outcome != 'reject':
                self.responses.append({'type': 'text_delta', 'payload': {'delta': 'OK'}})
            self.responses.append({'type': 'message_complete', 'payload': {}} if self.outcome == 'ok'
                                  else {'type': 'error', 'code': 'conversation_format'})
            for frame in self.responses:
                frame.update(thread_id=thread, message_id='reply')

        async def recv(self):
            return json.dumps(self.responses.pop(0))

    @asynccontextmanager
    async def connect(*args, **kwargs):
        socket = Socket(outcomes.pop(0) if outcomes else 'ok')
        connections.append(socket)
        yield socket

    async def upload(session, files, thread_id):
        uploads.append((thread_id, files))
        return [{'type': f['type'], 'name': f['name'], 'size': len(f['data']), 'mimeType': f['mimeType'],
                 'path': '/workspace/' + f['name'], 'attachmentId': 'fixture'} for f in files]

    cache = SessionCache()
    auth = SimpleNamespace(credentials=AsyncMock(), _audit=Mock())
    upstream = Upstream(None, auth, connect=connect, session_cache=cache)
    upstream.session = AsyncMock(return_value=session)
    upstream.upload_files = upload
    return SimpleNamespace(upstream=upstream, cache=cache, session=session,
                           connections=connections, outcomes=outcomes, uploads=uploads)


@pytest.mark.parametrize('new_image', [False, True])
async def test_reused_stream_sends_only_delta_and_uploads_only_with_new_image(native, conversation, new_image):
    images = [(b'old', 'image/png')]
    if new_image:
        conversation['messages'][-1]['content'] += ' [图片附件 2]'
        images.append((b'new', 'image/png'))
    result = [part async for part in native.upstream._stream(native.session, 'a', [], context=conversation,
                images=images, reuse_thread_id='reused', delta_offset=3)]
    assert result == ['OK']
    assert [f['type'] for f in native.connections[0].sent] == [
        'session_connect', 'federate_status', 'session_connect', 'user_message']
    conversation_connect, message = native.connections[0].sent[2:]
    assert conversation_connect['payload']['session_id'] == message['payload']['thread_id'] == 'reused'
    prompt = message['payload']['text']
    assert '<tool_call>' in prompt and 'follow-up turn' in prompt
    for item in conversation['messages'][3:]:
        if item['role'] != 'system':
            assert f"[{item['role']}]\n" + json.dumps(item, ensure_ascii=False, separators=(',', ':')) in prompt
    for omitted in ('OLD_SYSTEM_RULE', 'OLD_USER_QUESTION', 'OLD_ASSISTANT_REPLY', 'DELTA_SYSTEM_RULE',
                    'TOOL_SCHEMA_MARKER'):
        assert omitted not in prompt
    assert len(native.uploads) == int(new_image)
    if new_image:
        thread, files = native.uploads[0]
        assert thread == 'reused' and [f['data'] for f in files] == [b'old', b'new']


async def test_large_delta_offloads_only_incremental_messages(native, conversation):
    conversation['messages'][-1]['content'] += '中文' * 9000
    result = [part async for part in native.upstream._stream(native.session, 'a', [], context=conversation,
                reuse_thread_id='reused', delta_offset=3)]
    assert result == ['OK']
    [(thread, [document])] = native.uploads
    assert thread == 'reused' and document['name'] == 'conversation-context.txt'
    doc_txt = document['data'].decode('utf-8')
    assert 'NEW_USER_QUESTION' in doc_txt and 'SECOND_TOOL_RESULT' in doc_txt
    assert 'OLD_USER_QUESTION' not in doc_txt
    [socket] = native.connections
    payload = socket.sent[-1]['payload']
    assert socket.wire_bytes <= 60_000
    assert payload['attachments'][0]['name'] == 'conversation-context.txt'
    assert 'NEW_USER_QUESTION' in payload['text'] and 'SECOND_TOOL_RESULT' in payload['text']
    for omitted in ('OLD_SYSTEM_RULE', 'OLD_USER_QUESTION', 'OLD_ASSISTANT_REPLY', 'DELTA_SYSTEM_RULE',
                    'TOOL_SCHEMA_MARKER'):
        assert omitted not in payload['text']


@pytest.mark.parametrize('refresh_first', [False, True])
async def test_reuse_error_before_first_output_invalidates_and_resends_full_prompt(native, conversation, refresh_first):
    native.outcomes.append('reject')
    if refresh_first:
        native.upstream.session.side_effect = [UpstreamError(401, 'expired'), native.session, native.session]
    native.cache.save('a', 'reused', {**conversation, 'messages': conversation['messages'][:2]}, 'smart', 'off')
    full_prompt = json.dumps(conversation, ensure_ascii=False)
    result = [part async for part in native.upstream.stream({'id': 'a'}, [{'role': 'user', 'content': full_prompt}],
                context=conversation, reuse_thread_id='reused', delta_offset=3)]
    assert result == ['OK']
    assert native.cache.stats()['active_sessions'] == 0
    assert native.upstream.auth.credentials.await_count == int(refresh_first)
    reused, fresh = native.connections
    assert reused.sent[-1]['payload']['thread_id'] == 'reused'
    assert 'OLD_USER_QUESTION' not in reused.sent[-1]['payload']['text']
    assert [f['type'] for f in fresh.sent] == [
        'session_connect', 'federate_status', 'session_create', 'session_connect', 'user_message']
    thread = fresh.sent[-1]['payload']['thread_id']
    assert thread != 'reused'
    assert fresh.sent[2]['payload']['session_id'] == fresh.sent[3]['payload']['session_id'] == thread
    assert 'OLD_USER_QUESTION' in fresh.sent[-1]['payload']['text']
    assert 'OLD_SYSTEM_RULE' in (fresh.sent[-1]['payload']['custom_prompt'] or '')


@pytest.mark.parametrize('outcome,attempts', [('reject', 2), ('partial', 1)])
async def test_reuse_retries_at_most_once_and_never_after_output(native, conversation, outcome, attempts):
    native.outcomes.extend([outcome, 'reject'])
    native.cache.save('a', 'reused', {**conversation, 'messages': conversation['messages'][:2]}, 'smart', 'off')
    messages = [{'role': 'user', 'content': 'FULL'}]
    iterator = native.upstream.stream({'id': 'a'}, messages, context=conversation,
                                      reuse_thread_id='reused', delta_offset=3)
    if outcome == 'partial':
        assert await anext(iterator) == 'OK'
    with pytest.raises(UpstreamError, match='conversation_format'):
        await anext(iterator)
    assert len(native.connections) == attempts
    assert native.cache.stats()['active_sessions'] == int(outcome == 'partial')


@pytest.mark.parametrize('offset', [0, -1, None], ids=['zero', 'negative', 'at-end'])
async def test_invalid_delta_offset_uses_existing_fresh_path(native, conversation, offset):
    native.outcomes.append('reject')
    messages = [{'role': 'user', 'content': 'FULL'}]
    if offset is None:
        offset = len(conversation['messages'])
    with pytest.raises(UpstreamError, match='conversation_format'):
        await anext(native.upstream.stream({'id': 'a'}, messages, context=conversation,
                    reuse_thread_id='reused', delta_offset=offset))
    [socket] = native.connections
    assert socket.sent[2]['type'] == 'session_create'
    assert socket.sent[-1]['payload']['thread_id'] != 'reused'
    assert 'NEW_USER_QUESTION' in socket.sent[-1]['payload']['text']


def test_sqlite_persistence_survives_restart(tmp_path):
    from quickpool.store import Store

    store1 = Store(tmp_path)
    cache1 = SessionCache(ttl=1800.0, store=store1)
    cache1.save('acc-1', 'thread-persist', FIRST, 'smart', 'off')
    cache1.save('acc-2', 'thread-drop', FIRST, 'smart', 'off')
    cache1.invalidate('thread-drop')
    store1.close()

    store2 = Store(tmp_path)
    cache2 = SessionCache(ttl=1800.0, store=store2)
    assert cache2.stats()['active_sessions'] == 1
    follow_up = context(('user', 'first question'), ('assistant', 'ok'), ('user', 'second'))
    entry, offset = cache2.match(follow_up, 'smart', 'off')
    assert (entry.account_id, entry.thread_id, offset) == ('acc-1', 'thread-persist', 2)
    cache2.invalidate_account('acc-1')
    store2.close()

    store3 = Store(tmp_path)
    cache3 = SessionCache(ttl=1800.0, store=store3)
    assert cache3.stats()['active_sessions'] == 0
    store3.close()


def test_sqlite_persistence_skips_expired_rows(tmp_path):
    from quickpool.store import Store

    store1 = Store(tmp_path)
    store1.save_session('expired-thread', 'acc-1', 'cfg', ('h1',), 1, ttl=0.0)
    store1.close()

    store2 = Store(tmp_path)
    cache2 = SessionCache(ttl=1800.0, store=store2)
    assert cache2.stats()['active_sessions'] == 0
    store2.close()
