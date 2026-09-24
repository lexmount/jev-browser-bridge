# Jev NoLayout

**A browser agent that never asks the layout engine anything.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element. A small model writes text only when the operation is `TYPE_TEXT`.

Built for [Moli](https://browser.lexmount.com), which keeps page structure and interaction state in memory and renders only when a picture is actually needed. Geometry there is a snapshot from the last render, so this agent reads **structure** instead: semantics for what is live, `textContent` for what it says, and element dispatch for what it does.

Because it never asks about layout, it runs unchanged on **any browser that speaks CDP** — including the ones that never lay a page out at all.

## Why no layout

Same page, same selectors. The only difference is what the extractor asks:

| Asking | Controls found | Text available |
| --- | --- | --- |
| Geometry — `getBoundingClientRect`, `innerText` | **5** | **25 chars** |
| Structure — semantics, `textContent` | **134** | **89,091 chars** |

*Google Flights on Moli. The DOM is identical in both cases — 146 interactive elements — but 140 of them report a zero-sized box, because the box was measured before the page finished changing.*

Nothing in `snapshot.js` calls `getBoundingClientRect`, `checkVisibility`, `elementFromPoint` or `innerText`. Actions are dispatched on the element, never at a coordinate, so a stale layout cannot misdirect a click.

## Every browser

The same `snapshot.js`, next to a reader that filters by position, on six browsers. Controls / characters of page text:

| Browser | Page | Structure | Geometry |
| --- | --- | --- | --- |
| Moli | Google Flights | 155 / 34,189 | **5 / 7** |
| Chrome | Google Flights | 145 / 25,535 | 17 / 69 |
| chrome-headless-shell | Google Flights | 145 / 25,535 | 20 / 217 |
| Cloudflare Kitesurf | Google Flights | 209 / 36,569 | **error** — no `checkVisibility` |
| Lightpanda | Wikipedia: Jupiter | 2,875 / 137,723 | **18 / 221** — no layout at all |
| Obscura (no-render build) | Wikipedia: Jupiter | 2,876 / 137,745 | **250 / 6,000** — placeholder boxes, so everything counts as on screen |

A reader that asks where things are fails differently on every engine without a real layout: too little, too much, or an exception. Asking what things are gives the same answer everywhere.

Every Chromium, local or hosted, reads the same page identically: 2,876 controls and the same text on the Jupiter article, every time.

### End to end

Five goals — switch a page's language, follow a footer link, jump to another reference page, open a linked article, type a search and open the result — each run twice, success judged by the URL the agent ends on:

| Browser | How | Result | Steps |
| --- | --- | --- | --- |
| Moli (Lexmount) | `lexmount_session()` | 10/10 | 2.2 |
| Chrome image (Lexmount) | `lexmount_session("normal")` | 10/10 | 2.3 |
| Cloudflare Kitesurf | `connect(url, headers=…)` | 10/10 | 2.2 |
| Cloudflare Browser Run, Chromium | `connect(url, headers=…)` | 7/7 ¹ | 2.1 |
| Browserbase | `connect(connectUrl)` | 10/10 | 2.2 |
| Chrome, chrome-headless-shell, Playwright Chromium | `connect("http://127.0.0.1:9222")` | 10/10 each | 2.2 |
| Lightpanda | `connect("http://127.0.0.1:9222")` | 9/10 ² | 2.2 |
| Obscura, no-render build | `connect(...)` | 9/10 ³ | 2.3 |
| browserless, Steel, chromedp, Kernel (self-hosted) | `connect("http://127.0.0.1:<port>")` | 10/10 each | 2.2 |
| Selenium Grid | `selenium_session("http://127.0.0.1:4444")` | 5/5 | 2.2 |

¹ Every run that got a browser; the rest were refused by the free plan's daily quota. ² The miss reached the article and kept clicking. ³ The miss was Obscura's own 30-second navigation deadline.

Two limits of the browsers themselves, not of this layer: Lightpanda does not run enough of Google Flights' JavaScript to render the page, and Kitesurf's public playground meters CPU per page tightly enough that a very large page (the full Jupiter article) runs it out — through an authenticated Cloudflare account it reads the same page in full.

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

Links that share a name *and* a destination are one control repeated, and are offered once.

### The page text is retrieved, not truncated

A structure-first snapshot hands over the whole document — 50,000 to 150,000 characters on a long article. The first few thousand are navigation. So the page is split into rows (a table row stays one row, `Elevation | 8,848.86 m`) and the rows that bear on the goal are kept, in page order, up to `JEV_EVIDENCE_CHARS` (default 20,000). On four long articles, a 6,000-character prefix missed the answer every time; retrieval at 20,000 kept it every time.

## Try it

```bash
git clone https://github.com/lexmount/jev-nolayout.git
cd jev-nolayout
uv sync --extra lexmount
cp .env.example .env
# Add JEV_API_KEY and your Lexmount credentials.
# TEXT_MODEL_API_KEY is only needed for goals that type into a field.

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

`--browser normal` runs the same agent on Lexmount's standard Chrome. `--cdp` runs it on any other browser — a `ws://` URL, or the `http://host:port` a local browser serves:

```bash
lightpanda serve --port 9222 &
uv run jev-nolayout --cdp http://127.0.0.1:9222 https://en.wikipedia.org/wiki/Espresso "Switch to the Deutsch edition"
```

## In code

```python
from jev_nolayout import Agent, lexmount_session

with lexmount_session() as browser:          # Moli
    browser.navigate("https://docs.python.org/3/")
    for state in Agent(browser, "Go to the Standard Library reference").run():
        print(state.steps[-1])
```

Any other browser is one line different:

```python
from jev_nolayout import connect

with connect("http://127.0.0.1:9222") as browser:
    ...
```

`connect()` takes a `ws://` / `wss://` URL or an `http(s)://` address serving `/json/version`, uses the browser's page or opens one, and closes what it opened. It also handles what hosted browsers tend to need:

- `headers=` for services that authenticate the websocket handshake (Cloudflare: `{"Authorization": "Bearer …"}`); `user:pass@` in the URL is sent as Basic auth.
- A websocket address advertised from inside a container (`ws://0.0.0.0:3000`, a container IP) is pointed back at the address you connected to.
- A 429 on connect is waited out when it asks for seconds, and reported plainly when it asks for hours.

`selenium_session(grid)` does the same for a Selenium Grid, which hands out CDP only per session.

## What it handles

Multi-step navigation, autocomplete fields, calendar widgets built from unlabelled `<div>`s, and controls that share a name — all without a single layout query.

`examples/quickstart.py` is the shortest complete run on Moli. `examples/flights.py` drives a live Google Flights search and checks the result against the page itself rather than against the model's claim of success.

## License

Apache 2.0
