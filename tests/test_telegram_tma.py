"""Offline security and lifecycle checks for the Telegram Mini App surface."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import sys
import time
import types
import urllib.parse

import pytest
from aiohttp import ClientSession, web

import talk_telegram_tma as tma


def _init_data(*, bot_token="123:secret", auth_date=1_000, user_id=42):
    fields = {
        "auth_date": str(auth_date),
        "query_id": "query",
        "user": json.dumps({"id": user_id, "username": "owner"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


def test_init_data_signature_allowlist_and_freshness(monkeypatch):
    monkeypatch.delenv("TELEGRAM_TMA_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    identity = tma.validate_telegram_init_data(
        _init_data(), "123:secret", {"42"}, now=1_000
    )
    assert identity.user_id == 42
    assert identity.chat_id == "42"

    with pytest.raises(tma.TmaAuthError, match="not allowed"):
        tma.validate_telegram_init_data(
            _init_data(user_id=43), "123:secret", {42}, now=1_000
        )
    with pytest.raises(tma.TmaAuthError, match="expired"):
        tma.validate_telegram_init_data(
            _init_data(auth_date=600), "123:secret", {42}, now=1_000
        )
    with pytest.raises(tma.TmaAuthError, match="future"):
        tma.validate_telegram_init_data(
            _init_data(auth_date=1_040), "123:secret", {42}, now=1_000
        )


def test_allowlist_is_numeric_and_fail_closed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*, 42, owner")
    assert tma.configured_telegram_allowed_users() == frozenset({42})
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")
    assert tma.configured_telegram_allowed_users() == frozenset()


def test_tma_listener_requires_explicit_enable(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
    monkeypatch.delenv(tma.TMA_ENABLE_ENV, raising=False)
    assert tma._tma_is_connected(types.SimpleNamespace(extra={})) is False
    monkeypatch.setenv(tma.TMA_ENABLE_ENV, "true")
    assert tma._tma_is_connected(types.SimpleNamespace(extra={})) is True
    assert tma._tma_validate_config(types.SimpleNamespace(enabled=True, extra={})) is True
    monkeypatch.delenv(tma.TMA_ENABLE_ENV, raising=False)
    assert tma._tma_is_connected(types.SimpleNamespace(extra={"_enabled_explicit": True})) is True


def test_audio_queue_is_bounded_and_drain_invalidates_pending_audio():
    async def check():
        audio = tma.TmaAudio()
        flushed = []
        audio.on_flush = flushed.append
        for _ in range(tma.TMA_OUTPUT_MAX_CHUNKS + 5):
            audio.queue_playback(b"\0\0" * 10)
        assert audio._output.qsize() == tma.TMA_OUTPUT_MAX_CHUNKS
        audio.report_playback(17, 25)
        _item, played = audio.drain_playback()
        assert played == 17
        assert flushed == [1]
        assert not audio.playback_pending

    asyncio.run(check())


def test_tma_announcement_isolated_provider_command():
    async def check():
        connection = tma.TmaConnection(
            tma.TmaRuntime(),
            types.SimpleNamespace(send_json=lambda _payload: None),
            tma.TmaIdentity(42, "owner", None, None, 1_000, "42"),
        )
        connection._queue_announcement_now("Telegram Hermes result", "3973")
        commands = connection.announce_queue.get_nowait()
        assert len(commands) == 1
        command = commands[0]
        assert isinstance(command, tma.talk_realtime.StartResponse)
        assert command.allow_tools is False
        assert command.input == ()
        assert command.conversation == "none"
        assert "3973" in command.instructions
        assert "UNTRUSTED DATA" in command.instructions
        assert not any(
            isinstance(item, (tma.talk_realtime.AddContext, tma.talk_realtime.RemoveContext))
            for item in commands
        )

    asyncio.run(check())


def test_gateway_admission_requires_exact_final_turn():
    async def check():
        calls = []

        class Context:
            def inject_message(self, content, *, role, origin):
                calls.append((content, role, origin))
                return True

        runtime = tma.TmaRuntime(Context())
        connection = tma.TmaConnection(
            runtime,
            types.SimpleNamespace(send_json=lambda _payload: None),
            tma.TmaIdentity(42, "owner", None, None, 1_000, "42"),
        )
        assert runtime.claim(connection)
        assert "admitted" in await asyncio.to_thread(
            connection.admit_request, "run the checked task"
        )
        assert calls[0][1] == "user"
        assert calls[0][2]["platform"] == "telegram"
        assert calls[0][0].startswith("run the checked task\n\u2063tma:")
        assert (
            await asyncio.to_thread(
                connection.queue_final_response, "wrong result", "other request"
            )
            is False
        )
        assert connection.pending_request == "run the checked task"
        assert (
            await asyncio.to_thread(
                connection.queue_final_response,
                "right result",
                f"[timestamp] {calls[0][0]}",
            )
            is True
        )
        assert connection.pending_request is None
        await asyncio.sleep(0)
        assert connection.announce_queue.qsize() == 1

    asyncio.run(check())


def test_pending_gateway_request_expires(monkeypatch):
    async def check():
        monkeypatch.setattr(tma, "TMA_REQUEST_TIMEOUT_S", 0.01)

        class Context:
            def inject_message(self, content, *, role, origin):
                return True

        connection = tma.TmaConnection(
            tma.TmaRuntime(Context()),
            types.SimpleNamespace(send_json=lambda _payload: None),
            tma.TmaIdentity(42, "owner", None, None, 1_000, "42"),
        )
        assert "admitted" in await asyncio.to_thread(
            connection.admit_request, "expire this request"
        )
        await asyncio.sleep(0.03)
        assert connection.pending_request is None
        assert connection.announce_queue.qsize() == 1

    asyncio.run(check())


def test_final_hook_routes_from_worker_thread(monkeypatch):
    async def check():
        session_context = types.ModuleType("gateway.session_context")
        session_context.get_session_env = lambda name, default="": (
            "agent:main:telegram:dm:42" if name == "HERMES_SESSION_KEY" else default
        )
        gateway = types.ModuleType("gateway")
        monkeypatch.setitem(sys.modules, "gateway", gateway)
        monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)

        injected = []

        class Context:
            def inject_message(self, content, *, role, origin):
                injected.append(content)
                return True

        runtime = tma.TmaRuntime(Context())
        connection = tma.TmaConnection(
            runtime,
            types.SimpleNamespace(send_json=lambda _payload: None),
            tma.TmaIdentity(42, "owner", None, None, 1_000, "42"),
        )
        assert runtime.claim(connection)
        await asyncio.to_thread(connection.admit_request, "worker routed request")
        await asyncio.to_thread(
            runtime.post_llm_call,
            session_id="session",
            turn_id="turn",
            user_message=injected[0],
            assistant_response="worker result",
        )
        await asyncio.sleep(0)
        assert connection.pending_request is None
        assert connection.announce_queue.qsize() == 1

    asyncio.run(check())


def test_cross_thread_timeout_cancels_pending_submission(monkeypatch):
    async def check():
        monkeypatch.setattr(tma, "TMA_AUTH_TIMEOUT_S", 0.01)
        connection = tma.TmaConnection(
            tma.TmaRuntime(),
            types.SimpleNamespace(send_json=lambda _payload: None),
            tma.TmaIdentity(42, "owner", None, None, 1_000, "42"),
        )
        pending = concurrent.futures.Future()

        def submit(coroutine, loop):
            del loop
            coroutine.close()
            return pending

        monkeypatch.setattr(tma.asyncio, "run_coroutine_threadsafe", submit)
        assert await asyncio.to_thread(connection.admit_request, "cancel me")
        assert pending.cancelled()

        pending = concurrent.futures.Future()
        assert not await asyncio.to_thread(
            connection.queue_final_response, "late result", "late request"
        )
        assert pending.cancelled()

    asyncio.run(check())


def test_tma_tool_executor_rejects_unadvertised_controls():
    async def check():
        connection = tma.TmaConnection(
            tma.TmaRuntime(),
            types.SimpleNamespace(send_json=lambda _payload: None),
            tma.TmaIdentity(42, "owner", None, None, 1_000, "42"),
        )
        with pytest.raises(tma.talk_tools.TalkToolError, match="unavailable"):
            connection.tool_executor("resolve_approval", {})

    asyncio.run(check())


def test_register_platform_and_final_hook():
    registered = {}

    class Context:
        def register_platform(self, **kwargs):
            registered.update(kwargs)

        def register_hook(self, name, callback):
            registered["hook"] = (name, callback)

    tma.register(Context())
    assert registered["name"] == tma.TMA_PLATFORM
    assert registered["required_env"] == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"]
    assert registered["hook"][0] == "post_llm_call"


def test_register_rejects_named_profile_and_defers_multiplex_check(monkeypatch):
    class NamedContext:
        profile_name = "ops"

        def register_platform(self, **kwargs):
            del kwargs

    with pytest.raises(tma.TmaUnavailable, match="default Hermes profile"):
        tma.register(NamedContext())

    calls = []

    class MultiplexedContext:
        profile_name = "default"
        gateway_config = types.SimpleNamespace(multiplex_profiles=True)

        def register_platform(self, **kwargs):
            calls.append(kwargs)

        def register_hook(self, name, callback):
            del name, callback

    monkeypatch.setattr(
        tma, "_context_is_multiplexed", lambda context: pytest.fail("checked during register")
    )
    tma.register(MultiplexedContext())
    assert calls


def test_adapter_rejects_multiplexed_gateway_after_registration(monkeypatch):
    async def check():
        adapter = object.__new__(tma._TelegramTmaAdapterMethods)
        adapter.config = types.SimpleNamespace(enabled=True, token="123:secret")
        adapter.runtime = tma.TmaRuntime(object())
        adapter._runner = adapter._site = adapter._connection = adapter._server_app = None
        fatal = []
        adapter._set_fatal_error = lambda *args, **kwargs: fatal.append((args, kwargs))
        monkeypatch.setattr(tma, "_context_is_multiplexed", lambda context: True)

        assert await adapter.connect() is False
        assert fatal[0][0][1] == (
            "Telegram Mini App is unavailable on a multiplexed Hermes gateway"
        )

    asyncio.run(check())


def test_websocket_auth_and_single_call_lifecycle(monkeypatch):
    async def check():
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "42")
        adapter = object.__new__(tma._TelegramTmaAdapterMethods)
        adapter.config = types.SimpleNamespace(token="")
        adapter.runtime = tma.TmaRuntime()
        adapter._runner = adapter._site = adapter._connection = adapter._server_app = None
        sessions = []

        async def hold(connection):
            sessions.append(connection)
            await asyncio.Event().wait()

        adapter._run_session = hold
        app = web.Application()
        app.router.add_get(tma.TMA_WS_PATH, adapter._websocket)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}{tma.TMA_WS_PATH}"
        try:
            async with ClientSession() as client:
                for init_data in (
                    _init_data(auth_date=int(time.time()), user_id=43),
                    _init_data(auth_date=int(time.time()) - 301),
                ):
                    async with client.ws_connect(url) as websocket:
                        await websocket.send_json({"type": "auth", "initData": init_data})
                        error = await websocket.receive_json()
                        assert error["type"] == "error"
                async with client.ws_connect(url) as first:
                    await first.send_json(
                        {"type": "auth", "initData": _init_data(auth_date=int(time.time()))}
                    )
                    assert (await first.receive_json())["type"] == "ready"
                    async with client.ws_connect(url) as second:
                        await second.send_json(
                            {"type": "auth", "initData": _init_data(auth_date=int(time.time()))}
                        )
                        error = await second.receive_json()
                        assert error["type"] == "error"
                    assert len(sessions) == 1
                    await first.send_json({"type": "end"})
                    await first.close()
                assert adapter.runtime._active is None
                assert adapter._connection is None
                assert not [
                    task
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task() and not task.done()
                ]
        finally:
            await runner.cleanup()

    asyncio.run(check())
