import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from evo.adapters.tools.web import DuckDuckGoParser, WebLimits, WebToolError, WebTools
from tests.fixtures.web import Response, Transport, dns


HTML = '''<html><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2Fdocs%23top">Python <b>Docs</b></a>
<a class="result__a" href="https://www.python.org:443/docs#other">Duplicate</a>
<a class="result-link" href="https://example.com/a?x=1&amp;y=2">A &amp; B</a>
<a href="https://ads.example.com/">Ignored navigation</a></html>'''


class SearchParserTests(unittest.TestCase):
    def test_lite_html_wrappers_titles_urls_and_deduplication(self):
        results = DuckDuckGoParser.parse(HTML, 5)
        self.assertEqual([(r.title, r.url) for r in results], [
            ("Python Docs", "https://www.python.org/docs"),
            ("A & B", "https://example.com/a?x=1&y=2")])
        self.assertTrue(all(r.untrusted for r in results))

    def test_unsafe_or_missing_targets_are_filtered(self):
        links = ["http://example.com/", "javascript:alert(1)", "file:///etc/passwd",
                 "https://127.0.0.1/", "https://[::1]/", "https://user:pw@example.com/",
                 "https://host.internal/", "https://example.com:8443/", "/l/?uddg=http%3A%2F%2Flocalhost/",
                 "//evil.example/x?uddg=https%3A%2F%2Fexample.com%2F", "https://example.com/" ]
        html = ''.join('<a class="result-link" href="%s">Result</a>' % url for url in links[:-1])
        html += '<a class="result-link">Missing</a><a class="result-link" href="https://example.com/"> </a>'
        self.assertEqual(DuckDuckGoParser.parse(html, 5), ())

    def test_empty_malformed_and_limit(self):
        self.assertEqual(DuckDuckGoParser.parse("<p>No results</p>", 5), ())
        self.assertEqual(DuckDuckGoParser.parse('<a class href="https://example.com">ignored</a>', 5), ())
        self.assertEqual(DuckDuckGoParser.parse('<a class="result-link" href="https://example.com/">unfinished', 5), ())
        self.assertEqual(len(DuckDuckGoParser.parse(HTML, 1)), 1)

    def test_search_encoded_query_and_provider_request(self):
        transport = Transport(Response(HTML.encode(), headers=[("Content-Type", "text/html; charset=utf-8")]))
        tool = WebTools("task", resolver=dns)
        with patch("evo.adapters.tools.web._request", transport):
            self.assertEqual(len(tool.search("Python & 安全")), 2)
        self.assertEqual(tool.searches_used, 1)
        self.assertIn("q=Python+%26+%E5%AE%89%E5%85%A8", transport.calls[0][0])
        self.assertTrue(transport.calls[0][0].startswith("https://lite.duckduckgo.com/lite/?"))
        self.assertTrue(transport.responses == [])

    def test_parser_is_replaceable(self):
        parser = Mock()
        parser.parse.return_value = ()
        transport = Transport(Response(b"custom", headers=[("Content-Type", "text/html")]))
        with patch("evo.adapters.tools.web._request", transport):
            self.assertEqual(WebTools("task", resolver=dns, parser=parser).search("q"), ())
        parser.parse.assert_called_once_with("custom", 5)

    def test_failure_consumes_budget_and_instances_are_task_scoped(self):
        tool = WebTools("task", resolver=Mock(side_effect=OSError("private detail")))
        for _ in range(3):
            with self.assertRaises(WebToolError):
                tool.search("valid query")
        with self.assertRaisesRegex(WebToolError, "budget"):
            tool.search("another")
        self.assertEqual(tool.searches_used, 3)
        self.assertEqual(WebTools("other", resolver=dns).searches_used, 0)

    def test_invalid_queries_do_not_use_budget(self):
        tool = WebTools("task", resolver=dns)
        for query in [None, "", "  ", "a\nb", "a\x00b", "x" * 513, "你" * 171, "\ud800"]:
            with self.subTest(query=query), self.assertRaises(WebToolError):
                tool.search(query)
        self.assertEqual(tool.searches_used, 0)

    def test_concurrent_searches_cannot_exceed_budget(self):
        tool = WebTools("task", resolver=Mock(side_effect=OSError()))
        def search(_):
            try:
                tool.search("query")
            except WebToolError:
                pass
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(search, range(20)))
        self.assertEqual(tool.searches_used, 3)
        self.assertEqual(tool._resolver.call_count, 3)

    def test_non_html_search_response_fails(self):
        with patch("evo.adapters.tools.web._request", Transport(Response())):
            with self.assertRaises(WebToolError):
                WebTools("task", resolver=dns).search("query")

    def test_limits_and_task_identity_are_validated(self):
        for config in [{"timeout": 0}, {"timeout": float("nan")}, {"timeout": True},
                       {"max_redirects": 4}, {"max_results": 11}, {"max_searches": 4},
                       {"max_response_bytes": 1048577}, {"max_query_bytes": True},
                       {"max_url_bytes": -1}]:
            with self.subTest(config=config), self.assertRaises(WebToolError):
                WebLimits(**config)
        for task_id in [None, "", " " , "x" * 129]:
            with self.assertRaises(WebToolError):
                WebTools(task_id)
        with self.assertRaisesRegex(WebToolError, "budget"):
            WebTools("task", limits=WebLimits(max_searches=0)).search("query")
