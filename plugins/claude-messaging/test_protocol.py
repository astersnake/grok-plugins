"""Tests for the Claude Code peer protocol. They never touch ~/.claude."""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import protocol  # noqa: E402
from protocol import (  # noqa: E402
    AUTH_LINE_LENGTH,
    MAX_LINE,
    PeerRuntime,
    ProtocolError,
    encode_address,
    escape_body,
    events_directory,
    grok_permission_class,
    load_peer,
    parse_envelope,
    pid_domain,
    proc_start,
    render_envelope,
    sanitize_name,
    sessions_dir,
)

_TMP: tempfile.TemporaryDirectory[str] | None = None
_ENV: Any = None


def setUpModule() -> None:
    """Point every test at a private config and runtime dir."""
    global _TMP, _ENV
    _TMP = tempfile.TemporaryDirectory(prefix="cm-", dir="/tmp")
    root = Path(_TMP.name)
    (root / "run").mkdir()
    (root / "claude").mkdir()
    (root / "grok").mkdir()
    _ENV = mock.patch.dict(
        os.environ,
        {
            "XDG_RUNTIME_DIR": str(root / "run"),
            "CLAUDE_CONFIG_DIR": str(root / "claude"),
            "GROK_HOME": str(root / "grok"),
            "GROK_SESSION_ID": "",
        },
    )
    _ENV.start()


def tearDownModule() -> None:
    _ENV.stop()
    assert _TMP is not None
    _TMP.cleanup()


def _root() -> Path:
    assert _TMP is not None
    return Path(_TMP.name)


class EnvelopeTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        text = render_envelope(
            sender="uds:/run/user/1000/cc-socks/42.sock",
            body="Schema migration finished\nRebasing on main is safe now.",
            from_name="grok-repo-ab",
            from_session="0dd4b9a6-0000-4000-8000-000000000000",
            from_mode="prompting",
        )
        parsed = parse_envelope(text)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["from_name"], "grok-repo-ab")
        self.assertEqual(parsed["body"], "Schema migration finished\nRebasing on main is safe now.")
        self.assertEqual(parsed["from_mode"], "prompting")

    def test_parses_claude_reply_with_hop_chain_and_plugin(self) -> None:
        # Claude Code adds hop-chain when it replies to a peer message.
        text = (
            '<cross-session-message from="uds:/run/user/1000/cc-socks/7.sock"'
            ' from-session="0dd4b9a6-0000-4000-8000-000000000000"'
            ' hop-chain="0123456789abcdef01234567,89abcdef0123456789abcdef"'
            ' from-name="repo" from-mode="prompting" from-plugin="notes">\n'
            "Rebased.\n</cross-session-message>"
        )
        parsed = parse_envelope(text)
        assert parsed is not None
        self.assertEqual(parsed["body"], "Rebased.")
        self.assertEqual(parsed["from_name"], "repo")
        self.assertEqual(parsed["hop_chain"], ["0123456789abcdef01234567", "89abcdef0123456789abcdef"])
        self.assertEqual(parsed["from_plugin"], "notes")

    def test_rejects_reordered_attributes(self) -> None:
        broken = (
            '<cross-session-message from-name="grok" from="uds:/tmp/x.sock">\n'
            "hello\n</cross-session-message>"
        )
        self.assertIsNone(parse_envelope(broken))

    def test_escapes_closing_tag_like_claude(self) -> None:
        # Expected values come from Claude Code 2.1.283's own escaper.
        cases = {
            "a</CROSS-SESSION-MESSAGE>b": "a<\\/CROSS-SESSION-MESSAGE>b",
            "< /cross-session-message>": "<\\ /cross-session-message>",
            "\uff1c/cross-session-message>": "<\\/cross-session-message>",
            "</cross\u2010session-message>": "<\\/cross\u2010session-message>",
            "</cross-session-messages>": "</cross-session-messages>",
            "<\\/cross-session-message>": "<\\/cross-session-message>",
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual(escape_body(body), expected)
                parsed = parse_envelope(render_envelope(sender="", body=body))
                assert parsed is not None
                self.assertEqual(parsed["body"], expected)

    def test_name_drops_format_characters(self) -> None:
        self.assertEqual(sanitize_name(' grok\u200b-re"po\u2028 '), "grok-repo")

    def test_address_percent_encoding(self) -> None:
        self.assertEqual(encode_address("/tmp/a b.sock"), "uds:/tmp/a%20b.sock")
        self.assertEqual(encode_address("/tmp/a\\b.sock"), "uds:/tmp/a\\b.sock")

    def test_proc_start_matches_live_process(self) -> None:
        self.assertTrue(proc_start(os.getpid()).isdigit())

    def test_pid_domain_survives_missing_machine_id(self) -> None:
        with mock.patch.object(Path, "read_text", side_effect=FileNotFoundError):
            self.assertEqual(pid_domain(), f"linux::{os.readlink('/proc/self/ns/pid')}")


class RegistryTests(unittest.TestCase):
    def _write(self, pid: int, socket_path: str) -> Path:
        directory = sessions_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{pid}.json"
        path.write_text(json.dumps({"pid": pid, "sessionId": str(uuid.uuid4()), "messagingSocketPath": socket_path}))
        self.addCleanup(path.unlink)
        return path

    def test_accepts_moved_aside_socket(self) -> None:
        # Claude Code binds <pid>-<hex>.sock when another session holds <pid>.sock.
        path = self._write(424242, "/run/user/1000/cc-socks/424242-0a1b2c3d.sock")
        peer = load_peer(path)
        assert peer is not None
        self.assertEqual(peer.socket_path, "/run/user/1000/cc-socks/424242-0a1b2c3d.sock")

    def test_rejects_relative_socket(self) -> None:
        self.assertIsNone(load_peer(self._write(424243, "cc-socks/424243.sock")))


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = PeerRuntime.start(cwd=str(_root()))
        self.addCleanup(self.runtime.close)

    def _listener(self) -> tuple[socket.socket, str]:
        return _listener(self)

    def _frame(self, **fields: Any) -> str:
        return json.dumps({"msgV": 1, "msg_id": str(uuid.uuid4()), **fields})

    def test_accepted_message_sends_no_receipt(self) -> None:
        listener, address = self._listener()
        self.runtime._on_line(
            self._frame(type="user", **{"from": address}, message={"role": "user", "content": "hi"})
        )
        self.assertEqual([row["text"] for row in self.runtime.read_inbox()], ["hi"])
        with self.assertRaises(TimeoutError):
            listener.accept()

    def test_empty_message_is_not_queued(self) -> None:
        self.runtime._on_line(self._frame(type="user", message={"role": "user", "content": ""}))
        self.assertEqual(self.runtime.read_inbox(), [])

    def test_refused_receipt_is_reported_as_refused(self) -> None:
        msg_id = str(uuid.uuid4())
        self.runtime._on_line(
            self._frame(
                type="control",
                action="peer_message_status",
                status="expired",
                status_detail="refused",
                reason="The recipient session is not accepting cross-session messages.",
                orig_msg_id=msg_id,
            )
        )
        receipt = self.runtime.wait_receipt(msg_id, 0.1)
        assert receipt is not None
        self.assertEqual(receipt.status, "refused")
        self.assertEqual(receipt.detail, "The recipient session is not accepting cross-session messages.")

    def test_uncorrelated_idle_notice_is_ignored(self) -> None:
        self.runtime._on_line(
            self._frame(type="control", action="peer_idle_notice", orig_msg_id=str(uuid.uuid4()), state="idle")
        )
        self.assertEqual(self.runtime.read_inbox(), [])

    def test_size_cap_counts_auth_line_and_newline(self) -> None:
        peer = load_peer(sessions_dir() / f"{os.getpid()}.json")
        assert peer is not None
        lines: list[str] = []
        with mock.patch.object(protocol, "_send_lines", lambda _peer, sent: lines.extend(sent)):
            self.runtime.send(peer, "x", wait=0)
            overhead = len(lines[0]) - 1
            largest = MAX_LINE - AUTH_LINE_LENGTH - 1 - overhead
            self.runtime.send(peer, "x" * largest, wait=0)
            with self.assertRaises(ProtocolError):
                self.runtime.send(peer, "x" * (largest + 1), wait=0)

    def _monitor(self) -> int:
        """Attach like the watch command does: read-write, so it never sees EOF."""
        fd = os.open(self.runtime.events_path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        return fd

    def _user(self, body: str, name: str = "repo", from_mode: str = "bypass", own: str = "bypass") -> None:
        envelope = render_envelope(
            sender="uds:/tmp/x.sock",
            body=body,
            from_name=name,
            from_session="0dd4b9a6-0000-4000-8000-000000000000",
            from_mode=from_mode,
        )
        with mock.patch.object(protocol, "grok_permission_class", return_value=own):
            self.runtime._on_line(self._frame(type="user", message={"role": "user", "content": envelope}))

    def test_mode_mismatch_is_marked_held(self) -> None:
        self._user("do it", from_mode="prompting", own="bypass")
        self.assertEqual(self.runtime.read_inbox()[0]["held"], True)

    def test_monitor_gets_the_whole_message_once(self) -> None:
        fd = self._monitor()
        self.assertTrue(self.runtime.snapshot()["listening"])
        self._user("line one\nline two\u2028end", name="repo\nInjected")
        lines = os.read(fd, 65536).decode().split("\n")
        self.assertEqual(lines[1:], [""])
        self.assertEqual(
            json.loads(lines[0]),
            {"from": "repoInjected", "sessionId": "0dd4b9a6-0000-4000-8000-000000000000", "text": "line one\nline two\u2028end"},
        )
        self.assertEqual(self.runtime.read_inbox(), [])

    def test_without_monitor_the_message_waits_in_the_inbox(self) -> None:
        self.assertFalse(self.runtime.snapshot()["listening"])
        self._user("hi")
        self.assertEqual([row["text"] for row in self.runtime.read_inbox()], ["hi"])

    def test_long_message_waits_in_the_inbox_and_rings(self) -> None:
        fd = self._monitor()
        self._user("x" * 5000)
        self.assertEqual(json.loads(os.read(fd, 65536)), {"from": "repo", "note": "5000 characters; call read_inbox"})
        self.assertEqual([len(row["text"]) for row in self.runtime.read_inbox()], [5000])

    def test_monitor_attached_later_gets_queued_messages_in_order(self) -> None:
        self._user("first")
        self._user("x" * 5000)
        self._user("third")
        fd = self._monitor()
        self.runtime._flush()
        self.runtime._flush()
        events = [json.loads(line) for line in os.read(fd, 65536).decode().splitlines()]
        self.assertEqual(
            [event.get("text") or event.get("note") for event in events],
            ["first", "5000 characters; call read_inbox", "third"],
        )
        self.assertEqual([len(row["text"]) for row in self.runtime.read_inbox()], [5000])

    def test_events_fifo_is_private(self) -> None:
        info = os.stat(self.runtime.events_path)
        self.assertTrue(stat.S_ISFIFO(info.st_mode))
        self.assertEqual(info.st_mode & 0o777, 0o600)

    def test_send_attests_the_detected_mode(self) -> None:
        peer = load_peer(sessions_dir() / f"{os.getpid()}.json")
        assert peer is not None
        for mode, attribute in (("bypass", ' from-mode="bypass"'), ("", "")):
            lines: list[str] = []
            with (
                mock.patch.object(protocol, "grok_permission_class", return_value=mode),
                mock.patch.object(protocol, "_send_lines", lambda _peer, sent: lines.extend(sent)),
            ):
                self.runtime.send(peer, "x", wait=0)
            content = json.loads(lines[0])["message"]["content"]
            header = content.split("\n", 1)[0]
            self.assertEqual(header.endswith(f"{attribute}>"), True, header)
            self.assertEqual("from-mode" in header, bool(mode))

    def test_close_removes_every_file(self) -> None:
        paths = [
            Path(self.runtime.socket_path),
            sessions_dir() / f"{os.getpid()}.json",
            protocol.key_path(os.getpid(), self.runtime.socket_path),
            Path(self.runtime.events_path),
        ]
        self.assertEqual([path for path in paths if not path.exists()], [])
        self.runtime.close()
        self.assertEqual([path for path in paths if path.exists()], [])


class IdleNoticeTests(unittest.TestCase):
    """notify_when_idle answers when Grok's turn is over, as Claude Code does."""

    session = "01a0e489-855d-7563-b344-2deb911b7777"

    def setUp(self) -> None:
        directory = _root() / "grok" / "sessions" / "%2Ftmp%2Fidle" / self.session
        directory.mkdir(parents=True, exist_ok=True)
        self.events = directory / "events.jsonl"
        self.events.write_text("")
        self.addCleanup(self.events.unlink)
        for patcher in (
            mock.patch.dict(os.environ, {"GROK_SESSION_ID": self.session}),
            mock.patch.object(protocol, "SETTLE", 0.0),
            mock.patch.object(protocol, "WAKE_GRACE", 5.0),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runtime = PeerRuntime.start(cwd=str(_root()))
        self.addCleanup(self.runtime.close)

    def _event(self, kind: str, at: float | None = None) -> float:
        at = at or time.time()
        stamp = datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        event: dict[str, Any] = {"ts": stamp, "type": kind}
        if kind == "turn_started":
            event.update(session_id=self.session, yolo_mode=True)
        with self.events.open("a") as handle:
            handle.write(json.dumps(event) + "\n")
        return at

    def _subscribe(self, listener: tuple[socket.socket, str] | None = None) -> tuple[socket.socket, str, str]:
        server, address = listener or _listener(self)
        msg_id = str(uuid.uuid4())
        frame = {"msgV": 1, "type": "control", "action": "notify_when_idle", "msg_id": msg_id, "from": address}
        self.runtime._on_line(json.dumps(frame))
        return server, address, msg_id

    def _notice(self, server: socket.socket) -> dict[str, Any] | None:
        self.runtime._check_idle()
        try:
            connection, _ = server.accept()
        except TimeoutError:
            return None
        with connection:
            return json.loads(connection.makefile().readline())

    def test_waits_while_a_turn_runs(self) -> None:
        self._event("turn_started")
        server, _, msg_id = self._subscribe()
        self.assertIsNone(self._notice(server))
        ended = self._event("turn_ended")
        notice = self._notice(server)
        assert notice is not None
        self.assertEqual((notice["orig_msg_id"], notice["state"]), (msg_id, "idle"))
        self.assertLessEqual(abs(notice["finished_at"] - int(ended * 1000)), 1)
        self.assertEqual(notice["from_mode"], "bypass")

    def test_answers_at_once_when_idle(self) -> None:
        self._event("turn_started")
        self._event("turn_ended")
        server, _, _ = self._subscribe()
        self.assertEqual((self._notice(server) or {}).get("state"), "idle")

    def test_a_delivered_message_is_work_still_owed(self) -> None:
        self._event("turn_started")
        self._event("turn_ended")
        fd = os.open(self.runtime.events_path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, fd)
        envelope = render_envelope(sender="uds:/tmp/x.sock", body="run the QA pass", from_name="repo", from_mode="bypass")
        self.runtime._on_line(json.dumps({"msgV": 1, "type": "user", "message": {"role": "user", "content": envelope}}))
        server, _, _ = self._subscribe()
        self.assertIsNone(self._notice(server))
        self._event("turn_started")
        self.assertIsNone(self._notice(server))
        self._event("turn_ended")
        self.assertEqual((self._notice(server) or {}).get("state"), "idle")

    def test_without_a_turn_log_answers_at_once(self) -> None:
        with mock.patch.dict(os.environ, {"GROK_SESSION_ID": ""}):
            server, _, _ = self._subscribe()
            notice = self._notice(server)
        assert notice is not None
        self.assertEqual(notice["state"], "idle")
        self.assertNotIn("finished_at", notice)

    def test_same_requester_replaces_its_subscription(self) -> None:
        self._event("turn_started")
        listener = _listener(self)
        self._subscribe(listener)
        _, _, latest = self._subscribe(listener)
        self._event("turn_ended")
        self.assertEqual((self._notice(listener[0]) or {}).get("orig_msg_id"), latest)
        self.assertIsNone(self._notice(listener[0]))

    def test_close_tells_watchers_the_session_exited(self) -> None:
        self._event("turn_started")
        server, _, msg_id = self._subscribe()
        self.runtime.close()
        connection, _ = server.accept()
        with connection:
            notice = json.loads(connection.makefile().readline())
        self.assertEqual((notice["orig_msg_id"], notice["state"]), (msg_id, "exited"))


class ParityTests(unittest.TestCase):
    def test_matches_claude_inbound_rule(self) -> None:
        cases = {
            ("bypass", "bypass"): False,
            ("prompting", "prompting"): False,
            ("prompting", "bypass"): True,
            ("bypass", "prompting"): True,
            ("", "prompting"): False,
            ("", "bypass"): True,
            ("bypass", ""): True,
        }
        for (from_mode, own), held in cases.items():
            with self.subTest(from_mode=from_mode, own=own):
                self.assertEqual(protocol.parity_hold(from_mode, own), held)


class PermissionClassTests(unittest.TestCase):
    session = "01a0e489-855d-7563-b344-2deb911b6715"

    def _events(self, *events: dict[str, Any]) -> None:
        directory = _root() / "grok" / "sessions" / "%2Ftmp%2Frepo" / self.session
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "events.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.addCleanup(path.unlink, True)

    def _turn(self, yolo: bool, session: str | None = None) -> dict[str, Any]:
        return {"type": "turn_started", "session_id": session or self.session, "yolo_mode": yolo}

    def test_latest_turn_wins(self) -> None:
        self._events(self._turn(False), {"type": "tool_call"}, self._turn(True), {"type": "tool_call"})
        with mock.patch.dict(os.environ, {"GROK_SESSION_ID": self.session}):
            self.assertEqual(grok_permission_class(), "bypass")
        self._events(self._turn(True), self._turn(False))
        with mock.patch.dict(os.environ, {"GROK_SESSION_ID": self.session}):
            self.assertEqual(grok_permission_class(), "prompting")

    def test_reads_back_across_blocks(self) -> None:
        filler = [{"type": "tool_call", "pad": "x" * 1000} for _ in range(200)]
        self._events(self._turn(True), *filler)
        with mock.patch.dict(os.environ, {"GROK_SESSION_ID": self.session}):
            self.assertEqual(grok_permission_class(), "bypass")

    def test_ignores_other_sessions_turns(self) -> None:
        self._events(self._turn(False), self._turn(True, session="01a0e489-0000-0000-0000-000000000000"))
        with mock.patch.dict(os.environ, {"GROK_SESSION_ID": self.session}):
            self.assertEqual(grok_permission_class(), "prompting")

    def test_unknown_mode_asserts_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"GROK_SESSION_ID": self.session}):
            self.assertEqual(grok_permission_class(), "")
        with mock.patch.dict(os.environ, {"GROK_SESSION_ID": "../*"}):
            self.assertEqual(grok_permission_class(), "")


class SocketTests(unittest.TestCase):
    def test_two_processes_exchange_a_message(self) -> None:
        tmp = str(_root())
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).parent)
        snapshot = _root() / "snapshot.json"
        inbox = _root() / "inbox.json"
        child = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(
                f"""
                import json, time
                from protocol import PeerRuntime
                runtime = PeerRuntime.start(cwd="{tmp}")
                open({str(snapshot)!r}, "w").write(json.dumps(runtime.snapshot()))
                deadline = time.time() + 15
                rows = []
                while time.time() < deadline:
                    rows = runtime.read_inbox(peek=True)
                    if rows:
                        break
                    time.sleep(0.05)
                open({str(inbox)!r}, "w").write(json.dumps(rows))
                time.sleep(0.5)
                runtime.close()
                """
            )],
            env=env,
        )
        try:
            deadline = time.time() + 10
            while not snapshot.exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(snapshot.exists(), "child did not register")
            parent = PeerRuntime.start(cwd=tmp)
            try:
                info = json.loads(snapshot.read_text())
                sent = parent.send(_peer_from_snapshot(info), "hello from grok", wait=0.5)
                # Like Claude Code, a peer that accepts a message sends no receipt.
                self.assertEqual(sent["receipt"], "unconfirmed")
            finally:
                parent.close()
        finally:
            child.wait(timeout=10)
        rows = json.loads(inbox.read_text())
        self.assertEqual(rows[0]["text"], "hello from grok")
        self.assertTrue(rows[0]["from"].startswith("grok-"))


