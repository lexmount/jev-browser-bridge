"""Talk to a Moli session over CDP, without ever consulting layout.

Two rules hold everywhere in this file:

* **Read structure, not geometry.** `snapshot.js` never calls
  `getBoundingClientRect`, `checkVisibility` or `innerText`.
* **Act on elements, not coordinates.** A coordinate is only meaningful while
  the layout that produced it is current. Dispatching on the element itself
  needs no layout at all.
"""
from __future__ import annotations

import contextlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

SNAPSHOT_JS = (Path(__file__).with_name("snapshot.js")).read_text()

# Wait for the DOM to stop mutating, or 700 ms, whichever comes first.
# `element.click()` returns as soon as its handler does, so without this the
# next observation can read the pre-click DOM.
SETTLE_JS = """new Promise(resolve => {
  let timer = setTimeout(finish, 700);
  const observer = new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(finish, 120);
  });
  observer.observe(document.documentElement,
    {childList: true, subtree: true, attributes: true, characterData: true});
  function finish() { observer.disconnect(); resolve(true); }
})"""

ACT_JS = """(action => {
  // Mark this document. If the action replaces it, the next document will not
  // carry the mark -- which is how the caller tells "the page changed" from
  // "the page is still the one we clicked on and has not caught up yet".
  window.__jevNoLayoutDoc = action.token;
  const e = window.__jevNoLayout?.nodes.get(action.node);
  if (!e?.isConnected || e.matches(':disabled') ||
      e.closest('[aria-disabled="true"],[inert],[hidden]')) return null;

  if (action.kind === 'select') {
    if (e.tagName !== 'SELECT') return null;
    const option = [...e.options].find(o => o.value === action.value &&
      !o.disabled && !o.closest('optgroup[disabled]'));
    if (!option) return null;
    e.value = action.value;
    e.dispatchEvent(new Event('input', {bubbles: true}));
    e.dispatchEvent(new Event('change', {bubbles: true}));
    return 'selected';
  }

  if (action.kind === 'fill') {
    if (e.readOnly || e.getAttribute('aria-readonly') === 'true') return null;
    e.focus();
    // Assign through the native setter so frameworks that patch `value` still
    // see the change; then fire the events they actually listen for.
    const proto = e.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value');
    if (setter?.set) setter.set.call(e, action.text || '');
    else e.value = action.text || '';
    e.dispatchEvent(new Event('input', {bubbles: true}));
    e.dispatchEvent(new Event('change', {bubbles: true}));
    // How to find this field again if the page swaps it out; see REFILL_JS.
    window.__jevNoLayoutField = {node: action.node, name: e.name || '',
      aria: e.getAttribute('aria-label') || '', placeholder: e.placeholder || '',
      form: e.form?.id || ''};
    return 'filled';
  }

  e.scrollIntoView({block: 'center'});
  // A link that leaves this document. Said up front, because `click()` returns
  // before the browser has even started loading the next page -- measured on
  // Lightpanda, the agent read the old page, saw no change, and clicked the
  // same link again, every time.
  const link = e.closest('a[href]');
  const href = link?.getAttribute('href') || '';
  const leaves = link && !href.startsWith('#') && !href.startsWith('javascript:')
    && (link.target || '_self') === '_self' && !link.hasAttribute('download');
  // A form's submit button leaves the document too, as far as anyone can tell
  // in advance -- and not waiting for it cost the same. Measured on
  // Lightpanda: the search form did submit and did navigate, but the next
  // observation read the page being left, and the agent spent some twenty
  // steps waiting and re-clicking before it saw the result.
  const form = e.form || null;
  const submits = form && (e.type === 'submit' || e.type === 'image'
    || (e.tagName === 'BUTTON' && !e.hasAttribute('type')));
  e.click();
  if (leaves) return {navigating: link.href};
  if (submits) return {navigating: form.action || location.href};
  return 'clicked';
})"""

