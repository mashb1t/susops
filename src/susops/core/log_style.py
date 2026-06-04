"""Shared log-line styler.

Splits a raw log line into colored segments. Used by every frontend (TUI's
RichLog, the macOS NSTextView, the Linux GtkTextView) so the colour rules
stay consistent across surfaces.

Each segment is a ``(text, color)`` tuple where ``color`` is one of:

    None     — default text colour
    "tag"    — connection / debug tag prefix (cyan)
    "ok"     — success keywords (green)
    "warn"   — non-fatal status keywords (yellow)
    "err"    — failure keywords (red)
    "dim"    — supplementary detail like PID numbers (gray)
    "info"   — neutral highlight (blue)

Frontends map these labels to concrete colours.
"""
from __future__ import annotations

import re
from typing import List, Tuple

LogSegment = Tuple[str, str | None]

# Order matters — first match wins per character offset. Earlier entries take
# precedence when ranges overlap.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Timestamp at the very start of the line: [HH:MM:SS]. Must match before
    # the generic "tag prefix" rule so the clock doesn't get coloured like a
    # connection tag.
    (re.compile(r"^\[\d{2}:\d{2}:\d{2}\]"), "dim"),
    # Tag prefix at the start of the line: [pi3]  or  [debug]  or  [error].
    # Anchored to either the line start or the position immediately after a
    # timestamp + space so both `[pi3] ...` and `[16:42:03] [pi3] ...` work.
    (re.compile(r"(?:^|(?<=^\[\d{2}:\d{2}:\d{2}\] ))\[[^\]]+\]"), "tag"),
    # Parenthesised PID detail (and similar dim suffixes).
    (re.compile(r"\(PID \d+\)", re.IGNORECASE), "dim"),
    (re.compile(r"\(pid=\d+\)", re.IGNORECASE), "dim"),
    # Port numbers in "port 1234" / "on port 1234"
    (re.compile(r"\b(?:on )?port \d+\b"), "info"),
    # Warning / non-fatal status keywords (must come before "ok" so compound
    # phrases like "already running" / "Connection lost" win over the lone
    # "running" / "Connection restored" keywords).
    (re.compile(
        r"\b(Stopped|stopped|Disabled|disabled|skipping|skipped|"
        r"already running|Connection lost|reconnecting|stale)\b"
    ), "warn"),
    # Success keywords.
    (re.compile(
        r"\b(Started|started|Restored|restored|Assigned|assigned|running|Running|"
        r"Connected|connected|Connection restored|Reconnected)\b"
    ), "ok"),
    # Error keywords.
    (re.compile(r"\b(Failed|failed|Error|error|crash(?:ed)?|denied)\b"), "err"),
]


def style_log_line(line: str) -> List[LogSegment]:
    """Split ``line`` into colored segments using the rule table above.

    Greedy first-match, non-overlapping. Regions not matched by any pattern
    fall through as default-coloured text.
    """
    if not line:
        return [("", None)]

    # Collect all non-overlapping matches in source order.
    spans: list[tuple[int, int, str]] = []
    occupied = [False] * len(line)

    for pat, label in _PATTERNS:
        for m in pat.finditer(line):
            s, e = m.start(), m.end()
            if any(occupied[s:e]):
                continue
            spans.append((s, e, label))
            for i in range(s, e):
                occupied[i] = True

    spans.sort(key=lambda t: t[0])

    out: list[LogSegment] = []
    cursor = 0
    for s, e, label in spans:
        if s > cursor:
            out.append((line[cursor:s], None))
        out.append((line[s:e], label))
        cursor = e
    if cursor < len(line):
        out.append((line[cursor:], None))
    return out
