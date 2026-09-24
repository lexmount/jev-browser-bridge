"""A live Google Flights search on Moli, verified independently of the model."""
import base64
import os
import sys
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jev_browser_bridge import Agent, moli_session  # noqa: E402
from jev_browser_bridge.cli import load_env  # noqa: E402

URL = "https://www.google.com/travel/flights?hl=en"
DEPART = os.environ.get("DEPART_ON", "September 27, 2026")
GOAL = (f"Find one-way flights from Zurich to London on {DEPART}, for one adult in economy. "
        "Stop when matching flight options are visible. Do not select or book a flight.")


def verify(snapshot):
    """Check the page itself, not the model's claim of success."""
    parsed = urlparse(snapshot.url)
    encoded = parse_qs(parsed.query).get("tfs", [""])[0]
    try:
        month, day, year = DEPART.replace(",", "").split()
        months = "JanFebMarAprMayJunJulAugSepOctNovDec"
        stamp = f"{year}-{months.index(month[:3]) // 3 + 1:02d}-{int(day):02d}"
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        date_in_url = stamp.encode() in decoded
    except Exception:
        date_in_url = False
    values = {a.label.split(" · ")[0].strip(): (a.current_value or a.value)
              for a in snapshot.actions}
    flights = [a.label for a in snapshot.actions if "Select flight" in a.label]
    return {
        "search_page": parsed.path == "/travel/flights/search",
        "origin": values.get("Where from?") == "Zürich",
        "destination": values.get("Where to?") == "London",
        "date_in_url": date_in_url,
        "has_results": bool(flights),
    }


def main():
    load_env()
    with moli_session(os.environ.get("BROWSER_MODE", "light")) as browser:
        browser.navigate(URL)
        agent = Agent(browser, GOAL, on_step=lambda s: print(
            f"  {s.elapsed_ms:>6} ms  {s.operation:<10} {s.label[:46]}", flush=True))
        state = None
        for state in agent.run():  # noqa: B007 - we want the last yield
            pass
        checks = verify(browser.observe())

    print(f"\n  {state.status}  {len(state.steps)} steps  {state.elapsed_ms / 1000:.1f}s")
    for name, ok in checks.items():
        print(f"    {'PASS' if ok else 'FAIL'}  {name}")
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
