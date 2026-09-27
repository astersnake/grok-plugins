"""Local peer for Claude Code cross-session messaging.

The public contract is documented at
https://code.claude.com/docs/en/cross-session-messaging
Claude Code does not publish the frame format. This module follows the
envelope serializer in the local Claude Code 2.1.283 binary (peerProtocol 1):
attribute order is from, from-session, hop-chain, from-name, from-mode,
from-plugin, and a body that does not round-trip loses its attributes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import socket
import stat
import subprocess
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
ENTRYPOINT = "grok-claude-messaging"
MAX_LINE = 1_048_576
MAX_INBOX = 50
# Linux writes at most PIPE_BUF bytes to a FIFO atomically: all of it or nothing.
PIPE_BUF = 4096
SOCKET_PATH_LIMIT = 103
NAME_LIMIT = 64
MODES = ("bypass", "prompting")
TAG = "cross-session-message"

_ADDRESS_UNSAFE = re.compile(r"[^A-Za-z0-9:_/.\\\-]")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
_HOP_CHAIN = re.compile(r"[0-9a-f]{24}(?:,[0-9a-f]{24}){0,31}")
_ENVELOPE = re.compile(
    r"^<cross-session-message"
    r'(?: from="([A-Za-z0-9%:_/.\\\-]+)")?'
    r'(?: from-session="([A-Za-z0-9_-]{1,80})")?'
    r'(?: hop-chain="([0-9a-f]{24}(?:,[0-9a-f]{24}){0,31})")?'
    r'(?: from-name="([^"<>\n\r]+)")?'
    r'(?: from-mode="(bypass|prompting)")?'
    r'(?: from-plugin="([^"<>\n\r]+)")?'
    r">\n([\s\S]*)\n</cross-session-message>$"
)
_PID_JSON = re.compile(r"^[1-9][0-9]*\.json$")
_NAME_STRIPPED = {"Cf", "Cc", "Cs", "Zl", "Zp"}

# Claude Code escapes anything that reads as the closing tag: "<" or a
# lookalike, filler, "/" or a lookalike, filler, then the tag name in any case
# with invisible characters between letters and any dash for "-".
_OPENERS = "<\uff1c\ufe64\u2329\u27e8\u3008\u2039\u02c2\u1438\u276c\u276e\u2770\u29fc\u226e\u227a\u22d6"
_CLOSERS = ">\uff1e\ufe65\u232a\u27e9\u3009\u203a\u02c3\u1433\u276d\u276f\u2771\u29fd\u226f\u227b\u22d7"
_SLASHES = "/\uff0f\u2215\u2044"
_INVISIBLE = (
    r"\u00ad\u034f\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u115f\u1160\u17b4\u17b5"
    r"\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0"
    r"\ufff0-\ufffb\U000110bd\U000110cd\U00013430-\U0001343f\U0001bca0-\U0001bca3"
    r"\U0001d173-\U0001d17a\U00016fe4\U000e0000-\U000e0fff"
    r"\u0300-\u0344\u0346-\u036f\u0483-\u0489\u0591-\u05bd\u05bf\u05c1\u05c2\u05c4\u05c5\u05c7"
    r"\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e4\u06e7\u06e8\u06ea-\u06ed"
    r"\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\u3099\u309a\ufe20-\ufe2f"
    r"\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029"
)
# "_", Unicode Pc and Pd, and the extra dash lookalikes Claude Code lists.
_DASHES = (
    r"_\-\u203f\u2040\u2054\ufe33\ufe34\ufe4d-\ufe4f\uff3f\u058a\u05be\u1400\u1806\u2010-\u2015"
    r"\u2e17\u2e1a\u2e3a\u2e3b\u2e40\u2e5d\u301c\u3030\u30a0\ufe31\ufe32\ufe58\ufe63\uff0d"
    r"\U00010d6e\U00010ead\u2017\u02cd\u07fa\u0640\u2212\u207b\u208b\u02d7\u2796\u2043\u30fc\uff70"
)


def _closing_tag_pattern(tag: str) -> re.Pattern[str]:
    word = r"A-Za-z0-9_\-"
    parts = [
        rf"(?=([^{word}{_OPENERS}{_CLOSERS}{_SLASHES}]*))\1[{_SLASHES}]",
        rf"(?=([^{word}{_OPENERS}{_CLOSERS}]*))\2",
    ]
    for index, char in enumerate(tag):
        if index:
            parts.append(rf"(?=([{_INVISIBLE}]*))\{index + 2}")
        parts.append(f"[{_DASHES}]" if char in "-_" else re.escape(char))
    return re.compile(rf"[{_OPENERS}](?!\\)(?={''.join(parts)}(?:[^{word}]|$))", re.IGNORECASE)


_CLOSING_TAG = _closing_tag_pattern(TAG)


class ProtocolError(Exception):
    pass


def config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def sessions_dir() -> Path:
    return config_dir() / "sessions"


def grok_home() -> Path:
    override = os.environ.get("GROK_HOME")
    if override:
        return Path(override)
    return Path.home() / ".grok"


def grok_permission_class() -> str:
    """This Grok session's permission class as Claude Code compares it.

    Claude Code holds a message whose from-mode differs from its own class, and
    holds one with no from-mode when it bypasses prompts. Grok records
    yolo_mode (always-approve) on every turn_started event, and a tool call runs
    inside a turn, so the latest event is the current mode. Returns "" when the
    mode cannot be read, so the envelope asserts none rather than guessing.
    """
    session = os.environ.get("GROK_SESSION_ID", "")
    if not _SESSION_ID.fullmatch(session):
        return ""
    for events in (grok_home() / "sessions").glob(f"*/{session}/events.jsonl"):
        try:
            for line in _lines_from_end(events):
                if '"turn_started"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") != "turn_started" or event.get("session_id") != session:
                    continue
                if isinstance(event.get("yolo_mode"), bool):
                    return "bypass" if event["yolo_mode"] else "prompting"
        except OSError:
            continue
    return ""


def parity_hold(from_mode: str, own_class: str) -> bool:
    """Claude Code's inbound rule: hold unless both sides are in the same permission class.

    With no from-mode, only a bypassing receiver holds. An unknown own class holds.
    """
    if not own_class:
        return True
    if from_mode:
        return from_mode != own_class
    return own_class == "bypass"


def _lines_from_end(path: Path, block: int = 65536):
    with open(path, "rb") as handle:
        position = handle.seek(0, os.SEEK_END)
        tail = b""
        while position > 0:
            size = min(block, position)
            position -= size
            handle.seek(position)
            lines = (handle.read(size) + tail).split(b"\n")
            tail = lines[0]
            for line in reversed(lines[1:]):
                yield line.decode(errors="replace")
        if tail:
            yield tail.decode(errors="replace")


def _event_line(row: dict[str, str]) -> bytes:
    """One JSON line; the monitor turns each line into one notification."""
    text = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    # json.dumps escapes \n and \r but leaves these other line breaks raw.
    for char in "\x85  ":
        text = text.replace(char, f"\\u{ord(char):04x}")
    return (text + "\n").encode()


def events_directory() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "grok-claude-messaging"
    return Path(f"/tmp/grok-claude-messaging-{os.getuid()}")


def encode_address(path: str) -> str:
    encoded = _ADDRESS_UNSAFE.sub(
        lambda match: "".join(f"%{byte:02X}" for byte in match.group(0).encode()),
        path,
    )
    return f"uds:{encoded}"


def decode_address(address: str) -> str:
    if not address.startswith("uds:"):
        raise ProtocolError(f"not a uds address: {address}")
    return _percent_decode(address[4:])


def _percent_decode(value: str) -> str:
    out = bytearray()
    index = 0
    while index < len(value):
        if value[index] == "%" and index + 2 < len(value):
            try:
                out.append(int(value[index + 1 : index + 3], 16))
            except ValueError as exc:
                raise ProtocolError(f"bad percent-encoding in {value}") from exc
            index += 3
            continue
        out.extend(value[index].encode())
        index += 1
    return out.decode()


def sanitize_name(name: str) -> str:
    cleaned = name.replace('"', "").replace("<", "").replace(">", "")
    cleaned = "".join(char for char in cleaned if unicodedata.category(char) not in _NAME_STRIPPED).strip()
    chars = list(cleaned)
    if len(chars) > NAME_LIMIT:
        cleaned = "".join(chars[:NAME_LIMIT]) + "…"
    return cleaned


def escape_body(body: str) -> str:
    return _CLOSING_TAG.sub(lambda _match: "<\\", body)


def render_envelope(
    *,
    sender: str,
    body: str,
    from_name: str = "",
    from_session: str = "",
    from_mode: str = "",
    hop_chain: list[str] | None = None,
    from_plugin: str = "",
) -> str:
    attrs: list[str] = []
    if sender:
        attrs.append(f'from="{sender}"')
    if from_session and _SESSION_ID.fullmatch(from_session):
        attrs.append(f'from-session="{from_session}"')
    if hop_chain:
        chain = ",".join(hop_chain)
        if _HOP_CHAIN.fullmatch(chain):
            attrs.append(f'hop-chain="{chain}"')
    name = sanitize_name(from_name)
    if name:
        attrs.append(f'from-name="{name}"')
    if from_mode:
        if from_mode not in MODES:
            raise ProtocolError(f"from-mode must be one of {MODES}")
        attrs.append(f'from-mode="{from_mode}"')
    plugin = sanitize_name(from_plugin)
    if plugin:
        attrs.append(f'from-plugin="{plugin}"')
    prefix = f" {' '.join(attrs)}" if attrs else ""
    return f"<{TAG}{prefix}>\n{escape_body(body)}\n</{TAG}>"


def parse_envelope(text: str) -> dict[str, Any] | None:
    match = _ENVELOPE.fullmatch(text)
    if not match:
        return None
    sender, from_session, hop, from_name, from_mode, from_plugin, body = match.groups()
    hop_chain = hop.split(",") if hop is not None else None
    rendered = render_envelope(
        sender=sender or "",
        body=body or "",
        from_name=from_name or "",
        from_session=from_session or "",
        from_mode=from_mode or "",
        hop_chain=hop_chain,
        from_plugin=from_plugin or "",
    )
    if rendered != text:
        return None
    parsed: dict[str, Any] = {"body": body or ""}
    if sender:
        parsed["from"] = sender
    if from_session:
        parsed["from_session"] = from_session
    if hop_chain is not None:
        parsed["hop_chain"] = hop_chain
    if from_name:
        parsed["from_name"] = from_name
    if from_mode:
        parsed["from_mode"] = from_mode
    if from_plugin:
        parsed["from_plugin"] = from_plugin
    return parsed


def proc_start(pid: int) -> str:
    """Linux process start token: field 22 of /proc/<pid>/stat."""
    text = Path(f"/proc/{pid}/stat").read_text()
    end = text.rfind(")")
    if end < 0:
        raise ProtocolError(f"unreadable /proc/{pid}/stat")
    fields = text[end + 2 :].split()
    if len(fields) < 20:
        raise ProtocolError(f"short /proc/{pid}/stat")
    return fields[19]


def pid_domain() -> str:
    try:
        machine = Path("/etc/machine-id").read_text().strip()
    except OSError:
        machine = ""
    try:
        namespace = os.readlink("/proc/self/ns/pid")
    except OSError:
        namespace = ""
    return f"linux:{machine}:{namespace}"


def socket_directory() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.environ.get("CLAUDE_CODE_TMPDIR")
    if base:
        candidate = Path(base) / "cc-socks"
        probe = str(candidate / f"{os.getpid()}.sock")
        if len(probe.encode()) <= SOCKET_PATH_LIMIT:
            return candidate
    return Path(f"/tmp/cc-socks-{os.getuid()}")


def _require_private_dir(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProtocolError(f"{path} is not a real directory")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProtocolError(f"{path} must be owned by you and mode 0700")


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    _require_private_dir(path)


def _read_nofollow(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
            raise ProtocolError(f"refusing to read {path}")
        return os.read(fd, limit)
    finally:
        os.close(fd)


def key_path(pid: int, socket_path: str) -> Path:
    digest = hashlib.sha256(socket_path.encode()).hexdigest()
    return sessions_dir() / f"{pid}.{digest}.key"


@dataclass
class Peer:
    pid: int
    session_id: str
    socket_path: str
    name: str
    cwd: str
    status: str
    proc_start: str
    entrypoint: str
    kind: str
    raw: dict[str, Any]

    @property
    def address(self) -> str:
        return encode_address(self.socket_path)

    def alive(self) -> bool:
        if self.pid <= 1:
            return False
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        if self.proc_start:
            try:
                if proc_start(self.pid) != self.proc_start:
                    return False
            except OSError:
                return False
        return _socket_accepts(self.socket_path)


def _socket_accepts(path: str, timeout: float = 0.25) -> bool:
    if not path:
        return False
    try:
        info = Path(path).lstat()
    except OSError:
        return False
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def load_peer(path: Path) -> Peer | None:
    match = _PID_JSON.fullmatch(path.name)
    if not match:
        return None
    try:
        raw = json.loads(_read_nofollow(path, 256 * 1024))
    except (OSError, ProtocolError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    pid = raw.get("pid")
    session_id = raw.get("sessionId")
    socket_path = raw.get("messagingSocketPath")
    if not isinstance(pid, int) or f"{pid}.json" != path.name:
        return None
    if not isinstance(session_id, str) or not isinstance(socket_path, str):
        return None
    # Claude Code binds <pid>-<hex>.sock or <hex>.sock when <pid>.sock is taken.
    if not os.path.isabs(socket_path):
        return None
    return Peer(
        pid=pid,
        session_id=session_id,
        socket_path=socket_path,
        name=str(raw.get("name") or ""),
        cwd=str(raw.get("cwd") or ""),
        status=str(raw.get("status") or ""),
        proc_start=str(raw.get("procStart") or ""),
        entrypoint=str(raw.get("entrypoint") or ""),
        kind=str(raw.get("kind") or ""),
        raw=raw,
    )


def list_peers() -> list[Peer]:
    directory = sessions_dir()
    if not directory.is_dir():
        return []
    peers: list[Peer] = []
    for path in directory.iterdir():
        peer = load_peer(path)
        if peer is not None:
            peers.append(peer)
    peers.sort(key=lambda peer: (peer.raw.get("startedAt") or 0, peer.pid))
    return peers


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(payload, separators=(",", ":")).encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _slug(cwd: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", Path(cwd).name.lower()).strip("-") or "task"
    if len(slug) <= 22:
        return slug
    return f"{slug[:14].strip('-')}-{slug[-7:].strip('-')}"


def choose_name(cwd: str, session_id: str, taken: set[str]) -> str:
    folder = _slug(cwd)
    for length in (2, 4, 8):
        candidate = f"grok-{folder}-{session_id[-length:]}"
        if candidate not in taken and len(candidate) <= NAME_LIMIT:
            return candidate
    return f"grok-{folder}-{session_id}"[:NAME_LIMIT]


@dataclass
class Incoming:
    sender: str
    session_id: str
    body: str
    held: bool = False
    announced: bool = False

    def row(self) -> dict[str, Any]:
        row: dict[str, Any] = {"from": self.sender}
        if self.session_id:
            row["sessionId"] = self.session_id
        if self.held:
            row["held"] = True
        row["text"] = self.body
        return row

    def note(self) -> dict[str, str]:
        return {"from": self.sender, "note": f"{len(self.body)} characters; call read_inbox"}


@dataclass
class Receipt:
    orig_msg_id: str
    status: str
    detail: str


@dataclass
class PeerRuntime:
    session_id: str
    name: str
    cwd: str
    socket_path: str
    address: str
    proc_start_token: str
    token: str
    events_path: str
    _server: socket.socket
    _thread: threading.Thread
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _inbox: list[Incoming] = field(default_factory=list)
    _receipts: dict[str, Receipt] = field(default_factory=dict)
    _janitor: subprocess.Popen[bytes] | None = None
    _janitor_fd: int = -1
    _closed: bool = False

    @classmethod
    def start(cls, cwd: str | None = None) -> PeerRuntime:
        cwd = os.path.abspath(cwd or os.getcwd())
        session_id = str(uuid.uuid4())
        token = secrets.token_hex(16)
        start_token = proc_start(os.getpid())
        sock_dir = socket_directory()
        ensure_private_dir(sock_dir)
        ensure_private_dir(sessions_dir())
        ensure_private_dir(events_directory())
        _cleanup_dead_peers()
        events_path = str(events_directory() / f"{os.getpid()}.events")
        with contextlib.suppress(FileNotFoundError):
            os.unlink(events_path)
        os.mkfifo(events_path, 0o600)
        socket_path = str(sock_dir / f"{os.getpid()}.sock")
        if len(socket_path.encode()) > SOCKET_PATH_LIMIT:
            raise ProtocolError(f"socket path is longer than {SOCKET_PATH_LIMIT} bytes")
        if os.path.exists(socket_path):
            raise ProtocolError(f"socket already exists: {socket_path}")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o077)
        try:
            server.bind(socket_path)
        finally:
            os.umask(old_umask)
        os.chmod(socket_path, 0o600)
        server.listen(16)
        server.settimeout(0.5)
        taken = {peer.name for peer in list_peers() if peer.alive()}
        name = choose_name(cwd, session_id, taken)
        address = encode_address(socket_path)
        now = int(time.time() * 1000)
        record = {
            "pid": os.getpid(),
            "sessionId": session_id,
            "cwd": cwd,
            "name": name,
            "nameSource": "derived",
            "startedAt": now,
            "procStart": start_token,
            "peerProtocol": PROTOCOL_VERSION,
            "pidDomain": pid_domain(),
            "kind": "interactive",
            "entrypoint": ENTRYPOINT,
            "messagingSocketPath": socket_path,
            "status": "idle",
            "statusUpdatedAt": now,
            "updatedAt": now,
            "peerFeatures": ["notify_idle"],
            "version": ENTRYPOINT,
        }
        registry = sessions_dir() / f"{os.getpid()}.json"
        _write_private_json(registry, record)
        _write_private_json(
            key_path(os.getpid(), socket_path),
            {"peerToken": token, "procStart": start_token},
        )
        runtime = cls(
            session_id=session_id,
            name=name,
            cwd=cwd,
            socket_path=socket_path,
            address=address,
            proc_start_token=start_token,
            token=token,
            events_path=events_path,
            _server=server,
            _thread=threading.Thread(target=lambda: None),
        )
        runtime._janitor_fd, runtime._janitor = _spawn_janitor(runtime.files)
        runtime._thread = threading.Thread(target=runtime._accept_loop, name="claude-messaging", daemon=True)
        runtime._thread.start()
        return runtime

    @property
    def files(self) -> list[Path]:
        return [
            Path(self.socket_path),
            sessions_dir() / f"{os.getpid()}.json",
            key_path(os.getpid(), self.socket_path),
            Path(self.events_path),
        ]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        self._server.close()
        self._thread.join(timeout=2)
        if self._janitor is not None:
            os.close(self._janitor_fd)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._janitor.wait(timeout=5)
        for path in self.files:
            with contextlib.suppress(OSError):
                path.unlink()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            pending = len(self._inbox)
        return {
            "name": self.name,
            "sessionId": self.session_id,
            "pending": pending,
            "listening": self._push(b""),
            "fromMode": grok_permission_class() or "none",
            "watchCommand": self.watch_command,
        }

    @property
    def watch_command(self) -> str:
        """Read the events FIFO forever: <> keeps a writer open, so cat never sees EOF."""
        return f"cat <>{shlex.quote(self.events_path)}"

    def read_inbox(self, peek: bool = False) -> list[dict[str, str]]:
        with self._lock:
            rows = [item.row() for item in self._inbox]
            if not peek:
                self._inbox.clear()
        return rows

    def _push(self, data: bytes) -> bool:
        """Write to the monitor's FIFO. False when no monitor reads it or it is full."""
        try:
            fd = os.open(self.events_path, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except OSError:
            return False
        try:
            return os.write(fd, data) == len(data) if data else True
        except OSError:
            return False
        finally:
            os.close(fd)

    def wait_receipt(self, msg_id: str, timeout: float) -> Receipt | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                found = self._receipts.get(msg_id)
            if found:
                return found
            time.sleep(0.05)
        return None

    def send(self, peer: Peer, text: str, wait: float = 3.0) -> dict[str, Any]:
        if not text.strip():
            raise ProtocolError("message text is empty")
        # Attest the real mode: a guessed "bypass" would skip the receiver's hold.
        from_mode = grok_permission_class()
        if not peer.alive():
            raise ProtocolError(f"{peer.name or peer.session_id} is not reachable")
        msg_id = str(uuid.uuid4())
        content = render_envelope(
            sender=self.address,
            body=text,
            from_name=self.name,
            from_session=self.session_id,
            from_mode=from_mode,
        )
        frame = {
            "msgV": PROTOCOL_VERSION,
            "msg_id": msg_id,
            "type": "user",
            "priority": "next",
            "from": self.address,
            "session_id": peer.session_id,
            "message": {"role": "user", "content": content},
        }
        line = json.dumps(frame, separators=(",", ":"))
        # Claude Code counts the auth line and newline against its line cap.
        size = AUTH_LINE_LENGTH + len(line) + 1
        if size > MAX_LINE:
            raise ProtocolError(f"message is {size} characters; the cap is {MAX_LINE}")
        _send_lines(peer, [line])
        receipt = self.wait_receipt(msg_id, wait) if wait > 0 else None
        result: dict[str, Any] = {"msgId": msg_id, "to": peer.name, "sessionId": peer.session_id}
        if receipt:
            result["receipt"] = receipt.status
            if receipt.detail:
                result["detail"] = receipt.detail
        else:
            result["receipt"] = "unconfirmed"
        return result

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                # Replays what arrived before a monitor attached, within one tick.
                self._flush()
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection: socket.socket) -> None:
        connection.settimeout(30)
        buffer = b""
        try:
            while not self._stop.is_set():
                if b"\n" not in buffer:
                    chunk = connection.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                    if len(buffer) > MAX_LINE and b"\n" not in buffer:
                        return
                    continue
                line, buffer = buffer.split(b"\n", 1)
                if len(line) > MAX_LINE:
                    return
                self._on_line(line.decode())
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        finally:
            connection.close()

    def _on_line(self, line: str) -> None:
        if not line.strip():
            return
        frame = json.loads(line)
        if not isinstance(frame, dict):
            return
        if frame.get("type") == "auth":
            return
        if frame.get("msgV") != PROTOCOL_VERSION:
            return
        kind = frame.get("type")
        if kind == "user":
            self._on_user(frame)
        elif kind == "control":
            self._on_control(frame)

    def _on_user(self, frame: dict[str, Any]) -> None:
        target = frame.get("session_id")
        msg_id = str(frame.get("msg_id") or "")
        sender = str(frame.get("from") or "")
        if target and target != self.session_id:
            self._receipt(sender, msg_id, "dropped", "session id mismatch")
            return
        message = frame.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content:
            self._receipt(sender, msg_id, "dropped", "empty message")
            return
        parsed = parse_envelope(content) or {}
        body = parsed.get("body", content)
        name = sanitize_name(parsed.get("from_name", "")) or sender
        held = parity_hold(parsed.get("from_mode", ""), grok_permission_class())
        with self._lock:
            self._inbox.append(Incoming(name, parsed.get("from_session", ""), body, held))
            if len(self._inbox) > MAX_INBOX:
                del self._inbox[: len(self._inbox) - MAX_INBOX]
        self._flush()
        # Claude Code sends no receipt for a message it accepts directly.
        # "delivered" means a held message was approved and released.

    def _flush(self) -> None:
        """Hand queued messages to the monitor, oldest first.

        A message the monitor got in full leaves the inbox. One too long to write
        atomically stays for read_inbox and is announced once. Stops at the first
        failed write: no monitor, or one that is not draining.
        """
        with self._lock:
            kept: list[Incoming] = []
            for index, item in enumerate(self._inbox):
                if item.announced:
                    kept.append(item)
                    continue
                line = _event_line(item.row())
                if len(line) <= PIPE_BUF and self._push(line):
                    continue
                if len(line) > PIPE_BUF and self._push(_event_line(item.note())):
                    item.announced = True
                    kept.append(item)
                    continue
                kept.extend(self._inbox[index:])
                break
            self._inbox = kept

    def _on_control(self, frame: dict[str, Any]) -> None:
        action = frame.get("action")
        sender = str(frame.get("from") or "")
        if action == "peer_message_status":
            orig = str(frame.get("orig_msg_id") or "")
            status = str(frame.get("status") or "")
            detail = str(frame.get("status_detail") or frame.get("reason") or frame.get("drop_reason") or "")
            # Claude Code sends "refused" on the wire as expired + status_detail.
            if status == "expired" and frame.get("status_detail") == "refused":
                status, detail = "refused", str(frame.get("reason") or "")
            if orig and status:
                with self._lock:
                    self._receipts[orig] = Receipt(orig, status, detail)
            return
        if action == "notify_when_idle":
            msg_id = str(frame.get("msg_id") or "")
            self._send_idle_notice(sender, msg_id)
            return
        # peer_idle_notice answers a notify_when_idle this peer never sends.

    def _receipt(self, sender: str, msg_id: str, status: str, detail: str) -> None:
        if not sender.startswith("uds:") or not msg_id:
            return
        frame: dict[str, Any] = {
            "msgV": PROTOCOL_VERSION,
            "type": "control",
            "action": "peer_message_status",
            "status": status,
            "orig_msg_id": msg_id,
            "from": self.address,
        }
        if detail:
            frame["status_detail"] = detail
        _send_raw(sender, frame)

    def _send_idle_notice(self, sender: str, msg_id: str) -> None:
        if not sender.startswith("uds:") or not msg_id:
            return
        _send_raw(
            sender,
            {
                "msgV": PROTOCOL_VERSION,
                "type": "control",
                "action": "peer_idle_notice",
                "msg_id": str(uuid.uuid4()),
                "orig_msg_id": msg_id,
                "from": self.address,
                "state": "idle",
                "finished_at": int(time.time() * 1000),
                "from_mode": "prompting",
            },
        )


