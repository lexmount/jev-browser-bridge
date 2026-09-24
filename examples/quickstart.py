"""The shortest complete run: one goal on Moli, printed step by step.

    uv sync --extra lexmount
    cp .env.example .env          # JEV_API_KEY and Lexmount credentials
    uv run python examples/quickstart.py

For any other browser, replace `lexmount_session()` with
`connect("http://127.0.0.1:9222")` (or a ws:// URL) -- nothing else changes.
"""
from jev_nolayout import Agent, lexmount_session
from jev_nolayout.cli import load_env

load_env()

with lexmount_session() as browser:
    browser.navigate("https://en.wikipedia.org/wiki/Espresso")
    state = None
    for state in Agent(browser, "Switch this page to the Deutsch language edition").run():
        step = state.steps[-1] if state.steps else None
        if step:
            print(f"{step.n:>2}. {step.operation:<9} {step.label[:50]}")
    print(f"\n{state.status} in {len(state.steps)} steps -> {state.url}")
