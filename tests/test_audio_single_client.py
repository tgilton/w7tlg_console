"""One audio client per receiver, newest wins (2026-10-10: console tabs left
open across a restart each played the same audio, heard as flanging).

Real routes through Starlette's TestClient with the lifespan stubbed out, so
no hardware and no console start. /ws/audio is shared by console.html and
panadapter.html; /ws/audio_b is the console's RX2; /ws/audio_0 belongs to
diversity.html alone and is left non-exclusive.
"""
import contextlib
import logging
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import dashboard.server as server

DASHBOARD = Path(server.__file__).parent
PCM = b"\x01\x00" * 131


@pytest.fixture
def client(monkeypatch):
    for name, label, exclusive in (("audio_manager", "RX1", True),
                                   ("audio_manager_b", "RX2", True),
                                   ("audio_manager_0", "RX0", False)):
        monkeypatch.setattr(server, name, server.AudioConnectionManager(label, exclusive=exclusive))

    @contextlib.asynccontextmanager
    async def no_lifespan(_app):
        yield   # the real one starts the rig, amplifier and SDR

    monkeypatch.setattr(server.app.router, "lifespan_context", no_lifespan)
    # Entered, so every websocket shares one event loop, as under uvicorn.
    with TestClient(server.app) as c:
        yield c


def _broadcast(client, mgr, data):
    client.portal.call(mgr.broadcast_audio, data)


def _closed(ws):
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_bytes()
    return exc.value


def test_real_managers_rx1_rx2_exclusive_rx0_not():
    assert server.AudioConnectionManager("x").exclusive is True
    src = Path(server.__file__).read_text()
    assert 'audio_manager = AudioConnectionManager("RX1")' in src
    assert 'audio_manager_b = AudioConnectionManager("RX2")' in src
    assert 'audio_manager_0 = AudioConnectionManager("RX0", exclusive=False)' in src


@pytest.mark.parametrize("path,attr", [("/ws/audio", "audio_manager"),
                                       ("/ws/audio_b", "audio_manager_b")])
def test_second_client_replaces_the_first(client, path, attr):
    mgr = getattr(server, attr)
    with client.websocket_connect(path) as first:
        assert len(mgr.active) == 1
        with client.websocket_connect(path) as second:
            closed = _closed(first)
            assert closed.code == 4001 == mgr.TAKEOVER_CODE
            assert closed.reason == "Audio taken over by another console window"
            assert len(mgr.active) == 1
            # The survivor still gets audio.
            _broadcast(client, mgr, PCM)
            assert second.receive_bytes() == PCM
    assert mgr.active == []


def test_three_stale_tabs_leave_one(client):
    mgr = server.audio_manager
    with client.websocket_connect("/ws/audio") as a, \
            client.websocket_connect("/ws/audio") as b, \
            client.websocket_connect("/ws/audio") as c:
        assert _closed(a).code == 4001
        assert _closed(b).code == 4001
        assert len(mgr.active) == 1
        _broadcast(client, mgr, PCM)
        assert c.receive_bytes() == PCM


def test_taking_it_back_closes_the_other(client):
    """The page that lost the audio reconnects on unmute: now the other one loses it."""
    with client.websocket_connect("/ws/audio") as first:
        with client.websocket_connect("/ws/audio") as second:
            assert _closed(first).code == 4001
            with client.websocket_connect("/ws/audio") as first_again:
                assert _closed(second).code == 4001
                _broadcast(client, server.audio_manager, PCM)
                assert first_again.receive_bytes() == PCM


def test_receivers_are_independent(client):
    with client.websocket_connect("/ws/audio") as rx1, \
            client.websocket_connect("/ws/audio_b") as rx2, \
            client.websocket_connect("/ws/audio_0") as rx0:
        assert len(server.audio_manager.active) == 1
        assert len(server.audio_manager_b.active) == 1
        assert len(server.audio_manager_0.active) == 1
        _broadcast(client, server.audio_manager, PCM)
        _broadcast(client, server.audio_manager_b, PCM + PCM)
        _broadcast(client, server.audio_manager_0, PCM * 3)
        assert rx1.receive_bytes() == PCM
        assert rx2.receive_bytes() == PCM + PCM
        assert rx0.receive_bytes() == PCM * 3


def test_single_client_alone_is_never_closed(client):
    """A standalone panadapter with no console open: one client, plain audio."""
    with client.websocket_connect("/ws/audio") as only:
        for _ in range(3):
            _broadcast(client, server.audio_manager, PCM)
            assert only.receive_bytes() == PCM
        assert len(server.audio_manager.active) == 1


def test_rx0_diversity_socket_still_takes_several_clients(client):
    mgr = server.audio_manager_0
    with client.websocket_connect("/ws/audio_0") as a, \
            client.websocket_connect("/ws/audio_0") as b:
        assert len(mgr.active) == 2
        _broadcast(client, mgr, PCM)
        assert a.receive_bytes() == PCM
        assert b.receive_bytes() == PCM


def test_one_log_line_per_replacement(client, caplog):
    caplog.set_level(logging.INFO, logger=server.logger.name)
    with client.websocket_connect("/ws/audio_b") as first:
        assert not [r for r in caplog.records if "replaced" in r.getMessage()]
        with client.websocket_connect("/ws/audio_b"):
            _closed(first)
            lines = [r.getMessage() for r in caplog.records if "replaced" in r.getMessage()]
    assert len(lines) == 1
    assert re.fullmatch(
        r"Audio WS RX2: client \S+ replaced by \S+ "
        r"\(one audio client per receiver, newest wins\)", lines[0])
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


# --- the pages: static checks of the takeover handling -----------------------

def _page(name):
    return (DASHBOARD / name).read_text()


def test_pages_use_the_servers_close_code_and_text():
    code = str(server.AudioConnectionManager.TAKEOVER_CODE)
    text = server.AudioConnectionManager.TAKEOVER_REASON
    console, pan = _page("console.html"), _page("panadapter.html")
    assert console.count(f"const AUDIO_TAKEOVER_CODE = {code};") == 2   # RX1 and RX2
    assert console.count(f"const AUDIO_TAKEOVER_TEXT = '{text}';") == 2
    assert f"const AUDIO_TAKEOVER_CODE = {code};" in pan
    assert f"btn.textContent = '{text}';" in pan


@pytest.mark.parametrize("page,sockets", [("console.html", 2), ("panadapter.html", 1)])
def test_takeover_close_never_reconnects(page, sockets):
    """In every audio onclose, the takeover branch returns before the reconnect."""
    handlers = re.findall(r"audioWs2?\.onclose = evt => \{(.*?)\n  \};", _page(page), re.S)
    assert len(handlers) == sockets
    for body in handlers:
        takeover = body.index("evt.code === AUDIO_TAKEOVER_CODE")
        assert "return;" in body[takeover:body.index("setTimeout(ensureAudioWs")]


@pytest.mark.parametrize("page", ["console.html", "panadapter.html"])
def test_takeover_does_not_switch_audio_off_for_the_winner(page):
    """Going quiet is local: set_audio_enabled false is sent only by disableAudio."""
    src = _page(page)
    sends = [m.start() for m in re.finditer(r"cmd: 'set_audio_enabled', enabled: false", src)]
    for at in sends:
        assert "function disableAudio()" in src[at - 400:at]


def test_diversity_page_untouched_by_the_rule():
    div = _page("diversity.html")
    assert "/ws/audio_0" in div
    assert "/ws/audio`" not in div and "/ws/audio_b" not in div
