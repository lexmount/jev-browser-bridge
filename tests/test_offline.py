"""Checks that run without a browser or an API key.

Each one pins a behaviour that was once broken and measured: the fix is only
worth keeping if a regression turns this file red.
"""
import base64
import http.server
import threading

import pytest

from jev_browser_bridge import model
from jev_browser_bridge.browser import Action
from jev_browser_bridge.evidence import keywords, select
from jev_browser_bridge.session import _auth_from_url, _reachable

# -- evidence.py: the page text a decision reads ---------------------------

def test_answer_deep_in_a_long_page_is_kept():
    # The failure this replaced: a fixed-size prefix of the page, which on
    # long articles was all navigation and never reached the answer.
    rows = [f"Navigation link {i} to another section of the site" for i in range(400)]
    rows.append("Jupiter has 95 officially recognised moons as of 2023.")
    rows += [f"Footer boilerplate row {i}" for i in range(50)]
    assert "95 officially recognised moons" not in " ".join(rows)[:6000]
    assert "95 officially recognised moons" in select(rows, "how many moons does Jupiter have", 2000)


def test_table_row_keeps_label_and_value_together():
    rows = ["Mount Everest", "Elevation | 8,848.86 m", "First ascent | 29 May 1953"]
    assert "Elevation | 8,848.86 m" in select(rows, "what is the elevation", 200)


def test_target_kind_words_do_not_rank():
    # "article" once pulled a citation ending "... Article 5" above the link
    # the goal was about.
    assert keywords("open the article about the Berne Convention linked from this page") \
        == ["berne", "convention"]


# -- session.py: reaching a browser ----------------------------------------

@pytest.mark.parametrize("advertised, reached, expected", [
    ("ws://0.0.0.0:3000/", "http://127.0.0.1:9730", "ws://127.0.0.1:9730/"),
    ("ws://172.17.0.3:4444/session/1/se/cdp", "http://127.0.0.1:9760",
     "ws://127.0.0.1:9760/session/1/se/cdp"),
    ("ws://127.0.0.1/devtools/browser/y", "http://127.0.0.1:9223",
     "ws://127.0.0.1:9223/devtools/browser/y"),
    # Already reachable, or a public host a service may legitimately use.
    ("ws://127.0.0.1:9222/devtools/browser/x", "http://127.0.0.1:9222",
     "ws://127.0.0.1:9222/devtools/browser/x"),
    ("wss://connect.example.com/abc", "https://api.example.com", "wss://connect.example.com/abc"),
])
def test_container_address_points_back_at_the_one_reached(advertised, reached, expected):
    assert _reachable(advertised, reached) == expected


def test_credentials_in_url_become_basic_auth():
    url, headers = _auth_from_url("wss://alice:s3cr%40t@proxy.example.com:9222/x", {})
    assert url == "wss://proxy.example.com:9222/x"
    assert base64.b64decode(headers["Authorization"].split()[1]).decode() == "alice:s3cr@t"


def test_explicit_authorization_header_wins():
    url, headers = _auth_from_url("wss://alice:pw@host/x", {"Authorization": "Bearer T"})
    assert url == "wss://alice:pw@host/x" and headers == {"Authorization": "Bearer T"}


# -- model.py: what Jev is offered, and how it is asked --------------------

def _link(node, label, href):
    return Action(node=node, kind="click", role="link", label=label, href=href)


def test_same_name_same_destination_is_offered_once():
    actions = [_link(1, "Berne Convention", "https://w/wiki/Berne"),
               _link(2, "Berne Convention", "https://w/wiki/Berne"),
               _link(3, "Berne Convention", "https://w/wiki/Other")]
    offered = model.action_space(actions, "open the Berne Convention article")["CLICK"]
    assert sorted(a.node for a in offered.values()) == [1, 3]


def test_candidates_stay_within_the_choice_limit():
    actions = [_link(i, f"link {i}", f"https://w/{i}") for i in range(2000)]
    offered = model.action_space(actions, "anything")["CLICK"]
    assert len(offered) == model.MAX_CHOICES


def _server(statuses):
    """A local endpoint answering with each status in turn, then 200."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            code = statuses[len(seen)] if len(seen) < len(statuses) else 200
            seen.append(code)
            body = b'{"ok": true}' if code == 200 else b"boom"
            self.send_response(code)
            # A well-formed response: without a length the client cannot tell
            # where it ends on a reused connection, and retries a request the
            # server already answered.
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(model.time, "sleep", lambda seconds: None)


def test_transient_errors_are_retried():
    server, seen = _server([500, 502])
    try:
        assert model.post(f"http://127.0.0.1:{server.server_port}/x", "k", {}) == {"ok": True}
        assert seen == [500, 502, 200]
    finally:
        server.shutdown()


def test_persistent_errors_say_how_many_attempts():
    server, _ = _server([500] * 10)
    try:
        with pytest.raises(RuntimeError, match=r"HTTP 500 after 4 attempts"):
            model.post(f"http://127.0.0.1:{server.server_port}/x", "k", {})
    finally:
        server.shutdown()


def test_a_bad_request_is_not_retried():
    server, seen = _server([400])
    try:
        with pytest.raises(RuntimeError, match=r"HTTP 400"):
            model.post(f"http://127.0.0.1:{server.server_port}/x", "k", {})
        assert seen == [400]
    finally:
        server.shutdown()
