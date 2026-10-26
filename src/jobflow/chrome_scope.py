"""Attach Playwright to one normal-Chrome tab, not every existing tab.

This local-only CDP relay changes target attachment, not site traffic, browser
identity or protection handling. Child frame/worker sessions pass unchanged.
It never closes Chrome or any existing tab.
"""

import asyncio
import json
import secrets
import threading
from urllib.parse import urlparse
from urllib.request import urlopen


class ScopedChromeConnection:
    def __init__(self, endpoint, target_id):
        parsed = urlparse(endpoint)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port:
            raise ValueError('Scoped Chrome endpoint must be loopback HTTP')
        self.endpoint = endpoint
        self.target_id = target_id
        self._ready = threading.Event()
        self._token = secrets.token_urlsafe(24)
        self._error = None

    def __enter__(self):
        with urlopen(self.endpoint + '/json/version', timeout=5) as response:
            self._upstream = json.load(response)['webSocketDebuggerUrl']
        if urlparse(self._upstream).hostname != '127.0.0.1':
            raise ValueError('Chrome websocket must remain on loopback')
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(10):
            raise TimeoutError('Scoped Chrome relay startup timed out')
        if self._error:
            raise self._error
        return self.url

    def __exit__(self, *args):
        if hasattr(self, '_loop') and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set)
        if hasattr(self, '_thread'):
            self._thread.join(timeout=10)

    def _run(self):
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self._error = exc
            self._ready.set()

    async def _serve(self):
        from websockets.asyncio.server import serve
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        async with serve(self._handle, '127.0.0.1', 0, max_size=None) as server:
            self.url = 'ws://127.0.0.1:{}/{}'.format(server.sockets[0].getsockname()[1], self._token)
            self._ready.set()
            await self._stop.wait()

    async def _handle(self, client):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed
        if client.request.path != '/' + self._token:
            await client.close(code=1008)
            return
        async with connect(self._upstream, proxy=None, max_size=None) as upstream:
            pending = {}
            next_id = -1
            allowed = {self.target_id}
            attached = set()
            tasks = set()

            async def rpc(method, params):
                nonlocal next_id
                key = next_id
                next_id -= 1
                future = asyncio.get_running_loop().create_future()
                pending[key] = future
                try:
                    await upstream.send(json.dumps({'id': key, 'method': method, 'params': params}))
                    return await asyncio.wait_for(future, 20)
                finally:
                    pending.pop(key, None)

            async def attach(target_id):
                if target_id in attached:
                    return
                attached.add(target_id)
                allowed.add(target_id)
                await rpc('Target.attachToTarget', {'targetId': target_id, 'flatten': True})

            def schedule(coro):
                task = asyncio.create_task(coro)
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                # A popup attachment failure must close the connection rather
                # than silently present a partly controlled application.
                def completed(result):
                    if not result.cancelled() and result.exception():
                        asyncio.create_task(client.close(code=1011))
                task.add_done_callback(completed)

            async def receive():
                async for raw in upstream:
                    message = json.loads(raw)
                    if message.get('id') in pending:
                        future = pending[message['id']]
                        if not future.done():
                            if 'error' in message:
                                future.set_exception(RuntimeError(message['error'].get('message', 'CDP error')))
                            else:
                                future.set_result(message.get('result', {}))
                        continue
                    if not message.get('sessionId') and message.get('method') in {
                        'Target.targetCreated', 'Target.targetInfoChanged',
                    }:
                        info = message['params']['targetInfo']
                        if info.get('openerId') in allowed and info.get('type') == 'page':
                            schedule(attach(info['targetId']))
                        if info.get('targetId') not in allowed:
                            continue
                    await client.send(raw)

            async def command(message):
                key = message['id']
                method = message['method']
                params = message.get('params', {})
                try:
                    if method == 'Target.setAutoAttach':
                        # Only root auto-attach is replaced. Per-page recursive
                        # attachment still discovers its frames and workers.
                        if params.get('autoAttach'):
                            await rpc('Target.setDiscoverTargets', {'discover': True})
                            await attach(self.target_id)
                        result = {}
                    elif method == 'Target.createTarget':
                        result = await rpc(method, params)
                        await attach(result['targetId'])
                    elif method == 'Target.getTargets':
                        result = await rpc(method, params)
                        result['targetInfos'] = [info for info in result['targetInfos'] if info['targetId'] in allowed]
                    elif method == 'Browser.close':
                        raise ValueError('Scoped connections cannot close the shared browser')
                    elif method == 'Target.closeTarget' and params.get('targetId') not in allowed:
                        raise ValueError('Cannot close an unrelated Chrome tab')
                    else:
                        await upstream.send(json.dumps(message))
                        return
                    await client.send(json.dumps({'id': key, 'result': result}))
                except Exception as exc:
                    await client.send(json.dumps({'id': key, 'error': {'code': -32000, 'message': str(exc)}}))

            reader = asyncio.create_task(receive())
            def upstream_finished(task):
                if not task.cancelled():
                    task.exception()  # Retrieve the error; disconnect is fail-closed.
                    asyncio.create_task(client.close(code=1011))
            reader.add_done_callback(upstream_finished)
            try:
                async for raw in client:
                    message = json.loads(raw)
                    if message.get('sessionId'):
                        await upstream.send(raw)
                    else:
                        schedule(command(message))
            except ConnectionClosed:
                pass  # Playwright's driver may disconnect without a close frame.
            finally:
                reader.cancel()
                for task in list(tasks):
                    task.cancel()
                await asyncio.gather(reader, *tasks, return_exceptions=True)