class ServerTests(unittest.TestCase):
    def test_stdio_uses_newline_delimited_json(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, _ = process.communicate("".join(json.dumps(request) + "\n" for request in requests), timeout=15)
        self.assertEqual(process.returncode, 0)
        responses = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2])
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "claude-messaging")
        self.assertIn(f"cat <>{events_directory()}/{process.pid}.events", responses[0]["result"]["instructions"])
        self.assertIn("send_message", [tool["name"] for tool in responses[1]["result"]["tools"]])
        self.assertFalse((sessions_dir() / f"{process.pid}.json").exists())

    def test_sigkill_of_the_process_group_still_removes_the_registration(self) -> None:
        # Grok stops stdio MCP servers by SIGKILLing their process group.
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "server.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        files = [
            sessions_dir() / f"{process.pid}.json",
            events_directory() / f"{process.pid}.events",
            _root() / "run" / "cc-socks" / f"{process.pid}.sock",
        ]
        deadline = time.time() + 10
        while not all(path.exists() for path in files) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(all(path.exists() for path in files))
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        deadline = time.time() + 5
        while any(path.exists() for path in files) and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual([path for path in files if path.exists()], [])

    def test_socket_error_is_a_tool_error(self) -> None:
        import server

        runtime = mock.Mock()
        runtime.send.side_effect = ConnectionRefusedError("peer went away")
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "send_message", "arguments": {"name": "repo", "text": "hi"}},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]
        stdin = io.TextIOWrapper(io.BytesIO("".join(json.dumps(r) + "\n" for r in requests).encode()))
        stdout = io.TextIOWrapper(io.BytesIO())
        with (
            mock.patch.object(server, "resolve_peer"),
            mock.patch.object(server.sys, "stdin", stdin),
            mock.patch.object(server.sys, "stdout", stdout),
        ):
            server.serve(runtime)
        responses = [json.loads(line) for line in stdout.buffer.getvalue().decode().splitlines()]
        self.assertTrue(responses[0]["result"]["isError"])
        self.assertIn("peer went away", responses[0]["result"]["content"][0]["text"])
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 2, "result": {}})


def _listener(test: unittest.TestCase) -> tuple[socket.socket, str]:
    """A socket standing in for a Claude session, to capture what the peer sends back."""
    path = str(_root() / "run" / f"capture-{uuid.uuid4().hex[:8]}.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)
    server.settimeout(0.3)
    test.addCleanup(os.unlink, path)
    test.addCleanup(server.close)
    return server, encode_address(path)


def _peer_from_snapshot(info: dict) -> protocol.Peer:
    for path in sessions_dir().iterdir():
        peer = load_peer(path) if path.suffix == ".json" else None
        if peer and peer.session_id == info["sessionId"]:
            return peer
    raise AssertionError("child session was not registered")


if __name__ == "__main__":
    unittest.main()
