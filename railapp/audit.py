"""Cross-day refund audit: batch validation and row extraction.

A day batch takes part in refund auditing only when the audit feature is
enabled at publish time *and* the source CSV contains a ``sale_day`` column.
Such batches must also carry ``ticket_id`` and ``amount`` columns and every
row has to satisfy:

  * ``ticket_id`` is non-empty;
  * ``amount`` is a canonical base-10 integer amount in cents — sales
    (``sale_day`` empty) are positive, refunds (``sale_day`` filled with the
    original sale day) are negative;
  * a filled ``sale_day`` is a strict ``YYYY-MM-DD`` date and must not be
    later than the batch (refund) day.

Extracted rows are normalised once, at publish time, and stored next to the
immutable publication so audit reports never need to re-read raw CSV blobs.
"""

import re
from datetime import date

SALE_DAY_COLUMN = "sale_day"
TICKET_COLUMN = "ticket_id"
AMOUNT_COLUMN = "amount"
REQUIRED_COLUMNS = (SALE_DAY_COLUMN, TICKET_COLUMN, AMOUNT_COLUMN)

# Mirrors convert._INT_RE / its 64-bit range so audit integers follow exactly
# the same canonical rule as the downstream integer columns.
_INT_RE = re.compile(r"^[+-]?[0-9]+$")
_INT_MIN = -(2**63)
_INT_MAX = 2**63 - 1
MAX_VIOLATIONS = 200

# Machine-readable exclusion reasons shown on the audit page.
REASON_AUDIT_DISABLED = "audit_disabled"
REASON_PUBLISHED_BEFORE_ENABLED = "published_before_enabled"
REASON_MISSING_SALE_DAY = "missing_sale_day_column"

EXCLUSION_REASONS = {
    REASON_AUDIT_DISABLED: "核查功能当前已停用",
    REASON_PUBLISHED_BEFORE_ENABLED: "发布时核查功能未启用",
    REASON_MISSING_SALE_DAY: "原件缺少 sale_day 列，不参与核查",
}

# Report row statuses.
STATUS_OK = "ok"
STATUS_OVER_REFUND = "over_refund"
STATUS_PENDING = "pending"

PENDING_NOT_FOUND = "original_not_found"
PENDING_DUPLICATE = "duplicate_original"

STATUS_LABELS = {
    STATUS_OK: "正常",
    STATUS_OVER_REFUND: "超退",
    STATUS_PENDING: "待核",
}

PENDING_REASONS = {
    PENDING_NOT_FOUND: "找不到对应原票",
    PENDING_DUPLICATE: "原票不唯一",
}


def parse_iso_day(value):
    """Strict YYYY-MM-DD parse; returns None for anything else."""
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if parsed.isoformat() != value:
        return None
    return parsed


def _parse_int(value):
    text = value.strip()
    if not _INT_RE.match(text) or len(text) > 32:
        return None
    number = int(text)
    if number < _INT_MIN or number > _INT_MAX:
        return None
    return number


def inspect_batch(parsed, batch_day, enabled):
    """Validate and extract audit rows from a parsed upload.

    Returns a dict with ``has_sale_day``, ``applicable`` (feature enabled and
    the column present), a capped ``violations`` list and the normalised
    ``rows`` that passed every check.  Validation only runs for batches the
    audit feature actually covers.
    """
    header_index = {name: i for i, name in enumerate(parsed.header)}
    has_sale_day = SALE_DAY_COLUMN in header_index
    result = {
        "enabled": bool(enabled),
        "has_sale_day": has_sale_day,
        "applicable": False,
        "violations": [],
        "violations_truncated": False,
        "rows": [],
        "valid_row_count": 0,
    }
    if not has_sale_day or not enabled:
        return result
    result["applicable"] = True
    batch_date = parse_iso_day(batch_day)

    missing = [
        name
        for name in (TICKET_COLUMN, AMOUNT_COLUMN)
        if name not in header_index
    ]
    for name in missing:
        result["violations"].append(
            {
                "row": None,
                "line": None,
                "column": name,
                "value": None,
                "reason": "含 sale_day 列时必须同时提供该核查必需列",
            }
        )
    if missing:
        # Row-level checks are impossible without the sibling columns.
        return result

    pos_ticket = header_index[TICKET_COLUMN]
    pos_amount = header_index[AMOUNT_COLUMN]
    pos_sale = header_index[SALE_DAY_COLUMN]

    for row_no, (line_no, fields) in enumerate(parsed.rows, start=1):
        ticket_raw = fields[pos_ticket]
        amount_raw = fields[pos_amount]
        sale_raw = fields[pos_sale]
        ticket = ticket_raw.strip()
        sale_day = sale_raw.strip()
        is_refund = sale_day != ""
        problems = []

        if not ticket:
            problems.append((TICKET_COLUMN, ticket_raw, "ticket_id 不能为空"))

        amount = _parse_int(amount_raw)
        if amount is None:
            problems.append((AMOUNT_COLUMN, amount_raw, "amount 必须是整数分金额"))
        elif is_refund and amount >= 0:
            problems.append((AMOUNT_COLUMN, amount_raw, "退票（sale_day 已填）金额必须为负整数"))
        elif not is_refund and amount <= 0:
            problems.append((AMOUNT_COLUMN, amount_raw, "售票（sale_day 留空）金额必须为正整数"))

        parsed_sale_day = None
        if is_refund:
            parsed_sale_day = parse_iso_day(sale_day)
            if parsed_sale_day is None:
                problems.append(
                    (SALE_DAY_COLUMN, sale_raw, "sale_day 必须留空或填写 YYYY-MM-DD 日期")
                )
            elif batch_date is not None and parsed_sale_day > batch_date:
                problems.append(
                    (SALE_DAY_COLUMN, sale_raw, "原售票日不得晚于退票日（批次日期）")
                )

        if problems:
            for column, value, reason in problems:
                if len(result["violations"]) < MAX_VIOLATIONS:
                    result["violations"].append(
                        {
                            "row": row_no,
                            "line": line_no,
                            "column": column,
                            "value": (value or "")[:200],
                            "reason": reason,
                        }
                    )
            continue

        result["rows"].append(
            {
                "row_index": row_no,
                "source_line": line_no,
                "ticket_id": ticket,
                "amount": amount,
                "sale_day": sale_day if is_refund else None,
            }
        )

    result["valid_row_count"] = len(result["rows"])
    result["violations_truncated"] = len(result["violations"]) >= MAX_VIOLATIONS
    return result


