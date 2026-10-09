"""library_search calls LocalEvidence /api/ask and keeps the aviation subset."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from isobar_data.library_mcp.search import library_search
from isobar_data.library_mcp.search import MAX_RESPONSE_BYTES

COLLECTION = [
    {
        "slug": "faa-weather",
        "title": "Aviation Weather Handbook",
        "source_url": "https://www.faa.gov/sites/faa.gov/files/FAA-H-8083-28B.pdf",
        "licence": "public-domain (US government work)",
    },
    {
        "slug": "bom-sigmet",
        "title": "SIGMET: aviation products",
        "source_url": "https://www.bom.gov.au/aviation/data/education/sigmet.pdf",
        "licence": "CC BY 4.0",
    },
]


class _AskHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.requests.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
        status, payload = self.server.reply
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            # The bounded client may close after reading the headers.
            pass

    def log_message(self, *_args):
        return


@pytest.fixture
def ask_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AskHandler)
    server.reply = (200, {"status": "queued", "evidence": []})
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)


def _url(server) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}"


def test_library_search_keeps_aviation_passages_with_licence_and_page(ask_server):
    ask_server.reply = (200, {
        "status": "queued",
        "evidence": [
            {"slug": "nejm-review", "title": "A medical review", "text": "not aviation", "tier": "review"},
            {
                "slug": "faa-weather",
                "title": "Aviation Weather Handbook",
                "text": "A microburst is a small downburst.",
                "source_url": COLLECTION[0]["source_url"],
                "page": "88",
                "section": "Chapter 19",
            },
            {"slug": "bom-sigmet", "title": "SIGMET: aviation products", "text": "A SIGMET warns of hazardous weather."},
        ],
    })
    got = library_search("microburst", collection=COLLECTION, base_url=_url(ask_server))
    assert ask_server.requests == [("/api/ask", {"question": "microburst"})]
    assert [row["title"] for row in got["passages"]] == [
        "Aviation Weather Handbook",
        "SIGMET: aviation products",
    ]
    first = got["passages"][0]
    assert first["source"] == "https://www.faa.gov/sites/faa.gov/files/FAA-H-8083-28B.pdf"
    assert first["licence"] == "public-domain (US government work)"
    assert first["page"] == "88"
    assert first["section"] == "Chapter 19"
    assert first["passage"] == "A microburst is a small downburst."
    assert first["provenance"] == "source-matched-excerpt"
    assert got["passages"][1]["page"] is None
    assert got["passages"][1]["licence"] == "CC BY 4.0"
    assert got["passages"][1]["provenance"] == "unverified-excerpt"


def test_library_search_limits_to_k(ask_server):
    ask_server.reply = (200, {"status": "queued", "evidence": [
        {"slug": "faa-weather", "title": "Aviation Weather Handbook", "text": "first"},
        {"slug": "bom-sigmet", "title": "SIGMET: aviation products", "text": "second"},
    ]})
    got = library_search("sigmet", k=1, collection=COLLECTION, base_url=_url(ask_server))
    assert [row["passage"] for row in got["passages"]] == ["first"]


def test_library_search_does_not_return_a_prior_answer(ask_server):
    ask_server.reply = (200, {"status": "answered", "similarity": 1, "answer": {"answer": "Give steroids."}})
    got = library_search("croup", collection=COLLECTION, base_url=_url(ask_server))
    assert got["passages"] == []
    assert "prior" in got["reason"]


def test_library_search_reports_an_http_error(ask_server):
    ask_server.reply = (503, {"error": "not ready"})
    got = library_search("icing", collection=COLLECTION, base_url=_url(ask_server))
    assert got["passages"] == []
    assert "503" in got["reason"]


def test_library_search_rejects_a_blank_query_and_a_bad_k():
    with pytest.raises(ValueError):
        library_search("  ", collection=COLLECTION, base_url="http://127.0.0.1:9")
    with pytest.raises(ValueError):
        library_search("icing", k=0, collection=COLLECTION, base_url="http://127.0.0.1:9")


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:8765",
    "http://example.com:8765",
    "ftp://127.0.0.1:8765",
    "http://127.0.0.1:8765/?redirect=1",
])
def test_library_search_requires_an_http_loopback_url(url):
    with pytest.raises(ValueError):
        library_search("icing", collection=COLLECTION, base_url=url)


def test_library_search_rejects_redirects(ask_server):
    ask_server.reply = (302, {"evidence": []})
    got = library_search("icing", collection=COLLECTION, base_url=_url(ask_server))
    assert got["passages"] == []
    assert "redirect" in got["reason"]


def test_library_search_bounds_response_size(ask_server):
    ask_server.reply = (200, {"evidence": "x" * MAX_RESPONSE_BYTES})
    got = library_search("icing", collection=COLLECTION, base_url=_url(ask_server))
    assert got["passages"] == []
    assert "size" in got["reason"]


def test_tool_call_returns_the_filtered_passage(ask_server, tmp_path, monkeypatch):
    from isobar_data.library_mcp.protocol import MCPServer

    ask_server.reply = (200, {"status": "queued", "evidence": [
        {"slug": "nejm-review", "title": "A medical review", "text": "steroids"},
        {"slug": "faa-weather", "title": "Aviation Weather Handbook", "text": "microburst"},
    ]})
    path = tmp_path / "aviation-meteorology.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in COLLECTION) + "\n")
    monkeypatch.setenv("ISOBAR_LIBRARY_URL", _url(ask_server))
    monkeypatch.setenv("ISOBAR_LIBRARY_COLLECTION", str(path))
    server = MCPServer()
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "isobar-library"
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed["result"]["tools"][0]["name"] == "library_search"
    reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "library_search",
        "arguments": {"query": "microburst"},
    }})
    body = json.loads(reply["result"]["content"][0]["text"])
    assert [row["passage"] for row in body["passages"]] == ["microburst"]
    assert body["passages"][0]["licence"] == "public-domain (US government work)"


def test_missing_source_urls_do_not_establish_provenance(ask_server):
    ask_server.reply = (200, {"evidence": [{"slug": "unknown", "text": "some text"}]})
    got = library_search("icing", collection=[{"slug": "unknown"}], base_url=_url(ask_server))
    assert got["passages"][0]["provenance"] == "unverified-excerpt"


def test_local_library_bypasses_environment_proxy(ask_server, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    got = library_search("icing", collection=COLLECTION, base_url=_url(ask_server))
    assert got["reason"] is None
    assert ask_server.requests == [("/api/ask", {"question": "icing"})]
