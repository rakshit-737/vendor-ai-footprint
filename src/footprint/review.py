"""Analyst review store: HC1 tier overrides, HC2 evidence reviews, risk-input overrides and cell edits.

Each store is an append-only JSONL file under review/ (overrides.jsonl, reviews.jsonl). Lines are never rewritten
or deleted: a later record for the same (kind, vendor_id, key) supersedes an earlier one, so the file is also the
audit trail. Every record names the analyst and the date and gives a reason of at least 10 characters.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from footprint.models import Tier, TierOverride

REVIEW_DIR: Path = Path(__file__).resolve().parents[2] / "review"
OVERRIDES_PATH: Path = REVIEW_DIR / "overrides.jsonl"  # tier and risk-input overrides, cell edits
REVIEWS_PATH: Path = REVIEW_DIR / "reviews.jsonl"  # evidence accept / reject decisions
OVERRIDES_ENV = "FOOTPRINT_OVERRIDES"  # names another override store (tests, a second engagement)


def default_overrides_path() -> Path:
    """The override store used when none is given: $FOOTPRINT_OVERRIDES, else OVERRIDES_PATH (never the cwd)."""
    return Path(os.environ.get(OVERRIDES_ENV) or OVERRIDES_PATH)

ReviewKind = Literal["tier_override", "evidence_review", "risk_input_override", "cell_edit"]
TIER_KEY = "tier"
MIN_REASON_CHARS = 10
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def utc_now() -> str:
    """Current UTC time, e.g. '2026-10-02T14:40:00Z'. Record metadata only, never decision logic."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReviewRecord(BaseModel):
    """One analyst decision. The same (kind, vendor_id, key) may be recorded again; the last record wins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ReviewKind
    vendor_id: str
    key: str = Field(description="'tier', an evidence hash or a field name")
    value: str = Field(description="the decision, e.g. 'High', 'accepted' or the edited cell text")
    reason: str = Field(description=f"why; at least {MIN_REASON_CHARS} characters")
    analyst: str
    date: str = Field(description="ISO date YYYY-MM-DD of the decision")
    recorded_at: str = Field(default_factory=utc_now, description="ISO 8601 UTC time the record was made")
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("vendor_id", "key", "analyst")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("reason")
    @classmethod
    def _reason_long_enough(cls, value: str) -> str:
        if len(value.strip()) < MIN_REASON_CHARS:
            raise ValueError(f"reason must have at least {MIN_REASON_CHARS} characters")
        return value

    @field_validator("date")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        if not _ISO_DATE.fullmatch(value) or not _is_calendar_date(value):
            raise ValueError("date must be an ISO date YYYY-MM-DD")
        return value

    @field_validator("recorded_at")
    @classmethod
    def _utc_timestamp(cls, value: str) -> str:
        try:
            offset = dt.datetime.fromisoformat(value).utcoffset()
        except ValueError:
            offset = None
        if offset != dt.timedelta(0):
            raise ValueError("recorded_at must be an ISO 8601 UTC time, e.g. 2026-10-02T14:40:00Z")
        return value

    @model_validator(mode="after")
    def _tier_override_names_a_tier(self) -> ReviewRecord:
        if self.kind == "tier_override":
            if self.key != TIER_KEY:
                raise ValueError(f"a tier_override record must use key '{TIER_KEY}'")
            if self.value not in {t.value for t in Tier}:
                raise ValueError(f"tier_override value must be one of {', '.join(t.value for t in Tier)}")
        return self


def _is_calendar_date(value: str) -> bool:
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


class OverrideStore:
    """Append-only JSONL store of ReviewRecords: one JSON object per line, UTF-8, LF, sorted keys."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def add(self, record: ReviewRecord | Mapping[str, Any]) -> ReviewRecord:
        """Validate the record, then append it as one line. Nothing is written when validation fails."""
        data = record.model_dump() if isinstance(record, ReviewRecord) else dict(record)
        checked = ReviewRecord.model_validate(data)
        line = json.dumps(checked.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        separator = "\n" if self._ends_mid_line() else ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{separator}{line}\n")
        return checked

    def records(self, kind: ReviewKind | None = None, vendor_id: str | None = None) -> list[ReviewRecord]:
        """Records in file order, optionally filtered by kind and vendor. A malformed line raises ValueError."""
        if not self.path.exists():
            return []
        found: list[ReviewRecord] = []
        with self.path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    rec = ReviewRecord.model_validate_json(line.strip())
                except ValidationError as exc:
                    raise ValueError(f"{self.path}:{lineno}: not a valid review record: {exc}") from exc
                if (kind is None or rec.kind == kind) and (vendor_id is None or rec.vendor_id == vendor_id):
                    found.append(rec)
        return found

    def latest(self, kind: ReviewKind, vendor_id: str, key: str) -> ReviewRecord | None:
        """The last record for (kind, vendor_id, key): last write wins."""
        matches = [r for r in self.records(kind, vendor_id) if r.key == key]
        return matches[-1] if matches else None

    def add_tier_override(self, override: TierOverride, recorded_at: str | None = None) -> ReviewRecord:
        """Record an HC1 tier override as a 'tier_override' record keyed 'tier'."""
        data: dict[str, Any] = {
            "kind": "tier_override",
            "vendor_id": override.vendor_id,
            "key": TIER_KEY,
            "value": override.tier.value,
            "reason": override.reason,
            "analyst": override.analyst,
            "date": override.date,
        }
        if recorded_at is not None:
            data["recorded_at"] = recorded_at
        return self.add(data)

    def tier_override(self, vendor_id: str) -> TierOverride | None:
        """The vendor's current tier override, or None if no analyst has overridden the computed tier."""
        rec = self.latest("tier_override", vendor_id, TIER_KEY)
        if rec is None:
            return None
        return TierOverride(
            vendor_id=rec.vendor_id, tier=Tier(rec.value), reason=rec.reason, analyst=rec.analyst, date=rec.date
        )

    def _ends_mid_line(self) -> bool:
        """True when the last line lacks its LF (interrupted write), so the next record starts a new line."""
        try:
            with self.path.open("rb") as fh:
                if fh.seek(0, os.SEEK_END) == 0:
                    return False
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) != b"\n"
        except FileNotFoundError:
            return False