def _spawn_janitor(paths: list[Path]) -> tuple[int, subprocess.Popen[bytes]]:
    """Remove this peer's files once this process is gone, even after SIGKILL.

    Grok stops a stdio MCP server by killing its process group, so no handler
    here runs. The janitor sits in its own session, outside that group, and
    blocks on a pipe whose only writer is this process.
    """
    read_end, write_end = os.pipe()
    try:
        janitor = subprocess.Popen(
            ["sh", "-c", 'read -r _; rm -f -- "$@"', "claude-messaging-janitor", *map(str, paths)],
            stdin=read_end,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        os.close(read_end)
    return write_end, janitor


def _cleanup_dead_peers() -> None:
    for peer in list_peers():
        if peer.entrypoint != ENTRYPOINT or peer.pid == os.getpid():
            continue
        try:
            os.kill(peer.pid, 0)
            continue
        except OSError:
            pass
        for path in (
            sessions_dir() / f"{peer.pid}.json",
            key_path(peer.pid, peer.socket_path),
            Path(peer.socket_path),
            events_directory() / f"{peer.pid}.events",
        ):
            try:
                path.unlink()
            except OSError:
                pass


def _lookup_token(peer: Peer) -> str | None:
    path = key_path(peer.pid, peer.socket_path)
    try:
        raw = json.loads(_read_nofollow(path, 4096))
    except (OSError, ProtocolError, json.JSONDecodeError):
        return None
    token = raw.get("peerToken") if isinstance(raw, dict) else None
    recorded = raw.get("procStart") if isinstance(raw, dict) else None
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        return None
    if peer.proc_start and recorded and recorded != peer.proc_start:
        return None
    return token


def _auth_line(token: str) -> str:
    return json.dumps({"type": "auth", "token": token}, separators=(",", ":")) + "\n"


AUTH_LINE_LENGTH = len(_auth_line("0" * 32))


def _send_lines(peer: Peer, lines: list[str]) -> None:
    token = _lookup_token(peer)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(5)
        sock.connect(peer.socket_path)
        payload = _auth_line(token) if token else ""
        payload += "".join(f"{line}\n" for line in lines)
        sock.sendall(payload.encode())
        time.sleep(0.15)
        sock.shutdown(socket.SHUT_WR)
    finally:
        sock.close()


def _send_raw(address: str, frame: dict[str, Any]) -> None:
    try:
        path = decode_address(address)
    except ProtocolError:
        return
    line = json.dumps(frame, separators=(",", ":"))
    if len(line) > MAX_LINE:
        return
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        sock.connect(path)
        sock.sendall((line + "\n").encode())
        time.sleep(0.05)
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        return
    finally:
        sock.close()


def resolve_peer(name: str = "", session_id: str = "", ignore_pid: int | None = None) -> Peer:
    live = [peer for peer in list_peers() if peer.alive() and peer.pid != ignore_pid]
    if session_id:
        matches = [peer for peer in live if peer.session_id == session_id]
    elif name:
        matches = [peer for peer in live if peer.name == name]
    else:
        raise ProtocolError("pass a session name or session id")
    if not matches:
        raise ProtocolError(f"no reachable session matches {session_id or name!r}")
    if len(matches) > 1:
        names = ", ".join(f"{peer.name} ({peer.session_id})" for peer in matches)
        raise ProtocolError(f"more than one session matches: {names}")
    return matches[0]


def describe_peers(self_pid: int) -> list[dict[str, Any]]:
    rows = []
    for peer in list_peers():
        if peer.pid == self_pid or not peer.alive():
            continue
        rows.append({"name": peer.name, "sessionId": peer.session_id, "cwd": peer.cwd, "status": peer.status})
    return rows
