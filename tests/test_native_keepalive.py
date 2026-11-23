"""AIDP sends application keepalives but doesn't answer WebSocket Ping frames."""
import asyncio
import base64
import json

import cbor2
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.frames import OP_PONG

from quickpool.native_protocol import EventStreamDecoder
from quickpool.native_upstream import NativeSession, Upstream


@pytest.mark.parametrize('provider', ['social', 'gateway'])
async def test_long_turn_survives_native_keepalives(provider):
    pongs = []

    class QuickConnection(ServerConnection):
        def __init__(self, protocol, server, **kwargs):
            super().__init__(protocol, server, **kwargs)
            original = protocol.send_frame

            def send_frame(frame):
                if frame.opcode == OP_PONG:
                    pongs.append(frame)
                    if provider == 'social':
                        return
                original(frame)

            protocol.send_frame = send_frame

    async def handle(socket):
        decoder = EventStreamDecoder()
        try:
            async for raw in socket:
                if provider == 'social':
                    event = json.loads(base64.b64decode(json.loads(raw)['payload']['payload']))
                else:
                    event = json.loads(cbor2.loads(decoder.feed(raw)[0].payload)['payload'])
                if event['type'] != 'user_message':
                    continue
                thread = event['payload']['thread_id']
                # Keep receiving application traffic across several Ping deadlines.
                for _ in range(15):
                    await socket.send(json.dumps({'type': 'keepalive', 'payload': {}}))
                    await asyncio.sleep(.01)
                for kind, payload in [('text_delta', {'delta': 'LONG_REPLY_OK'}),
                                      ('message_complete', {})]:
                    await socket.send(json.dumps({'type': kind, 'thread_id': thread,
                                                 'message_id': 'reply', 'payload': payload}))
        except ConnectionClosed:
            pass

    async with serve(handle, '127.0.0.1', 0, create_connection=QuickConnection, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]

        def local_connect(url, **kwargs):
            # Scale production's timing down; retain whether Ping is enabled.
            if kwargs['ping_interval'] is not None:
                kwargs['ping_interval'] = .02
            kwargs['ping_timeout'] = .02
            return connect(f'ws://127.0.0.1:{port}', **kwargs)

        session = NativeSession(provider, 'FIXTURE', 'fixture.quick.aws.com', '', '', 'outpost')
        reply = ''.join([part async for part in Upstream(None, None, connect=local_connect)._stream(
            session, 'fixture', [{'role': 'user', 'content': 'reply after processing'}])])
        assert reply == 'LONG_REPLY_OK'
        assert bool(pongs) is (provider == 'gateway')
