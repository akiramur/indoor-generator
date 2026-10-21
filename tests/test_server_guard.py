import http.client
import threading

import pytest

from indoor_gen import server


@pytest.fixture
def httpd(tmp_path):
    out = tmp_path / "out" / "demo"
    out.mkdir(parents=True)
    (out / "index.html").write_text("<html></html>")
    s = server.make_server(out, "127.0.0.1", 0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


def request(httpd, method, path, headers=None, body=b"null"):
    c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    c.request(method, path, body=body if method == "POST" else None, headers=headers or {})
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def test_page_is_served_for_a_local_host(httpd):
    assert request(httpd, "GET", "/index.html") == 200


def test_foreign_host_is_refused_against_dns_rebinding(httpd):
    assert request(httpd, "GET", "/index.html", {"Host": "evil.example:8765"}) == 403
    assert request(httpd, "POST", "/nudge", {"Host": "evil.example"}) == 403


def test_cross_site_post_is_refused(httpd):
    port = httpd.server_address[1]
    assert request(httpd, "POST", "/nope", {"Origin": "https://evil.example"}) == 403
    # same-origin and origin-less (curl, agents) requests reach the handler (unknown path -> 404)
    assert request(httpd, "POST", "/nope", {"Origin": f"http://127.0.0.1:{port}", "Host": f"127.0.0.1:{port}"}) == 404
    assert request(httpd, "POST", "/nope") == 404


def test_host_name_parsing():
    assert server._host_name("localhost:8765") == "localhost"
    assert server._host_name("[::1]:8765") == "[::1]"
