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
    return 'filled';
  }

  e.scrollIntoView({block: 'center'});
  e.click();
  return 'clicked';
})"""

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
    text: str
    actions: list[Action]
    marker: str

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

    def __init__(self, cdp, session_id: str):
        self._cdp = cdp
        self.session = session_id

    # -- plumbing ---------------------------------------------------------

    def call(self, method: str, **params):
        return self._cdp(method, session_id=self.session, **params)

    def evaluate(self, expression: str, await_promise: bool = False):
        result = self.call("Runtime.evaluate", expression=expression,
                           returnByValue=True, awaitPromise=await_promise)
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
                current_value=a.get("current_value", ""),
                extra={k: a[k] for k in ("checked", "selected", "expanded") if k in a}))
        return Snapshot(url=raw["url"], title=raw["title"], text=raw["text"],
                        actions=actions, marker=raw["marker"])

    def act(self, action: Action, text: str = "") -> str:
        payload = {"node": action.node, "kind": action.kind,
                   "value": action.value, "text": text}
        outcome = self.evaluate(ACT_JS + "(" + json.dumps(payload) + ")")
        if outcome is None:
            raise PageChanged("Target is gone or not actionable. Observe again.")
        self.settle()
        return outcome

    def submit(self, action: Action) -> str | None:
        outcome = self.evaluate(SUBMIT_JS + "(" + json.dumps(action.node) + ")")
        self.settle()
        return outcome

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", url=url)
        self.settle()

    def settle(self) -> None:
        # Navigation during settle is not an error.
        with contextlib.suppress(PageChanged):
            self.evaluate(SETTLE_JS, await_promise=True)
