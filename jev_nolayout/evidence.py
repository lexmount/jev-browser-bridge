"""Choose which of a page's rows a decision gets to read.

A structure-first snapshot hands over the whole document: 1,000 to 2,000 rows
and 50,000 to 150,000 characters on a long article. No decision should read
all of that, and the first few thousand characters are the wrong few thousand
-- they are navigation, and the answer is further down. So the rows are ranked
against the goal and the best ones are kept, in page order, up to a budget.

This is deliberately plain: rare-word overlap, a bonus for numbers, and two
separate pools so that table rows and prose cannot starve each other. Every
part of it is there because a simpler version measurably lost an answer.
"""
from __future__ import annotations

import math
import os
import re

# How many characters of page text one decision sees. Measured on four long
# articles: at 6,000 the answer was dropped every time, at 12,000 three times
# in four came back, at 20,000 all four.
BUDGET = int(os.environ.get("JEV_EVIDENCE_CHARS", "20000"))

# Besides grammar, the words a goal uses to say what KIND of thing it wants
# ("open the article about X linked from this page") rather than what it is
# about. Left in, they match on the wrong thing: "article" pulled a citation
# ending "... Article 5" above the actual article link, and the model took it.
_STOP_WORDS = (
    "a an the of in on at to for is are was were be by with from and or as it its this "
    "that what which how many much do does did can could should would will your you my me "
    "open find go click type select search report page article link linked about here"
)
_STOP = frozenset(_STOP_WORDS.split())


def keywords(goal: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", goal.lower())
            if w not in _STOP and len(w) > 2]


def _tidy(row: str) -> str:
    # Re-insert the space a node boundary sometimes swallows between a word and
    # a number ("to9 bars"). Letters and closing brackets only: including , or
    # . once split 8,848.86 into "8, 848. 86".
    row = re.sub(r"([A-Za-z)])(\d)", r"\1 \2", row)
    row = re.sub(r"(\d)([A-Za-z(])", r"\1 \2", row)
    return re.sub(r"\s{2,}", " ", row).strip()


def select(rows: list[str], goal: str, budget: int = BUDGET) -> str:
    """The rows that bear on `goal`, in page order, within `budget` characters."""
    if not rows:
        return ""
    tidy = [_tidy(r) for r in rows]
    words = keywords(goal)
    if not words:
        return " ".join(tidy)[:budget]

    low = [r.lower() for r in tidy]
    n = len(low)
    # Rare words carry the signal. Plain hit-counting let "espresso" -- on
    # nearly every row of its own article -- decide the ranking on its own.
    idf = {w: math.log(n / max(1, sum(1 for t in low if w in t))) + 0.1 for w in words}

    # Table rows and prose compete in separate pools. In one pool, whichever
    # heuristic was winning starved the other: boosting table rows fixed an
    # infobox answer and immediately lost a sentence answer on another page.
    table, prose = [], []
    for i, t in enumerate(low):
        score = sum(idf[w] for w in words if w in t)
        if score <= 0:
            continue
        if re.search(r"\d", t):
            score *= 1.4                     # facts are usually numbers
        if " | " in rows[i]:
            table.append((score * 1.5, i))
        else:
            prose.append((score * (1.2 if len(t) > 60 else 1.0), i))
    table.sort(reverse=True)
    prose.sort(reverse=True)

    keep: set[int] = set()
    used = 0

    def take(ranked: list[tuple[float, int]], quota: float) -> None:
        nonlocal used
        spent = 0
        for _, i in ranked:
            if spent > quota:
                return
            # A hit rarely stands alone: keep the row before it and a few
            # after, which is where the value of a label usually is.
            for j in range(max(0, i - 1), min(n, i + 3)):
                if j not in keep:
                    keep.add(j)
                    spent += len(tidy[j]) + 1
                    used += len(tidy[j]) + 1

    take(table, budget * 0.35)
    take(prose, budget * 0.60)
    for i in range(n):                       # spend any slack on early prose
        if used > budget:
            break
        if i not in keep and len(tidy[i]) > 80:
            keep.add(i)
            used += len(tidy[i]) + 1
    return " ".join(tidy[i] for i in sorted(keep))[:budget]
