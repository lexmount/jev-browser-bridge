"""The loop: observe structurally, decide once, act on the element, repeat."""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field, replace

from .browser import Browser, PageChanged
from .model import choose, field_text

MAX_STEPS = 40


@dataclass
class Step:
    n: int
    operation: str
    label: str = ""
    text: str = ""
    outcome: str = ""
    actions_offered: int = 0
    decision_ms: int = 0
    elapsed_ms: int = 0


@dataclass
class Run:
    goal: str
    status: str = "ready"
    steps: list[Step] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    elapsed_ms: int = 0
    url: str = ""


class Agent:
    """Drive one goal to completion on an already-open page."""

    def __init__(self, browser: Browser, goal: str, on_step=None):
        self.browser = browser
        self.goal = goal
        self.on_step = on_step
        self.run_state = Run(goal=goal)

    def run(self):
        started = time.perf_counter()
        blank_reads = 0
        waits = 0
        # Controls that have been tried and changed nothing, by node id.
        spent: dict[int, int] = {}

        for n in range(1, MAX_STEPS + 1):
            step_started = time.perf_counter()
            try:
                snapshot = self.browser.observe()
            except PageChanged:
                blank_reads += 1
                if blank_reads >= 3:
                    self.run_state.status = "blocked"
                    break
                time.sleep(0.4)
                continue
            blank_reads = 0

            if not snapshot.actions:
                self.run_state.status = "blocked"
                break

            # Withdraw controls that have already been tried twice with no
            # effect. Some clicks land correctly but leave the marker
            # unchanged -- a dialog confirm that only updates state the
            # snapshot does not read -- and the model, seeing no progress,
            # picks the same control again. Offering it once more cannot
            # help; offering everything else can.
            usable = [a for a in snapshot.actions if spent.get(a.node, 0) < 2]
            if usable and len(usable) < len(snapshot.actions):
                snapshot = replace(snapshot, actions=usable)

            decision = choose(snapshot, self.goal, self.run_state.history)
            operation = decision["operation"]
            action = decision["action"]

            step = Step(n=n, operation=operation, decision_ms=decision["ms"],
                        actions_offered=len(snapshot.actions),
                        label=action.label if action else "")

            if operation in {"DONE", "BLOCKED"}:
                step.outcome = operation.lower()
                step.elapsed_ms = round((time.perf_counter() - step_started) * 1000)
                self._record(step)
                self.run_state.status = "done" if operation == "DONE" else "blocked"
                break

            if operation == "WAIT":
                self.browser.settle()
                waits += 1
                step.outcome = "waited"
                # Waiting is for a page that is still arriving. Three in a row
                # means the change being waited for is not coming -- usually a
                # click that took effect without moving the marker, so the
                # model reads it as "nothing happened" and stalls. Say so in
                # the history rather than burning the step budget.
                if waits >= 3:
                    self.run_state.history.append(
                        {"operation": "WAIT", "result":
                         "waited three times and the page did not change -- "
                         "the previous action has already taken effect; "
                         "move on to the next requirement"})
                    waits = 0
                step.elapsed_ms = round((time.perf_counter() - step_started) * 1000)
                self._record(step)
                yield self.run_state
                continue
            waits = 0

            text = ""
            if operation == "TYPE_TEXT":
                text = field_text(self.goal, action, self.run_state.history)
                step.text = text
                if not text:
                    # Better to lose a step than to submit an empty field.
                    step.outcome = "no value to type"
                    step.elapsed_ms = round((time.perf_counter() - step_started) * 1000)
                    self._record(step)
                    yield self.run_state
                    continue

            before = snapshot.marker
            before_actions = snapshot.actions
            try:
                step.outcome = self.browser.act(action, text)
            except PageChanged as error:
                step.outcome = f"stale: {error}"
                step.elapsed_ms = round((time.perf_counter() - step_started) * 1000)
                self._record(step)
                yield self.run_state
                continue

            try:
                after = self.browser.observe()
            except PageChanged:
                # act() swallows PageChanged inside settle(), so a navigation
                # still in flight when the settle timer expires surfaces here.
                # Record the action as taken -- it was -- and let the next
                # iteration read the page it landed on.
                self.run_state.history.append(
                    {"operation": operation, "label": action.label,
                     "text": text or None, "page_changed": True})
                step.outcome += " (navigating)"
                step.elapsed_ms = round((time.perf_counter() - step_started) * 1000)
                self._record(step)
                yield self.run_state
                continue

            changed = after.marker != before

            # Report the consequence, not just that something moved.
            #
            # Typing into an autocomplete field clears the field: the framework
            # owns its value and re-renders from state that does not have the
            # typed text yet. Measured on Google Flights -- the value reads
            # "Zurich" at the moment of writing and "" a second later, while
            # five suggestions appear. It is filled back in only once a
            # suggestion is chosen.
            #
            # A history line saying `page_changed: true` cannot express that.
            # The model looks for its text, does not find it, concludes the
            # typing failed, and types again -- forever. So name what appeared
            # and say plainly what it means.
            before_nodes = {b.node for b in before_actions}
            opened = [a.label for a in after.actions
                      if a.role in {"option", "gridcell", "menuitem"}
                      and a.node not in before_nodes][:8]

            entry = {"operation": operation, "label": action.label,
                     "text": text or None, "page_changed": changed}
            if operation == "TYPE_TEXT" and opened:
                entry["result"] = (
                    f"typed {text!r}; the field cleared itself and "
                    f"{len(opened)} suggestions opened -- choose one to commit "
                    f"the value. Do not type here again.")
                entry["suggestions"] = opened
            elif opened:
                entry["now_offered"] = opened
            elif not changed:
                entry["result"] = "nothing on the page changed"
            self.run_state.history.append(entry)
            if changed:
                spent.pop(action.node, None)
            else:
                spent[action.node] = spent.get(action.node, 0) + 1
            step.outcome += "" if changed else " (no change)"
            step.elapsed_ms = round((time.perf_counter() - step_started) * 1000)
            self._record(step)
            yield self.run_state
        else:
            self.run_state.status = "max_steps"

        self.run_state.elapsed_ms = round((time.perf_counter() - started) * 1000)
        with contextlib.suppress(PageChanged):
            self.run_state.url = self.browser.observe().url
        yield self.run_state

    def _record(self, step: Step) -> None:
        self.run_state.steps.append(step)
        if self.on_step:
            self.on_step(step)
