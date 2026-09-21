"""jev-nolayout <url> <goal> -- run one goal on Moli and print the trace."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="jev-nolayout")
    parser.add_argument("url")
    parser.add_argument("goal")
    parser.add_argument("--browser", default="light",
                        help="light = Moli (default), normal = standard Chrome")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()

    load_env(args.env)
    for required in ("JEV_API_KEY", "TEXT_MODEL_API_KEY"):
        if not os.environ.get(required):
            parser.error(f"{required} is not set (see .env.example)")

    from .agent import Agent
    from .session import moli_session

    def show(step):
        head = f"  {step.n:>2}. {step.operation:<10}"
        if step.label:
            head += f" {step.label[:52]}"
        print(head)
        detail = f"      {step.elapsed_ms:>5} ms   decision {step.decision_ms} ms" \
                 f"   {step.actions_offered} actions offered"
        print(detail)
        if step.text:
            print(f'      typed: "{step.text}"')
        if step.outcome:
            print(f"      {step.outcome}")

    print(f"\n  goal     {args.goal}\n  from     {args.url}\n"
          f"  browser  {'Moli' if args.browser == 'light' else 'Chrome'}\n")

    with moli_session(args.browser) as browser:
        browser.navigate(args.url)
        agent = Agent(browser, args.goal, on_step=show)
        state = None
        for state in agent.run():  # noqa: B007 - we want the last yield
            pass

    print(f"\n  {state.status}  ·  {len(state.steps)} steps  ·  "
          f"{state.elapsed_ms / 1000:.1f}s")
    print(f"  {state.url}\n")
    sys.exit(0 if state.status == "done" else 1)


if __name__ == "__main__":
    main()