def is_included(publication, enabled, enabled_at=None):
    """Inclusion predicate shared by overview and report queries.

    Eligibility is frozen on the publication row at publish time: a revision
    is eligible only when the feature was enabled and its original CSV
    carried ``sale_day``.  Reports additionally require the feature to be
    enabled right now, so disabling suspends auditing without rewriting the
    immutable history; re-enabling restores every eligible revision.
    """
    if not enabled:
        return False
    return bool(publication.get("audit_eligible"))


def exclusion_reason(publication, enabled, enabled_at=None):
    if not publication.get("has_sale_day"):
        return REASON_MISSING_SALE_DAY
    if not enabled:
        return REASON_AUDIT_DISABLED
    # has_sale_day but not eligible means the feature was off at publish time.
    return REASON_PUBLISHED_BEFORE_ENABLED


def build_report(refund_day, current_rev, audit_rows, included_days, generated_at):
    """Build the refund-day report from one consistent set of audit rows.

    ``audit_rows`` must already be restricted to the current revisions of the
    included days (a single read-transaction snapshot).  Groups are the
    tickets that have any refund on the selected refund day; their
    cumulative refund total deliberately spans **every** included day up to
    and including that day, so checking day by day cannot hide an over-refund
    spread across multiple days.
    """
    sales = {}
    refunds = {}
    for row in audit_rows:
        if row["sale_day"] is None:
            sales.setdefault((row["day"], row["ticket_id"]), []).append(row)
        else:
            key = (row["sale_day"], row["ticket_id"])
            bucket = refunds.setdefault(
                key, {"refund_total": 0, "refund_count": 0, "items": []}
            )
            # Refund amounts are stored negative; the report shows the
            # cumulative refunded amount as a positive number of cents.
            bucket["refund_total"] += -row["amount"]
            bucket["refund_count"] += 1
            bucket["items"].append(row)

    groups = []
    seen = set()
    for row in audit_rows:
        # Only groups with at least one refund on the selected day are
        # opened for review; their totals still span all included days.
        if row["day"] != refund_day or row["sale_day"] is None:
            continue
        key = (row["sale_day"], row["ticket_id"])
        if key in seen:
            continue
        seen.add(key)
        sale_day, ticket_id = key
        bucket = refunds[key]
        # Trace items in chronological (day, then source row) order.
        bucket_items = sorted(
            bucket["items"], key=lambda r: (r["day"], r["row_index"], r["id"])
        )
        group = {
            "ticket_id": ticket_id,
            "sale_day": sale_day,
            "refund_day": refund_day,
            "refund_total": bucket["refund_total"],
            "refund_count": bucket["refund_count"],
            "refunds": [_row_ref(item) for item in bucket_items],
        }
        candidates = sales.get(key, [])
        if not candidates:
            group["status"] = STATUS_PENDING
            group["pending_reason"] = PENDING_NOT_FOUND
            group["original"] = None
        elif len(candidates) > 1:
            group["status"] = STATUS_PENDING
            group["pending_reason"] = PENDING_DUPLICATE
            group["original"] = None
            group["candidate_count"] = len(candidates)
            group["candidates"] = [_row_ref(item) for item in candidates]
        else:
            original = candidates[0]
            group["original"] = _row_ref(original)
            if bucket["refund_total"] > original["amount"]:
                group["status"] = STATUS_OVER_REFUND
            else:
                group["status"] = STATUS_OK
        groups.append(group)

    order = {STATUS_OVER_REFUND: 0, STATUS_PENDING: 1, STATUS_OK: 2}
    groups.sort(key=lambda g: (order[g["status"]], g["sale_day"], g["ticket_id"]))

    counts = {STATUS_OVER_REFUND: 0, STATUS_PENDING: 0, STATUS_OK: 0}
    for group in groups:
        counts[group["status"]] += 1

    return {
        "day": refund_day,
        "rev": current_rev,
        "generated_at": generated_at,
        "included_days": included_days,
        "groups": groups,
        "summary": {
            "group_count": len(groups),
            "over_refund_count": counts[STATUS_OVER_REFUND],
            "pending_count": counts[STATUS_PENDING],
            "ok_count": counts[STATUS_OK],
        },
    }


def _row_ref(row):
    """Public shape of an audit row trace entry."""
    return {
        "day": row["day"],
        "rev": row["rev"],
        "row_index": row["row_index"],
        "source_line": row["source_line"],
        "ticket_id": row["ticket_id"],
        "amount": row["amount"],
        "sale_day": row["sale_day"],
    }
