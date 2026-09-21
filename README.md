# Jev NoLayout

**A browser agent that never asks the layout engine anything.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element. A small model writes text only when the operation is `TYPE_TEXT`.

Built for [Moli](https://browser.lexmount.com), which keeps page structure and interaction state in memory and renders only when a picture is actually needed. Geometry there is a snapshot from the last render, so this agent reads **structure** instead: semantics for what is live, `textContent` for what it says, and element dispatch for what it does.

## Why no layout

Same page, same selectors. The only difference is what the extractor asks:

| Asking | Controls found | Text available |
| --- | --- | --- |
| Geometry — `getBoundingClientRect`, `innerText` | **5** | **25 chars** |
| Structure — semantics, `textContent` | **134** | **89,091 chars** |

*Google Flights on Moli. The DOM is identical in both cases — 146 interactive elements — but 140 of them report a zero-sized box, because the box was measured before the page finished changing.*

Nothing in `snapshot.js` calls `getBoundingClientRect`, `checkVisibility`, `elementFromPoint` or `innerText`. Actions are dispatched on the element, never at a coordinate, so a stale layout cannot misdirect a click.

## The action space

Every observation produces a fresh element table:

```text
[1]  combobox  Where from?                 · Zürich
[2]  combobox  Where to?                   · empty
[3]  textbox   Departure · date picker     · empty
[4]  button    Done · date picker
[5]  button    Done · 2 of 3
...
```

Operations are `CLICK`, `TYPE_TEXT`, `SELECT`, `WAIT`, `DONE` and `BLOCKED`. Only observed elements are ever offered, so the model cannot name one that does not exist.

```text
                      one decision request
                     ┌───────────────────────────┐
page → element table → operation                 │
                     │ click_target              │
                     │ type_text_target          │
                     │ select_target, if present │
                     └─────────────┬─────────────┘
                         use the matching target
                                   │
                    CLICK [7] ─────┤──→ browser
                TYPE_TEXT [1] ─────┘
                          ↓
                 small model → text → browser
```

Target questions are speculative: if the operation is `CLICK`, only `click_target` can execute. Two decisions, **one network round trip**.

### Labels carry location

Dropping the viewport cull surfaces every control with a given name, not just the one on screen. Google's date picker has four buttons that all read `Done`, and only one commits the date. So same-named controls are labelled by where they live:

```text
Done · date picker        ← the one that confirms
Done · 2 of 3
Done · 3 of 3
```

## Try it

```bash
git clone https://github.com/lexmount/jev-nolayout.git
cd jev-nolayout
uv sync
cp .env.example .env
# Add JEV_API_KEY, TEXT_MODEL_API_KEY and your Lexmount credentials.

uv run jev-nolayout https://en.wikipedia.org/wiki/Espresso "Open the article about Latte"
```

```text
  goal     Open the article about Latte
  from     https://en.wikipedia.org/wiki/Espresso
  browser  Moli

   1. CLICK      caffè latte · 1 of 2
       5412 ms   decision 1397 ms   703 actions offered
      clicked
   2. DONE
       3885 ms   decision 1247 ms   282 actions offered

  done  ·  2 steps  ·  9.4s
  https://en.wikipedia.org/wiki/Latte
```

Pass `--browser normal` to run the same agent against standard Chrome. It works there too — reading structure is not a workaround, it is simply a better question.

## In code

```python
from jev_nolayout import Agent, moli_session

with moli_session() as browser:
    browser.navigate("https://docs.python.org/3/")
    for state in Agent(browser, "Go to the Standard Library reference").run():
        print(state.steps[-1])
```

`examples/flights.py` runs a live Google Flights search and verifies the result against the page itself, not against the model's claim of success.

## Status

Multi-step navigation is solid. Heavy single-page applications that swap a field for a popup mid-interaction are not yet reliable — see `examples/flights.py`. Progress and open problems are tracked in the issues.

## License

Apache 2.0
