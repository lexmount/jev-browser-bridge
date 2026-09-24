"""jev-browser-bridge <url> <goal> -- run one goal and print the trace."""
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
    parser = argparse.ArgumentParser(prog="jev-browser-bridge")
    parser.add_argument("url")
    parser.add_argument("goal")
    parser.add_argument("--browser", default="light",
                        help="Lexmount session type: light = Moli (default), "
                             "normal = standard Chrome")
    parser.add_argument("--cdp", metavar="ENDPOINT",
                        help="use any CDP browser instead of creating a Lexmount "
                             "session: ws://... or http://host:port")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()

    load_env(args.env)
    # The text model is only needed when a step types into a field, so it is
    # checked there, not here.
    if not os.environ.get("JEV_API_KEY"):
        parser.error("JEV_API_KEY is not set (see .env.example)")

    from .agent import Agent
    from .session import connect, lexmount_session

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

    where = args.cdp or ("Moli" if args.browser == "light" else "Lexmount Chrome")
    print(f"\n  goal     {args.goal}\n  from     {args.url}\n  browser  {where}\n")

    session = connect(args.cdp) if args.cdp else lexmount_session(args.browser)
    with session as browser:
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
