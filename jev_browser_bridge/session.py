"""Get a Browser bound to a page, on any browser that speaks CDP.

`connect()` is the whole contract: a CDP endpoint in, a Browser out. Nothing
below it cares which browser answered -- the snapshot reads the DOM, actions
are dispatched on elements, and neither asks the layout engine anything, so a
browser that never lays a page out works as well as one that always does.

`lexmount_session()` is a convenience on top: it creates a Lexmount session
(Moli by default) and hands its endpoint to `connect()`.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager

# Optional: route CDP through an external script instead of a websocket held
# in this process. Unset, the built-in websocket client is used.
CDP_SCRIPT = os.environ.get("JEV_CDP_SCRIPT", "")


def _cdp_via_script(script: str, ws: str):
    def cdp(method: str, session_id: str | None = None, **params):
        cmd = [sys.executable, script, "--websocket-url", ws, "call"]
        if session_id:
            cmd.append(session_id)
        cmd += [method, "--params", json.dumps(params)]
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = (done.stdout or "").strip()
        if not out:
            raise RuntimeError((done.stderr or "empty CDP response")[:300])
        payload = json.loads(out)
        return payload.get("result", payload)
    return cdp


def _auth_from_url(ws: str, headers: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Move `user:pass@` out of the URL and into a Basic Authorization header.

    Some services (Bright Data's scraping browser, for one) put the account in
    the websocket URL's userinfo. The websocket client does not turn that into
    a header on its own, so the handshake went out unauthenticated.
    """
    import base64
    from urllib.parse import unquote, urlsplit, urlunsplit

    parts = urlsplit(ws)
    if not parts.username or "authorization" in {k.lower() for k in headers}:
        return ws, headers
    token = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return (urlunsplit(parts._replace(netloc=host)),
            {**headers, "Authorization": "Basic " + base64.b64encode(token.encode()).decode()})


MAX_RATE_WAIT = 60.0


def _dial(wsclient, ws: str, headers: dict[str, str], attempts: int = 5):
    """Open the websocket, waiting out a rate limit instead of failing on it.

    A cloud browser service that caps how many browsers may start per minute
    answers the handshake with 429. That is "not yet", not "no": measured on
    Cloudflare's free plan, six runs in ten were refused this way when tasks
    started back to back, and every one would have been accepted a few seconds
    later. Honour Retry-After when the service sends one.
    """
    from websockets.exceptions import InvalidStatus

    for attempt in range(attempts):
        try:
            # No keepalive pings. A browser that is busy evaluating a long
            # script cannot answer one -- Kitesurf, mid-snapshot on a long
            # article, went quiet for over 20s -- and the client then drops a
            # perfectly healthy connection with "keepalive ping timeout".
            # Every call here waits for its own reply anyway, with its own
            # timeout.
            return wsclient.connect(ws, max_size=None, open_timeout=30, ping_interval=None,
                                    additional_headers=headers or None)
        except InvalidStatus as error:
            response = error.response
            if response.status_code != 429 or attempt == attempts - 1:
                raise
            retry = response.headers.get("Retry-After", "")
            wait = float(retry) if retry.replace(".", "", 1).isdigit() else 5.0 * 2 ** attempt
            # A per-minute limit asks for seconds; an exhausted daily quota
            # asks for hours -- Cloudflare's free plan answered Retry-After:
            # 57691. Waiting that out is not a retry, it is a hang, so say so.
            if wait > MAX_RATE_WAIT:
                raise RuntimeError(
                    f"The browser service is rate limiting this account and asks to retry "
                    f"in {wait / 3600:.1f}h (HTTP 429) -- a quota, not a transient limit.") \
                    from None
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _cdp_via_websocket(ws: str, headers: dict[str, str] | None = None):
    """Minimal CDP client, so the package has no transport dependency."""
    import itertools

    import websockets.sync.client as wsclient

    ws, headers = _auth_from_url(ws, dict(headers or {}))
    connection = _dial(wsclient, ws, headers)
    counter = itertools.count(1)

    def cdp(method: str, session_id: str | None = None, **params):
        message = {"id": next(counter), "method": method, "params": params}
        if session_id:
            message["sessionId"] = session_id
        connection.send(json.dumps(message))
        while True:
            reply = json.loads(connection.recv(timeout=120))
            if reply.get("id") != message["id"]:
                continue                      # an event, not our answer
            if "error" in reply:
                raise RuntimeError(reply["error"].get("message", "CDP error"))
            return reply.get("result", {})

    cdp.close = connection.close
    return cdp


def resolve(endpoint: str, headers: dict[str, str] | None = None) -> str:
    """Turn whatever a browser hands out into its browser-level websocket URL.

    Chrome, Lightpanda and most local browsers advertise an HTTP port and put
    the websocket URL behind `/json/version`; cloud services usually hand out
    the websocket URL itself, and some hand out an https one. Accept
    all of these, so a caller can pass whatever they were given.
    """
    if endpoint.startswith(("ws://", "wss://")):
        return endpoint
    base = endpoint.rstrip("/")
    request = urllib.request.Request(f"{base}/json/version", headers=headers or {})
    with urllib.request.urlopen(request, timeout=15) as reply:
        ws = json.loads(reply.read()).get("webSocketDebuggerUrl")
    if not ws:
        raise RuntimeError(f"{base}/json/version did not name a websocket URL")
    return _reachable(ws, base)


