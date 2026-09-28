"""Run MiniWoB++ on one browser and write one JSON line per episode.

    uv run python benchmarks/miniwob/run.py --cdp http://127.0.0.1:9222 --out chrome.jsonl
    uv run python benchmarks/miniwob/run.py --browser light --out moli.jsonl
    python benchmarks/miniwob/run.py --cdp URL --header "Authorization: Bearer T" --out cf.jsonl
    uv run python benchmarks/miniwob/summary.py *.jsonl

Every browser gets the same problems: each episode is seeded by task name and
seed number, so "click-button #1" is the same page on Chrome and on Moli. The
page grades itself -- an episode passes when MiniWoB's own reward is positive
-- so nothing here judges the agent.

The pages are MiniWoB++'s own, served from miniwob.farama.org, so a cloud
browser can reach them too. The episode timer is switched off: some engines
fire timers early, and a speed limit is not what is being measured.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jev_browser_bridge import Agent  # noqa: E402
from jev_browser_bridge.browser import PageChanged  # noqa: E402
from jev_browser_bridge.cli import load_env  # noqa: E402
from jev_browser_bridge.session import connect, lexmount_session  # noqa: E402

HERE = Path(__file__).resolve().parent
BASE = "https://miniwob.farama.org/demos/miniwob"

# Seed the problem, start the episode with no timer, and return the instruction.
# An engine without Math.seedrandom still gets a problem, just not the same one.
START_JS = """(() => {{
  let seeded = true;
  try {{ Math.seedrandom('{task}-{seed}'); }} catch (e) {{ seeded = false; }}
  core.EPISODE_MAX_TIME = 600000;
  core.startEpisodeReal();
  clearTimeout(core.EP_TIMER); core.EP_TIMER = -1;
  const utterance = core.getUtterance();
  return {{utterance: typeof utterance === 'string' ? utterance : utterance.utterance, seeded}};
}})()"""
READY_JS = "typeof core !== 'undefined' && typeof genProblem === 'function'"

# A cloud session can be dropped by the service mid-run. That is the session,
# not the page: open a fresh one and run the episode again.
LOST = ("ConnectionClosed", "Session with given id not found", "no targets")


def episode(browser, task: str, seed: int, max_steps: int) -> dict:
    record = {"task": task, "seed": seed}
    started = time.time()
    try:
        browser.navigate(f"{BASE}/{task}.html")
        for _ in range(8):             # some engines swap documents right after load
            with contextlib.suppress(PageChanged):
                if browser.evaluate(READY_JS):
                    break
            time.sleep(1)
        start = browser.evaluate(START_JS.format(task=task, seed=seed))
        record.update(goal=start["utterance"], seeded=start["seeded"])
        state, status = None, "max_steps"
        for state in Agent(browser, start["utterance"]).run():
            with contextlib.suppress(PageChanged):
                if browser.evaluate("WOB_DONE_GLOBAL"):
                    status = "episode_end"
                    break
            if state.status in ("done", "blocked"):
                status = state.status
                break
            if len(state.steps) >= max_steps:
                break
        done, reward = browser.evaluate("[WOB_DONE_GLOBAL, WOB_RAW_REWARD_GLOBAL]")
        record.update(done=done, reward=reward, status=status,
                      steps=len(state.steps) if state else 0,
                      ops=[f"{s.operation}:{s.label[:40]}" for s in (state.steps if state else [])])
    except Exception as error:  # noqa: BLE001 - one episode must not end the run
        record["error"] = f"{type(error).__name__}: {str(error)[:200]}"
    record["secs"] = round(time.time() - started, 1)
    record["ok"] = bool(record.get("done")) and (record.get("reward") or 0) > 0
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cdp", metavar="ENDPOINT", help="any CDP address")
    parser.add_argument("--header", action="append", default=[], metavar="NAME: VALUE",
                        help="handshake header for --cdp, e.g. an Authorization token")
    parser.add_argument("--browser", default="light",
                        help="Lexmount browser mode when --cdp is not given (light = Moli)")
    parser.add_argument("--tasks", default=str(HERE / "tasks.txt"))
    parser.add_argument("--seeds", type=int, default=2)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--rotate", type=int, default=20,
                        help="open a fresh session after this many episodes")
    parser.add_argument("--out", required=True)
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    load_env(args.env)

    tasks = [t.strip() for t in Path(args.tasks).read_text().split() if t.strip()]
    finished = set()
    if os.path.exists(args.out):
        for line in Path(args.out).read_text().splitlines():
            r = json.loads(line)
            if not any(k in (r.get("error") or "") for k in LOST):
                finished.add((r["task"], r["seed"]))
    todo = [(t, s) for t in tasks for s in range(args.seeds) if (t, s) not in finished]

    headers = dict(h.split(":", 1) for h in args.header)
    headers = {k.strip(): v.strip() for k, v in headers.items()}

    def open_browser():
        if args.cdp:
            return connect(args.cdp, headers=headers or None, wait_for_page=5.0)
        return lexmount_session(args.browser)

    stack, browser, used = None, None, 0
    with open(args.out, "a") as out:
        for task, seed in todo:
            record = None
            for _ in range(3):
                if browser is None or used >= args.rotate:
                    if stack:
                        with contextlib.suppress(Exception):
                            stack.close()
                    stack = contextlib.ExitStack()
                    try:
                        browser, used = stack.enter_context(open_browser()), 0
                    except Exception as error:  # noqa: BLE001
                        browser = None
                        print(f"# could not open a session: {error}", flush=True)
                        time.sleep(10)
                        continue
                record, used = episode(browser, task, seed, args.max_steps), used + 1
                if not any(k in (record.get("error") or "") for k in LOST):
                    break
                browser = None
            if record is None:
                print("# no session available; stopping so the run can resume later", flush=True)
                break
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            print(f"{task}#{seed}  {'pass' if record['ok'] else 'fail'}  "
                  f"{record.get('steps', '-')} steps  {record['secs']}s  "
                  f"{record.get('error', '')[:80]}", flush=True)
    if stack:
        with contextlib.suppress(Exception):
            stack.close()


if __name__ == "__main__":
    main()
