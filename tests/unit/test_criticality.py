"""Tests for criticality rubric v1.1 (config/rubric.toml + footprint.criticality).

The seven profiles are read from the real workbook (row 5 = V-000 example, rows 6-11 = vendors), never retyped.
Sensitivity and perturbation are checked against an independent brute-force recomputation of the rubric.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import openpyxl
import pytest

from footprint.criticality import (
    factor_label,
    floor_description,
    load_rubric,
    render_rationale,
    rubric_table,
    score_profile,
)
from footprint.models import CriticalityResult, Tier, TierOverride, VendorProfile, max_tier

REPO = Path(__file__).resolve().parents[2]
WORKBOOK = REPO / "data" / "input" / "Meridian_Vendor_Input.xlsx"
COLUMNS = {
    "B": "vendor_id", "C": "name", "D": "description", "E": "service", "F": "category", "G": "website",
    "H": "business_process", "I": "operational_dependency", "J": "data_accessed", "K": "data_volume",
}

# vendor -> (levels, anchors that differ from "<factor><level>", score, floors, score tier, final tier)
PINS: dict[str, tuple[dict[str, int], dict[str, str], int, list[str], Tier, Tier]] = {
    "V-000": ({"O": 3, "D": 4, "P": 2, "R": 4, "V": 3}, {}, 80, ["F3", "F5", "F6"], Tier.CRITICAL, Tier.CRITICAL),
    "V-001": ({"O": 3, "D": 3, "P": 2, "R": 2, "V": 0}, {"D": "D3-P"}, 57, ["F4", "F5"], Tier.HIGH, Tier.HIGH),
    "V-002": ({"O": 4, "D": 4, "P": 4, "R": 4, "V": 4}, {}, 100, ["F1", "F2", "F3", "F5", "F6"], Tier.CRITICAL,
              Tier.CRITICAL),
    "V-003": ({"O": 2, "D": 4, "P": 1, "R": 4, "V": 4}, {}, 71, ["F3", "F5"], Tier.HIGH, Tier.HIGH),
    "V-004": ({"O": 2, "D": 3, "P": 2, "R": 3, "V": 2}, {}, 60, ["F5"], Tier.HIGH, Tier.HIGH),
    "V-005": ({"O": 3, "D": 3, "P": 3, "R": 4, "V": 3}, {}, 79, ["F2", "F5"], Tier.HIGH, Tier.CRITICAL),
    "V-006": ({"O": 4, "D": 3, "P": 4, "R": 4, "V": 4}, {}, 94, ["F1", "F2", "F5"], Tier.CRITICAL, Tier.CRITICAL),
}

# Exact sensitivity statements (AutomWorx and BNY are the wording given in the task brief).
SENSITIVITY: dict[str, list[str]] = {
    "V-000": ["Would drop to High if operational dependency were Moderate, if the data were only customer "
              "non-public information or if volume were moderate (population-scale floor no longer applies)."],
    "V-001": ["Would become Critical if operational dependency were Critical/Total (operational-dependency floor) "
              "or if the service "
              "transmitted payment messages (payment-path floor).", "Stays High for any single one-step decrease."],
    "V-002": ["Stays Critical for any single one-step decrease."],
    "V-003": ["Would become Critical if operational dependency were High (population-scale floor).",
              "Stays High for any single one-step decrease."],
    "V-004": ["Stays High for any single one-step increase or decrease."],
    "V-005": ["Would drop to High if operational dependency were Moderate or if the service did not transmit "
              "payment messages (payment-path floor no longer applies)."],
    "V-006": ["Stays Critical for any single one-step decrease."],
}

CODE = re.compile(r"\b[ODPRV][0-4](?:-P)?\b")


def _read_profiles() -> dict[str, VendorProfile]:
    workbook = openpyxl.load_workbook(WORKBOOK, read_only=True, data_only=True)
    try:
        sheet = workbook["Vendor Inventory"]
        profiles: dict[str, VendorProfile] = {}
        for row in range(5, 12):
            values = {field: sheet[f"{col}{row}"].value for col, field in COLUMNS.items()}
            fields = {field: "" if value is None else str(value) for field, value in values.items()}
            profiles[fields["vendor_id"]] = VendorProfile(row=row, is_example=row == 5, **fields)
        return profiles
    finally:
        workbook.close()


@pytest.fixture(scope="module")
def profiles() -> dict[str, VendorProfile]:
    return _read_profiles()


@pytest.fixture(scope="module")
def results(profiles: dict[str, VendorProfile]) -> dict[str, CriticalityResult]:
    return {vid: score_profile(profile) for vid, profile in profiles.items()}


def _profile(**fields: str) -> VendorProfile:
    """A synthetic vendor; only the fields a test cares about need to be given."""
    base = {"row": 99, "vendor_id": "T-001", "name": "Synthetic Vendor Ltd"}
    return VendorProfile(**{**base, **fields})


# --------------------------------------------------------------------------- independent brute force

WEIGHTS = {"O": 7, "D": 6, "P": 5, "R": 4, "V": 3}


def _brute_tier(levels: dict[str, int], privileged: bool, weights: dict[str, int] = WEIGHTS) -> Tier:
    """The rubric straight from the brief: score tier, then floors F1-F6."""
    score = sum(weights[f] * levels[f] for f in "ODPRV")
    tiers = [Tier.CRITICAL if score >= 80 else Tier.HIGH if score >= 50 else Tier.MEDIUM if score >= 25 else Tier.LOW]
    o, d, p, v = levels["O"], levels["D"], levels["P"], levels["V"]
    if o == 4 or (p >= 3 and o >= 3) or (d == 4 and v >= 3 and o >= 3):
        tiers.append(Tier.CRITICAL)
    if (d == 4 and v >= 3) or privileged:
        tiers.append(Tier.HIGH)
    if d >= 3:
        tiers.append(Tier.MEDIUM)
    return max_tier(*tiers)


def _one_step_changes(levels: dict[str, int], privileged: bool) -> list[tuple[str, int, Tier]]:
    """(factor, step, tier) for every single one-level move; moving D off D3-P drops the privileged anchor."""
    changes = []
    for factor in "ODPRV":
        for step in (1, -1):
            new = levels[factor] + step
            if 0 <= new <= 4:
                still_privileged = privileged and factor != "D"
                changes.append((factor, step, _brute_tier({**levels, factor: new}, still_privileged)))
    return changes


# --------------------------------------------------------------------------- pins (all 7 rows)


@pytest.mark.parametrize("vid", sorted(PINS))
def test_pinned_levels_anchors_score_floors_and_tier(results: dict[str, CriticalityResult], vid: str) -> None:
    levels, special_anchors, score, floors, score_tier, tier = PINS[vid]
    result = results[vid]
    assert list(result.factors) == ["O", "D", "P", "R", "V"]
    assert {f: fs.level for f, fs in result.factors.items()} == levels
    for factor, fs in result.factors.items():
        assert fs.anchor == special_anchors.get(factor, f"{factor}{fs.level}")
        assert fs.weight == WEIGHTS[factor]
        assert fs.points == fs.weight * fs.level
    assert result.score == score
    assert result.floors_fired == floors
    assert result.score_tier == score_tier
    assert result.computed_tier == tier
    assert result.tier == tier
    assert result.rubric_version == "1.1"
    assert result.override is None


@pytest.mark.parametrize("vid", sorted(PINS))
def test_triggers_are_verbatim_profile_text(
    profiles: dict[str, VendorProfile], results: dict[str, CriticalityResult], vid: str
) -> None:
    profile = profiles[vid]
    for fs in results[vid].factors.values():
        assert fs.trigger, f"{vid} {fs.factor}: empty trigger"
        sources = [getattr(profile, field) for field in fs.source_fields]
        assert any(fs.trigger in text for text in sources), f"{vid} {fs.factor}: {fs.trigger!r} not in profile"


def test_key_triggers(results: dict[str, CriticalityResult]) -> None:
    assert results["V-000"].factors["P"].trigger == "payment arrangements"  # not "core lending platform"
    assert results["V-000"].factors["D"].trigger == "health-related disclosures"
    assert results["V-001"].factors["D"].trigger == "Privileged access to production"
    assert results["V-001"].factors["P"].trigger == "Batch scheduling across core systems"
    assert results["V-001"].factors["V"].trigger == "Not applicable"
    assert results["V-002"].factors["D"].trigger == "Full customer master"
    assert results["V-002"].factors["V"].trigger == "1.4 million customers"
    assert results["V-003"].factors["D"].trigger == "tax identifiers"
    assert results["V-003"].factors["V"].trigger == "entire customer base"
    assert results["V-004"].factors["V"].trigger == "38,000 wealth clients"
    assert results["V-005"].factors["P"].trigger == "transmission of RTP payment files"
    assert results["V-005"].factors["V"].trigger == "2.1 million messages"
    assert results["V-006"].factors["P"].trigger == "clearing and settlement"
    assert results["V-006"].factors["D"].trigger == "Payment messages"


def test_notes_log_adjacent_levels_and_in_transit(results: dict[str, CriticalityResult]) -> None:
    assert "both fit" in results["V-004"].factors["P"].note  # P2 commission calculations + P1 reporting
    assert "both fit" in results["V-000"].factors["V"].note  # 240,000 accounts (3) + 660,000 interactions (2)
    assert "in transit" in results["V-006"].factors["D"].note
    assert results["V-005"].factors["O"].note == ""


# --------------------------------------------------------------------------- synthetic tiers and edge cases


def test_synthetic_low_vendor() -> None:
    result = score_profile(_profile(
        service="Marketing campaign analytics dashboards", business_process="Marketing analytics",
        operational_dependency="Low", data_accessed="Aggregated, de-identified campaign statistics.",
        data_volume="Not applicable",
    ))
    assert {f: fs.level for f, fs in result.factors.items()} == {"O": 1, "D": 1, "P": 1, "R": 0, "V": 0}
    assert result.score == 18
    assert result.floors_fired == []
    assert result.tier == Tier.LOW


def test_synthetic_medium_vendor_with_contact_data_only() -> None:
    result = score_profile(_profile(
        service="Customer newsletter and marketing email distribution",
        business_process="Customer marketing communications", operational_dependency="Moderate",
        data_accessed="Customer contact data only: names and email addresses for 25,000 customers.",
        data_volume="300,000 emails",
    ))
    assert {f: fs.level for f, fs in result.factors.items()} == {"O": 2, "D": 2, "P": 0, "R": 1, "V": 2}
    assert result.factors["P"].trigger == ""
    assert "HC1" in result.factors["P"].note
    assert result.score == 36
    assert result.tier == Tier.MEDIUM


def test_negated_customer_data_does_not_trigger_customer_patterns() -> None:
    none = score_profile(_profile(data_accessed="No direct customer data."))
    assert (none.factors["D"].level, none.factors["D"].anchor) == (0, "D0")
    assert none.factors["D"].trigger == "No direct customer data"
    stats = score_profile(_profile(data_accessed="No direct customer data. Read-only anonymised usage statistics."))
    assert (stats.factors["D"].level, stats.factors["D"].trigger) == (1, "anonymised")
    direct = score_profile(_profile(data_accessed="Direct customer data for 5,000 customers."))
    assert (direct.factors["D"].level, direct.factors["D"].trigger) == (3, "customer data")


def test_privileged_access_without_customer_data_is_d3p(results: dict[str, CriticalityResult]) -> None:
    d = results["V-001"].factors["D"]
    assert (d.level, d.anchor) == (3, "D3-P")
    assert "customer" not in d.trigger.lower()


@pytest.mark.parametrize(("volume", "level"), [
    ("1.4 million customers", 4), ("240 million transactions", 4), ("38,000 clients", 2),
    ("Approximately 240,000 active loan accounts", 3), ("2.1 million messages", 3), ("19 million documents", 4),
    ("50,000 records", 1), ("9,999 customers", 1), ("10,000 customers", 2), ("100,000 records", 2),
    ("2.1m payments", 3), ("the entire customer base", 4),
])
def test_volume_parsing(volume: str, level: int) -> None:
    v = score_profile(_profile(data_accessed="Account numbers.", data_volume=volume)).factors["V"]
    assert v.level == level
    assert v.trigger and v.trigger in volume


@pytest.mark.parametrize("volume", ["12 monthly cycles", "12 monthly statements"])
def test_frequencies_are_not_volumes(volume: str) -> None:
    v = score_profile(_profile(data_accessed="Advisor compensation data.", data_volume=volume)).factors["V"]
    assert (v.level, v.trigger) == (0, "")
    assert "HC1" in v.note


def test_terrapin_monthly_cycles_ignored(results: dict[str, CriticalityResult]) -> None:
    v = results["V-004"].factors["V"]
    assert (v.level, v.trigger) == (2, "38,000 wealth clients")


def test_operational_dependency_unknown_and_empty() -> None:
    unknown = score_profile(_profile(operational_dependency="Essential")).factors["O"]
    assert (unknown.level, unknown.trigger) == (2, "")
    assert "HC1" in unknown.note
    assert score_profile(_profile(operational_dependency="")).factors["O"].level == 0


def test_scoring_is_deterministic(profiles: dict[str, VendorProfile]) -> None:
    for profile in profiles.values():
        assert score_profile(profile).model_dump() == score_profile(profile).model_dump()


# --------------------------------------------------------------------------- sensitivity and perturbation


@pytest.mark.parametrize("vid", sorted(SENSITIVITY))
def test_sensitivity_statements(results: dict[str, CriticalityResult], vid: str) -> None:
    assert results[vid].sensitivity == SENSITIVITY[vid]


@pytest.mark.parametrize("vid", sorted(PINS))
def test_sensitivity_matches_brute_force(results: dict[str, CriticalityResult], vid: str) -> None:
    result = results[vid]
    levels = {f: fs.level for f, fs in result.factors.items()}
    privileged = result.factors["D"].anchor == "D3-P"
    base = _brute_tier(levels, privileged)
    assert base == result.computed_tier
    changes = _one_step_changes(levels, privileged)
    raised = Counter(t for _, _, t in changes if t.rank > base.rank)
    dropped = Counter(t for _, _, t in changes if t.rank < base.rank)
    for tier, count in raised.items():
        [statement] = [s for s in result.sensitivity if s.startswith(f"Would become {tier.value} if ")]
        assert statement.count(" if ") == count
    for tier, count in dropped.items():
        [statement] = [s for s in result.sensitivity if s.startswith(f"Would drop to {tier.value} if ")]
        assert statement.count(" if ") == count
    directions = []
    if base != Tier.CRITICAL and all(t == base for _, step, t in changes if step > 0):
        directions.append("increase")
    if base != Tier.LOW and all(t == base for _, step, t in changes if step < 0):
        directions.append("decrease")
    stays = [s for s in result.sensitivity if s.startswith("Stays ")]
    if directions:
        assert stays == [f"Stays {base.value} for any single one-step {' or '.join(directions)}."]
    else:
        assert stays == []
    assert len(result.sensitivity) == len(raised) + len(dropped) + len(stays)


@pytest.mark.parametrize("vid", sorted(PINS))
def test_weight_perturbation_is_stable(results: dict[str, CriticalityResult], vid: str) -> None:
    result = results[vid]
    levels = {f: fs.level for f, fs in result.factors.items()}
    privileged = result.factors["D"].anchor == "D3-P"
    for factor in WEIGHTS:
        for delta in (1, -1):
            weights = {**WEIGHTS, factor: WEIGHTS[factor] + delta}
            assert _brute_tier(levels, privileged, weights) == result.computed_tier
    assert result.perturbation_stable is True


def test_weight_perturbation_detects_a_knife_edge_score() -> None:
    result = score_profile(_profile(
        service="Commission calculations for the sales force", business_process="Sales commission payouts",
        operational_dependency="High", data_accessed="Employee compensation data.", data_volume="50,000 records",
    ))
    assert {f: fs.level for f, fs in result.factors.items()} == {"O": 3, "D": 2, "P": 2, "R": 1, "V": 1}
    assert (result.score, result.tier) == (50, Tier.HIGH)
    assert result.perturbation_stable is False


# --------------------------------------------------------------------------- column M rationale


def _outside_parentheses(text: str) -> str:
    previous = None
    while previous != text:
        previous, text = text, re.sub(r"\([^()]*\)", "", text)
    return text


@pytest.mark.parametrize("vid", sorted(PINS))
def test_rationale_shape(profiles: dict[str, VendorProfile], results: dict[str, CriticalityResult], vid: str) -> None:
    profile, result = profiles[vid], results[vid]
    text = render_rationale(profile, result)
    assert len(text) <= 1050
    assert text.startswith(f"{result.tier.value}. ")
    assert not CODE.search(text), CODE.search(text)
    assert not re.search(r"\bF[1-6]\b", _outside_parentheses(text)), "floor ids only inside parentheses"
    assert "How the tier was reached (rubric v1.1, profile only): operational dependency " in text
    assert f"Score {result.score} of 100" in text
    assert "Sensitivity: " in text
    assert text == render_rationale(profile, result)
    process = profile.business_process.strip().rstrip(".")
    assert process[1:] in text  # the opening names the business process in the profile's own words


def test_rationale_bny(profiles: dict[str, VendorProfile], results: dict[str, CriticalityResult]) -> None:
    text = render_rationale(profiles["V-005"], results["V-005"])
    assert text.startswith("Critical. BNY supports inbound real-time payments; ")
    assert "payment instructions, counterparty names and account identifiers" in text
    assert 'payment-flow involvement in the payment path (3 of 4, "transmission of RTP payment files")' in text
    assert ("Score 79 of 100; raised to Critical by the payment-path floor "
            "(in the payment message path with High dependency).") in text
    assert text.endswith(
        "Sensitivity: Would drop to High if operational dependency were Moderate or if the service did not "
        "transmit payment messages (payment-path floor no longer applies)."
    )


def test_rationale_automworx_joins_statements(profiles: dict[str, VendorProfile],
                                              results: dict[str, CriticalityResult]) -> None:
    text = render_rationale(profiles["V-001"], results["V-001"])
    assert "privileged access to production job schedules, system configuration and service credentials" in text
    assert text.endswith(
        "Sensitivity: Would become Critical if operational dependency were Critical/Total "
        "(operational-dependency floor) or if the "
        "service transmitted payment messages (payment-path floor); stays High for any single one-step decrease."
    )


def test_rationale_score_alone_mentions_matching_floors(profiles: dict[str, VendorProfile],
                                                       results: dict[str, CriticalityResult]) -> None:
    text = render_rationale(profiles["V-000"], results["V-000"])
    assert "Score 80 of 100, Critical on score alone and by the population-scale floor." in text
    assert "health-related disclosures" in text


def test_override_changes_tier_and_is_mentioned(profiles: dict[str, VendorProfile]) -> None:
    reason = "Advisor compensation feeds regulatory filings; the risk committee asked for Critical."
    override = TierOverride(vendor_id="V-004", tier=Tier.CRITICAL, reason=reason, analyst="RS", date="2026-10-05")
    result = score_profile(profiles["V-004"], override=override)
    assert (result.computed_tier, result.tier, result.override) == (Tier.HIGH, Tier.CRITICAL, override)
    text = render_rationale(profiles["V-004"], result)
    assert text.startswith("Critical. ")
    assert text.endswith(f"Analyst override to Critical: {reason}")
    assert len(text) <= 1050


def test_override_for_another_vendor_is_rejected(profiles: dict[str, VendorProfile]) -> None:
    override = TierOverride(vendor_id="V-001", tier=Tier.LOW, reason="wrong vendor entirely", analyst="RS",
                            date="2026-10-05")
    with pytest.raises(ValueError, match="V-001"):
        score_profile(profiles["V-004"], override=override)


def test_rationale_is_capped_for_long_inputs() -> None:
    long_text = ", ".join(f"customer data element number {i}" for i in range(60))
    profile = _profile(name="A Vendor With An Extremely Long Legal Name Holdings Incorporated " * 3,
                       service="Core banking " + long_text, business_process="Core account processing " + long_text,
                       operational_dependency="Critical", data_accessed="Account numbers, " + long_text,
                       data_volume="1.4 million customers")
    reason = "A very long analyst reason. " * 40
    result = score_profile(profile, override=TierOverride(vendor_id="T-001", tier=Tier.HIGH, reason=reason,
                                                          analyst="RS", date="2026-10-05"))
    text = render_rationale(profile, result)
    assert len(text) <= 1050
    assert text.startswith("High. ")
    assert " Analyst override to High: A very long analyst reason." in text  # the override survives the cut
    assert text.endswith("...")
    plain = result.model_copy(update={"override": None, "tier": result.computed_tier})
    assert len(render_rationale(profile, plain)) <= 1050


def test_rationale_does_not_repeat_a_trigger_named_in_the_process(
    profiles: dict[str, VendorProfile], results: dict[str, CriticalityResult]
) -> None:
    text = render_rationale(profiles["V-006"], results["V-006"])
    assert text.startswith(
        "Critical. The Clearing House Payments Company supports ACH and RTP clearing and settlement; an outage "
        'would disrupt that process (dependency rated "Critical"), and it executes or settles funds. How the tier'
    )


# --------------------------------------------------------------------------- rubric file, legend, labels


def test_load_rubric_default_and_explicit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)  # the default path is resolved from the package, not the working directory
    rubric = load_rubric()
    assert rubric.version == "1.1"
    assert rubric.weights == WEIGHTS
    copy = tmp_path / "rubric.toml"
    copy.write_bytes((REPO / "config" / "rubric.toml").read_bytes())
    assert load_rubric(copy) == rubric


def test_load_rubric_rejects_a_bad_pattern(tmp_path: Path) -> None:
    text = (REPO / "config" / "rubric.toml").read_text(encoding="utf-8")
    bad = tmp_path / "bad.toml"
    bad.write_text(text.replace(r"patterns = ['\bhigh\b']", "patterns = ['(unclosed']"), encoding="utf-8")
    with pytest.raises(ValueError, match="unclosed"):
        load_rubric(bad)


def test_scoring_uses_the_given_rubric(tmp_path: Path, profiles: dict[str, VendorProfile]) -> None:
    text = (REPO / "config" / "rubric.toml").read_text(encoding="utf-8")
    custom = tmp_path / "custom.toml"
    custom.write_text(text.replace('version = "1.1"', 'version = "1.1-test"'), encoding="utf-8")
    assert score_profile(profiles["V-005"], load_rubric(custom)).rubric_version == "1.1-test"


def test_rubric_table_lists_every_anchor() -> None:
    rows = rubric_table(load_rubric())
    keys = {"factor", "factor_name", "weight", "level", "anchor", "label", "description", "patterns", "rules",
            "source_fields"}
    assert all(set(row) == keys for row in rows)
    for factor in WEIGHTS:
        assert {row["level"] for row in rows if row["factor"] == factor} == {0, 1, 2, 3, 4}
    d3p = next(row for row in rows if row["anchor"] == "D3-P")
    assert (d3p["level"], d3p["weight"]) == (3, 6)
    o4 = next(row for row in rows if row["anchor"] == "O4")
    assert "critical" in o4["patterns"]
    assert rubric_table() == rows


def test_factor_labels_and_floor_descriptions() -> None:
    assert [factor_label(f) for f in "ODPRV"] == [
        "operational dependency", "data sensitivity", "payment-flow involvement", "regulatory exposure", "volume"]
    assert "payment" in floor_description("F2").lower()
    assert all(floor_description(f"F{i}") for i in range(1, 7))
    with pytest.raises(ValueError):
        factor_label("X")
    with pytest.raises(ValueError):
        floor_description("F9")


# --------------------------------------------------------------------------- reviewer regressions (P1 fixer)


@pytest.mark.parametrize(("service", "process", "level"), [
    ("ACH origination service", "Originates ACH credit and debit files", 3),
    ("Wire gateway", "Outgoing wire transfers to Fedwire", 3),
    ("Card authorization and processing", "Card payments", 4),
    ("Fraud scoring", "Real-time fraud screening of card transactions", 3),
])
def test_payment_rails_are_in_the_payment_path(service: str, process: str, level: int) -> None:
    result = score_profile(_profile(service=service, business_process=process, operational_dependency="High",
                                    data_accessed="Account numbers"))
    assert result.factors["P"].level == level
    assert result.factors["R"].level == 4
    assert "F2" in result.floors_fired and result.tier == Tier.CRITICAL


@pytest.mark.parametrize("data", ["Server health metrics", "System health checks"])
def test_infrastructure_health_is_not_medical_data(data: str) -> None:
    result = score_profile(_profile(data_accessed=data, operational_dependency="Low"))
    assert result.factors["D"].level < 4
    assert "F5" not in result.floors_fired


def test_personal_health_data_is_d4() -> None:
    assert score_profile(_profile(data_accessed="Employee health records")).factors["D"].level == 4


@pytest.mark.parametrize("data", ["Domain admin credentials to production servers", "Root access to servers",
                                  "Credentials for production databases", "Privileged accounts"])
def test_privileged_access_wording_is_d3p(data: str) -> None:
    result = score_profile(_profile(data_accessed=data, operational_dependency="Moderate"))
    assert result.factors["D"].anchor == "D3-P"
    assert "F4" in result.floors_fired and result.tier.rank >= Tier.HIGH.rank


@pytest.mark.parametrize(("volume", "level"), [
    ("5 million ACH entries a year", 3), ("30,000 wires per month", 2), ("2,300 employees", 1),
    ("10,000 items a day", 3), ("40,000 payments each week", 3),
])
def test_volume_nouns_and_periods(volume: str, level: int) -> None:
    assert score_profile(_profile(data_accessed="Account numbers", data_volume=volume)).factors["V"].level == level


def test_rationale_has_no_floor_codes(profiles: dict[str, VendorProfile],
                                      results: dict[str, CriticalityResult]) -> None:
    for vid, result in results.items():
        assert not re.search(r"\bF[1-6]\b", render_rationale(profiles[vid], result)), vid
