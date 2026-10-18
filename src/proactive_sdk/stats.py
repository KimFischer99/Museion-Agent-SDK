"""W6 delivery statistics: pure aggregation + ``--since`` parsing.

The report answers two questions the ledger alone cannot: do the
notifications we send get used (per decision reason), and do they come
from sources that spend their context slots on something worth reading
(per source). Thresholds are *never* adjusted automatically; the report
only surfaces numbers for a human to act on.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

__all__ = ["aggregate_delivery_stats", "parse_since"]

_SINCE_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_UNIT_MS = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
DEFAULT_SINCE = "7d"
UNKNOWN_GROUP = "unknown"
UNSPECIFIED_REASON = "unspecified"


def parse_since(text: str | None, *, now_ms: int) -> int:
    """Parse ``7d`` / ``24h`` / ``30m`` / ISO-8601 into an epoch-ms instant.

    Anything unparseable is a configuration error, not a silent default —
    a report about the wrong window is worse than no report.
    """
    raw = DEFAULT_SINCE if text is None else text.strip()
    if not raw:
        raise ValueError("--since must not be empty (use e.g. 7d, 24h, or an ISO timestamp)")
    match = _SINCE_RE.match(raw)
    if match is not None:
        amount, unit = int(match.group(1)), match.group(2).lower()
        if amount <= 0:
            raise ValueError("--since amount must be positive")
        return now_ms - amount * _UNIT_MS[unit]
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise ValueError(f"cannot parse --since {raw!r} (use e.g. 7d, 24h, or an ISO timestamp)") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _bucket(signals: set[str]) -> str:
    # Strongest signal wins: a message that got a reply is accepted even if
    # the host backfill had already marked it ignored a minute earlier.
    if "replied" in signals:
        return "accepted"
    if "dismissed" in signals or "complained" in signals:
        return "negative"
    if any(signal in ("dismissed", "complained") for signal in signals):
        return "negative"
    if "ignored_24h" in signals:
        return "weak_ignored"
    return "pending"


def _aggregate(
    messages: Iterable[dict[str, Any]], feedback_by_message: dict[str, set[str]], key: str
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, int]] = {}
    for message in messages:
        group_key = message.get(key)
        label = group_key if isinstance(group_key, str) and group_key else (
            UNSPECIFIED_REASON if key == "reason" else UNKNOWN_GROUP
        )
        bucket = _bucket(feedback_by_message.get(str(message.get("message_id")), set()))
        row = groups.setdefault(
            label,
            {"sent": 0, "accepted": 0, "negative": 0, "weak_ignored": 0, "pending": 0},
        )
        row["sent"] += 1
        row[bucket] += 1
    rows: list[dict[str, Any]] = []
    for label, counts in groups.items():
        sent = counts["sent"]
        rows.append(
            {
                key: label,
                **counts,
                "acceptance_rate": round(counts["accepted"] / sent, 4) if sent else 0.0,
            }
        )
    rows.sort(key=lambda row: (-row["sent"], row[key]))
    return rows


def aggregate_delivery_stats(
    messages: Iterable[dict[str, Any]],
    feedback: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate sent messages + delivery feedback into the two W6 reports.

    ``messages``: ``{message_id, reason, source_id}`` (reason/source may be
    absent/None). ``feedback``: ``{message_id, signal, source}``. Percentages
    are fractions rounded to four decimals; ``ignored_24h`` is reported
    separately as a weak signal and never folded into the acceptance rate.
    """
    messages = list(messages)
    feedback_by_message: dict[str, set[str]] = {}
    for row in feedback:
        message_id = row.get("message_id")
        signal = row.get("signal")
        if not isinstance(message_id, str) or not isinstance(signal, str):
            continue
        feedback_by_message.setdefault(message_id, set()).add(signal)
    by_reason = _aggregate(messages, feedback_by_message, "reason")
    by_source = _aggregate(messages, feedback_by_message, "source_id")
    totals = {
        "sent": len(messages),
        "accepted": sum(row["accepted"] for row in by_reason),
        "negative": sum(row["negative"] for row in by_reason),
        "weak_ignored": sum(row["weak_ignored"] for row in by_reason),
        "pending": sum(row["pending"] for row in by_reason),
    }
    totals["acceptance_rate"] = (
        round(totals["accepted"] / totals["sent"], 4) if totals["sent"] else 0.0
    )
    return {"totals": totals, "by_reason": by_reason, "by_source": by_source}
