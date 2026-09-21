"""The loop: observe structurally, decide once, act on the element, repeat."""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field

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
                step.outcome = "waited"
                step.elapsed_ms = round((time.perf_counter() - step_started) * 1000)
                self._record(step)
                yield self.run_state
                continue

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

            after = self.browser.observe()
            changed = after.marker != before
            # Say what the action produced, not just that something moved. A
            # fill that opens an autocomplete list replaces the field it was
            # typed into, so the next observation no longer shows the value --
            # without this note the model reads that as "the text did not take"
            # and types it again, forever. Naming the new options tells it the
            # next move is to pick one.
            opened = [a.label for a in after.actions
                      if a.role in {"option", "gridcell", "menuitem"}
                      and a.node not in {b.node for b in before_actions}][:6] if changed else []
            self.run_state.history.append(
                {"operation": operation, "label": action.label,
                 "text": text or None, "page_changed": changed,
                 "now_offered": opened or None})
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
