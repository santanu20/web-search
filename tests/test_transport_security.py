


def test_curl_resolve_rule_pins_every_validated_address():
    target = ws.ResolvedTarget(
        original_url="https://example.test/",
        final_url="https://example.test/",
        scheme="https",
        host="example.test",
        port=443,
        resolved_ips=("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"),
        is_trusted=False,
    )
    rule = ws._curl_resolve_rule(target)
    assert rule == (
        "+example.test:443:93.184.216.34,"
        "[2606:2800:220:1:248:1893:25c8:1946]"
    )


def test_curl_resolve_rule_skips_ip_literal_targets():
    target = ws.ResolvedTarget(
        original_url="https://[2606:4700:4700::1111]/",
        final_url="https://[2606:4700:4700::1111]/",
        scheme="https",
        host="2606:4700:4700::1111",
        port=443,
        resolved_ips=("2606:4700:4700::1111",),
        is_trusted=False,
    )
    assert ws._curl_resolve_rule(target) is None


def test_curl_fetch_pins_validated_dns_and_uses_bounded_body_callback(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(ws, "_impersonation_rotation", lambda: ["chrome124"])
    target = ws.ResolvedTarget(
        original_url="https://example.test/",
        final_url="https://example.test/",
        scheme="https",
        host="example.test",
        port=443,
        resolved_ips=("93.184.216.34",),
        is_trusted=False,
    )

    class Resolver:
        def resolve_and_validate(self, url):
            assert url == "https://example.test/"
            return target

    monkeypatch.setattr(ws, "get_safe_url_resolver", lambda: Resolver())
    calls = []
    session_options = {}

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        cookies = {}
        encoding = "utf-8"

    class Session:
        def __init__(self, **kwargs):
            session_options.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            kwargs["content_callback"](b"<html><body>" + b"x" * 700 + b"</body></html>")
            return Response()

    fake_module = SimpleNamespace(
        CurlOpt=SimpleNamespace(RESOLVE=999),
        requests=SimpleNamespace(Session=Session),
    )
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_module)

    body, _cookies, content_type = ws._fetch_curl_cffi("https://example.test/")

    assert len(body) > 500
    assert content_type == "text/html"
    assert session_options == {"trust_env": False}
    assert calls[0][1]["curl_options"] == {
        999: ["+example.test:443:93.184.216.34"]
    }
    assert callable(calls[0][1]["content_callback"])


def test_curl_fetch_aborts_oversized_stream_before_buffering(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(ws, "_impersonation_rotation", lambda: ["chrome124"])
    monkeypatch.setattr(ws, "DEFAULT_MAX_RESPONSE_BYTES", 8)
    target = ws.ResolvedTarget(
        original_url="https://example.test/",
        final_url="https://example.test/",
        scheme="https",
        host="example.test",
        port=443,
        resolved_ips=("93.184.216.34",),
        is_trusted=False,
    )

    class Resolver:
        def resolve_and_validate(self, url):
            return target

    monkeypatch.setattr(ws, "get_safe_url_resolver", lambda: Resolver())

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        cookies = {}
        encoding = "utf-8"

    class Session:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            kwargs["content_callback"](b"x" * 9)
            raise AssertionError("callback should abort before response completion")

    monkeypatch.setitem(
        sys.modules,
        "curl_cffi",
        SimpleNamespace(
            CurlOpt=SimpleNamespace(RESOLVE=999),
            requests=SimpleNamespace(Session=Session),
        ),
    )

    with pytest.raises(ws.BudgetExceeded) as exc:
        ws._fetch_curl_cffi("https://example.test/")
    assert exc.value.budget == "MAX_RESPONSE_BYTES"
    assert exc.value.actual == 9


def test_curl_fetch_revalidates_and_repins_every_redirect(monkeypatch):
    import sys
    from types import SimpleNamespace

    monkeypatch.setattr(ws, "_impersonation_rotation", lambda: ["chrome124"])
    first = ws.ResolvedTarget(
        original_url="https://first.example/",
        final_url="https://first.example/",
        scheme="https",
        host="first.example",
        port=443,
        resolved_ips=("93.184.216.34",),
        is_trusted=False,
    )
    second = ws.ResolvedTarget(
        original_url="https://second.example/page",
        final_url="https://second.example/page",
        scheme="https",
        host="second.example",
        port=443,
        resolved_ips=("1.1.1.1",),
        is_trusted=False,
    )

    class Resolver:
        def __init__(self):
            self.validated_redirects = []

        def resolve_and_validate(self, url):
            return first

        def validate_redirect_target(self, url):
            self.validated_redirects.append(url)
            return second

    resolver = Resolver()
    monkeypatch.setattr(ws, "get_safe_url_resolver", lambda: resolver)
    calls = []

    class Response:
        def __init__(self, status, headers):
            self.status_code = status
            self.headers = headers
            self.cookies = {}
            self.encoding = "utf-8"

    class Session:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            calls.append((url, kwargs["curl_options"]))
            if len(calls) == 1:
                return Response(302, {"Location": "https://second.example/page"})
            kwargs["content_callback"](b"<html><body>" + b"y" * 700 + b"</body></html>")
            return Response(200, {"Content-Type": "text/html"})

    monkeypatch.setitem(
        sys.modules,
        "curl_cffi",
        SimpleNamespace(
            CurlOpt=SimpleNamespace(RESOLVE=999),
            requests=SimpleNamespace(Session=Session),
        ),
    )

    body, _cookies, _content_type = ws._fetch_curl_cffi("https://first.example/")

    assert len(body) > 500
    assert resolver.validated_redirects == ["https://second.example/page"]
    assert calls == [
        ("https://first.example/", {999: ["+first.example:443:93.184.216.34"]}),
        ("https://second.example/page", {999: ["+second.example:443:1.1.1.1"]}),
    ]
