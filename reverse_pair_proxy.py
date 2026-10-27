#!/usr/bin/env python3
"""
Reverse PairProxy.

Linux broker:
  - exposes one public proxy port for SOCKS5 and HTTP proxy clients
  - accepts Windows agent connections on a separate TLS port

Windows agent:
  - keeps a pool of outbound TLS connections to Linux
  - dials requested target host:port from the Windows side

Third party usage:
  SOCKS5/HTTP proxy address = Linux public IP, proxy port = 27499.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import queue
import random
import select
import socket
import ssl
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit


MAGIC_AGENT = "PAIRPROXY_AGENT/1"
BUFFER_SIZE = 64 * 1024
HEADER_LIMIT = 1024 * 1024


class ProxyError(Exception):
    pass


@dataclass
class AgentChannel:
    conn: socket.socket
    peer: Tuple[str, int]
    created_at: float
    last_checked_at: float


class AgentPool:
    def __init__(self, max_idle: int = 2048) -> None:
        self._queue: "queue.Queue[AgentChannel]" = queue.Queue(maxsize=max_idle)

    def add(self, channel: AgentChannel) -> bool:
        try:
            self._queue.put_nowait(channel)
            return True
        except queue.Full:
            return False

    def acquire(self, timeout: float) -> Optional[AgentChannel]:
        try:
            return self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def acquire_nowait(self) -> Optional[AgentChannel]:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def size(self) -> int:
        return self._queue.qsize()


def setup_logging(level: str, log_file: str = "") -> None:
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def normalize_fingerprint(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch in "0123456789abcdef")


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("connection closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_line(sock: socket.socket, limit: int = 8192) -> bytes:
    data = bytearray()
    while True:
        ch = sock.recv(1)
        if not ch:
            raise EOFError("connection closed while reading line")
        if ch == b"\n":
            return bytes(data)
        data += ch
        if len(data) > limit:
            raise ProxyError("line too long")


def send_json(sock: socket.socket, obj: dict) -> None:
    payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    sock.sendall(payload + b"\n")


def recv_json(sock: socket.socket) -> dict:
    line = read_line(sock)
    return json.loads(line.decode("utf-8"))


def parse_bind(value: str) -> Tuple[str, int]:
    if value.startswith("["):
        end = value.find("]")
        if end == -1 or end + 2 > len(value) or value[end + 1] != ":":
            raise ValueError(f"bad bind address: {value}")
        return value[1:end], int(value[end + 2 :])
    host, port = value.rsplit(":", 1)
    return host, int(port)


def parse_host_port(value: str, default_port: int) -> Tuple[str, int]:
    value = value.strip()
    if not value:
        raise ProxyError("empty host")
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            raise ProxyError(f"bad IPv6 host: {value}")
        host = value[1:end]
        if end + 1 == len(value):
            return host, default_port
        if value[end + 1] != ":":
            raise ProxyError(f"bad host: {value}")
        return host, int(value[end + 2 :])
    if ":" in value:
        host, maybe_port = value.rsplit(":", 1)
        if maybe_port.isdigit():
            return host, int(maybe_port)
    return value, default_port


def socket_family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def make_listener(bind: str) -> socket.socket:
    host, port = parse_bind(bind)
    listener = socket.socket(socket_family(host), socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(512)
    return listener


def enable_tcp_keepalive(sock: socket.socket, args: argparse.Namespace) -> None:
    if not getattr(args, "tcp_keepalive", True):
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return

    idle = int(getattr(args, "keepalive_idle", 60))
    interval = int(getattr(args, "keepalive_interval", 15))
    count = int(getattr(args, "keepalive_count", 4))

    for name, value in (
        ("TCP_KEEPIDLE", idle),
        ("TCP_KEEPINTVL", interval),
        ("TCP_KEEPCNT", count),
    ):
        option = getattr(socket, name, None)
        if option is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
        except OSError:
            pass

    ioctl_value = getattr(socket, "SIO_KEEPALIVE_VALS", None)
    if ioctl_value is not None:
        try:
            sock.ioctl(ioctl_value, (1, idle * 1000, interval * 1000))
        except OSError:
            pass


def close_quietly(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.close()
    except OSError:
        pass


def relay(left: socket.socket, right: socket.socket, idle_timeout: float) -> None:
    sockets = [left, right]
    last_activity = time.monotonic()
    try:
        while sockets:
            timeout = None
            if idle_timeout > 0:
                elapsed = time.monotonic() - last_activity
                remaining = idle_timeout - elapsed
                if remaining <= 0:
                    logging.debug("relay idle timeout after %.1fs", elapsed)
                    return
                timeout = min(remaining, 60.0)
            readable, _, _ = select.select(sockets, [], [], timeout)
            if not readable:
                continue
            last_activity = time.monotonic()
            for src in readable:
                dst = right if src is left else left
                try:
                    data = src.recv(BUFFER_SIZE)
                    if not data:
                        return
                    dst.sendall(data)
                except OSError:
                    return
    finally:
        close_quietly(left)
        close_quietly(right)


def make_agent_tls_client(raw: socket.socket, args: argparse.Namespace) -> socket.socket:
    if not args.agent_tls:
        return raw

    ctx = ssl.create_default_context()
    if args.ca_file:
        ctx.load_verify_locations(args.ca_file)
    if args.tls_no_verify or args.tls_fingerprint:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    server_name = args.tls_server_name or args.server
    wrapped = ctx.wrap_socket(raw, server_hostname=server_name)

    if args.tls_fingerprint:
        cert = wrapped.getpeercert(binary_form=True)
        actual = hashlib.sha256(cert).hexdigest()
        expected = normalize_fingerprint(args.tls_fingerprint)
        if actual != expected:
            wrapped.close()
            raise ProxyError(f"TLS fingerprint mismatch: got {actual}, want {expected}")

    return wrapped


def check_proxy_auth(args: argparse.Namespace, username: str, password: str) -> bool:
    if not args.proxy_user and not args.proxy_pass:
        return True
    return username == args.proxy_user and password == args.proxy_pass


def require_http_auth(args: argparse.Namespace, lines: list[str]) -> bool:
    if not args.proxy_user and not args.proxy_pass:
        return True
    prefix = "proxy-authorization:"
    for line in lines[1:]:
        if not line.lower().startswith(prefix):
            continue
        value = line.split(":", 1)[1].strip()
        scheme, _, encoded = value.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except Exception:
            return False
        username, sep, password = decoded.partition(":")
        return bool(sep) and check_proxy_auth(args, username, password)
    return False


def send_http_auth_required(client: socket.socket) -> None:
    client.sendall(
        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
        b'Proxy-Authenticate: Basic realm="PairProxy"\r\n'
        b"Content-Length: 0\r\n"
        b"Connection: close\r\n\r\n"
    )


def send_socks_reply(client: socket.socket, code: int) -> None:
    client.sendall(b"\x05" + bytes([code]) + b"\x00\x01\x00\x00\x00\x00\x00\x00")


def authenticate_socks(client: socket.socket, args: argparse.Namespace) -> bool:
    head = recv_exact(client, 2)
    if head[0] != 5:
        raise ProxyError("not socks5")
    methods = recv_exact(client, head[1])

    auth_required = bool(args.proxy_user or args.proxy_pass)
    if auth_required:
        if b"\x02" not in methods:
            client.sendall(b"\x05\xff")
            return False
        client.sendall(b"\x05\x02")
        version = recv_exact(client, 1)[0]
        if version != 1:
            return False
        ulen = recv_exact(client, 1)[0]
        username = recv_exact(client, ulen).decode("utf-8", "replace")
        plen = recv_exact(client, 1)[0]
        password = recv_exact(client, plen).decode("utf-8", "replace")
        if check_proxy_auth(args, username, password):
            client.sendall(b"\x01\x00")
            return True
        client.sendall(b"\x01\x01")
        return False

    if b"\x00" not in methods:
        client.sendall(b"\x05\xff")
        return False
    client.sendall(b"\x05\x00")
    return True


def read_socks_target(client: socket.socket) -> Tuple[str, int]:
    req = recv_exact(client, 4)
    version, command, _reserved, atyp = req
    if version != 5 or command != 1:
        send_socks_reply(client, 7)
        raise ProxyError("unsupported SOCKS5 command")

    if atyp == 1:
        target_host = socket.inet_ntoa(recv_exact(client, 4))
    elif atyp == 3:
        size = recv_exact(client, 1)[0]
        target_host = recv_exact(client, size).decode("idna")
    elif atyp == 4:
        target_host = socket.inet_ntop(socket.AF_INET6, recv_exact(client, 16))
    else:
        send_socks_reply(client, 8)
        raise ProxyError("unsupported SOCKS5 address type")

    target_port = struct.unpack("!H", recv_exact(client, 2))[0]
    return target_host, target_port


def read_http_head(client: socket.socket) -> Tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = client.recv(4096)
        if not chunk:
            raise EOFError("connection closed")
        data += chunk
        if len(data) > HEADER_LIMIT:
            raise ProxyError("HTTP header too large")
    marker = data.index(b"\r\n\r\n") + 4
    return bytes(data[:marker]), bytes(data[marker:])


def rewrite_http_request(head: bytes, args: argparse.Namespace) -> Tuple[str, int, bytes, bool]:
    text = head.decode("iso-8859-1")
    lines = text.split("\r\n")
    if not lines or not lines[0]:
        raise ProxyError("empty HTTP request")
    if not require_http_auth(args, lines):
        raise PermissionError("proxy authentication required")

    parts = lines[0].split(" ", 2)
    if len(parts) != 3:
        raise ProxyError("bad HTTP request line")
    method, target, version = parts
    is_connect = method.upper() == "CONNECT"

    if is_connect:
        host, port = parse_host_port(target, 443)
        return host, port, b"", True

    host = ""
    port = 80
    new_target = target
    parsed = urlsplit(target)
    if parsed.scheme and parsed.netloc:
        if parsed.scheme.lower() not in ("http", "https"):
            raise ProxyError(f"unsupported scheme: {parsed.scheme}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        path = parsed.path or "/"
        new_target = urlunsplit(("", "", path, parsed.query, ""))
    else:
        for line in lines[1:]:
            if line.lower().startswith("host:"):
                host_value = line.split(":", 1)[1].strip()
                host, port = parse_host_port(host_value, 80)
                break

    if not host:
        raise ProxyError("missing Host header")

    clean_lines = [f"{method} {new_target} {version}"]
    for line in lines[1:]:
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("proxy-connection:") or lower.startswith("proxy-authorization:"):
            continue
        clean_lines.append(line)

    rewritten = ("\r\n".join(clean_lines) + "\r\n\r\n").encode("iso-8859-1")
    return host, port, rewritten, False


def request_agent_channel(pool: AgentPool, args: argparse.Namespace, host: str, port: int) -> socket.socket:
    deadline = time.time() + args.agent_wait_timeout
    last_error = ""
    while time.time() < deadline:
        channel = pool.acquire(deadline - time.time())
        if channel is None:
            break
        conn = channel.conn
        try:
            remaining = max(0.5, min(args.connect_timeout, deadline - time.time()))
            conn.settimeout(remaining)
            send_json(conn, {"host": host, "port": int(port)})
            response = recv_json(conn)
            if not response.get("ok"):
                raise ProxyError(response.get("error", "agent rejected target"))
            conn.settimeout(None)
            return conn
        except Exception as exc:
            last_error = str(exc)
            close_quietly(conn)
            continue
    detail = f": {last_error}" if last_error else ""
    raise ProxyError(f"no usable Windows agent available idle={pool.size()}{detail}")


def probe_agent_channel(channel: AgentChannel, args: argparse.Namespace) -> bool:
    conn = channel.conn
    try:
        conn.settimeout(max(0.5, args.agent_probe_timeout))
        send_json(conn, {"ping": True})
        response = recv_json(conn)
        if not response.get("ok") or not response.get("pong"):
            raise ProxyError("bad agent probe response")
        channel.last_checked_at = time.time()
        conn.settimeout(None)
        return True
    except Exception as exc:
        logging.warning("agent probe failed for %s:%s: %s", channel.peer[0], channel.peer[1], exc)
        close_quietly(conn)
        return False


def reap_agent_pool(pool: AgentPool, args: argparse.Namespace) -> None:
    while True:
        time.sleep(max(1.0, args.agent_probe_interval))
        now = time.time()
        total = pool.size()
        kept = 0
        closed = 0
        probed = 0
        recycled = 0
        # Do not sweep the whole pool at once. A probe timeout blocks this
        # thread, and removing too many channels would make the idle pool look empty.
        probe_budget = max(1, min(total, args.agent_probe_per_cycle))
        recycle_budget = max(1, min(total, args.agent_recycle_per_cycle))

        for _ in range(probe_budget):
            channel = pool.acquire_nowait()
            if channel is None:
                break

            lifetime = now - channel.created_at
            since_check = now - channel.last_checked_at
            recycle = (
                args.agent_max_idle_lifetime > 0
                and lifetime >= args.agent_max_idle_lifetime
                and recycled < recycle_budget
            )
            should_probe = args.agent_probe_interval > 0 and since_check >= args.agent_probe_interval

            if recycle:
                close_quietly(channel.conn)
                closed += 1
                recycled += 1
                continue

            if should_probe:
                probed += 1
                if not probe_agent_channel(channel, args):
                    closed += 1
                    continue

            if pool.add(channel):
                kept += 1
            else:
                close_quietly(channel.conn)
                closed += 1

        if probed or closed:
            logging.info(
                "agent reaper probed=%d kept=%d closed=%d recycled=%d idle=%d probe_budget=%d",
                probed,
                kept,
                closed,
                recycled,
                pool.size(),
                probe_budget,
            )


def handle_socks_public(client: socket.socket, peer: Tuple[str, int], args: argparse.Namespace, pool: AgentPool) -> None:
    agent = None
    try:
        if not authenticate_socks(client, args):
            return
        host, port = read_socks_target(client)
        agent = request_agent_channel(pool, args, host, port)
        send_socks_reply(client, 0)
        client.settimeout(None)
        logging.info("socks %s:%s -> windows -> %s:%d", peer[0], peer[1], host, port)
        relay(client, agent, args.relay_idle_timeout)
    except Exception as exc:
        logging.warning("socks failed from %s:%s: %s", peer[0], peer[1], exc)
        try:
            send_socks_reply(client, 1)
        except Exception:
            pass
        close_quietly(agent)
        close_quietly(client)


def handle_http_public(client: socket.socket, peer: Tuple[str, int], args: argparse.Namespace, pool: AgentPool) -> None:
    agent = None
    try:
        head, rest = read_http_head(client)
        try:
            host, port, rewritten, is_connect = rewrite_http_request(head, args)
        except PermissionError:
            send_http_auth_required(client)
            return

        agent = request_agent_channel(pool, args, host, port)
        client.settimeout(None)
        logging.info("http %s:%s -> windows -> %s:%d", peer[0], peer[1], host, port)
        if is_connect:
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            agent.sendall(rewritten + rest)
        relay(client, agent, args.relay_idle_timeout)
    except Exception as exc:
        logging.warning("http failed from %s:%s: %s", peer[0], peer[1], exc)
        try:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
        except Exception:
            pass
        close_quietly(agent)
        close_quietly(client)


def handle_public_client(client: socket.socket, peer: Tuple[str, int], args: argparse.Namespace, pool: AgentPool) -> None:
    try:
        client.settimeout(args.public_handshake_timeout)
        enable_tcp_keepalive(client, args)
        first = client.recv(1, socket.MSG_PEEK)
        if first == b"\x05":
            handle_socks_public(client, peer, args, pool)
        else:
            handle_http_public(client, peer, args, pool)
    except Exception as exc:
        logging.warning("public connection failed from %s:%s: %s", peer[0], peer[1], exc)
        close_quietly(client)


def handle_agent_registration(conn: socket.socket, peer: Tuple[str, int], args: argparse.Namespace, pool: AgentPool) -> None:
    added = False
    try:
        conn.settimeout(args.agent_handshake_timeout)
        hello = recv_json(conn)
        if hello.get("magic") != MAGIC_AGENT or hello.get("token") != args.agent_token:
            send_json(conn, {"ok": False, "error": "auth failed"})
            raise ProxyError("agent auth failed")
        send_json(conn, {"ok": True})
        conn.settimeout(None)
        now = time.time()
        added = pool.add(AgentChannel(conn=conn, peer=peer, created_at=now, last_checked_at=now))
        if not added:
            send_json(conn, {"ok": False, "error": "agent pool full"})
            raise ProxyError("agent pool full")
        logging.debug("agent ready %s:%s idle=%d", peer[0], peer[1], pool.size())
    except Exception as exc:
        logging.warning("agent registration failed from %s:%s: %s", peer[0], peer[1], exc)
        if not added:
            close_quietly(conn)


def make_agent_tls_server_context(args: argparse.Namespace) -> Optional[ssl.SSLContext]:
    if not args.agent_tls:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)
    return context


def dispatch_broker_connection(
    conn: socket.socket,
    peer: Tuple[str, int],
    args: argparse.Namespace,
    pool: AgentPool,
    tls_context: Optional[ssl.SSLContext],
) -> None:
    try:
        conn.settimeout(args.public_handshake_timeout)
        enable_tcp_keepalive(conn, args)
        first = conn.recv(1, socket.MSG_PEEK)
        is_tls_agent = first in (b"\x14", b"\x15", b"\x16", b"\x17")
        if is_tls_agent:
            if tls_context is None:
                raise ProxyError("TLS agent connection is disabled")
            conn = tls_context.wrap_socket(conn, server_side=True)
            thread = threading.Thread(
                target=handle_agent_registration,
                args=(conn, peer, args, pool),
                daemon=True,
            )
        else:
            thread = threading.Thread(
                target=handle_public_client,
                args=(conn, peer, args, pool),
                daemon=True,
            )
        thread.start()
    except Exception as exc:
        logging.warning("broker connection failed from %s:%s: %s", peer[0], peer[1], exc)
        close_quietly(conn)


def run_agent_listener(args: argparse.Namespace, pool: AgentPool) -> None:
    listener = make_listener(f"{args.agent_bind}:{args.agent_port}")
    tls_context = make_agent_tls_server_context(args)

    logging.info("agent listener on %s:%d tls=%s", args.agent_bind, args.agent_port, bool(args.agent_tls))
    while True:
        conn, peer = listener.accept()
        enable_tcp_keepalive(conn, args)
        if tls_context is not None:
            try:
                conn = tls_context.wrap_socket(conn, server_side=True)
            except Exception as exc:
                logging.warning("agent TLS handshake failed from %s:%s: %s", peer[0], peer[1], exc)
                close_quietly(conn)
                continue
        thread = threading.Thread(target=handle_agent_registration, args=(conn, peer, args, pool), daemon=True)
        thread.start()


def run_shared_listener(args: argparse.Namespace, pool: AgentPool) -> None:
    listener = make_listener(f"{args.proxy_bind}:{args.proxy_port}")
    tls_context = make_agent_tls_server_context(args)
    logging.info(
        "shared public/agent listener on %s:%d public-auth=%s agent-tls=%s",
        args.proxy_bind,
        args.proxy_port,
        "enabled" if args.proxy_user or args.proxy_pass else "disabled",
        bool(args.agent_tls),
    )
    while True:
        conn, peer = listener.accept()
        thread = threading.Thread(
            target=dispatch_broker_connection,
            args=(conn, peer, args, pool, tls_context),
            daemon=True,
        )
        thread.start()


def run_broker(args: argparse.Namespace) -> None:
    setup_logging(args.log_level, args.log_file)
    pool = AgentPool(max_idle=args.max_idle_agents)
    reaper_thread = threading.Thread(target=reap_agent_pool, args=(pool, args), daemon=True)
    reaper_thread.start()
    if args.agent_port == args.proxy_port:
        run_shared_listener(args, pool)
        return

    agent_thread = threading.Thread(target=run_agent_listener, args=(args, pool), daemon=True)
    agent_thread.start()
    listener = make_listener(f"{args.proxy_bind}:{args.proxy_port}")
    auth_text = "enabled" if args.proxy_user or args.proxy_pass else "disabled"
    logging.info("public proxy on %s:%d auth=%s", args.proxy_bind, args.proxy_port, auth_text)
    while True:
        client, peer = listener.accept()
        thread = threading.Thread(target=handle_public_client, args=(client, peer, args, pool), daemon=True)
        thread.start()


def open_agent_connection(args: argparse.Namespace) -> socket.socket:
    raw = socket.create_connection((args.server, args.agent_port), timeout=args.connect_timeout)
    enable_tcp_keepalive(raw, args)
    raw.settimeout(args.connect_timeout)
    try:
        conn = make_agent_tls_client(raw, args)
        send_json(conn, {"magic": MAGIC_AGENT, "token": args.agent_token})
        response = recv_json(conn)
        if not response.get("ok"):
            raise ProxyError(response.get("error", "agent login rejected"))
        conn.settimeout(None)
        return conn
    except Exception:
        close_quietly(raw)
        raise


def agent_worker(worker_id: int, args: argparse.Namespace) -> None:
    last_reconnect_log = 0.0
    while True:
        channel = None
        remote = None
        try:
            channel = open_agent_connection(args)
            while True:
                job = recv_json(channel)
                if job.get("ping"):
                    send_json(channel, {"ok": True, "pong": True})
                    continue
                break
            host = str(job.get("host", "")).strip()
            port = int(job.get("port", 0))
            if not host or not (1 <= port <= 65535):
                raise ProxyError("bad target from broker")
            try:
                remote = socket.create_connection((host, port), timeout=args.connect_timeout)
                enable_tcp_keepalive(remote, args)
                remote.settimeout(None)
            except Exception as exc:
                send_json(channel, {"ok": False, "error": str(exc)})
                raise
            send_json(channel, {"ok": True})
            logging.info("worker-%d connected %s:%d", worker_id, host, port)
            relay(channel, remote, args.relay_idle_timeout)
        except Exception as exc:
            now = time.monotonic()
            if now - last_reconnect_log >= 30.0:
                logging.warning("worker-%d reconnecting: %s", worker_id, exc)
                last_reconnect_log = now
            close_quietly(remote)
            close_quietly(channel)
            jitter = random.uniform(0.0, args.reconnect_jitter)
            time.sleep(args.reconnect_delay + jitter)


def run_agent(args: argparse.Namespace) -> None:
    setup_logging(args.log_level, args.log_file)
    logging.info(
        "windows agent connecting to %s:%d pool=%d tls=%s",
        args.server,
        args.agent_port,
        args.pool_size,
        bool(args.agent_tls),
    )
    for worker_id in range(1, args.pool_size + 1):
        thread = threading.Thread(target=agent_worker, args=(worker_id, args), daemon=True)
        thread.start()
    while True:
        time.sleep(3600)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Public Linux proxy backed by a Windows reverse agent")
    sub = parser.add_subparsers(dest="mode", required=True)

    def add_common_tuning(p: argparse.ArgumentParser) -> None:
        p.add_argument("--log-file", default="")
        p.add_argument("--relay-idle-timeout", type=float, default=300.0)
        p.add_argument("--keepalive-idle", type=int, default=60)
        p.add_argument("--keepalive-interval", type=int, default=15)
        p.add_argument("--keepalive-count", type=int, default=4)
        p.add_argument("--no-tcp-keepalive", dest="tcp_keepalive", action="store_false")
        p.set_defaults(tcp_keepalive=True)

    broker = sub.add_parser("broker", help="run Linux public proxy broker")
    broker.add_argument("--proxy-bind", default="0.0.0.0")
    broker.add_argument("--proxy-port", type=int, default=27499)
    broker.add_argument("--proxy-user", default="")
    broker.add_argument("--proxy-pass", default="")
    broker.add_argument("--agent-bind", default="0.0.0.0")
    broker.add_argument("--agent-port", type=int, default=27500)
    broker.add_argument("--agent-token", required=True)
    broker.add_argument("--agent-wait-timeout", type=float, default=15.0)
    broker.add_argument("--agent-handshake-timeout", type=float, default=10.0)
    broker.add_argument("--agent-probe-interval", type=float, default=30.0)
    broker.add_argument("--agent-probe-timeout", type=float, default=3.0)
    broker.add_argument("--agent-probe-per-cycle", type=int, default=8)
    broker.add_argument("--agent-max-idle-lifetime", type=float, default=1800.0)
    broker.add_argument("--agent-recycle-per-cycle", type=int, default=8)
    broker.add_argument("--public-handshake-timeout", type=float, default=15.0)
    broker.add_argument("--max-idle-agents", type=int, default=2048)
    broker.add_argument("--connect-timeout", type=float, default=10.0)
    broker.add_argument("--agent-tls", action="store_true")
    broker.add_argument("--cert")
    broker.add_argument("--key")
    broker.add_argument("--log-level", default=os.environ.get("PAIRPROXY_LOG_LEVEL", "INFO"))
    add_common_tuning(broker)
    broker.set_defaults(func=run_broker)

    agent = sub.add_parser("agent", help="run Windows reverse proxy agent")
    agent.add_argument("--server", required=True)
    agent.add_argument("--agent-port", type=int, default=27500)
    agent.add_argument("--agent-token", required=True)
    agent.add_argument("--pool-size", type=int, default=32)
    agent.add_argument("--connect-timeout", type=float, default=10.0)
    agent.add_argument("--reconnect-delay", type=float, default=3.0)
    agent.add_argument("--reconnect-jitter", type=float, default=5.0)
    agent.add_argument("--agent-tls", action="store_true")
    agent.add_argument("--tls-fingerprint", default="")
    agent.add_argument("--tls-server-name", default="")
    agent.add_argument("--tls-no-verify", action="store_true")
    agent.add_argument("--ca-file", default="")
    agent.add_argument("--log-level", default=os.environ.get("PAIRPROXY_LOG_LEVEL", "INFO"))
    add_common_tuning(agent)
    agent.set_defaults(func=run_agent)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.mode == "broker":
        if bool(args.proxy_user) != bool(args.proxy_pass):
            parser.error("--proxy-user and --proxy-pass must be set together")
        if args.agent_tls and (not args.cert or not args.key):
            parser.error("broker --agent-tls requires --cert and --key")
        if not args.agent_token:
            parser.error("--agent-token must not be empty")
        for name in (
            "agent_wait_timeout",
            "agent_handshake_timeout",
            "agent_probe_interval",
            "agent_probe_timeout",
            "public_handshake_timeout",
            "connect_timeout",
            "relay_idle_timeout",
        ):
            if getattr(args, name) <= 0:
                parser.error(f"--{name.replace('_', '-')} must be > 0")
        if args.agent_recycle_per_cycle < 1:
            parser.error("--agent-recycle-per-cycle must be >= 1")
        if args.agent_probe_per_cycle < 1:
            parser.error("--agent-probe-per-cycle must be >= 1")
    if args.mode == "agent":
        if not args.agent_token:
            parser.error("--agent-token must not be empty")
        if args.pool_size < 1:
            parser.error("--pool-size must be >= 1")
        for name in ("connect_timeout", "reconnect_delay", "relay_idle_timeout"):
            if getattr(args, name) <= 0:
                parser.error(f"--{name.replace('_', '-')} must be > 0")
        if args.reconnect_jitter < 0:
            parser.error("--reconnect-jitter must be >= 0")

    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
