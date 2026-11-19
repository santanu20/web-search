from __future__ import annotations

import socket
import socketserver
import threading
from urllib.parse import urlsplit

import pytest

from conftest import ws


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request.recv(16)
        if data:
            self.request.sendall(data)


def _read_proxy_response(sock):
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) < 8192:
        chunk = sock.recv(1024)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def test_pinned_connect_proxy_connects_to_resolver_validated_ip():
    upstream = socketserver.TCPServer(("127.0.0.1", 0), _EchoHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    port = upstream.server_address[1]
    resolved_urls = []

    class Resolver:
        def resolve_and_validate(self, url):
            resolved_urls.append(url)
            parsed = urlsplit(url)
            return ws.ResolvedTarget(
                original_url=url,
                final_url=url,
                scheme="https",
                host=parsed.hostname,
                port=parsed.port,
                resolved_ips=("127.0.0.1",),
                is_trusted=False,
            )

    proxy = ws._PinnedConnectProxy(Resolver())
    proxy.start()
    try:
        with socket.create_connection(("127.0.0.1", proxy.server.server_address[1]), timeout=3) as client:
            client.settimeout(3)
            client.sendall(
                f"CONNECT example.test:{port} HTTP/1.1\r\n"
                f"Host: example.test:{port}\r\n\r\n".encode("ascii")
            )
            response = _read_proxy_response(client)
            assert response.startswith(b"HTTP/1.1 200 Connection Established")
            client.sendall(b"ping")
            assert client.recv(4) == b"ping"

        assert resolved_urls == [f"https://example.test:{port}/"]
        assert proxy.server.last_connect_ips == ["127.0.0.1"]
    finally:
        proxy.close()
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=2)


def test_pinned_connect_proxy_blocks_ssrf_destination_before_connect():
    class Resolver:
        def resolve_and_validate(self, url):
            raise ws.SSRFBlocked(url, "private address", ["127.0.0.1"])

    proxy = ws._PinnedConnectProxy(Resolver())
    proxy.start()
    try:
        with socket.create_connection(("127.0.0.1", proxy.server.server_address[1]), timeout=3) as client:
            client.settimeout(3)
            client.sendall(
                b"CONNECT metadata.internal:443 HTTP/1.1\r\n"
                b"Host: metadata.internal:443\r\n\r\n"
            )
            response = _read_proxy_response(client)
            assert response.startswith(b"HTTP/1.1 403 Destination Blocked")
        assert proxy.server.last_connect_ips == []
        assert isinstance(proxy.server.last_policy_error, ws.SSRFBlocked)
    finally:
        proxy.close()


def test_pinned_connect_proxy_rejects_plain_http_proxy_requests():
    class Resolver:
        def resolve_and_validate(self, url):
            raise AssertionError("non-CONNECT request must not resolve a destination")

    proxy = ws._PinnedConnectProxy(Resolver())
    proxy.start()
    try:
        with socket.create_connection(("127.0.0.1", proxy.server.server_address[1]), timeout=3) as client:
            client.settimeout(3)
            client.sendall(
                b"GET http://example.test/path HTTP/1.1\r\n"
                b"Host: example.test\r\n\r\n"
            )
            response = _read_proxy_response(client)
            assert response.startswith(b"HTTP/1.1 405 HTTPS CONNECT Required")
    finally:
        proxy.close()


def test_pinned_proxy_enforces_aggregate_byte_budget():
    class Resolver:
        def resolve_and_validate(self, url):
            raise AssertionError("no request should be made")

    server = ws._PinnedConnectProxyServer(
        ("127.0.0.1", 0),
        Resolver(),
        max_tunnel_bytes=100,
        max_total_bytes=10,
        max_tunnel_seconds=2,
    )
    try:
        assert server.reserve_bytes(7, "example.test") is True
        assert server.reserve_bytes(4, "example.test") is False
        assert isinstance(server.last_budget_error, ws.BudgetExceeded)
        assert server.last_budget_error.budget == "MAX_BROWSER_PROXY_BYTES"
    finally:
        server.server_close()
