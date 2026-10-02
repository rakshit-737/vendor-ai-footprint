"""Review store tests: append-only JSONL, last write wins, every decision carries a reason (HC1, HC2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from footprint.models import Tier, TierOverride
from footprint.review import OverrideStore, ReviewRecord

STAMP = "2026-10-02T14:40:00Z"


def fields(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "evidence_review",
        "vendor_id": "V-005",
        "key": "3f2a9c41d0",
        "value": "accepted",
        "reason": "Quote verified against the captured RTP page.",
        "analyst": "Analyst A",
        "date": "2026-10-02",
        "recorded_at": STAMP,
    }
    return base | changes


def record(**changes: Any) -> ReviewRecord:
    return ReviewRecord(**fields(**changes))


def override(tier: Tier, reason: str = "Dependency confirmed as High with the business owner.") -> TierOverride:
    return TierOverride(vendor_id="V-003", tier=tier, reason=reason, analyst="Analyst B", date="2026-10-03")


@pytest.fixture
def store(tmp_path: Path) -> OverrideStore:
    return OverrideStore(tmp_path / "review" / "overrides.jsonl")


# --------------------------------------------------------------------------- append-only file


def test_two_adds_append_two_lf_terminated_lines(store: OverrideStore) -> None:
    store.add(record())
    store.add(record(value="rejected", reason="Trap: Intelligent Mail barcode is not AI."))
    raw = store.path.read_bytes()
    assert raw.count(b"\n") == 2
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    assert [r.value for r in store.records()] == ["accepted", "rejected"]


def test_lines_are_json_with_sorted_keys(store: OverrideStore) -> None:
    store.add(record(extra={"zeta": 1, "alpha": {"y": 2, "b": 1}}))
    line = store.path.read_text(encoding="utf-8").splitlines()[0]
    data = json.loads(line)
    assert list(data) == sorted(data)
    assert list(data["extra"]) == ["alpha", "zeta"]
    assert list(data["extra"]["alpha"]) == ["b", "y"]


def test_existing_lines_are_never_rewritten(store: OverrideStore) -> None:
    store.add(record())
    before = store.path.read_bytes()
    store.add(record(key="other-hash"))
    after = store.path.read_bytes()
    assert after.startswith(before)
    assert len(after) > len(before)


def test_add_creates_the_parent_directory(tmp_path: Path) -> None:
    store = OverrideStore(tmp_path / "nested" / "review" / "reviews.jsonl")
    store.add(record())
    assert store.path.is_file()


def test_missing_file_reads_as_empty(store: OverrideStore) -> None:
    assert store.records() == []
    assert store.latest("evidence_review", "V-005", "3f2a9c41d0") is None
    assert store.tier_override("V-005") is None


def test_add_after_an_unterminated_last_line_keeps_records_apart(store: OverrideStore) -> None:
    store.path.parent.mkdir(parents=True)
    first = json.dumps(record().model_dump(mode="json"), sort_keys=True)
    store.path.write_bytes(first.encode("utf-8"))  # no trailing newline, e.g. after an interrupted write
    store.add(record(key="second"))
    assert store.path.read_bytes().startswith(first.encode("utf-8"))
    assert [r.key for r in store.records()] == ["3f2a9c41d0", "second"]


def test_corrupt_line_is_reported_with_its_line_number(store: OverrideStore) -> None:
    store.add(record())
    with store.path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('{"kind": "tier_override"}\n')
    with pytest.raises(ValueError, match=":2:"):
        store.records()


# --------------------------------------------------------------------------- queries


def test_records_filter_by_kind_and_vendor_in_file_order(store: OverrideStore) -> None:
    store.add(record(vendor_id="V-001", key="a"))
    store.add(record(kind="cell_edit", vendor_id="V-001", key="risk_rationale", value="edited text"))
    store.add(record(vendor_id="V-002", key="b"))
    store.add(record(vendor_id="V-001", key="c"))
    assert [r.key for r in store.records()] == ["a", "risk_rationale", "b", "c"]
    assert [r.key for r in store.records(kind="evidence_review")] == ["a", "b", "c"]
    assert [r.key for r in store.records(vendor_id="V-001")] == ["a", "risk_rationale", "c"]
    assert [r.key for r in store.records(kind="evidence_review", vendor_id="V-001")] == ["a", "c"]


def test_latest_record_wins(store: OverrideStore) -> None:
    store.add(record(value="accepted"))
    store.add(record(key="unrelated", value="accepted"))
    store.add(record(value="rejected", reason="Second look: the quote is reader advice, not use."))
    latest = store.latest("evidence_review", "V-005", "3f2a9c41d0")
    assert latest is not None and latest.value == "rejected"
    assert store.latest("evidence_review", "V-006", "3f2a9c41d0") is None
    assert store.latest("cell_edit", "V-005", "3f2a9c41d0") is None


def test_tier_override_round_trip_and_last_write_wins(store: OverrideStore) -> None:
    store.add_tier_override(override(Tier.HIGH), recorded_at=STAMP)
    store.add_tier_override(override(Tier.CRITICAL, "Dependency upgraded to Critical after the BCP review."))
    assert store.tier_override("V-003") == override(
        Tier.CRITICAL, "Dependency upgraded to Critical after the BCP review."
    )
    assert [r.value for r in store.records(kind="tier_override")] == ["High", "Critical"]
    assert store.tier_override("V-004") is None


def test_add_tier_override_returns_the_stored_record(store: OverrideStore) -> None:
    stored = store.add_tier_override(override(Tier.MEDIUM), recorded_at=STAMP)
    assert (stored.kind, stored.vendor_id, stored.key) == ("tier_override", "V-003", "tier")
    assert stored.value == "Medium"
    assert stored.recorded_at == STAMP
    assert store.records() == [stored]


def test_add_accepts_a_mapping(store: OverrideStore) -> None:
    stored = store.add(fields(kind="risk_input_override", key="E", value="2"))
    assert store.latest("risk_input_override", "V-005", "E") == stored


# --------------------------------------------------------------------------- validation


def test_reason_shorter_than_ten_characters_is_rejected() -> None:
    with pytest.raises(ValidationError, match="reason"):
        record(reason="too short")


def test_padding_does_not_count_towards_the_reason() -> None:
    with pytest.raises(ValidationError, match="reason"):
        record(reason="   short   ")


def test_file_untouched_on_validation_error(store: OverrideStore) -> None:
    store.add(record())
    before = store.path.read_bytes()
    with pytest.raises(ValidationError):
        store.add(fields(reason="nope"))
    with pytest.raises(ValidationError):
        store.add_tier_override(TierOverride(vendor_id="V-003", tier=Tier.HIGH, reason="A valid reason here.",
                                             analyst="Analyst B", date="03-10-2026"))
    assert store.path.read_bytes() == before


def test_validation_error_does_not_create_the_file(store: OverrideStore) -> None:
    with pytest.raises(ValidationError):
        store.add(fields(reason=""))
    assert not store.path.exists()


def test_unicode_reason_round_trips(store: OverrideStore) -> None:
    reason = "Analyst note – the vendor’s “Trust Centre” confirms it; café ✓ 日本語 données"
    store.add(record(reason=reason))
    assert store.records()[0].reason == reason
    assert reason in store.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ["Severe", "critical", "HIGH", ""])
def test_tier_override_value_must_be_a_tier(value: str) -> None:
    with pytest.raises(ValidationError, match="tier"):
        record(kind="tier_override", vendor_id="V-003", key="tier", value=value)


def test_tier_override_key_must_be_tier() -> None:
    with pytest.raises(ValidationError, match="tier"):
        record(kind="tier_override", vendor_id="V-003", key="criticality", value="High")


@pytest.mark.parametrize("tier", list(Tier))
def test_every_tier_value_is_accepted(tier: Tier) -> None:
    assert record(kind="tier_override", key="tier", value=tier.value).value == tier.value


@pytest.mark.parametrize("bad", ["02-10-2026", "2026-13-01", "2026-02-30", "20261002", "2026-10-02T10:00:00", ""])
def test_date_must_be_iso_yyyy_mm_dd(bad: str) -> None:
    with pytest.raises(ValidationError, match="date"):
        record(date=bad)


@pytest.mark.parametrize("field", ["vendor_id", "key", "analyst"])
def test_identifying_fields_must_not_be_blank(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        record(**{field: "  "})


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="kind"):
        record(kind="approval")


def test_recorded_at_is_generated_in_utc_when_not_supplied() -> None:
    data = fields()
    del data["recorded_at"]
    stamp = ReviewRecord(**data).recorded_at
    assert stamp.endswith("Z") and "T" in stamp and len(stamp) == len("2026-10-02T14:40:00Z")


@pytest.mark.parametrize("stamp", ["2026-10-02T14:40:00+00:00", "2026-10-02T14:40:00.123456Z"])
def test_recorded_at_accepts_utc_iso_strings_verbatim(stamp: str) -> None:
    assert record(recorded_at=stamp).recorded_at == stamp


@pytest.mark.parametrize("stamp", ["2026-10-02T16:40:00+02:00", "2026-10-02T14:40:00", "yesterday", ""])
def test_recorded_at_must_be_utc(stamp: str) -> None:
    with pytest.raises(ValidationError, match="recorded_at"):
        record(recorded_at=stamp)


def test_records_are_immutable() -> None:
    with pytest.raises(ValidationError):
        record().value = "rejected"  # type: ignore[misc]