def _reachable(ws: str, base: str) -> str:
    """Point an advertised websocket URL at the address that actually answered.

    A browser in a container reports the address it sees from the inside:
    `ws://0.0.0.0:3000/` (browserless, Steel), the container's own IP
    (Selenium), or a host with no port at all. None of those can be dialled
    from outside, and the connection is refused -- while the address the caller
    just reached /json/version on is, by definition, reachable. Use it, keeping
    the advertised path. Public hostnames are left alone: a cloud service may
    legitimately hand its websocket to a different host.
    """
    import ipaddress
    from urllib.parse import urlsplit, urlunsplit

    advertised, asked = urlsplit(ws), urlsplit(base)
    host = advertised.hostname or ""
    try:
        internal = ipaddress.ip_address(host)
        unroutable = internal.is_unspecified or internal.is_private or internal.is_loopback
    except ValueError:
        unroutable = host in ("", "localhost")
    if not unroutable or advertised.netloc == asked.netloc:
        return ws
    scheme = "wss" if asked.scheme == "https" else "ws"
    return urlunsplit(advertised._replace(scheme=scheme, netloc=asked.netloc))


@contextmanager
def connect(endpoint: str, *, headers: dict[str, str] | None = None,
            wait_for_page: float = 0.0):
    """Attach to a CDP browser and yield a Browser bound to one page.

    `endpoint` is a `ws://` / `wss://` URL, or an `http(s)://` address that
    serves `/json/version`. `headers` go on the websocket handshake, for
    services that authenticate there rather than in the URL (Cloudflare Browser
    Run and Airtop want `Authorization: Bearer ...`); credentials written into
    the URL as `user:pass@host` are sent as Basic auth.

    An existing page is used if there is one; otherwise a page is created, and
    closed again on exit. Many browsers start with no page at all -- Lightpanda,
    Obscura, headless-shell containers -- and waiting for one that never comes
    cost 15s per run on each of them. Pass `wait_for_page` for a service that
    opens its first tab a moment after handing out the endpoint, and prefers
    you to use that tab.
    """
    from .browser import Browser

    ws = resolve(endpoint, headers)
    cdp = (_cdp_via_script(CDP_SCRIPT, ws) if CDP_SCRIPT
           else _cdp_via_websocket(ws, headers))
    created = None
    try:
        page = None
        deadline = time.monotonic() + wait_for_page
        while True:
            with contextlib.suppress(RuntimeError):
                targets = cdp("Target.getTargets").get("targetInfos") or []
                page = next((t for t in targets if t.get("type") == "page"), None)
            if page is not None or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        if page is None:
            created = cdp("Target.createTarget", url="about:blank")["targetId"]
        target = created or page["targetId"]
        attached = cdp("Target.attachToTarget", targetId=target, flatten=True)
        yield Browser(cdp, attached["sessionId"], target=target)
    finally:
        if created:
            with contextlib.suppress(Exception):
                cdp("Target.closeTarget", targetId=created)
        if hasattr(cdp, "close"):
            cdp.close()


@contextmanager
def lexmount_session(browser_mode: str = "light"):
    """Create a Lexmount session, connect to it, and delete it on exit.

    `browser_mode="light"` is Moli, a browser that keeps structure in memory
    and lays pages out only when a picture is asked for. `"normal"` is the
    standard Chrome image. Needs the `lexmount` extra and LEXMOUNT_* credentials.
    """
    try:
        from lexmount import Lexmount
    except ImportError:
        raise RuntimeError(
            'Lexmount support is an extra: pip install "jev-browser-bridge[lexmount]"') from None

    client = Lexmount()
    session = client.sessions.create(browser_mode=browser_mode, poll_timeout_sec=180)
    try:
        ws = getattr(session, "ws", None) or getattr(session, "connect_url", None)
        if not ws:
            raise RuntimeError("Session came back without a websocket URL")
        with connect(ws, wait_for_page=20.0) as browser:
            yield browser
    finally:
        with contextlib.suppress(Exception):
            client.sessions.delete(session_id=getattr(session, "session_id", None))


@contextmanager
def selenium_session(grid: str, browser: str = "chrome"):
    """Start a session on a Selenium Grid and connect to it over CDP.

    A Grid serves no /json/version; it hands out a CDP address only as the
    `se:cdp` capability of a session it has just created -- and that address
    names the node from inside the Grid's network, often a container IP. So
    create the session, point the address at the Grid that answered, connect,
    and end the session on exit.
    """
    base = grid.rstrip("/")
    request = urllib.request.Request(
        f"{base}/session", method="POST",
        data=json.dumps({"capabilities": {"alwaysMatch": {"browserName": browser}}}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as reply:
        value = json.loads(reply.read())["value"]
    session_id = value["sessionId"]
    try:
        cdp_url = (value.get("capabilities") or {}).get("se:cdp")
        if not cdp_url:
            raise RuntimeError("The Grid did not offer a CDP address (se:cdp) for this browser")
        with connect(_reachable(cdp_url, base)) as page:
            yield page
    finally:
        with contextlib.suppress(Exception):
            urllib.request.urlopen(urllib.request.Request(
                f"{base}/session/{session_id}", method="DELETE"), timeout=30)


# The name this module was first published under.
moli_session = lexmount_session
