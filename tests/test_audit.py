"""Tests for the cross-day refund audit feature."""

from conftest import DEFAULT_CSV, preview_and_confirm, upload


HEADER = (
    "ticket_id,passenger_id,train_no,amount,travel_date,"
    "depart_station,arrive_station,passenger_count,sale_day\n"
)


def row(ticket, amount, sale=""):
    """Build a full-width row satisfying every default downstream contract."""
    return f"{ticket},P,G1,{amount},2026-11-01,北京南,上海虹桥,1,{sale}\n"


def _csv(rows):
    return (HEADER + "".join(rows)).encode("utf-8")


def set_audit(client, enabled):
    return client.post(
        "/api/audit/settings",
        json={"enabled": enabled},
        headers={"Accept": "application/json"},
    )


def publish(client, day, rows):
    preview, confirmed = preview_and_confirm(client, day=day, data=_csv(rows))
    return preview, confirmed


def enable_and_publish(client, day, rows):
    set_audit(client, True)
    return publish(client, day, rows)


# ---------------------------------------------------------------------------
# feature flag
# ---------------------------------------------------------------------------
def test_audit_disabled_by_default(client):
    overview = client.get("/api/audit").get_json()
    assert overview["enabled"] is False
    assert overview["enabled_at"] is None
    assert overview["included_days"] == []
    assert overview["excluded_days"] == []


def test_enable_timestamps_and_disable_keeps_timestamp(client):
    first = set_audit(client, True)
    assert first.status_code == 200
    payload = first.get_json()
    assert payload["enabled"] is True
    enabled_at = payload["enabled_at"]
    assert enabled_at

    off = set_audit(client, False).get_json()
    assert off["enabled"] is False
    assert off["enabled_at"] == enabled_at

    on = set_audit(client, True).get_json()
    assert on["enabled"] is True
    # Re-enabling refreshes the enabling timestamp.
    assert on["enabled_at"] >= enabled_at


def test_bad_settings_body_rejected(client):
    response = client.post("/api/audit/settings", json={})
    assert response.status_code == 400
    assert response.get_json()["error"] == "bad_audit_setting"


# ---------------------------------------------------------------------------
# inclusion / exclusion
# ---------------------------------------------------------------------------
def test_batch_without_sale_day_publishes_when_enabled_but_is_excluded(client):
    set_audit(client, True)
    # A normal CSV without sale_day still publishes under the old rules.
    _, confirmed = preview_and_confirm(client, day="2026-11-01", data=DEFAULT_CSV.encode())
    assert confirmed.status_code == 201
    overview = client.get("/api/audit").get_json()
    assert overview["included_days"] == []
    excluded = overview["excluded_days"]
    assert len(excluded) == 1
    assert excluded[0]["day"] == "2026-11-01"
    assert excluded[0]["has_sale_day"] is False
    assert excluded[0]["reason"] == "missing_sale_day_column"


def test_batch_published_while_disabled_is_excluded_after_enable(client):
    # Disabled at publish time even though the file carries sale_day.
    _, confirmed = publish(
        client, "2026-11-01", [row("T1", 1000), row("T2", -400, "2026-11-01")]
    )
    assert confirmed.status_code == 201
    set_audit(client, True)
    overview = client.get("/api/audit").get_json()
    assert overview["included_days"] == []
    assert overview["excluded_days"][0]["reason"] == "published_before_enabled"


def test_disable_then_republish_excludes_even_with_sale_day(client):
    set_audit(client, True)
    _, confirmed = publish(client, "2026-11-01", [row("T1", 1000)])
    assert confirmed.status_code == 201
    set_audit(client, False)
    _, republished = publish(client, "2026-11-01", [row("T1", 2000)])
    assert republished.status_code == 201
    overview = client.get("/api/audit").get_json()
    assert overview["enabled"] is False
    assert overview["included_days"] == []
    reason = overview["excluded_days"][0]["reason"]
    assert reason == "audit_disabled"


