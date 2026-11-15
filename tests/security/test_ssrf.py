import io
import socket
import ssl
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

from evo.adapters.tools.web import (UNTRUSTED_NOTICE, WebLimits, WebToolError,
                                    WebTools, _PinnedHTTPS, _request,
                                    normalize_url, resolve_public)
from tests.fixtures.web import Response, Transport, dns


class SSRFTests(unittest.TestCase):
    def test_url_rejection_happens_before_dns_or_request(self):
        resolver = Mock(side_effect=AssertionError("DNS must not run"))
        tool = WebTools("task", resolver=resolver)
        for url in ["http://example.com/", "file:///etc/passwd", "ftp://example.com/",
                    "https://user@example.com/", "https://:pw@example.com/", "https://example.com:80/",
                    "https://example.com:444/", "https://example.com:bad/", "https://localhost/",
                    "https://foo.localhost/", "https://foo.local/", "https://metadata.google.internal/",
                    "https://router.home.arpa/", "https://intranet/", "https://host.lan/",
                    "https://127.0.0.1/", "https://127.1/", "https://2130706433/",
                    "https://0177.0.0.1/", "https://0x7f000001/", "https://10.1.2.3/",
                    "https://172.16.0.1/", "https://192.168.1.1/", "https://169.254.169.254/",
                    "https://100.64.0.1/", "https://0.0.0.0/", "https://224.0.0.1/",
                    "https://192.0.2.1/", "https://192.0.0.8/", "https://240.1.2.3/",
                    "https://[::]/", "https://[::1]/", "https://[fc00::1]/", "https://[fe80::1]/",
                    "https://[::ffff:8.8.8.8]/", "https://[64:ff9b::808:808]/",
                    "https://[2002:0808:0808::1]/", "https://[2001:db8::1]/", "https://[ff02::1]/",
                    "https://[fe80::1%25eth0]/", "https://example.com\\@127.0.0.1/",
                    "https://%31%32%37.0.0.1/", "https://example.com/\r\nHost:localhost",
                    " https://example.com/", "https://example.com/" + "x" * 4096, None]:
            with self.subTest(url=url), self.assertRaises(WebToolError):
                tool.fetch_url(url)
        resolver.assert_not_called()

    def test_canonical_url_and_international_path(self):
        self.assertEqual(normalize_url("HTTPS://Example.COM.:443/中文?q=你好#fragment"),
                         "https://example.com/%E4%B8%AD%E6%96%87?q=%E4%BD%A0%E5%A5%BD")
        self.assertEqual(normalize_url("https://[2606:4700:4700::1111]/"),
                         "https://[2606:4700:4700::1111]/")

    def test_every_dns_address_is_validated(self):
        for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "224.0.0.1", "192.0.2.1"]:
            records = dns("", 1) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
            transport = Mock()
            with self.subTest(address=address), patch("evo.adapters.tools.web._request", transport):
                with self.assertRaises(WebToolError):
                    WebTools("task", resolver=lambda *a: records).fetch_url("https://example.com/")
            transport.assert_not_called()

    def test_dns_empty_family_port_scope_and_count_fail_closed(self):
        for records in [[], dns("", 1) * 17,
                        [(socket.AF_UNIX, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))],
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
                        [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 443, 0, 1))],
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("::1", 443))]]:
            with self.subTest(records=records), self.assertRaises(WebToolError):
                WebTools("task", resolver=lambda *a: records).fetch_url("https://example.com/")

    def test_plain_text_and_html_are_labeled_untrusted_and_closed(self):
        for body, mime in [("hello 世界", "text/plain"),
                           ("<h1>Hello</h1><script>bad()</script><style>secret</style><p>世界 &amp; more</p>", "text/html")]:
            response = Response(body.encode(), headers=[("Content-Type", mime + "; charset=utf-8")])
            with patch("evo.adapters.tools.web._request", Transport(response)):
                page = WebTools("task", resolver=dns).fetch_url("https://example.com/#x")
            self.assertEqual(page.url, "https://example.com/")
            self.assertTrue(page.untrusted)
            self.assertTrue(page.text.startswith(UNTRUSTED_NOTICE))
            self.assertIn("世界", page.text)
            self.assertNotIn("bad()", page.text)
            self.assertNotIn("secret", page.text)
            self.assertTrue(response.closed)

    def test_declared_charset_and_unsupported_binary_or_encoding(self):
        response = Response("café".encode("latin-1"), headers=[("Content-Type", "text/plain; charset=iso-8859-1")])
        with patch("evo.adapters.tools.web._request", Transport(response)):
            self.assertIn("café", WebTools("t", resolver=dns).fetch_url("https://example.com").text)
        for body, headers in [(b"%PDF", [("Content-Type", "application/pdf")]),
                              (b"x", []), (b"x", [("Content-Type", "text/plain")] * 2),
                              (b"x", [("Content-Type", "image/svg+xml")]),
                              (b"x", [("Content-Type", "text/plain"), ("Content-Encoding", "gzip")]),
                              (b"\xff", [("Content-Type", "text/plain")]),
                              (b"a\x00b", [("Content-Type", "text/plain")]),
                              (b"x", [("Content-Type", "text/plain; charset=not-a-codec")])]:
            response = Response(body, headers=headers)
            with self.subTest(headers=headers), patch("evo.adapters.tools.web._request", Transport(response)):
                with self.assertRaises(WebToolError):
                    WebTools("t", resolver=dns).fetch_url("https://example.com/")
            self.assertTrue(response.closed)

    def test_byte_limit_declared_and_streamed_and_truncation(self):
        for body, extra in [(b"12345", []), (b"12345", [("Content-Length", "5")]),
                            (b"12", [("Content-Length", "4")]), (b"1", [("Content-Length", "-1")]),
                            (b"1", [("Content-Length", "1")] * 2),
                            (b"1", [("Transfer-Encoding", "gzip")]),
                            (b"1", [("Transfer-Encoding", "chunked"), ("Content-Length", "1")])]:
            response = Response(body, headers=[("Content-Type", "text/plain")] + extra)
            with self.subTest(extra=extra), patch("evo.adapters.tools.web._request", Transport(response)):
                with self.assertRaises(WebToolError):
                    WebTools("task", resolver=dns, limits=WebLimits(max_response_bytes=4)).fetch_url("https://example.com")
            self.assertTrue(response.closed)
        with patch("evo.adapters.tools.web._request", Transport(Response(b"1234"))):
            self.assertIn("1234", WebTools("t", resolver=dns, limits=WebLimits(max_response_bytes=4)).fetch_url("https://example.com").text)

    def test_redirect_rechecks_dns_and_records_final_url(self):
        first = Response(status=302, headers=[("Location", "/new")])
        second = Response(status=307, headers=[("Location", "https://www.python.org/docs")])
        final = Response()
        resolver = Mock(side_effect=[dns("", 1)] * 3)
        transport = Transport(first, second, final)
        with patch("evo.adapters.tools.web._request", transport):
            page = WebTools("t", resolver=resolver).fetch_url("https://example.com/start")
        self.assertEqual(page.url, "https://www.python.org/docs")
        self.assertEqual(resolver.call_count, 3)
        self.assertTrue(all(r.closed for r in (first, second, final)))

    def test_redirect_to_private_url_http_credentials_or_private_dns_is_rejected(self):
        for destination in ["https://127.0.0.1/", "http://example.com/", "https://u:p@example.com/",
                            "https://host.internal/", "https://example.com:8443/", "https://other.example.com/"]:
            response = Response(status=302, headers=[("Location", destination)])
            resolver = Mock(side_effect=[dns("", 1), dns("", 1) + [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]])
            transport = Transport(response)
            with self.subTest(destination=destination), patch("evo.adapters.tools.web._request", transport):
                with self.assertRaises(WebToolError):
                    WebTools("t", resolver=resolver).fetch_url("https://example.com")
            self.assertEqual(len(transport.calls), 1)
            self.assertTrue(response.closed)

    def test_rebinding_same_host_is_rejected_on_second_hop(self):
        resolver = Mock(side_effect=[dns("", 1), [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]])
        transport = Transport(Response(status=302, headers=[("Location", "/again")]))
        with patch("evo.adapters.tools.web._request", transport):
            with self.assertRaises(WebToolError):
                WebTools("t", resolver=resolver).fetch_url("https://example.com")
        self.assertEqual(len(transport.calls), 1)

    def test_redirect_limits_and_ambiguous_location(self):
        for headers in [[], [("Location", "/x")] * 2, [("Location", "/x")]]:
            response = Response(status=302, headers=headers)
            with patch("evo.adapters.tools.web._request", Transport(response)):
                with self.assertRaises(WebToolError):
                    WebTools("t", resolver=dns, limits=WebLimits(max_redirects=0)).fetch_url("https://example.com")
        transport = Transport(*(Response(status=302, headers=[("Location", "/x")]) for _ in range(4)))
        with patch("evo.adapters.tools.web._request", transport):
            with self.assertRaises(WebToolError):
                WebTools("t", resolver=dns).fetch_url("https://example.com")
        self.assertEqual(len(transport.calls), 4)

    def test_timeout_covers_dns_and_body_and_errors_hide_details(self):
        clock = Mock(return_value=0)
        def slow_dns(*args):
            clock.return_value = 20
            return dns(*args)
        with patch("evo.adapters.tools.web.time.monotonic", clock):
            with self.assertRaisesRegex(WebToolError, "timed out"):
                WebTools("t", resolver=slow_dns).fetch_url("https://example.com")
        response = Response()
        original_read = response.read1
        def slow_read(size):
            clock.return_value = 20
            return original_read(size)
        response.read1 = slow_read
        clock.return_value = 0
        with patch("evo.adapters.tools.web.time.monotonic", clock), patch("evo.adapters.tools.web._request", Transport(response)):
            with self.assertRaisesRegex(WebToolError, "timed out"):
                WebTools("t", resolver=dns).fetch_url("https://example.com")
        for error in [TimeoutError("private-secret"), ssl.SSLCertVerificationError("private-secret"), OSError("private-secret")]:
            with self.subTest(error=type(error)), self.assertRaises(WebToolError) as raised:
                WebTools("t", resolver=Mock(side_effect=error)).fetch_url("https://example.com")
            self.assertNotIn("private-secret", str(raised.exception))

    def test_pinned_connection_uses_numeric_ip_and_original_tls_hostname(self):
        raw, wrapped, context = Mock(), Mock(), Mock()
        context.wrap_socket.return_value = wrapped
        with patch("evo.adapters.tools.web.socket.socket", return_value=raw), patch(
                "evo.adapters.tools.web.ssl.create_default_context", return_value=context), patch(
                "evo.adapters.tools.web.socket.getaddrinfo", side_effect=AssertionError("second lookup")):
            connection = _PinnedHTTPS("example.com", (socket.AF_INET, "93.184.216.34"), time.monotonic() + 10)
            connection.connect()
            raw.connect.assert_called_once_with(("93.184.216.34", 443))
            context.wrap_socket.assert_called_once_with(raw, server_hostname="example.com")
            self.assertIs(connection.sock, wrapped)
            connection.close()
        wrapped.close.assert_called_once()

    def test_tls_failure_closes_raw_socket(self):
        raw, context = Mock(), Mock()
        context.wrap_socket.side_effect = ssl.SSLError("certificate error")
        with patch("evo.adapters.tools.web.socket.socket", return_value=raw), patch(
                "evo.adapters.tools.web.ssl.create_default_context", return_value=context):
            connection = _PinnedHTTPS("example.com", (socket.AF_INET, "93.184.216.34"), time.monotonic() + 10)
            with self.assertRaises(ssl.SSLError):
                connection.connect()
        raw.close.assert_called_once()

    def test_request_uses_safe_headers_closes_and_deadline_covers_detached_socket(self):
        connection, timer = MagicMock(), Mock()
        response = connection.getresponse.return_value.__enter__.return_value
        with patch("evo.adapters.tools.web._PinnedHTTPS", return_value=connection), patch(
                "evo.adapters.tools.web.threading.Timer", return_value=timer) as timer_type:
            with _request("https://example.com/path?q=1", (socket.AF_INET, "93.184.216.34"), time.monotonic() + 10) as result:
                self.assertIs(result, response)
                connection.sock = None
                timer_type.call_args.args[1]()
                connection.transport_socket.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("GET", "/path?q=1"))
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertNotIn("Authorization", kwargs["headers"])
        timer.cancel.assert_called_once()
        connection.close.assert_called_once()

    def test_os_dns_timeout_failure_and_saturation_are_bounded(self):
        gate = threading.Event()
        done = threading.Event()
        def lookup(*args):
            try:
                gate.wait(1)
                return dns("", 1)
            finally:
                done.set()
        with patch("evo.adapters.tools.web._DNS_SLOTS", threading.BoundedSemaphore(1)), patch(
                "evo.adapters.tools.web.socket.getaddrinfo", side_effect=lookup):
            try:
                with self.assertRaisesRegex(WebToolError, "timed out"):
                    resolve_public("example.com", .01)
                with self.assertRaisesRegex(WebToolError, "busy"):
                    resolve_public("example.com", .01)
            finally:
                gate.set()
                self.assertTrue(done.wait(1))
        with patch("evo.adapters.tools.web.socket.getaddrinfo", side_effect=socket.gaierror("private")):
            with self.assertRaisesRegex(WebToolError, "DNS lookup failed"):
                resolve_public("example.com", 1)

    def test_http_error_is_not_success(self):
        for status in [204, 304, 400, 403, 429, 500]:
            with self.subTest(status=status), patch("evo.adapters.tools.web._request", Transport(Response(status=status))):
                with self.assertRaises(WebToolError):
                    WebTools("t", resolver=dns).fetch_url("https://example.com")

    def test_real_http_parser_handles_chunked_body_and_rejects_bad_framing(self):
        wires = [
            (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhello\r\n0\r\n\r\n", True),
            (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nhello", True),
            (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhi", False),
            (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 5\r\n\r\nhi", False),
            (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nX-Large: " + b"x" * 65536 + b"\r\n\r\n", False),
        ]
        for wire, valid in wires:
            raw, tls, context = Mock(), Mock(), Mock()
            stream = io.BytesIO(wire)
            tls.makefile.return_value = stream
            context.wrap_socket.return_value = tls
            with self.subTest(valid=valid), patch("evo.adapters.tools.web.socket.socket", return_value=raw), patch(
                    "evo.adapters.tools.web.ssl.create_default_context", return_value=context):
                if valid:
                    self.assertIn("hello", WebTools("t", resolver=dns).fetch_url("https://example.com").text)
                else:
                    with self.assertRaises(WebToolError):
                        WebTools("t", resolver=dns).fetch_url("https://example.com")
            self.assertTrue(stream.closed)
            tls.close.assert_called_once()

    def test_wall_deadline_interrupts_stalled_headers_and_detached_response_body(self):
        # Connected Unix sockets exercise real blocking reads, without a server
        # or any TCP request; the production TLS handshake is tested separately.
        for headers in [b"HTTP/1.1 200 OK\r\n", b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"]:
            client, server = socket.socketpair()
            server.sendall(headers)
            original_connection = _PinnedHTTPS
            def factory(host, target, deadline):
                connection = original_connection(host, target, deadline)
                def connect():
                    client.settimeout(1)
                    connection.sock = client
                    connection.transport_socket = client
                connection.connect = connect
                return connection
            started = time.monotonic()
            try:
                with patch("evo.adapters.tools.web._PinnedHTTPS", side_effect=factory):
                    with self.assertRaises(WebToolError):
                        WebTools("t", resolver=dns, limits=WebLimits(timeout=.1)).fetch_url("https://example.com")
                self.assertLess(time.monotonic() - started, .8)
                self.assertEqual(client.fileno(), -1)
            finally:
                server.close()
                client.close()