# After typing: is the text still in the field the page will actually use?
#
# Some pages replace a field the moment it is first typed into. Wikipedia's
# search box is a plain <input> until then, and a framework component after:
# the text went into the node being thrown away, the new one was empty, and
# the form submitted an empty search -- measured on Kitesurf, every time. So
# if the node we filled is gone, find its replacement by name, label or
# placeholder, and put the text there too.
REFILL_JS = """(text => {
  const was = window.__jevNoLayoutField;
  if (!was) return 'kept';
  const old = window.__jevNoLayout?.nodes.get(was.node);
  if (old?.isConnected && old.value === text) return 'kept';
  const fields = [...document.querySelectorAll('input,textarea')].filter(f =>
    !f.disabled && !f.readOnly && f.type !== 'hidden'
    && ((was.name && f.name === was.name)
        || (was.aria && f.getAttribute('aria-label') === was.aria)
        || (was.placeholder && f.placeholder === was.placeholder)));
  if (!fields.length) return 'lost';
  const next = fields.find(f => (f.form?.id || '') === was.form) || fields[0];
  if (next.value === text) return 'kept';
  next.focus();
  const proto = next.tagName === 'TEXTAREA'
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value');
  if (setter?.set) setter.set.call(next, text); else next.value = text;
  next.dispatchEvent(new Event('input', {bubbles: true}));
  next.dispatchEvent(new Event('change', {bubbles: true}));
  return 'refilled';
})"""

# Has the document been replaced since ACT_JS marked it, and is the new one done?
LOADED_JS = """(token => ({
  replaced: window.__jevNoLayoutDoc !== token,
  ready: document.readyState === 'complete' || document.readyState === 'interactive',
}))"""

SUBMIT_JS = """(node => {
  const e = window.__jevNoLayout?.nodes.get(node);
  if (!e?.isConnected) return null;
  e.focus();
  for (const type of ['keydown', 'keypress', 'keyup']) {
    e.dispatchEvent(new KeyboardEvent(type,
      {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
  }
  if (e.form?.requestSubmit) e.form.requestSubmit();
  return 'submitted';
})"""


class PageChanged(RuntimeError):
    """The page moved while we were reading or acting on it."""


@dataclass
class Action:
    """One thing the page says it can do, addressed by node id."""

    node: int
    kind: str                       # click | fill | select
    role: str
    label: str
    value: str = ""
    region: str = ""
    depth: int = 0
    closed: bool = False       # sits inside a dialog that is not open
    current_value: str = ""
    href: str = ""             # where a link goes; empty for everything else
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.kind}_{self.node}"

    def describe(self) -> str:
        bits = [f"{self.role:<10}", self.label]
        if self.value:
            bits.append(f"· {self.value[:40]}")
        return " ".join(bits)


@dataclass
class Snapshot:
    url: str
    title: str
    rows: list[str]
    actions: list[Action]
    marker: str

    def evidence(self, goal: str, budget: int | None = None) -> str:
        """The page text that bears on `goal`, within a character budget."""
        from .evidence import BUDGET, select
        return select(self.rows, goal, BUDGET if budget is None else budget)

    @property
    def text(self) -> str:
        """Every row, in order. Use `evidence()` for anything sent to a model."""
        return " ".join(self.rows)

    def find(self, *needles: str, kind: str | None = None) -> Action | None:
        """First action whose label contains every needle."""
        for action in self.actions:
            if kind and action.kind != kind:
                continue
            if all(n.lower() in action.label.lower() for n in needles):
                return action
        return None