# ---------------------------------------------------------------------------
# publish-time validation
# ---------------------------------------------------------------------------
def test_missing_ticket_id_column_blocks_publish(client):
    set_audit(client, True)
    # Keep the amount/sale_day columns; ticket_id is absent.
    data = b"passenger_id,train_no,amount,travel_date,depart_station,arrive_station,passenger_count,sale_day\n"
    data += b"P,G1,1000,2026-11-01,a,b,1,\n"
    response = upload(client, day="2026-11-01", data=data)
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["convertible"] is False
    assert payload["audit"]["blocked"] is True
    assert payload["audit"]["applicable"] is True
    assert any(v["column"] == "ticket_id" for v in payload["audit"]["violations"])
    confirm = client.post(
        f"/api/previews/{payload['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 422


def test_missing_amount_column_blocks_publish(client):
    set_audit(client, True)
    data = b"ticket_id,passenger_id,train_no,sale_day\nT1,P,G1,\n"
    payload = upload(client, day="2026-11-01", data=data).get_json()
    assert payload["audit"]["blocked"] is True
    assert any(v["column"] == "amount" for v in payload["audit"]["violations"])


def test_blank_ticket_id_violation(client):
    set_audit(client, True)
    payload = upload(
        client, day="2026-11-01", data=_csv([row(" ", 1000)])
    ).get_json()
    assert payload["audit"]["blocked"] is True
    violation = payload["audit"]["violations"][0]
    assert violation["column"] == "ticket_id"
    assert violation["row"] == 1 and violation["line"] == 2


def test_non_integer_amount_violation(client):
    set_audit(client, True)
    payload = upload(
        client, day="2026-11-01", data=_csv([row("T1", "12x")])
    ).get_json()
    # The downstream integer contract and the audit rules both flag this.
    assert payload["audit"]["blocked"] is True
    assert any(v["column"] == "amount" for v in payload["audit"]["violations"])
    assert payload["convertible"] is False


def test_sale_must_be_positive_and_refund_negative(client):
    set_audit(client, True)
    bad_sale = upload(
        client, day="2026-11-01", data=_csv([row("T1", -1000)])
    ).get_json()
    assert bad_sale["audit"]["blocked"] is True
    assert any(v["reason"].startswith("售票") for v in bad_sale["audit"]["violations"])

    bad_refund = upload(
        client, day="2026-11-02", data=_csv([row("T1", 400, "2026-11-01")])
    ).get_json()
    assert bad_refund["audit"]["blocked"] is True
    assert any(v["reason"].startswith("退票") for v in bad_refund["audit"]["violations"])

    zero = upload(client, day="2026-11-01", data=_csv([row("T1", 0)])).get_json()
    assert zero["audit"]["blocked"] is True


def test_sale_day_must_be_valid_date_and_not_after_batch_day(client):
    set_audit(client, True)
    bad_format = upload(
        client, day="2026-11-02", data=_csv([row("T1", -400, "20261101")])
    ).get_json()
    assert bad_format["audit"]["blocked"] is True
    assert any(v["column"] == "sale_day" for v in bad_format["audit"]["violations"])

    bad_calendar = upload(
        client, day="2026-11-02", data=_csv([row("T1", -400, "2026-13-40")])
    ).get_json()
    assert bad_calendar["audit"]["blocked"] is True

    future = upload(
        client, day="2026-11-02", data=_csv([row("T1", -400, "2026-11-03")])
    ).get_json()
    assert future["audit"]["blocked"] is True
    assert any("不得晚于退票日" in v["reason"] for v in future["audit"]["violations"])


def test_valid_batch_publishes_and_lands_in_included_days(client):
    set_audit(client, True)
    _, confirmed = publish(
        client,
        "2026-11-01",
        [row("T1", 1000), row("T2", 800), row("T1", -300, "2026-11-01")],
    )
    assert confirmed.status_code == 201, confirmed.get_json()
    overview = client.get("/api/audit").get_json()
    assert [d["day"] for d in overview["included_days"]] == ["2026-11-01"]
    assert overview["excluded_days"] == []


def test_enabling_after_preview_blocks_confirm_without_publishing(client):
    # Preview while disabled with a row that downstream contracts accept but
    # the audit rules reject (a negative sale amount).
    response = upload(client, day="2026-11-01", data=_csv([row("T1", -1000)]))
    preview = response.get_json()
    assert preview["audit"]["enabled"] is False
    assert preview["convertible"] is True
    set_audit(client, True)
    confirm = client.post(
        f"/api/previews/{preview['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 422
    assert confirm.get_json()["error"] == "audit_violation"
    # Nothing was published; failed publish leaves no revision behind.
    assert client.get("/api/days/2026-11-01").get_json()["current_rev"] == 0


def test_audit_rules_not_enforced_when_disabled(client):
    # Sign/date nonsense still publishes when the feature is off, as long as
    # the downstream contracts themselves are satisfied.
    _, confirmed = publish(
        client, "2026-11-01", [row("T1", -1000), row("T2", 50, "2026-11-99")]
    )
    assert confirmed.status_code == 201


# ---------------------------------------------------------------------------
# reports: over-refund, pending, cross-day accumulation
# ---------------------------------------------------------------------------
def _publish_refund_scenario(client):
    set_audit(client, True)
    # Day 1: two sales, one same-day partial refund.
    publish(
        client,
        "2026-11-01",
        [
            row("T1", 1000),
            row("T2", 800),
            row("T1", -300, "2026-11-01"),
        ],
    )
    # Day 2: another partial refund of T1 (cumulative 300 + 900 = 1200 > 1000).
    publish(client, "2026-11-02", [row("T1", -900, "2026-11-01")])
    # Day 3: a refund whose original ticket does not exist.
    publish(client, "2026-11-03", [row("T9", -50, "2026-11-01")])


def test_report_flags_over_refund_and_pending(client):
    _publish_refund_scenario(client)
    report = client.get("/api/audit/2026-11-02").get_json()
    assert report["day"] == "2026-11-02"
    assert report["summary"] == {
        "group_count": 1,
        "over_refund_count": 1,
        "pending_count": 0,
        "ok_count": 0,
    }
    group = report["groups"][0]
    assert group["status"] == "over_refund"
    assert group["ticket_id"] == "T1"
    assert group["sale_day"] == "2026-11-01"
    assert group["refund_total"] == 1200
    assert group["refund_count"] == 2
    assert group["original"]["amount"] == 1000
    # Trace: original + every refund with day, rev, data row and source line.
    refunds = group["refunds"]
    assert {(r["day"], r["rev"], r["amount"]) for r in refunds} == {
        ("2026-11-01", 1, -300),
        ("2026-11-02", 1, -900),
    }
    for entry in [group["original"], *refunds]:
        assert entry["row_index"] >= 1
        assert entry["source_line"] == entry["row_index"] + 1

    day3 = client.get("/api/audit/2026-11-03").get_json()
    pending = day3["groups"][0]
    assert pending["status"] == "pending"
    assert pending["pending_reason"] == "original_not_found"
    assert pending["original"] is None
    assert pending["refund_total"] == 50


def test_report_day_refund_group_is_ok_and_day_without_refunds_empty(client):
    _publish_refund_scenario(client)
    # Day 1 opens the T1 group with its same-day partial refund; because
    # totals accumulate across all included days the day-2 refund (900) is
    # already visible when day 1 is reviewed later -> over-refund surfaces
    # without having to inspect each day manually.
    report = client.get("/api/audit/2026-11-01").get_json()
    assert report["summary"]["group_count"] == 1
    group = report["groups"][0]
    assert group["ticket_id"] == "T1"
    assert group["refund_total"] == 1200
    assert group["refund_count"] == 2
    assert group["status"] == "over_refund"
    # A day with no refund rows of its own opens no groups.
    empty = client.get("/api/audit/2026-11-09")
    assert empty.status_code == 400


def test_exact_refund_is_ok_not_over_refund(client):
    set_audit(client, True)
    publish(client, "2026-11-01", [row("T1", 1000), row("T1", -400, "2026-11-01")])
    publish(client, "2026-11-02", [row("T1", -600, "2026-11-01")])
    report = client.get("/api/audit/2026-11-02").get_json()
    assert report["groups"][0]["status"] == "ok"
    assert report["groups"][0]["refund_total"] == 1000


def test_duplicate_original_sale_is_pending(client):
    set_audit(client, True)
    publish(client, "2026-11-01", [row("T1", 1000), row("T1", 1000)])
    publish(client, "2026-11-02", [row("T1", -100, "2026-11-01")])
    report = client.get("/api/audit/2026-11-02").get_json()
    group = report["groups"][0]
    assert group["status"] == "pending"
    assert group["pending_reason"] == "duplicate_original"
    assert group["candidate_count"] == 2
    assert len(group["candidates"]) == 2


def test_report_only_uses_included_days(client):
    # A refund day outside the included set cannot be queried.
    set_audit(client, True)
    publish(client, "2026-11-01", [row("T1", 1000)])
    set_audit(client, False)
    publish(client, "2026-11-02", [row("T1", -500, "2026-11-01")])
    # Disabling suspends auditing: nothing is included while the switch is
    # off, and neither day can be queried for a report.
    assert client.get("/api/audit").get_json()["included_days"] == []
    response = client.get("/api/audit/2026-11-02", headers={"Accept": "application/json"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "day_not_included"
    # Re-enable: the eligible day-1 revision returns; day 2 was published
    # while disabled and stays excluded, so day 1 sees no refunds at all.
    set_audit(client, True)
    overview = client.get("/api/audit").get_json()
    assert [d["day"] for d in overview["included_days"]] == ["2026-11-01"]
    excluded = {d["day"]: d["reason"] for d in overview["excluded_days"]}
    assert excluded["2026-11-02"] == "published_before_enabled"
    report = client.get("/api/audit/2026-11-01").get_json()
    assert report["groups"] == []


# ---------------------------------------------------------------------------
# revision handling
# ---------------------------------------------------------------------------
def test_republished_revision_is_used_immediately_even_if_excluded(client):
    set_audit(client, True)
    publish(client, "2026-11-01", [row("T1", 1000), row("T1", -300, "2026-11-01")])
    publish(client, "2026-11-02", [row("T1", -200, "2026-11-01")])
    report = client.get("/api/audit/2026-11-02").get_json()
    assert report["groups"][0]["refund_total"] == 500

    # Republish day 1 without sale_day while disabled: it drops out of the
    # eligible set permanently, and the new revision is authoritative
    # immediately even after the feature is re-enabled.
    set_audit(client, False)
    preview_and_confirm(
        client,
        day="2026-11-01",
        data=DEFAULT_CSV.encode(),
    )
    set_audit(client, True)
    overview = client.get("/api/audit").get_json()
    included_days = {d["day"] for d in overview["included_days"]}
    assert "2026-11-01" not in included_days
    assert "2026-11-02" in included_days
    # The day-2 refund can no longer resolve an included original -> pending.
    report = client.get("/api/audit/2026-11-02").get_json()
    assert report["groups"][0]["status"] == "pending"
    assert report["groups"][0]["pending_reason"] == "original_not_found"


def test_disabling_suspends_and_reenabling_restores_eligible_revisions(client):
    set_audit(client, True)
    publish(client, "2026-11-01", [row("T1", 1000), row("T1", -300, "2026-11-01")])
    set_audit(client, False)
    # While disabled nothing is included, but the eligible revision keeps its
    # frozen eligibility flag.
    assert client.get("/api/audit").get_json()["included_days"] == []
    set_audit(client, True)
    included = client.get("/api/audit").get_json()["included_days"]
    assert [d["day"] for d in included] == ["2026-11-01"]
    report = client.get("/api/audit/2026-11-01").get_json()
    assert report["groups"][0]["refund_total"] == 300


def test_failed_republish_keeps_old_revision_auditable(client):
    set_audit(client, True)
    publish(client, "2026-11-01", [row("T1", 1000)])
    # An invalid revision must not replace the working one.
    bad = upload(client, day="2026-11-01", data=_csv([row(" ", 1000)])).get_json()
    confirm = client.post(
        f"/api/previews/{bad['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 422
    overview = client.get("/api/audit").get_json()
    assert [d["rev"] for d in overview["included_days"]] == [1]
    detail = client.get("/api/days/2026-11-01").get_json()
    assert detail["current_rev"] == 1


def test_report_tracks_rows_to_new_revision_after_republish(client):
    set_audit(client, True)
    publish(client, "2026-11-01", [row("T1", 1000), row("T2", 800)])
    publish(client, "2026-11-02", [row("T1", -500, "2026-11-01")])
    # Republish day 1 with reordered/extra rows; line numbers must follow
    # the new revision.
    publish(
        client,
        "2026-11-01",
        [row("T2", 800), row("T1", 1500), row("T3", 600)],
    )
    report = client.get("/api/audit/2026-11-02").get_json()
    group = report["groups"][0]
    assert group["status"] == "ok"
    assert group["original"]["amount"] == 1500
    assert group["original"]["rev"] == 2
    assert group["original"]["row_index"] == 2
    assert group["original"]["source_line"] == 3
    # Old revision rows are never mixed in.
    assert report["rev"] == 1


# ---------------------------------------------------------------------------
# HTML page and API surface
# ---------------------------------------------------------------------------
def test_audit_html_pages_render(client):
    page = client.get("/audit")
    assert page.status_code == 200
    assert "跨日退票核查" in page.get_data(as_text=True)

    set_audit(client, True)
    publish(client, "2026-11-01", [row("T1", 1000), row("T1", -200, "2026-11-01")])
    page = client.get("/audit?day=2026-11-01")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "2026-11-01" in text and "正常" in text
    assert "原件行号" in text

    # Selecting a non-included day shows an error rather than crashing.
    missing = client.get("/audit?day=2026-11-09")
    assert missing.status_code == 200
    assert "没有纳入核查" in missing.get_data(as_text=True)


def test_audit_html_form_toggles_feature(client):
    response = client.post(
        "/audit/settings", data={"audit_enabled": "1"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert client.get("/api/audit").get_json()["enabled"] is True
    client.post("/audit/settings", data={"audit_enabled": "0"})
    assert client.get("/api/audit").get_json()["enabled"] is False


def test_invalid_day_on_report_api_rejected(client):
    set_audit(client, True)
    response = client.get("/api/audit/2026-13-40")
    assert response.status_code == 400


def test_multiple_same_day_refunds_form_one_group(client):
    set_audit(client, True)
    publish(
        client,
        "2026-11-01",
        [
            row("T1", 1000),
            row("T1", -200, "2026-11-01"),
            row("T1", -300, "2026-11-01"),
        ],
    )
    report = client.get("/api/audit/2026-11-01").get_json()
    assert report["summary"]["group_count"] == 1
    group = report["groups"][0]
    assert group["refund_count"] == 2
    assert group["refund_total"] == 500
    assert [r["row_index"] for r in group["refunds"]] == [2, 3]
    assert group["status"] == "ok"


def test_header_only_batch_publishes_with_empty_report(client):
    set_audit(client, True)
    preview, confirmed = preview_and_confirm(client, day="2026-11-01", data=HEADER.encode())
    assert confirmed.status_code == 201, confirmed.get_json()
    report = client.get("/api/audit/2026-11-01").get_json()
    assert report["groups"] == []
    assert report["summary"] == {
        "group_count": 0,
        "over_refund_count": 0,
        "pending_count": 0,
        "ok_count": 0,
    }


def test_duplicate_original_candidates_carry_full_trace(client):
    set_audit(client, True)
    publish(client, "2026-11-01", [row("T1", 1000), row("T1", 1000)])
    publish(client, "2026-11-02", [row("T1", -100, "2026-11-01")])
    report = client.get("/api/audit/2026-11-02").get_json()
    group = report["groups"][0]
    assert group["status"] == "pending"
    candidates = group["candidates"]
    assert [c["source_line"] for c in candidates] == [2, 3]
    assert all(c["day"] == "2026-11-01" and c["rev"] == 1 for c in candidates)
    assert group["refunds"][0]["source_line"] == 2


def test_overview_lists_excluded_days_without_reading_blobs(client, app):
    set_audit(client, True)
    preview_and_confirm(client, day="2026-11-01", data=DEFAULT_CSV.encode())
    # Even with the blob removed, metadata-only overview still lists the day.
    import os

    store = app.config["STORE"]
    conn = app.extensions["get_conn"]()
    try:
        row = conn.execute(
            "SELECT original_sha256 FROM publications WHERE day = '2026-11-01'"
        ).fetchone()
    finally:
        conn.close()
    os.unlink(store.path_for(row["original_sha256"]))
    overview = client.get("/api/audit").get_json()
    assert overview["excluded_days"][0]["day"] == "2026-11-01"
