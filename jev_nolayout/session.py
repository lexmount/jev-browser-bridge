"""Open a Moli session and hand back a Browser bound to its first page."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager

# The CDP transport is a small script that owns one daemon per browser endpoint.
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


def _cdp_via_websocket(ws: str):
    """Minimal CDP client, so the package has no transport dependency."""
    import itertools

    import websockets.sync.client as wsclient

    connection = wsclient.connect(ws, max_size=None, open_timeout=30)
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


@contextmanager
def moli_session(browser_mode: str = "light"):
    """Create a Moli session, attach to its page, clean up on exit.

    `browser_mode="light"` is Moli. Pass `"normal"` for standard Chrome when you
    want a control run -- this layer works on both.
    """
    from lexmount import Lexmount

    client = Lexmount()
    session = client.sessions.create(browser_mode=browser_mode, poll_timeout_sec=180)
    ws = getattr(session, "ws", None) or getattr(session, "connect_url", None)
    if not ws:
        raise RuntimeError("Session came back without a websocket URL")

    cdp = _cdp_via_script(CDP_SCRIPT, ws) if CDP_SCRIPT else _cdp_via_websocket(ws)
    try:
        targets = cdp("Target.getTargets")["targetInfos"]
        if not targets:
            raise RuntimeError("Session reported no browser targets")
        page = next((t for t in targets if t["type"] == "page"), targets[0])
        attached = cdp("Target.attachToTarget", targetId=page["targetId"], flatten=True)
        from .browser import Browser
        yield Browser(cdp, attached["sessionId"])
    finally:
        if hasattr(cdp, "close"):
            cdp.close()
        with contextlib.suppress(Exception):
            client.sessions.delete(session_id=getattr(session, "session_id", None))