class Browser:
    """A Moli page, observed structurally and driven by element dispatch."""

    def __init__(self, cdp, session_id: str, target: str | None = None):
        self._cdp = cdp
        self.session = session_id
        self.target = target

    # -- plumbing ---------------------------------------------------------

    def call(self, method: str, **params):
        return self._cdp(method, session_id=self.session, **params)

    def evaluate(self, expression: str, await_promise: bool = False):
        try:
            result = self.call("Runtime.evaluate", expression=expression,
                               returnByValue=True, awaitPromise=await_promise)
        except RuntimeError as error:
            # Asked mid-navigation, a browser answers with a protocol error
            # ("context was destroyed", "cannot find context") rather than a
            # JavaScript exception. Same meaning, so the same signal.
            raise PageChanged(f"Document changed during evaluation: {error}") from None
        if result.get("exceptionDetails"):
            raise PageChanged("Document changed during evaluation")
        return (result.get("result") or {}).get("value")

    # -- the two operations that matter ------------------------------------

    def observe(self) -> Snapshot:
        raw = self.evaluate(SNAPSHOT_JS)
        if raw is None:
            raise PageChanged("Document is navigating")
        actions = []
        for a in raw["actions"]:
            actions.append(Action(
                node=a["node"], kind=a["kind"], role=a["role"], label=a["label"],
                value=a.get("value", ""), region=a.get("region", ""),
                depth=a.get("depth", 0), closed=bool(a.get("closed")),
                current_value=a.get("current_value", ""), href=a.get("href", ""),
                extra={k: a[k] for k in ("checked", "selected", "expanded") if k in a}))
        return Snapshot(url=raw["url"], title=raw["title"], rows=raw.get("rows") or [],
                        actions=actions, marker=raw["marker"])

    def act(self, action: Action, text: str = "") -> str:
        token = uuid.uuid4().hex
        payload = {"node": action.node, "kind": action.kind,
                   "value": action.value, "text": text, "token": token}
        outcome = self.evaluate(ACT_JS + "(" + json.dumps(payload) + ")")
        if outcome is None:
            raise PageChanged("Target is gone or not actionable. Observe again.")
        if outcome == "filled":
            self.settle()
            with contextlib.suppress(PageChanged):
                if self.evaluate(REFILL_JS + "(" + json.dumps(text) + ")") == "refilled":
                    outcome = "filled (the page replaced the field; typed again)"
        if isinstance(outcome, dict):
            href = outcome.get("navigating", "")
            if self._await_new_document(token) or self._follow_new_page(href):
                outcome = "clicked"
            else:
                outcome = "clicked (stayed)"
        self.settle()
        return outcome

    def _follow_new_page(self, href: str) -> bool:
        """Move to the page a link opened elsewhere, if it opened one.

        Most browsers load a followed link into the same page. Cloudflare's
        Kitesurf does not: measured, `location.href` changed at once but the
        attached document never did, and the new document turned up on a new,
        unattached page target -- so an agent clicked the right link and never
        arrived. Look for that page and re-attach to it.
        """
        if not href:
            return False
        try:
            targets = self._cdp("Target.getTargets").get("targetInfos") or []
        except RuntimeError:
            return False
        fresh = [t for t in targets if t.get("type") == "page"
                 and t.get("targetId") != self.target and not t.get("attached")
                 and t.get("url", "").split("#")[0] == href.split("#")[0]]
        if not fresh:
            return False
        page = fresh[-1]
        try:
            attached = self._cdp("Target.attachToTarget", targetId=page["targetId"],
                                 flatten=True)
        except RuntimeError:
            return False
        self.session, self.target = attached["sessionId"], page["targetId"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with contextlib.suppress(PageChanged):
                if self.evaluate("document.readyState") in ("interactive", "complete"):
                    break
            time.sleep(0.15)
        return True

    def _await_new_document(self, token: str, start: float = 5.0,
                            finish: float = 20.0) -> bool:
        """Wait for a link click to replace the document; False if it never did.

        Two stages, because they fail differently. A navigation that is going
        to happen starts within a few seconds; one that has not started by then
        was intercepted -- a single-page app handling the click itself -- and
        waiting longer only wastes the step. Once it has started, a slow page
        is still worth waiting for.
        """
        deadline = time.monotonic() + start
        while time.monotonic() < deadline:
            try:
                state = self.evaluate(LOADED_JS + "(" + json.dumps(token) + ")")
            except PageChanged:
                state = None                    # mid-swap: no document to ask
            if state and state.get("replaced"):
                deadline = time.monotonic() + finish
                while not state.get("ready") and time.monotonic() < deadline:
                    time.sleep(0.15)
                    with contextlib.suppress(PageChanged):
                        state = self.evaluate(
                            LOADED_JS + "(" + json.dumps(token) + ")") or state
                return True
            time.sleep(0.15)
        return False

    def submit(self, action: Action) -> str | None:
        outcome = self.evaluate(SUBMIT_JS + "(" + json.dumps(action.node) + ")")
        self.settle()
        return outcome

    def navigate(self, url: str, wait: float = 20.0) -> None:
        # `Page.navigate` returns once the navigation commits, well before
        # there is anything to read. Measured on a form page, both extractors
        # saw zero controls for three seconds after it returned.
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            with contextlib.suppress(PageChanged):
                if self.evaluate("document.readyState") == "complete":
                    break
            time.sleep(0.15)
        self.settle()

    def settle(self) -> None:
        # Navigation during settle is not an error.
        with contextlib.suppress(PageChanged):
            self.evaluate(SETTLE_JS, await_promise=True)
