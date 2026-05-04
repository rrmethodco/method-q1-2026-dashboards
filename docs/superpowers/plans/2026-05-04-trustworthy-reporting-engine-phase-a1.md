# Trustworthy Reporting Engine — Phase A.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the foundation of Method Co's agentic reporting engine — Pydantic validation gates on every sync, a Supabase Edge Function agent worker running schema-drift / anomaly / retry agents, Slack alerts on hard-fails, and a per-outlet validation panel on the dashboard.

**Architecture:** Hybrid. Existing GitHub Actions sync workflows continue to ingest data; Pydantic validators are added inline to each sync script and write a `_validation/<source>_<timestamp>.json` summary alongside the data payload. A Supabase Edge Function (Deno + TypeScript), triggered by `pg_cron` every 5 minutes, reads the latest validation summaries from a Supabase Storage bucket, runs the agent loops (schema-drift detector via Anthropic API, anomaly detector via in-process stats, retry/repair via GitHub API, alert dispatcher via Slack `chat.postMessage`), and writes back banner state + audit log.

**Tech Stack:**
- Python 3.12, Pydantic V2, pytest (existing)
- GitHub Actions (existing)
- Supabase (Edge Functions = Deno/TypeScript runtime; Storage; pg_cron)
- Anthropic API (Sonnet for drift detector classification)
- Slack Web API (`@slack/web-api` from Deno)
- Existing dashboard HTML/JS (vanilla, no build step)

**Spec:** `docs/superpowers/specs/2026-05-04-trustworthy-reporting-engine-design.md`

---

## File structure overview

**New files:**

```
toast-etl/
├── schemas/                                # NEW package — Pydantic models
│   ├── __init__.py
│   ├── _base.py                            # Common base + business-rule mixin
│   ├── toast_order.py
│   ├── toast_time_entry.py
│   ├── resy_survey.py
│   ├── marginedge_invoice.py
│   ├── tripleseat_event.py
│   ├── helixo2_forecast.py
│   └── sage_budget.py
├── validation/
│   ├── __init__.py
│   ├── runner.py                           # Pipes raw rows → models → _validation/*.json
│   ├── pii_redact.py                       # Redacts known PII fields before logging samples
│   └── retention.py                        # Auto-prunes old _validation files
└── tests/
    ├── schemas/
    │   ├── test_toast_order.py
    │   ├── test_toast_time_entry.py
    │   ├── ... (one per schema)
    └── validation/
        ├── test_runner.py
        ├── test_pii_redact.py
        └── test_retention.py

config/
└── metric_classes.yml                      # hard-fail vs annotate vs auto-heal map

data/                                       # Output dirs created by syncs + agents
├── _validation/<source>_<timestamp>.json   # one per sync run
├── _validation_errors/<source>_<timestamp>.json
├── _banner/<outlet>.json                   # written by alert dispatcher
├── _audit/agent_decisions.jsonl            # append-only
├── _schemas/<source>.json                  # last-known schema, updated by drift agent
└── _anomalies/<outlet>.json                # rolling history per outlet

supabase/
├── functions/
│   └── agent-worker/
│       ├── index.ts                        # Edge Function entry — orchestrates the agents
│       ├── deno.json
│       ├── lib/
│       │   ├── github.ts                   # gh-API client
│       │   ├── slack.ts                    # Slack chat.postMessage client
│       │   ├── anthropic.ts                # Claude API client
│       │   ├── storage.ts                  # Supabase Storage helpers
│       │   └── types.ts                    # shared types
│       └── agents/
│           ├── drift_detector.ts
│           ├── anomaly_detector.ts
│           ├── retry_repair.ts
│           └── alert_dispatcher.ts
├── migrations/
│   └── 20260504_agent_worker.sql           # pg_cron schedule + storage bucket policy
└── seed.sql                                # local dev seed

Method_Co_FB_Performance_Dashboard.html     # MODIFIED — add validation panel UI
```

**Modified files:**
- `toast-etl/toast_sync.py` — add validation pipeline before commit
- `toast-etl/resy_os_scraper.py` — same
- `toast-etl/marginedge_sync.py` — same
- `toast-etl/tripleseat_sync.py` — same
- `toast-etl/forecast_engine.py` — same
- `toast-etl/budget_sync.py` — same
- `Method_Co_FB_Performance_Dashboard.html` — add validation panel UI element + JS
- `.github/workflows/{toast,guest,budget,marginedge,tripleseat,forecast}-sync.yml` — push validation files to Supabase Storage after commit

---

## Sprint 1 — Validation foundation (Tasks 1-17)

### Task 1: Scaffold the schemas package

**Files:**
- Create: `toast-etl/schemas/__init__.py`
- Create: `toast-etl/schemas/_base.py`
- Create: `toast-etl/tests/__init__.py`
- Create: `toast-etl/tests/schemas/__init__.py`
- Modify: `toast-etl/requirements.txt` — add `pydantic>=2.5,<3` and `pytest>=8`

- [ ] **Step 1: Add Pydantic + pytest to requirements**

Edit `toast-etl/requirements.txt` — append:
```
pydantic>=2.5,<3
pytest>=8.0,<9
```

- [ ] **Step 2: Install + verify**

```bash
cd toast-etl && pip install -r requirements.txt
python3 -c "import pydantic; print(pydantic.VERSION)"
```
Expected: `2.x.y` printed.

- [ ] **Step 3: Create the base module**

Write `toast-etl/schemas/_base.py`:

```python
"""Shared base for all source-row Pydantic models.

Every source row gets a Pydantic V2 model that:
1. Declares required + optional fields with types
2. Declares value bounds (e.g. amount >= 0)
3. Implements validate_business_rules() for cross-field invariants

The validation runner (toast-etl/validation/runner.py) pipes raw rows
through these models and writes a _validation/<source>_<ts>.json file
per run so the agent worker can detect drift, anomalies, and failures.
"""
from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict


class SourceRow(BaseModel):
    """Base for all source-row models.

    Subclasses MUST override _source_name. Subclasses SHOULD override
    validate_business_rules() if cross-field invariants apply.
    """
    model_config = ConfigDict(
        # Permissive ingestion: don't crash on extra Resy fields, etc.
        # If we silently dropped them and a critical field showed up
        # under an unexpected name, the schema-drift detector would
        # eventually flag it (Phase A.1 scope) — but during validation
        # we want to be tolerant.
        extra="allow",
        # Coerce JSON strings to int/float when shape is unambiguous.
        # Toast and MarginEdge both occasionally return numeric fields
        # as strings.
        str_strip_whitespace=True,
        validate_assignment=False,
    )

    _source_name: str = "unknown"

    def validate_business_rules(self) -> list[str]:
        """Return list of human-readable error strings.

        Empty list = all invariants hold. Subclasses override to add
        cross-field checks. Caller (validation runner) decides whether
        to fail or annotate based on the source's metric class.
        """
        return []
```

- [ ] **Step 4: Create the package `__init__.py`s**

Write `toast-etl/schemas/__init__.py`:

```python
"""Source-row Pydantic schemas for Method Co data ingestion."""
from ._base import SourceRow

__all__ = ["SourceRow"]
```

Write `toast-etl/tests/__init__.py` and `toast-etl/tests/schemas/__init__.py` as empty files (`touch`).

- [ ] **Step 5: Verify import works**

```bash
cd toast-etl && python3 -c "from schemas import SourceRow; print(SourceRow.__name__)"
```
Expected: `SourceRow`.

- [ ] **Step 6: Commit**

```bash
git add toast-etl/schemas/ toast-etl/tests/__init__.py toast-etl/tests/schemas/__init__.py toast-etl/requirements.txt
git commit -m "feat(schemas): scaffold Pydantic schemas package + pytest"
```

---

### Task 2: ToastOrder Pydantic schema

**Files:**
- Create: `toast-etl/schemas/toast_order.py`
- Create: `toast-etl/tests/schemas/test_toast_order.py`

- [ ] **Step 1: Inspect the actual Toast order shape from current data**

```bash
cd "$(git rev-parse --show-toplevel)" && python3 -c "
import json
# Use the in-flight order_details.main.daily as a proxy — Toast raw orders
# aren't kept post-transform. Read the daily aggregates structure to
# confirm field names and types we DO surface to the dashboard.
d = json.load(open('data/lsbr.json'))
sample = d['order_details']['main']['daily'][-1]
print('daily row keys:', list(sample.keys()))
print('sample:', sample)
"
```
Expected: keys like `date, orders, guests, amount, tip, gratuity, discount, ticket_time_sec_sum, ticket_time_count`.

- [ ] **Step 2: Inspect raw Toast order shape from a fresh sync (one-time)**

```bash
# Pull the most recent toast-sync workflow log to see a sample raw row.
# We're after the structure of a single check inside a single order from /ordersBulk.
gh run list --workflow=toast-sync.yml --limit 1 --json databaseId -q '.[0].databaseId' | xargs -I {} gh run view {} --log 2>&1 | grep -A 1 -E "checks|amount|paidDate|guestCount" | head -40
```
Note: Toast raw rows aren't logged in full. Reference is `toast-etl/toast_sync.py:733` which reads `check.get("paidDate")`, `check.get("amount")`, `check.get("appliedDiscounts")`, etc.

- [ ] **Step 3: Write the failing test**

Write `toast-etl/tests/schemas/test_toast_order.py`:

```python
"""Tests for ToastOrder schema.

Built from current Toast /ordersBulk row shape as observed at
toast_sync.py:730-810. Goal: every row CURRENTLY produced by a
healthy sync MUST validate. Strictness comes later via business rules.
"""
import pytest
from datetime import datetime
from schemas.toast_order import ToastOrder, ToastCheck


def test_minimal_valid_order():
    raw = {
        "guid": "abc-123",
        "openedDate": "2026-04-22T19:00:00.000Z",
        "closedDate": "2026-04-22T20:30:00.000Z",
        "voided": False,
        "deleted": False,
        "numberOfGuests": 2,
        "checks": [{
            "guid": "chk-1",
            "voided": False,
            "deleted": False,
            "amount": 87.50,
            "tipAmount": 17.50,
            "openedDate": "2026-04-22T19:00:00.000Z",
            "paidDate": "2026-04-22T20:30:00.000Z",
            "selections": [],
            "appliedDiscounts": [],
            "appliedServiceCharges": [],
        }],
    }
    o = ToastOrder.model_validate(raw)
    assert o.guid == "abc-123"
    assert o.checks[0].amount == 87.50
    assert o.validate_business_rules() == []


def test_voided_order_validates_but_zero_amount_ok():
    """Voided orders are emitted by Toast with amount=0; must validate."""
    raw = {
        "guid": "v-1", "voided": True, "deleted": False,
        "openedDate": "2026-04-22T19:00:00.000Z",
        "closedDate": "2026-04-22T19:01:00.000Z",
        "checks": [{"guid": "v-c", "voided": True, "amount": 0,
                    "tipAmount": 0, "openedDate": "2026-04-22T19:00:00.000Z"}],
    }
    o = ToastOrder.model_validate(raw)
    assert o.voided is True
    assert o.validate_business_rules() == []


def test_paid_before_opened_fails_business_rule():
    raw = {
        "guid": "bad", "voided": False, "deleted": False,
        "openedDate": "2026-04-22T20:00:00.000Z",
        "closedDate": "2026-04-22T20:30:00.000Z",
        "checks": [{
            "guid": "c", "voided": False, "amount": 50, "tipAmount": 10,
            "openedDate": "2026-04-22T20:00:00.000Z",
            "paidDate": "2026-04-22T19:00:00.000Z",  # paid before opened — bug
        }],
    }
    o = ToastOrder.model_validate(raw)
    errs = o.validate_business_rules()
    assert any("paid_before_opened" in e for e in errs)


def test_negative_amount_fails():
    raw = {
        "guid": "neg", "voided": False, "deleted": False,
        "openedDate": "2026-04-22T19:00:00.000Z",
        "checks": [{"guid": "c", "voided": False, "amount": -10,
                    "tipAmount": 0, "openedDate": "2026-04-22T19:00:00.000Z"}],
    }
    with pytest.raises(Exception):
        ToastOrder.model_validate(raw)


def test_missing_required_check_fields_fails():
    raw = {
        "guid": "miss", "voided": False, "deleted": False,
        "openedDate": "2026-04-22T19:00:00.000Z",
        "checks": [{}],  # missing amount, tipAmount, etc.
    }
    with pytest.raises(Exception):
        ToastOrder.model_validate(raw)
```

- [ ] **Step 4: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/schemas/test_toast_order.py -v
```
Expected: All 5 tests FAIL with `ModuleNotFoundError: No module named 'schemas.toast_order'`.

- [ ] **Step 5: Implement the schema**

Write `toast-etl/schemas/toast_order.py`:

```python
"""Toast /ordersBulk row schema.

Built from the consumer at toast_sync.py:726-810. Toast emits orders
with one or more checks; checks have selections (line items),
discounts, and service charges. We require enough fields to compute:
  - net_sales (sum of check.amount)
  - covers (order.numberOfGuests OR check.customer.guestCount)
  - tip + gratuity
  - discount $
  - ticket time (paidDate - openedDate)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import Field, field_validator

from ._base import SourceRow


class ToastSelection(SourceRow):
    """A line item on a check. Permissive — Toast adds new fields here often."""
    guid: Optional[str] = None
    appliedDiscounts: list[dict] = Field(default_factory=list)


class ToastCheck(SourceRow):
    guid: str
    voided: bool = False
    deleted: bool = False
    amount: float = Field(ge=0, description="Pre-tax subtotal — must be >= 0")
    tipAmount: float = Field(default=0, ge=0)
    openedDate: datetime
    paidDate: Optional[datetime] = None
    closedDate: Optional[datetime] = None
    selections: list[ToastSelection] = Field(default_factory=list)
    appliedDiscounts: list[dict] = Field(default_factory=list)
    appliedServiceCharges: list[dict] = Field(default_factory=list)
    customer: Optional[dict] = None

    @field_validator("amount", "tipAmount", mode="before")
    @classmethod
    def _coerce_numeric(cls, v):
        # Toast occasionally emits "amount": "87.50" as a string
        if isinstance(v, str):
            return float(v)
        return v


class ToastOrder(SourceRow):
    _source_name = "toast_order"

    guid: str
    voided: bool = False
    deleted: bool = False
    openedDate: datetime
    closedDate: Optional[datetime] = None
    numberOfGuests: int = Field(default=0, ge=0)
    checks: list[ToastCheck]

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        # Skip business-rule checks on voided/deleted orders — Toast often
        # leaves zeroed-out fields on these.
        if self.voided or self.deleted:
            return errors
        for i, c in enumerate(self.checks):
            if c.paidDate and c.paidDate < c.openedDate:
                errors.append(f"check[{i}]: paid_before_opened "
                              f"(paid={c.paidDate.isoformat()}, "
                              f"opened={c.openedDate.isoformat()})")
            if self.closedDate and self.closedDate < self.openedDate:
                errors.append(f"order: closed_before_opened "
                              f"(closed={self.closedDate.isoformat()}, "
                              f"opened={self.openedDate.isoformat()})")
        return errors
```

- [ ] **Step 6: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/schemas/test_toast_order.py -v
```
Expected: All 5 PASS.

- [ ] **Step 7: Validate against current data (smoke test)**

```bash
cd toast-etl && python3 -c "
import json, sys
from schemas.toast_order import ToastOrder
# We don't store raw orders, but we can synthesize one from order_details
# to confirm the model loads. Real validation happens once Task 10 wires
# the model into toast_sync.py and the next sync run uses it.
sample = {
    'guid': 'smoke', 'voided': False, 'deleted': False,
    'openedDate': '2026-05-04T19:00:00.000Z',
    'closedDate': '2026-05-04T20:00:00.000Z',
    'numberOfGuests': 2,
    'checks': [{'guid':'c','voided':False,'amount':50,'tipAmount':10,
                'openedDate':'2026-05-04T19:00:00.000Z',
                'paidDate':'2026-05-04T20:00:00.000Z'}]
}
o = ToastOrder.model_validate(sample)
print('OK', o.guid, len(o.checks))
"
```
Expected: `OK smoke 1`.

- [ ] **Step 8: Commit**

```bash
git add toast-etl/schemas/toast_order.py toast-etl/tests/schemas/test_toast_order.py
git commit -m "feat(schemas): ToastOrder + ToastCheck Pydantic models with business rules"
```

---

### Task 3: ToastTimeEntry Pydantic schema

**Files:**
- Create: `toast-etl/schemas/toast_time_entry.py`
- Create: `toast-etl/tests/schemas/test_toast_time_entry.py`

- [ ] **Step 1: Reference current usage**

Read `toast-etl/toast_sync.py:439-540` (the `transform_time_entries` function) to see fields consumed: `deleted`, `businessDate`, `regularHours`, `overtimeHours`, `hourlyWage`, `employeeReference`, `jobReference`, `inDate`, `outDate`.

- [ ] **Step 2: Write the failing test**

Write `toast-etl/tests/schemas/test_toast_time_entry.py`:

```python
"""Tests for ToastTimeEntry schema.

Built from toast_sync.py:476-540. Each row is a single shift clock-in/out.
"""
import pytest
from schemas.toast_time_entry import ToastTimeEntry


def test_minimal_valid_entry():
    raw = {
        "guid": "te-1",
        "deleted": False,
        "businessDate": "20260424",
        "regularHours": 8.0,
        "overtimeHours": 0.0,
        "hourlyWage": 18.50,
        "inDate": "2026-04-24T15:00:00.000Z",
        "outDate": "2026-04-24T23:00:00.000Z",
        "employeeReference": {"guid": "emp-1"},
        "jobReference": {"guid": "job-bartender"},
    }
    e = ToastTimeEntry.model_validate(raw)
    assert e.regularHours == 8.0
    assert e.businessDate == "20260424"
    assert e.validate_business_rules() == []


def test_overtime_business_rule():
    """OT > 0 is normal; OT > 40h in a single shift is a sanity check."""
    raw = {
        "guid": "ot-bug", "deleted": False, "businessDate": "20260424",
        "regularHours": 0.0, "overtimeHours": 50.0, "hourlyWage": 18.5,
        "inDate": "2026-04-24T15:00:00.000Z",
        "outDate": "2026-04-25T15:00:00.000Z",
        "employeeReference": {"guid": "emp"}, "jobReference": {"guid": "job"},
    }
    e = ToastTimeEntry.model_validate(raw)
    errs = e.validate_business_rules()
    assert any("overtime_implausible" in s for s in errs)


def test_clockout_before_clockin_fails():
    raw = {
        "guid": "neg", "deleted": False, "businessDate": "20260424",
        "regularHours": 1.0, "overtimeHours": 0.0, "hourlyWage": 18.5,
        "inDate": "2026-04-24T20:00:00.000Z",
        "outDate": "2026-04-24T19:00:00.000Z",
        "employeeReference": {"guid": "e"}, "jobReference": {"guid": "j"},
    }
    e = ToastTimeEntry.model_validate(raw)
    assert any("clockout_before_clockin" in s for s in e.validate_business_rules())


def test_negative_hours_fails():
    with pytest.raises(Exception):
        ToastTimeEntry.model_validate({
            "guid": "n", "deleted": False, "businessDate": "20260424",
            "regularHours": -1.0, "overtimeHours": 0.0, "hourlyWage": 18.5,
            "inDate": "2026-04-24T15:00:00.000Z",
            "outDate": "2026-04-24T23:00:00.000Z",
            "employeeReference": {"guid": "e"}, "jobReference": {"guid": "j"},
        })


def test_business_date_format_must_be_yyyymmdd():
    with pytest.raises(Exception):
        ToastTimeEntry.model_validate({
            "guid": "fmt", "deleted": False,
            "businessDate": "2026-04-24",  # wrong: should be 20260424
            "regularHours": 1.0, "overtimeHours": 0.0, "hourlyWage": 18.5,
            "inDate": "2026-04-24T15:00:00.000Z",
            "outDate": "2026-04-24T16:00:00.000Z",
            "employeeReference": {"guid": "e"}, "jobReference": {"guid": "j"},
        })
```

- [ ] **Step 3: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/schemas/test_toast_time_entry.py -v
```
Expected: All 5 FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement the schema**

Write `toast-etl/schemas/toast_time_entry.py`:

```python
"""Toast /labor/v1/timeEntries row schema.

Built from toast_sync.py:476-540. Each row is one shift.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from pydantic import Field, field_validator

from ._base import SourceRow


class ToastEmployeeRef(SourceRow):
    guid: str


class ToastJobRef(SourceRow):
    guid: str


class ToastTimeEntry(SourceRow):
    _source_name = "toast_time_entry"

    guid: str
    deleted: bool = False
    # Toast emits businessDate as a YYYYMMDD digit string. The transform
    # at toast_sync.py:480 fails if len != 8 — model the same constraint.
    businessDate: str = Field(min_length=8, max_length=8, pattern=r"^\d{8}$")
    regularHours: float = Field(ge=0)
    overtimeHours: float = Field(ge=0)
    hourlyWage: float = Field(ge=0)
    inDate: datetime
    outDate: Optional[datetime] = None
    employeeReference: ToastEmployeeRef
    jobReference: ToastJobRef

    @field_validator("regularHours", "overtimeHours", "hourlyWage", mode="before")
    @classmethod
    def _coerce_numeric(cls, v):
        if isinstance(v, str):
            return float(v)
        return v

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        if self.deleted:
            return errors
        # Sanity: a single clock-in shouldn't claim more than 40 OT hours.
        if self.overtimeHours > 40:
            errors.append(f"overtime_implausible: ot={self.overtimeHours}")
        # Sanity: clockout before clockin (Toast occasionally emits these
        # for manual entries — we don't trust them).
        if self.outDate and self.outDate < self.inDate:
            errors.append(f"clockout_before_clockin: in={self.inDate.isoformat()} "
                          f"out={self.outDate.isoformat()}")
        # Sanity: total hours should be plausible vs span between in/out.
        if self.outDate:
            span_hours = (self.outDate - self.inDate).total_seconds() / 3600
            total = self.regularHours + self.overtimeHours
            # Allow 20% slack (breaks); if we report way more hours than
            # the span allows, something is off.
            if total > span_hours * 1.2 + 0.5:
                errors.append(f"hours_exceed_span: total={total} span={span_hours:.2f}")
        return errors
```

- [ ] **Step 5: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/schemas/test_toast_time_entry.py -v
```
Expected: All 5 PASS.

- [ ] **Step 6: Commit**

```bash
git add toast-etl/schemas/toast_time_entry.py toast-etl/tests/schemas/test_toast_time_entry.py
git commit -m "feat(schemas): ToastTimeEntry Pydantic model with shift-sanity rules"
```

---

### Task 4: ResySurvey Pydantic schema

**Files:**
- Create: `toast-etl/schemas/resy_survey.py`
- Create: `toast-etl/tests/schemas/test_resy_survey.py`

- [ ] **Step 1: Reference current shape**

Read `toast-etl/resy_os_scraper.py:275-291` for the shape `transform_resy_survey_row` returns. Also note the schema-drift incident: post 2026-04-17 all 5 score buckets (food/service/atmos/sentiment/recommend) come back null — this model must validate that case (annotate, not hard-fail) since it's the actual data we have.

- [ ] **Step 2: Inspect actual current rows**

```bash
cd "$(git rev-parse --show-toplevel)" && python3 -c "
import json
d = json.load(open('data/lsbr.json'))
surveys = d['guest']['surveys']
print('total surveys:', len(surveys))
print('keys on a recent row:', sorted(surveys[-1].keys()))
print('keys on an old row:', sorted(surveys[0].keys()))
print('recent (drift):', surveys[-1])
"
```
Expected output: keys `date, overall, sentiment, service, food, atmos, server, recommend, covers, dow, hour` (some may be `text` or `null`).

- [ ] **Step 3: Write the failing test**

Write `toast-etl/tests/schemas/test_resy_survey.py`:

```python
"""Tests for ResySurvey schema.

Critical: post-2026-04-17 surveys have null score buckets (food/service/
atmos/sentiment/recommend). Model must accept these — they're real data.
The schema-drift agent (Task 22) detects + classifies the issue; the
model itself doesn't crash on it.
"""
import pytest
from schemas.resy_survey import ResySurvey


def test_minimal_pre_drift_row():
    raw = {
        "date": "2026-04-15", "overall": 100, "sentiment": 100,
        "service": 100, "food": None, "atmos": None,
        "server": "Claire", "recommend": 10, "covers": 5,
        "dow": 2, "hour": 18,
    }
    s = ResySurvey.model_validate(raw)
    assert s.recommend == 10
    assert s.validate_business_rules() == []


def test_post_drift_row_validates():
    """Post-2026-04-17 shape: all 5 score buckets null. Must validate."""
    raw = {
        "date": "2026-04-29", "overall": 100, "sentiment": None,
        "service": None, "food": None, "atmos": None,
        "server": "Claire", "recommend": None, "covers": 2,
        "dow": 2, "hour": 22,
    }
    s = ResySurvey.model_validate(raw)
    errs = s.validate_business_rules()
    # Soft-signal: business rule warns but doesn't reject.
    assert any("all_score_buckets_null" in e for e in errs)


def test_recommend_out_of_range_fails():
    with pytest.raises(Exception):
        ResySurvey.model_validate({
            "date": "2026-04-15", "overall": 100, "recommend": 11,
            "covers": 1, "dow": 0, "hour": 18,
        })


def test_overall_out_of_range_fails():
    with pytest.raises(Exception):
        ResySurvey.model_validate({
            "date": "2026-04-15", "overall": 150, "recommend": 9,
            "covers": 1, "dow": 0, "hour": 18,
        })


def test_invalid_date_format_fails():
    with pytest.raises(Exception):
        ResySurvey.model_validate({
            "date": "April 15, 2026", "overall": 100, "recommend": 10,
            "covers": 1, "dow": 0, "hour": 18,
        })
```

- [ ] **Step 4: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/schemas/test_resy_survey.py -v
```
Expected: All 5 FAIL with `ModuleNotFoundError`.

- [ ] **Step 5: Implement the schema**

Write `toast-etl/schemas/resy_survey.py`:

```python
"""Resy OS survey row schema.

Built from resy_os_scraper.py transform_resy_survey_row output.
The 5 score buckets (food, service, atmos, sentiment, recommend) are
all Optional because Resy schema drift on/around 2026-04-17 nulled
them — the schema-drift detector (Task 22) flags this as a known
condition; the model accepts the rows so they still ingest.
"""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


class ResySurvey(SourceRow):
    _source_name = "resy_survey"

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    overall: Optional[int] = Field(default=None, ge=0, le=100)
    sentiment: Optional[int] = Field(default=None, ge=0, le=100)
    service: Optional[int] = Field(default=None, ge=0, le=100)
    food: Optional[int] = Field(default=None, ge=0, le=100)
    atmos: Optional[int] = Field(default=None, ge=0, le=100)
    server: Optional[str] = None
    recommend: Optional[int] = Field(default=None, ge=0, le=10,
                                     description="NPS-scale 0-10 promoter score")
    covers: Optional[int] = Field(default=None, ge=0)
    dow: Optional[int] = Field(default=None, ge=0, le=6)
    hour: Optional[int] = Field(default=None, ge=0, le=23)
    text: Optional[list[dict]] = None  # free-text comments

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        # Schema-drift signal: all 5 score buckets null = the keyword
        # router missed everything. The schema-drift agent uses this
        # signal too. This is an annotation, not a hard fail — Resy
        # IS giving us SOMETHING (overall + server + covers).
        score_buckets = [self.food, self.service, self.atmos,
                         self.sentiment, self.recommend]
        if all(b is None for b in score_buckets):
            errors.append("all_score_buckets_null: probable Resy schema drift "
                          "(see resy_os_scraper.py _DRIFT_SAMPLES + "
                          "Task 22 drift detector)")
        return errors
```

- [ ] **Step 6: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/schemas/test_resy_survey.py -v
```
Expected: All 5 PASS.

- [ ] **Step 7: Validate against current data**

```bash
cd toast-etl && python3 -c "
import json, sys
sys.path.insert(0, '.')
from schemas.resy_survey import ResySurvey
d = json.load(open('../data/lsbr.json'))
ok = err = warn = 0
for s in d['guest']['surveys']:
    try:
        m = ResySurvey.model_validate(s)
        rules = m.validate_business_rules()
        if rules:
            warn += 1
        else:
            ok += 1
    except Exception:
        err += 1
print(f'lsbr surveys: {ok} clean, {warn} drift-flagged, {err} hard-errors')
"
```
Expected: `clean` count > 0, `drift-flagged` count matches the post-Apr-17 surveys, `hard-errors` should be 0.

- [ ] **Step 8: Commit**

```bash
git add toast-etl/schemas/resy_survey.py toast-etl/tests/schemas/test_resy_survey.py
git commit -m "feat(schemas): ResySurvey model — accepts post-drift null buckets, flags via business rule"
```

---

### Task 5: MarginEdgeInvoice Pydantic schema

**Files:**
- Create: `toast-etl/schemas/marginedge_invoice.py`
- Create: `toast-etl/tests/schemas/test_marginedge_invoice.py`

- [ ] **Step 1: Reference current shape**

Read `toast-etl/marginedge_sync.py:255-310` (the `transform_order` function). Fields produced: `id, date, vendor, total, line_items[]`. Each line item: `id, product, qty, unit_cost, extended, cogs_bucket, category`.

- [ ] **Step 2: Inspect current data**

```bash
cd "$(git rev-parse --show-toplevel)" && python3 -c "
import json
d = json.load(open('data/lsbr.json'))
inv = d['cogs']['invoices'][-1]
print('invoice keys:', sorted(inv.keys()))
print('first line item keys:', sorted(inv['line_items'][0].keys()) if inv.get('line_items') else 'no line items')
"
```

- [ ] **Step 3: Write the failing test**

Write `toast-etl/tests/schemas/test_marginedge_invoice.py`:

```python
"""Tests for MarginEdgeInvoice + MarginEdgeLineItem schemas."""
import pytest
from schemas.marginedge_invoice import MarginEdgeInvoice


def test_minimal_valid_invoice():
    raw = {
        "id": "inv-123", "date": "2026-05-04",
        "vendor": "Sysco Detroit", "total": 1842.50,
        "line_items": [
            {"id": "li-1", "product": "Tomatoes", "qty": 24,
             "unit_cost": 2.10, "extended": 50.40,
             "cogs_bucket": "food", "category": "Produce"},
        ],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    assert inv.total == 1842.50
    assert len(inv.line_items) == 1
    assert inv.validate_business_rules() == []


def test_no_line_items_ok():
    """404-on-detail path (PR #88): order recorded with no line items."""
    raw = {
        "id": "inv-na", "date": "2026-05-04",
        "vendor": "Vendor", "total": 100.00, "line_items": [],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    assert inv.line_items == []
    assert inv.validate_business_rules() == []


def test_line_item_sum_mismatch_business_rule():
    """If line items sum != invoice total within 1%, business-rule warning."""
    raw = {
        "id": "mismatch", "date": "2026-05-04", "vendor": "v",
        "total": 100.00,
        "line_items": [
            {"id": "li", "product": "p", "qty": 1, "unit_cost": 50,
             "extended": 50.00, "cogs_bucket": "food"},
        ],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    errs = inv.validate_business_rules()
    assert any("line_items_sum_mismatch" in e for e in errs)


def test_negative_total_fails():
    with pytest.raises(Exception):
        MarginEdgeInvoice.model_validate({
            "id": "neg", "date": "2026-05-04", "vendor": "v",
            "total": -10, "line_items": [],
        })


def test_invalid_cogs_bucket_warned():
    raw = {
        "id": "wb", "date": "2026-05-04", "vendor": "v", "total": 50,
        "line_items": [
            {"id": "li", "product": "p", "qty": 1, "unit_cost": 50,
             "extended": 50, "cogs_bucket": "kitchen_supplies"},  # not a valid bucket
        ],
    }
    inv = MarginEdgeInvoice.model_validate(raw)
    errs = inv.validate_business_rules()
    assert any("unknown_cogs_bucket" in e for e in errs)
```

- [ ] **Step 4: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/schemas/test_marginedge_invoice.py -v
```
Expected: All 5 FAIL with `ModuleNotFoundError`.

- [ ] **Step 5: Implement the schema**

Write `toast-etl/schemas/marginedge_invoice.py`:

```python
"""MarginEdge invoice row schema.

Built from marginedge_sync.py transform_order output.
"""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


VALID_COGS_BUCKETS = {"food", "beer", "wine", "liquor", "na_beverages"}


class MarginEdgeLineItem(SourceRow):
    id: Optional[str] = None
    product: str
    qty: float = Field(ge=0)
    unit_cost: float = Field(ge=0)
    extended: float = Field(ge=0)
    cogs_bucket: Optional[str] = None
    category: Optional[str] = None


class MarginEdgeInvoice(SourceRow):
    _source_name = "marginedge_invoice"

    id: str
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    vendor: str
    total: float = Field(ge=0)
    line_items: list[MarginEdgeLineItem] = Field(default_factory=list)

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        # Line items sum should equal the invoice total within 1% — when
        # they diverge by more, MarginEdge has either dropped a line item
        # or mis-reported a unit cost. Skip when no line items (the
        # 404-on-detail path from PR #88).
        if self.line_items:
            li_sum = sum(li.extended for li in self.line_items)
            if self.total > 0:
                drift = abs(li_sum - self.total) / self.total
                if drift > 0.01:
                    errors.append(f"line_items_sum_mismatch: "
                                  f"total={self.total:.2f} li_sum={li_sum:.2f} "
                                  f"drift={drift*100:.1f}%")
        # Unknown cogs_bucket → can't roll up properly. Warn.
        for i, li in enumerate(self.line_items):
            if li.cogs_bucket and li.cogs_bucket not in VALID_COGS_BUCKETS:
                errors.append(f"unknown_cogs_bucket: line[{i}] "
                              f"bucket={li.cogs_bucket!r}")
        return errors
```

- [ ] **Step 6: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/schemas/test_marginedge_invoice.py -v
```
Expected: All 5 PASS.

- [ ] **Step 7: Commit**

```bash
git add toast-etl/schemas/marginedge_invoice.py toast-etl/tests/schemas/test_marginedge_invoice.py
git commit -m "feat(schemas): MarginEdgeInvoice + LineItem with sum + bucket sanity rules"
```

---

### Task 6: TripleseatEvent Pydantic schema

**Files:**
- Create: `toast-etl/schemas/tripleseat_event.py`
- Create: `toast-etl/tests/schemas/test_tripleseat_event.py`

- [ ] **Step 1: Reference + inspect**

Read `toast-etl/tripleseat_sync.py` for transform output shape. Then:

```bash
python3 -c "
import json
d = json.load(open('data/vessel.json'))
ev = d.get('events', {}).get('events', [])
if ev: print('keys:', sorted(ev[0].keys())); print('sample:', ev[0])
else: print('no events')
"
```

- [ ] **Step 2: Write the failing test**

Write `toast-etl/tests/schemas/test_tripleseat_event.py`:

```python
"""Tests for TripleseatEvent schema."""
import pytest
from schemas.tripleseat_event import TripleseatEvent


def test_minimal_valid_event():
    raw = {
        "id": "ev-1", "name": "Smith Wedding",
        "function_date": "2026-06-15",
        "event_total": 25000.00, "fb_total": 18000.00,
        "guests": 120, "status": "Definite",
        "account_name": "Smith Family",
    }
    e = TripleseatEvent.model_validate(raw)
    assert e.event_total == 25000.00
    assert e.validate_business_rules() == []


def test_fb_exceeds_total_fails_business_rule():
    raw = {
        "id": "ev-2", "name": "X", "function_date": "2026-06-15",
        "event_total": 1000, "fb_total": 1500, "guests": 50,
        "status": "Definite", "account_name": "x",
    }
    e = TripleseatEvent.model_validate(raw)
    errs = e.validate_business_rules()
    assert any("fb_exceeds_event_total" in s for s in errs)


def test_negative_total_fails():
    with pytest.raises(Exception):
        TripleseatEvent.model_validate({
            "id": "n", "name": "x", "function_date": "2026-06-15",
            "event_total": -100, "fb_total": 0, "guests": 1,
            "status": "Definite", "account_name": "x",
        })


def test_unknown_status_warned():
    raw = {
        "id": "u", "name": "x", "function_date": "2026-06-15",
        "event_total": 100, "fb_total": 50, "guests": 1,
        "status": "Maybe-Pending-Confirmed",  # not a known status
        "account_name": "x",
    }
    e = TripleseatEvent.model_validate(raw)
    assert any("unknown_status" in s for s in e.validate_business_rules())
```

- [ ] **Step 3: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/schemas/test_tripleseat_event.py -v
```

- [ ] **Step 4: Implement the schema**

Write `toast-etl/schemas/tripleseat_event.py`:

```python
"""Tripleseat event row schema."""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


KNOWN_STATUSES = {"Definite", "Tentative", "Prospect", "Cancelled", "Closed"}


class TripleseatEvent(SourceRow):
    _source_name = "tripleseat_event"

    id: str
    name: str
    function_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    event_total: float = Field(ge=0)
    fb_total: float = Field(default=0, ge=0)
    guests: int = Field(default=0, ge=0)
    status: str
    account_name: Optional[str] = None
    booking_contact: Optional[str] = None

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        if self.fb_total > self.event_total + 0.01:
            errors.append(f"fb_exceeds_event_total: fb={self.fb_total} "
                          f"total={self.event_total}")
        if self.status not in KNOWN_STATUSES:
            errors.append(f"unknown_status: {self.status!r} "
                          f"(known: {sorted(KNOWN_STATUSES)})")
        return errors
```

- [ ] **Step 5: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/schemas/test_tripleseat_event.py -v
```

- [ ] **Step 6: Commit**

```bash
git add toast-etl/schemas/tripleseat_event.py toast-etl/tests/schemas/test_tripleseat_event.py
git commit -m "feat(schemas): TripleseatEvent with f&b-vs-total + status sanity rules"
```

---

### Task 7: Helixo2Forecast Pydantic schema

**Files:**
- Create: `toast-etl/schemas/helixo2_forecast.py`
- Create: `toast-etl/tests/schemas/test_helixo2_forecast.py`

- [ ] **Step 1: Reference**

Read `toast-etl/forecast_engine.py:286-293` (the row construction). Fields: `date, net_sales, guests, orders, ai_confidence`.

- [ ] **Step 2: Write the failing test**

Write `toast-etl/tests/schemas/test_helixo2_forecast.py`:

```python
"""Tests for Helixo2Forecast schema."""
import pytest
from schemas.helixo2_forecast import Helixo2Forecast


def test_minimal_valid_row():
    raw = {"date": "2026-05-04", "net_sales": 7500.00,
           "guests": 120, "orders": None, "ai_confidence": 0.93}
    f = Helixo2Forecast.model_validate(raw)
    assert f.net_sales == 7500.00
    assert f.validate_business_rules() == []


def test_confidence_out_of_range_fails():
    with pytest.raises(Exception):
        Helixo2Forecast.model_validate({
            "date": "2026-05-04", "net_sales": 100,
            "guests": 5, "orders": None, "ai_confidence": 1.5,
        })


def test_zero_revenue_with_high_confidence_warned():
    """Forecast = $0 with ai_confidence > 0.7 is suspicious — likely an
    unmapped outlet falling through to a default value."""
    raw = {"date": "2026-05-04", "net_sales": 0,
           "guests": 0, "orders": None, "ai_confidence": 0.95}
    f = Helixo2Forecast.model_validate(raw)
    errs = f.validate_business_rules()
    assert any("zero_revenue_high_confidence" in s for s in errs)
```

- [ ] **Step 3: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/schemas/test_helixo2_forecast.py -v
```

- [ ] **Step 4: Implement the schema**

Write `toast-etl/schemas/helixo2_forecast.py`:

```python
"""helixo-2 daily_forecasts row schema."""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


class Helixo2Forecast(SourceRow):
    _source_name = "helixo2_forecast"

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    net_sales: float = Field(ge=0)
    guests: Optional[int] = Field(default=None, ge=0)
    orders: Optional[int] = Field(default=None, ge=0)
    ai_confidence: Optional[float] = Field(default=None, ge=0, le=1)

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        # An outlet with $0 net_sales forecast AND ai_confidence > 0.7 is
        # the failure pattern observed for unmapped outlets — helixo-2
        # confidently predicts 0 because it has no signal. Flag it.
        if (self.net_sales == 0 and self.ai_confidence is not None
                and self.ai_confidence > 0.7):
            errors.append(f"zero_revenue_high_confidence: "
                          f"sales=0 conf={self.ai_confidence}")
        return errors
```

- [ ] **Step 5: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/schemas/test_helixo2_forecast.py -v
```

- [ ] **Step 6: Commit**

```bash
git add toast-etl/schemas/helixo2_forecast.py toast-etl/tests/schemas/test_helixo2_forecast.py
git commit -m "feat(schemas): Helixo2Forecast with zero-revenue/high-confidence sanity rule"
```

---

### Task 8: SageBudgetLine Pydantic schema

**Files:**
- Create: `toast-etl/schemas/sage_budget.py`
- Create: `toast-etl/tests/schemas/test_sage_budget.py`

- [ ] **Step 1: Reference**

Read `toast-etl/budget_sync.py` for the transform output shape. Fields typically: `date, gl_account, dimension, amount` (or per-period budget rows).

- [ ] **Step 2: Inspect current data**

```bash
python3 -c "
import json
d = json.load(open('data/lsbr.json'))
b = d.get('budget', {})
print('budget keys:', sorted(b.keys()))
daily = b.get('daily', [])
if daily: print('daily sample:', daily[0])
"
```

- [ ] **Step 3: Write the failing test**

Write `toast-etl/tests/schemas/test_sage_budget.py`:

```python
"""Tests for SageBudgetLine schema."""
import pytest
from schemas.sage_budget import SageBudgetLine


def test_minimal_valid_line():
    raw = {"date": "2026-05-04", "net_sales": 8500.00,
           "labor_cost": 2400.00, "cogs": 2300.00}
    b = SageBudgetLine.model_validate(raw)
    assert b.net_sales == 8500.00
    assert b.validate_business_rules() == []


def test_negative_budget_fails():
    with pytest.raises(Exception):
        SageBudgetLine.model_validate({
            "date": "2026-05-04", "net_sales": -100,
            "labor_cost": 0, "cogs": 0,
        })


def test_implausible_labor_pct_warned():
    """Labor budgeted at >70% of net_sales is implausible for FSR."""
    raw = {"date": "2026-05-04", "net_sales": 100,
           "labor_cost": 80, "cogs": 0}
    b = SageBudgetLine.model_validate(raw)
    errs = b.validate_business_rules()
    assert any("labor_pct_implausible" in s for s in errs)
```

- [ ] **Step 4: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/schemas/test_sage_budget.py -v
```

- [ ] **Step 5: Implement the schema**

Write `toast-etl/schemas/sage_budget.py`:

```python
"""Sage Intacct budget row schema."""
from __future__ import annotations

from typing import Optional
from pydantic import Field

from ._base import SourceRow


class SageBudgetLine(SourceRow):
    _source_name = "sage_budget"

    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    net_sales: float = Field(ge=0)
    labor_cost: float = Field(default=0, ge=0)
    cogs: float = Field(default=0, ge=0)
    other_opex: Optional[float] = Field(default=None, ge=0)

    def validate_business_rules(self) -> list[str]:
        errors: list[str] = []
        if self.net_sales > 0:
            labor_pct = self.labor_cost / self.net_sales
            if labor_pct > 0.70:
                errors.append(f"labor_pct_implausible: "
                              f"labor=${self.labor_cost} sales=${self.net_sales} "
                              f"pct={labor_pct*100:.1f}%")
            cogs_pct = self.cogs / self.net_sales
            if cogs_pct > 0.55:
                errors.append(f"cogs_pct_implausible: "
                              f"cogs=${self.cogs} sales=${self.net_sales} "
                              f"pct={cogs_pct*100:.1f}%")
        return errors
```

- [ ] **Step 6: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/schemas/test_sage_budget.py -v
```

- [ ] **Step 7: Commit**

```bash
git add toast-etl/schemas/sage_budget.py toast-etl/tests/schemas/test_sage_budget.py
git commit -m "feat(schemas): SageBudgetLine with labor% / cogs% sanity rules"
```

---

### Task 9: Validation runner module

**Files:**
- Create: `toast-etl/validation/__init__.py`
- Create: `toast-etl/validation/runner.py`
- Create: `toast-etl/tests/validation/__init__.py`
- Create: `toast-etl/tests/validation/test_runner.py`

- [ ] **Step 1: Create package files**

```bash
mkdir -p toast-etl/validation toast-etl/tests/validation
touch toast-etl/validation/__init__.py toast-etl/tests/validation/__init__.py
```

- [ ] **Step 2: Write the failing test**

Write `toast-etl/tests/validation/test_runner.py`:

```python
"""Tests for the validation runner."""
import json
from pathlib import Path
import tempfile
from validation.runner import run_validation


class FakeRow:
    """Stand-in Pydantic model for the runner test (avoids coupling
    the test to any specific source)."""
    def __init__(self, data):
        self.data = data
        if data.get("bad"):
            raise ValueError(f"row invalid: {data}")
    def model_dump(self):
        return self.data
    def validate_business_rules(self):
        if self.data.get("warn"):
            return [f"warn_flag: {self.data['warn']}"]
        return []


def test_runner_writes_validation_file(tmp_path):
    rows = [{"id": 1}, {"id": 2}, {"id": 3, "warn": "low"},
            {"id": 4, "bad": True}]
    out = run_validation(
        rows=rows, model_cls=FakeRow, source="test_src",
        outlets_touched=["lsbr"], data_dir=tmp_path,
    )
    assert out["rows_in"] == 4
    assert out["rows_valid"] == 3   # bad row excluded
    assert out["rows_invalid"] == 1
    assert out["rows_warned"] == 1
    # Verify the file was written
    files = list((tmp_path / "_validation").glob("test_src_*.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text())
    assert written["source"] == "test_src"


def test_runner_returns_validated_rows(tmp_path):
    rows = [{"id": 1}, {"id": 2, "bad": True}]
    out = run_validation(
        rows=rows, model_cls=FakeRow, source="t", outlets_touched=[],
        data_dir=tmp_path,
    )
    assert len(out["valid_rows"]) == 1
    assert out["valid_rows"][0]["id"] == 1
```

- [ ] **Step 3: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/validation/test_runner.py -v
```
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement the runner**

Write `toast-etl/validation/runner.py`:

```python
"""Validation runner — pipes raw rows through Pydantic models, writes
a per-run summary file the agent worker consumes.

Returns a dict with both the summary (for caller logging) and the
valid_rows list (the caller uses these going forward; invalid rows
are dropped from the data payload but logged in _validation_errors).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Type


def run_validation(
    rows: list[dict],
    model_cls: Type,
    source: str,
    outlets_touched: list[str],
    data_dir: Path,
    schema_version: str = "v1",
) -> dict[str, Any]:
    """Validate raw rows against model_cls.

    Args:
        rows: raw dict rows from the sync
        model_cls: Pydantic model class with model_validate()
                   and validate_business_rules()
        source: short identifier (e.g. "toast_order", "resy_survey")
        outlets_touched: list of outlet ids this run wrote to
        data_dir: project data/ dir
        schema_version: bump when the model is materially changed

    Returns:
        dict with summary stats AND valid_rows for the caller to use
    """
    valid_rows: list[dict] = []
    errors: list[dict] = []
    warnings: list[dict] = []

    for i, row in enumerate(rows):
        try:
            m = model_cls.model_validate(row)
        except Exception as e:
            errors.append({
                "row_offset": i,
                "code": "model_validation_error",
                "message": str(e)[:500],
                # Caller is responsible for PII redaction before this point
                # if needed; runner stores small key list as a debug aid.
                "row_keys": sorted(row.keys()) if isinstance(row, dict) else [],
            })
            continue
        rule_errs = m.validate_business_rules()
        if rule_errs:
            warnings.append({
                "row_offset": i,
                "rules": rule_errs,
                "row_keys": sorted(row.keys()) if isinstance(row, dict) else [],
            })
        valid_rows.append(m.model_dump())

    ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ts = ran_at.replace(":", "").replace("-", "").replace("+0000", "Z")

    summary = {
        "source": source,
        "schema_version": schema_version,
        "ran_at": ran_at,
        "rows_in": len(rows),
        "rows_valid": len(valid_rows),
        "rows_invalid": len(errors),
        "rows_warned": len(warnings),
        "outlets_touched": outlets_touched,
        "errors_sample": errors[:10],   # first 10 only — not the full list
        "warnings_sample": warnings[:10],
    }

    val_dir = data_dir / "_validation"
    val_dir.mkdir(parents=True, exist_ok=True)
    out_path = val_dir / f"{source}_{ts}.json"
    out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    if errors:
        err_dir = data_dir / "_validation_errors"
        err_dir.mkdir(parents=True, exist_ok=True)
        err_path = err_dir / f"{source}_{ts}.json"
        err_path.write_text(json.dumps({
            "source": source, "ran_at": ran_at,
            "all_errors": errors,   # full list, not just sample
        }, indent=2, default=str), encoding="utf-8")

    return {
        **summary,
        "valid_rows": valid_rows,
        "summary_path": str(out_path),
    }
```

- [ ] **Step 5: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/validation/test_runner.py -v
```
Expected: 2 PASS.

- [ ] **Step 6: Commit**

```bash
git add toast-etl/validation/__init__.py toast-etl/validation/runner.py toast-etl/tests/validation/__init__.py toast-etl/tests/validation/test_runner.py
git commit -m "feat(validation): runner module — model rows, write _validation/ summary"
```

---

### Task 10: Wire validation into toast_sync.py

**Files:**
- Modify: `toast-etl/toast_sync.py`

- [ ] **Step 1: Locate the integration point**

Find `transform_orders` function around line 685 and `transform_time_entries` around line 439 in `toast_sync.py`. Validation runs on the RAW rows BEFORE these transform functions (so invalid rows get dropped from the transform).

- [ ] **Step 2: Add the import block**

In `toast-etl/toast_sync.py`, find the existing import section (top of file, ~line 78) and add:

```python
from pathlib import Path
from schemas.toast_order import ToastOrder
from schemas.toast_time_entry import ToastTimeEntry
from validation.runner import run_validation
```

- [ ] **Step 3: Wrap fetch_orders consumer**

Find the call site that consumes raw orders (search for `transform_orders(raw_orders` in `toast_sync.py`). Before the transform call, insert:

```python
# Validation gate (Phase A.1). Drops rows that fail Pydantic and writes
# _validation/toast_order_<ts>.json for the agent worker to inspect.
_v = run_validation(
    rows=raw_orders, model_cls=ToastOrder, source="toast_order",
    outlets_touched=[outlet_id],
    data_dir=Path(__file__).resolve().parent.parent / "data",
)
print(f"  toast_order validation: {_v['rows_valid']}/{_v['rows_in']} ok, "
      f"{_v['rows_invalid']} dropped, {_v['rows_warned']} warned")
raw_orders = [m for m in _v['valid_rows']]   # use validated payload going forward
```

- [ ] **Step 4: Wrap fetch_time_entries consumer**

Find the call site that calls `transform_time_entries(entries, jobs_lookup)`. Before that call, insert:

```python
_v = run_validation(
    rows=entries, model_cls=ToastTimeEntry, source="toast_time_entry",
    outlets_touched=[outlet_id],
    data_dir=Path(__file__).resolve().parent.parent / "data",
)
print(f"  toast_time_entry validation: {_v['rows_valid']}/{_v['rows_in']} ok, "
      f"{_v['rows_invalid']} dropped, {_v['rows_warned']} warned")
entries = _v['valid_rows']
```

- [ ] **Step 5: Run a dry-run sync against current data**

```bash
# Only run if you have TOAST credentials available locally; otherwise
# this gets verified during the next nightly run. Use a small days=1
# window to limit API spend.
cd toast-etl && python3 toast_sync.py --outlet=lsbr --days=1 2>&1 | grep -E "validation|✓|✗" | head -10
```
Expected: lines like `toast_order validation: N/N ok, 0 dropped, K warned`. If `dropped > 0`, investigate the model — current data MUST validate.

- [ ] **Step 6: Verify validation file landed**

```bash
ls -la "$(git rev-parse --show-toplevel)/data/_validation/" 2>&1 | head
```
Expected: `toast_order_<ts>.json` and `toast_time_entry_<ts>.json` present.

- [ ] **Step 7: Commit**

```bash
git add toast-etl/toast_sync.py
git commit -m "feat(toast): wire Pydantic validation into ordersBulk + timeEntries pipeline"
```

---

### Task 11: Wire validation into resy_os_scraper.py

**Files:**
- Modify: `toast-etl/resy_os_scraper.py`

- [ ] **Step 1: Add imports + wire in**

In `toast-etl/resy_os_scraper.py`, after the existing imports (around line 75), add:

```python
from schemas.resy_survey import ResySurvey
from validation.runner import run_validation
```

- [ ] **Step 2: Insert validation after the transform**

Find the function `transform_to_guest_block` (around line 293). Inside it, after the loop that builds `surveys` (the list of normalized survey dicts) and BEFORE the function returns, insert:

```python
# Validation gate. Bad survey rows are dropped from the payload but
# captured in _validation_errors/ for the schema-drift agent.
from pathlib import Path as _P
_v = run_validation(
    rows=surveys, model_cls=ResySurvey, source="resy_survey",
    outlets_touched=[],   # caller knows the outlet; passed in via captured.outlet if available
    data_dir=_P(__file__).resolve().parent.parent / "data",
)
sys.stderr.write(f"  resy_survey validation: {_v['rows_valid']}/{_v['rows_in']} ok, "
                 f"{_v['rows_invalid']} dropped, {_v['rows_warned']} warned (drift-flagged)\n")
surveys = _v['valid_rows']
```

- [ ] **Step 3: Smoke test**

```bash
cd toast-etl && python3 -c "
import sys, json
sys.path.insert(0, '.')
from schemas.resy_survey import ResySurvey
from validation.runner import run_validation
from pathlib import Path
d = json.load(open('../data/lsbr.json'))
surveys = d['guest']['surveys']
out = run_validation(
    rows=surveys, model_cls=ResySurvey, source='resy_survey',
    outlets_touched=['lsbr'],
    data_dir=Path('/tmp/resy_test_data')
)
print(f\"valid={out['rows_valid']} invalid={out['rows_invalid']} warned={out['rows_warned']}\")
"
ls /tmp/resy_test_data/_validation/
```
Expected: `valid=N invalid=0 warned=M` (warned = post-drift surveys). Validation file present.

- [ ] **Step 4: Commit**

```bash
git add toast-etl/resy_os_scraper.py
git commit -m "feat(resy): wire ResySurvey validation into transform; drift surfaces as warnings"
```

---

### Task 12: Wire validation into marginedge_sync.py

**Files:**
- Modify: `toast-etl/marginedge_sync.py`

- [ ] **Step 1: Add imports**

After existing imports, add:

```python
from pathlib import Path as _Path
from schemas.marginedge_invoice import MarginEdgeInvoice
from validation.runner import run_validation
```

- [ ] **Step 2: Insert validation right before payload write**

Find the section where `invoices` list is finalized per outlet (after the line-item fetch loop). Before writing the outlet payload, insert:

```python
_v = run_validation(
    rows=invoices, model_cls=MarginEdgeInvoice, source="marginedge_invoice",
    outlets_touched=[outlet_id],
    data_dir=_Path(__file__).resolve().parent.parent / "data",
)
print(f"  marginedge_invoice validation [{outlet_id}]: "
      f"{_v['rows_valid']}/{_v['rows_in']} ok, "
      f"{_v['rows_invalid']} dropped, {_v['rows_warned']} warned")
invoices = _v['valid_rows']
```

- [ ] **Step 3: Verify build still parses**

```bash
cd toast-etl && python3 -m py_compile marginedge_sync.py && echo "syntax OK"
```

- [ ] **Step 4: Commit**

```bash
git add toast-etl/marginedge_sync.py
git commit -m "feat(marginedge): wire MarginEdgeInvoice validation into per-outlet pipeline"
```

---

### Task 13: Wire validation into tripleseat_sync.py

**Files:**
- Modify: `toast-etl/tripleseat_sync.py`

- [ ] **Step 1: Add imports + wire in**

Identical pattern to Tasks 10-12. After existing imports:

```python
from pathlib import Path as _Path
from schemas.tripleseat_event import TripleseatEvent
from validation.runner import run_validation
```

Find where the `events` list is built per outlet, then before payload write:

```python
_v = run_validation(
    rows=events, model_cls=TripleseatEvent, source="tripleseat_event",
    outlets_touched=[outlet_id],
    data_dir=_Path(__file__).resolve().parent.parent / "data",
)
print(f"  tripleseat_event validation [{outlet_id}]: "
      f"{_v['rows_valid']}/{_v['rows_in']} ok, "
      f"{_v['rows_invalid']} dropped, {_v['rows_warned']} warned")
events = _v['valid_rows']
```

- [ ] **Step 2: Syntax check**

```bash
cd toast-etl && python3 -m py_compile tripleseat_sync.py && echo "syntax OK"
```

- [ ] **Step 3: Commit**

```bash
git add toast-etl/tripleseat_sync.py
git commit -m "feat(tripleseat): wire TripleseatEvent validation into per-outlet pipeline"
```

---

### Task 14: Wire validation into forecast_engine.py

**Files:**
- Modify: `toast-etl/forecast_engine.py`

- [ ] **Step 1: Wire in**

After existing imports:

```python
from pathlib import Path as _Path
from schemas.helixo2_forecast import Helixo2Forecast
from validation.runner import run_validation
```

Find `cmd_sync` function. Inside the loop where `daily` is built per outlet, after the sort and before `payload["forecast"] = ...`, insert:

```python
_v = run_validation(
    rows=daily, model_cls=Helixo2Forecast, source="helixo2_forecast",
    outlets_touched=[outlet],
    data_dir=_Path(__file__).resolve().parent.parent / "data",
)
print(f"  helixo2_forecast validation [{outlet}]: "
      f"{_v['rows_valid']}/{_v['rows_in']} ok, "
      f"{_v['rows_invalid']} dropped, {_v['rows_warned']} warned")
daily = _v['valid_rows']
```

- [ ] **Step 2: Syntax check**

```bash
cd toast-etl && python3 -m py_compile forecast_engine.py && echo "syntax OK"
```

- [ ] **Step 3: Commit**

```bash
git add toast-etl/forecast_engine.py
git commit -m "feat(forecast): wire Helixo2Forecast validation into per-outlet pipeline"
```

---

### Task 15: Wire validation into budget_sync.py

**Files:**
- Modify: `toast-etl/budget_sync.py`

- [ ] **Step 1: Wire in**

After existing imports:

```python
from pathlib import Path as _Path
from schemas.sage_budget import SageBudgetLine
from validation.runner import run_validation
```

Find where the `daily` budget rows are built per outlet. Before payload write:

```python
_v = run_validation(
    rows=daily, model_cls=SageBudgetLine, source="sage_budget",
    outlets_touched=[outlet_id],
    data_dir=_Path(__file__).resolve().parent.parent / "data",
)
print(f"  sage_budget validation [{outlet_id}]: "
      f"{_v['rows_valid']}/{_v['rows_in']} ok, "
      f"{_v['rows_invalid']} dropped, {_v['rows_warned']} warned")
daily = _v['valid_rows']
```

- [ ] **Step 2: Syntax check**

```bash
cd toast-etl && python3 -m py_compile budget_sync.py && echo "syntax OK"
```

- [ ] **Step 3: Commit**

```bash
git add toast-etl/budget_sync.py
git commit -m "feat(budget): wire SageBudgetLine validation into per-outlet pipeline"
```

---

### Task 16: metric_classes.yml + loader

**Files:**
- Create: `config/metric_classes.yml`
- Create: `toast-etl/validation/metric_class.py`
- Create: `toast-etl/tests/validation/test_metric_class.py`

- [ ] **Step 1: Create the config**

Write `config/metric_classes.yml`:

```yaml
# Metric → failure-class map (Phase A.1).
#
# Drives the dashboard's "is this metric trustworthy?" decision and the
# alert dispatcher's "should this trigger Slack?" routing. Aligns to the
# spec section "Failure semantics (locked decision)".
#
# Three classes:
#   hard_fail  — wrong number worse than no number; card hides on fail
#   annotate   — show last-known with stale/confidence stamp
#   auto_heal  — transient infra; agent retries silently

financial:
  class: hard_fail
  metrics:
    - net_sales
    - cogs_dollars
    - cogs_pct
    - labor_dollars
    - labor_pct
    - prime_cost_pct
    - budget_variance_pct
    - revpash
    - avg_guest_spend
    - comps_discounts_pct

soft_signal:
  class: annotate
  metrics:
    - nps
    - promoters_count
    - detractors_count
    - avg_food_score
    - avg_service_score
    - avg_atmos_score
    - dwell_time_min
    - reviews_count
    - ai_confidence
    - forecast_accuracy_wape

transient_infra:
  class: auto_heal
  patterns:
    - http_429
    - http_5xx
    - playwright_timeout
    - resy_session_expired
    - git_push_rejected_nonffwd
    - workflow_cancelled_concurrency
```

- [ ] **Step 2: Write the failing test**

Write `toast-etl/tests/validation/test_metric_class.py`:

```python
"""Tests for metric_class loader."""
from pathlib import Path
from validation.metric_class import classify_metric, classify_failure


def test_financial_metric_hard_fails():
    assert classify_metric("net_sales") == "hard_fail"
    assert classify_metric("cogs_pct") == "hard_fail"


def test_soft_signal_annotates():
    assert classify_metric("nps") == "annotate"
    assert classify_metric("dwell_time_min") == "annotate"


def test_unknown_metric_defaults_to_annotate():
    """Unknown metric → safe default = annotate (don't hard-fail surprises)."""
    assert classify_metric("totally_made_up_metric") == "annotate"


def test_failure_pattern_classifies_to_auto_heal():
    assert classify_failure("http_429") == "auto_heal"
    assert classify_failure("workflow_cancelled_concurrency") == "auto_heal"


def test_unknown_failure_defaults_to_alert():
    """Unknown failure pattern → don't auto-heal silently; alert."""
    assert classify_failure("some_new_error_we_havent_seen") == "alert"
```

- [ ] **Step 3: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/validation/test_metric_class.py -v
```
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 4: Implement the loader**

Write `toast-etl/validation/metric_class.py`:

```python
"""Loader for config/metric_classes.yml.

Returns:
  classify_metric(name)  -> "hard_fail" | "annotate" (default annotate)
  classify_failure(name) -> "auto_heal" | "alert"    (default alert)
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

try:
    import yaml
except ImportError:
    raise SystemExit("missing dependency: pip install pyyaml")


CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "metric_classes.yml"


@lru_cache(maxsize=1)
def _load() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}


def classify_metric(metric_name: str) -> str:
    """Returns 'hard_fail' or 'annotate'. Default: 'annotate'."""
    cfg = _load()
    for group_name, group in cfg.items():
        if not isinstance(group, dict):
            continue
        if metric_name in (group.get("metrics") or []):
            return group.get("class", "annotate")
    return "annotate"


def classify_failure(pattern: str) -> str:
    """Returns 'auto_heal' or 'alert'. Default: 'alert'."""
    cfg = _load()
    for group_name, group in cfg.items():
        if not isinstance(group, dict):
            continue
        if pattern in (group.get("patterns") or []):
            cls = group.get("class", "")
            if cls == "auto_heal":
                return "auto_heal"
    return "alert"
```

- [ ] **Step 5: Add pyyaml to requirements**

Append to `toast-etl/requirements.txt`:
```
pyyaml>=6.0
```

Then:
```bash
pip install pyyaml
```

- [ ] **Step 6: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/validation/test_metric_class.py -v
```
Expected: 5 PASS.

- [ ] **Step 7: Commit**

```bash
git add config/metric_classes.yml toast-etl/validation/metric_class.py toast-etl/tests/validation/test_metric_class.py toast-etl/requirements.txt
git commit -m "feat(validation): metric_classes.yml + loader (hard_fail / annotate / auto_heal)"
```

---

### Task 17: Dashboard validation panel UI

**Files:**
- Modify: `Method_Co_FB_Performance_Dashboard.html`

- [ ] **Step 1: Add CSS for the panel**

In `Method_Co_FB_Performance_Dashboard.html`, find the `<style>` block (~line 230) and add at the end:

```css
/* === Validation panel === */
.val-panel {
  position: fixed; top: 8px; right: 8px;
  background: #fff; border: 1px solid var(--grey);
  padding: 6px 10px; font-family: var(--font-stack);
  font-size: 11px; color: var(--royal); z-index: 100;
  cursor: pointer; max-width: 320px; border-radius: 2px;
}
.val-panel.ok    { border-left: 4px solid #1a8754; }
.val-panel.warn  { border-left: 4px solid #c79a00; }
.val-panel.err   { border-left: 4px solid #b03030; }
.val-panel-summary { font-weight: 600; letter-spacing: 0.04em; }
.val-panel-detail  { display: none; margin-top: 6px; font-size: 10.5px; line-height: 1.3; }
.val-panel.expanded .val-panel-detail { display: block; }
.val-panel-row { padding: 2px 0; border-top: 1px dashed var(--grey); }
.val-panel-row:first-child { border-top: 0; }
.val-panel-status { display: inline-block; width: 14px; }
@media print { .val-panel { display: none !important; } }
```

- [ ] **Step 2: Add panel HTML element**

Find the `<aside class="nav">` element (~line 323) and right BEFORE it, add:

```html
<div class="val-panel" id="valPanel" onclick="this.classList.toggle('expanded')">
  <div class="val-panel-summary" id="valPanelSummary">Checking…</div>
  <div class="val-panel-detail" id="valPanelDetail"></div>
</div>
```

- [ ] **Step 3: Add JS to populate the panel**

Find an appropriate spot in the `<script>` section (after `renderOutlet()` definition; around line 1000). Add:

```javascript
// === Validation panel ===
// Reads data/_validation/<source>_<ts>.json files (the latest per source)
// and data/_banner/<outlet>.json (when present, agent worker has annotated
// or hard-failed something). Updates the top-right validation panel.
async function refreshValidationPanel() {
  const panel = document.getElementById('valPanel');
  const summary = document.getElementById('valPanelSummary');
  const detail = document.getElementById('valPanelDetail');
  if (!panel || !summary) return;

  // Best-effort fetch of the per-outlet banner state. Agent worker writes
  // this; if missing, we fall back to "all sources current" optimistic.
  let bannerState = null;
  try {
    const r = await fetch(`data/_banner/${STATE.outlet}.json`, {cache: 'no-cache'});
    if (r.ok) bannerState = await r.json();
  } catch (e) { /* tolerate */ }

  // Fetch the index of validation summary files. We're statically hosted
  // (GH Pages) so directory listing isn't available — instead each outlet
  // stamps its latest validation timestamp into data/<outlet>.json under
  // a top-level `_validation_index` key (added by the syncs).
  const sources = ['toast_order', 'toast_time_entry', 'resy_survey',
                   'marginedge_invoice', 'tripleseat_event',
                   'helixo2_forecast', 'sage_budget'];
  const o = DATA.outlets[STATE.outlet];
  const idx = (o && o._validation_index) || {};

  const now = Date.now();
  const lines = [];
  let worstClass = 'ok';
  for (const src of sources) {
    const v = idx[src];
    if (!v) {
      lines.push(`<div class="val-panel-row">⚪ ${src}: not synced</div>`);
      if (worstClass === 'ok') worstClass = 'warn';
      continue;
    }
    const ageHrs = (now - new Date(v.ran_at).getTime()) / 3600_000;
    let status, cls;
    if (v.rows_invalid > 0)              { status = '🔴'; cls = 'err'; }
    else if (ageHrs > 26)                { status = '🟡'; cls = 'warn'; }
    else if (v.rows_warned > 0)          { status = '🟡'; cls = 'warn'; }
    else                                 { status = '✓'; cls = 'ok'; }
    if (cls === 'err' || (cls === 'warn' && worstClass !== 'err')) worstClass = cls;
    lines.push(`<div class="val-panel-row">${status} ${src}: `
      + `${v.rows_valid}/${v.rows_in} ok, ${ageHrs.toFixed(1)}h ago</div>`);
  }

  panel.classList.remove('ok', 'warn', 'err');
  panel.classList.add(worstClass);
  const summaryText = (worstClass === 'ok') ? '✓ All sources current'
                    : (worstClass === 'warn') ? '⚠ Some sources stale or warned'
                    : '🛑 Validation failures — see card details';
  summary.textContent = summaryText;
  detail.innerHTML = lines.join('') +
    (bannerState ? `<div class="val-panel-row" style="font-style:italic;color:var(--royal);">${bannerState.message || ''}</div>` : '');
}

// Refresh on outlet/section change + every 5min while the page is open
const _origRender = renderOutlet;
renderOutlet = function() { _origRender.apply(this, arguments); refreshValidationPanel(); };
setInterval(refreshValidationPanel, 5 * 60 * 1000);
```

- [ ] **Step 4: Update validation runner to write per-outlet index**

This requires a small change to `toast-etl/validation/runner.py` so each sync ALSO writes `_validation_index` into the outlet JSON. Modify `run_validation` to optionally update outlet payloads:

Add a parameter and post-write step in `toast-etl/validation/runner.py`:

```python
# Add to function signature:
def run_validation(
    rows: list[dict],
    model_cls: Type,
    source: str,
    outlets_touched: list[str],
    data_dir: Path,
    schema_version: str = "v1",
    update_outlet_index: bool = True,   # NEW
) -> dict[str, Any]:
```

At the end of the function (right before `return`), insert:

```python
    # Update the outlet payloads' _validation_index so the dashboard can
    # surface per-source status without needing a directory listing.
    if update_outlet_index:
        for oid in outlets_touched:
            outlet_path = data_dir / f"{oid}.json"
            if not outlet_path.exists():
                continue
            try:
                payload = json.loads(outlet_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            payload.setdefault("_validation_index", {})[source] = {
                "ran_at": ran_at,
                "rows_in": len(rows),
                "rows_valid": len(valid_rows),
                "rows_invalid": len(errors),
                "rows_warned": len(warnings),
            }
            tmp = outlet_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str),
                           encoding="utf-8")
            tmp.replace(outlet_path)
```

- [ ] **Step 5: Run a smoke test rendering the dashboard**

Open `Method_Co_FB_Performance_Dashboard.html` in a browser; the panel should appear top-right with "Checking…" then transition. (If you have no `_validation_index` yet, expect lots of "not synced" rows — that's correct; resolves after first post-Task-10 sync run.)

- [ ] **Step 6: Commit**

```bash
git add Method_Co_FB_Performance_Dashboard.html toast-etl/validation/runner.py
git commit -m "feat(dashboard): validation panel UI + per-outlet _validation_index in runner"
```

---

## Sprint 2 — Agent worker infrastructure (Tasks 18-20)

### Task 18: Supabase Edge Function scaffold + deploy

**Files:**
- Create: `supabase/config.toml` (if not present)
- Create: `supabase/functions/agent-worker/index.ts`
- Create: `supabase/functions/agent-worker/deno.json`
- Create: `supabase/functions/agent-worker/lib/types.ts`

- [ ] **Step 1: Initialize Supabase project (one-time)**

```bash
cd "$(git rev-parse --show-toplevel)"
# If supabase/ doesn't exist yet:
[ -d supabase ] || npx supabase init
# Link to the existing Method Co project (Ross's project ref):
npx supabase link --project-ref <METHOD_PROJECT_REF>
```

(Project ref is in the Supabase dashboard URL; the user has Supabase per the spec lock.)

- [ ] **Step 2: Create the function scaffold**

```bash
cd "$(git rev-parse --show-toplevel)"
mkdir -p supabase/functions/agent-worker/lib
mkdir -p supabase/functions/agent-worker/agents
```

Write `supabase/functions/agent-worker/deno.json`:

```json
{
  "tasks": {
    "dev": "deno run --allow-all --watch index.ts",
    "test": "deno test --allow-all"
  },
  "imports": {
    "@supabase/supabase-js": "https://esm.sh/@supabase/supabase-js@2",
    "@anthropic-ai/sdk": "npm:@anthropic-ai/sdk@^0.30",
    "@slack/web-api": "npm:@slack/web-api@^7"
  }
}
```

- [ ] **Step 3: Write a hello-world index.ts**

Write `supabase/functions/agent-worker/index.ts`:

```typescript
// agent-worker — Edge Function entry point.
//
// Triggered by pg_cron every 5 minutes. Each invocation:
//   1. Reads the latest data/_validation/*.json files (synced to a
//      Supabase Storage bucket by the GH Actions workflows)
//   2. Routes through the agent loops (drift, anomaly, retry, alert)
//   3. Writes back data/_banner/<outlet>.json + appends to
//      data/_audit/agent_decisions.jsonl
//
// Phase A.1 — hello-world plus the storage helper. Real agents added
// in Tasks 22-25.
import { createClient } from "@supabase/supabase-js";

interface AgentWorkerResult {
  status: "ok" | "error";
  ran_at: string;
  agents_invoked: string[];
  errors: string[];
}

Deno.serve(async (_req: Request): Promise<Response> => {
  const ranAt = new Date().toISOString();
  const result: AgentWorkerResult = {
    status: "ok", ran_at: ranAt, agents_invoked: [], errors: [],
  };

  // Sanity: SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY must be present
  // (set automatically inside the Edge Function runtime).
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const supabaseKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !supabaseKey) {
    result.status = "error";
    result.errors.push("missing supabase env (URL or SERVICE_ROLE_KEY)");
    return new Response(JSON.stringify(result), { status: 500 });
  }

  const supabase = createClient(supabaseUrl, supabaseKey);
  // Smoke: list the validation bucket (created by the migration in Task 19).
  const { data: files, error } = await supabase.storage
    .from("validation").list("", { limit: 5 });
  if (error) {
    result.errors.push(`storage list error: ${error.message}`);
    result.status = "error";
  } else {
    result.agents_invoked.push(`storage_smoke: ${files?.length ?? 0} files`);
  }

  return new Response(JSON.stringify(result, null, 2), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
});
```

Write `supabase/functions/agent-worker/lib/types.ts`:

```typescript
// Shared types for the agent worker.

export interface ValidationSummary {
  source: string;
  schema_version: string;
  ran_at: string;
  rows_in: number;
  rows_valid: number;
  rows_invalid: number;
  rows_warned: number;
  outlets_touched: string[];
  errors_sample: ValidationError[];
  warnings_sample: ValidationWarning[];
}

export interface ValidationError {
  row_offset: number;
  code: string;
  message: string;
  row_keys: string[];
}

export interface ValidationWarning {
  row_offset: number;
  rules: string[];
  row_keys: string[];
}

export interface BannerState {
  outlet: string;
  worst_class: "ok" | "warn" | "err";
  message: string;
  updated_at: string;
}

export type AuditAgent = "drift_detector" | "anomaly_detector" | "retry_repair" | "alert_dispatcher";

export interface AuditDecision {
  ts: string;
  agent: AuditAgent;
  source: string;
  decision: string;
  details: Record<string, unknown>;
  action_taken: string;
}
```

- [ ] **Step 4: Deploy the function**

```bash
cd "$(git rev-parse --show-toplevel)"
npx supabase functions deploy agent-worker --no-verify-jwt
```
Expected: deploy succeeds; URL printed like `https://<project>.supabase.co/functions/v1/agent-worker`.

- [ ] **Step 5: Smoke-invoke**

```bash
curl -i "https://<project>.supabase.co/functions/v1/agent-worker"
```
Expected: 200 with JSON `{"status":"error","errors":["storage list error: ..."]}` (the bucket doesn't exist yet — that's Task 19). Sanity: the function is reachable.

- [ ] **Step 6: Commit**

```bash
git add supabase/functions/agent-worker/ supabase/config.toml
git commit -m "feat(agent-worker): scaffold Edge Function + deploy hello-world"
```

---

### Task 19: Supabase Storage bucket + sync upload from GH Actions

**Files:**
- Create: `supabase/migrations/20260504000000_validation_bucket.sql`
- Create: `.github/actions/upload-validation/action.yml`
- Modify: All 6 sync workflows (`.github/workflows/{toast,guest,budget,marginedge,tripleseat,forecast}-sync.yml`) to call the new composite action

- [ ] **Step 1: Create the migration**

Write `supabase/migrations/20260504000000_validation_bucket.sql`:

```sql
-- Validation files bucket — the agent worker reads from here.
-- Synced by the GH Actions workflows after each successful commit.
insert into storage.buckets (id, name, public, file_size_limit)
values ('validation', 'validation', false, 1048576)  -- 1MB cap per file
on conflict (id) do nothing;

-- Banner state bucket — agent worker writes here; dashboard reads via
-- public URL (renders client-side from GH Pages).
insert into storage.buckets (id, name, public, file_size_limit)
values ('banner', 'banner', true, 16384)  -- 16KB cap, public
on conflict (id) do nothing;

-- Audit log bucket — append-only, agent-write only.
insert into storage.buckets (id, name, public, file_size_limit)
values ('audit', 'audit', false, 10485760)  -- 10MB cap (single rolling file)
on conflict (id) do nothing;
```

- [ ] **Step 2: Push the migration**

```bash
npx supabase db push
```

- [ ] **Step 3: Create the composite action**

Write `.github/actions/upload-validation/action.yml`:

```yaml
name: Upload validation files to Supabase Storage
description: Pushes the latest data/_validation/*.json files to the agent-worker bucket
inputs:
  supabase-url:
    description: Supabase project URL
    required: true
  supabase-service-role-key:
    description: Supabase service role key
    required: true
runs:
  using: composite
  steps:
    - name: Upload latest _validation files
      shell: bash
      env:
        SUPABASE_URL: ${{ inputs.supabase-url }}
        SUPABASE_KEY: ${{ inputs.supabase-service-role-key }}
      run: |
        # Upload each file in data/_validation/ as <source>/<filename>
        # in the validation bucket. Overwrites are fine — the agent-worker
        # picks the latest by mtime.
        for f in data/_validation/*.json; do
          [ -e "$f" ] || continue
          name="$(basename "$f")"
          source="${name%_*}"  # strip _YYYYMMDDTHHMMSSZ.json suffix
          curl -s -X POST \
            "$SUPABASE_URL/storage/v1/object/validation/$source/$name" \
            -H "Authorization: Bearer $SUPABASE_KEY" \
            -H "Content-Type: application/json" \
            -H "x-upsert: true" \
            --data-binary "@$f" >/dev/null && echo "uploaded $name"
        done
```

- [ ] **Step 4: Wire into one sync workflow first (toast-sync.yml)**

In `.github/workflows/toast-sync.yml`, AFTER the commit step and before `Post Set up Python`, insert:

```yaml
      - name: Upload validation files to Supabase
        if: success()
        uses: ./.github/actions/upload-validation
        with:
          supabase-url: ${{ secrets.SUPABASE_URL }}
          supabase-service-role-key: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
```

- [ ] **Step 5: Verify in toast-sync run**

After merge, kick a toast-sync run and verify:
1. The `Upload validation files to Supabase` step runs
2. Files appear in the Supabase dashboard → Storage → validation bucket
3. The agent-worker function can list them: `curl https://<proj>.supabase.co/functions/v1/agent-worker` should now return `storage_smoke: N files` (N > 0).

- [ ] **Step 6: Replicate the upload step into the other 5 sync workflows**

Add the same `Upload validation files to Supabase` step to `guest-sync.yml`, `budget-sync.yml`, `marginedge-sync.yml`, `tripleseat-sync.yml`, `forecast-sync.yml`.

- [ ] **Step 7: Commit**

```bash
git add supabase/migrations/ .github/actions/upload-validation/ .github/workflows/
git commit -m "feat(agent-worker): Supabase Storage buckets + per-sync upload step"
```

---

### Task 20: Audit log writer module

**Files:**
- Create: `supabase/functions/agent-worker/lib/audit.ts`
- Modify: `supabase/functions/agent-worker/index.ts`

- [ ] **Step 1: Write the audit lib**

Write `supabase/functions/agent-worker/lib/audit.ts`:

```typescript
// Append-only audit log writer.
//
// Stored as audit/agent_decisions.jsonl in the audit bucket. Each
// agent invocation appends one JSONL line per decision.
//
// Concurrency: Edge Functions are single-instance per invocation, but
// pg_cron may overlap. We append by READ → APPEND → WRITE which has
// a small race window; acceptable for Phase A.1 (audit loss is low
// impact). If overlap becomes an issue, switch to a Postgres table.

import { SupabaseClient } from "@supabase/supabase-js";
import type { AuditDecision } from "./types.ts";

const AUDIT_PATH = "agent_decisions.jsonl";

export async function appendAudit(
  supabase: SupabaseClient,
  decisions: AuditDecision[],
): Promise<void> {
  if (decisions.length === 0) return;
  const newLines = decisions.map((d) => JSON.stringify(d)).join("\n") + "\n";

  const { data: existing, error: readErr } = await supabase.storage
    .from("audit").download(AUDIT_PATH);
  let combined = newLines;
  if (!readErr && existing) {
    const prior = await existing.text();
    combined = prior + newLines;
  }

  const { error: writeErr } = await supabase.storage
    .from("audit").upload(AUDIT_PATH, combined, {
      contentType: "application/x-ndjson",
      upsert: true,
    });
  if (writeErr) {
    console.error("audit write failed:", writeErr.message);
  }
}
```

- [ ] **Step 2: Wire a smoke audit decision into index.ts**

In `supabase/functions/agent-worker/index.ts`, replace the storage smoke block with:

```typescript
import { createClient } from "@supabase/supabase-js";
import { appendAudit } from "./lib/audit.ts";
import type { AuditDecision } from "./lib/types.ts";

// ... (Deno.serve as before, up to the supabase = createClient line)

  // Smoke audit: write a single "agent_worker_invoked" decision so we can
  // verify the audit pipeline end-to-end.
  const decision: AuditDecision = {
    ts: ranAt,
    agent: "alert_dispatcher",
    source: "agent_worker",
    decision: "smoke_invocation",
    details: { phase: "A.1" },
    action_taken: "noop",
  };
  await appendAudit(supabase, [decision]);
  result.agents_invoked.push("audit_smoke");
```

- [ ] **Step 3: Re-deploy**

```bash
npx supabase functions deploy agent-worker --no-verify-jwt
```

- [ ] **Step 4: Smoke**

```bash
curl https://<project>.supabase.co/functions/v1/agent-worker
# Then download the audit file:
npx supabase storage cp ss:///audit/agent_decisions.jsonl /tmp/audit.jsonl
cat /tmp/audit.jsonl | tail -5
```
Expected: at least one JSONL line with `agent_worker_invoked`.

- [ ] **Step 5: Commit**

```bash
git add supabase/functions/agent-worker/lib/audit.ts supabase/functions/agent-worker/index.ts
git commit -m "feat(agent-worker): append-only audit log writer + smoke decision"
```

---

## Sprint 2-3 — Agents (Tasks 21-25)

### Task 21: Schema-drift detector agent

**Files:**
- Create: `supabase/functions/agent-worker/lib/anthropic.ts`
- Create: `supabase/functions/agent-worker/agents/drift_detector.ts`
- Modify: `supabase/functions/agent-worker/index.ts`

- [ ] **Step 1: Write the Anthropic client wrapper**

Write `supabase/functions/agent-worker/lib/anthropic.ts`:

```typescript
import Anthropic from "@anthropic-ai/sdk";

let _client: Anthropic | null = null;

export function anthropic(): Anthropic {
  if (_client) return _client;
  const apiKey = Deno.env.get("ANTHROPIC_API_KEY");
  if (!apiKey) throw new Error("ANTHROPIC_API_KEY not set");
  _client = new Anthropic({ apiKey });
  return _client;
}

export async function classifyDrift(opts: {
  source: string;
  storedSchemaKeys: string[];
  observedRowSamples: Record<string, unknown>[];
}): Promise<{
  classification: "stable" | "additive_non_breaking" | "breaking";
  reasoning: string;
  added_fields: string[];
  removed_fields: string[];
  changed_types: string[];
}> {
  const { source, storedSchemaKeys, observedRowSamples } = opts;
  const prompt = [
    "You are a schema drift classifier for a data ingestion pipeline.",
    `Source: ${source}`,
    `Stored schema field set: ${JSON.stringify(storedSchemaKeys)}`,
    "Observed sample rows (first 3 from latest sync):",
    JSON.stringify(observedRowSamples, null, 2),
    "",
    "Classify the diff as ONE OF:",
    "  - stable: no diff, or diff is in allowed mutation set (extra fields fine)",
    "  - additive_non_breaking: new optional field present, no existing field removed/null",
    "  - breaking: required field removed, type changed, or population pattern flipped",
    "    (e.g. previously-populated field is now consistently null)",
    "",
    "Respond with ONLY a JSON object matching this exact schema:",
    `{"classification": "...", "reasoning": "...", "added_fields": [...], "removed_fields": [...], "changed_types": [...]}`,
  ].join("\n");

  const resp = await anthropic().messages.create({
    model: "claude-sonnet-4-5",
    max_tokens: 1024,
    messages: [{ role: "user", content: prompt }],
  });
  const text = resp.content[0].type === "text" ? resp.content[0].text : "";
  // Try strict JSON parse first; fall back to extracting the first {...} block.
  try {
    return JSON.parse(text);
  } catch {
    const m = text.match(/\{[\s\S]*\}/);
    if (m) return JSON.parse(m[0]);
    throw new Error(`drift classifier returned non-JSON: ${text.slice(0, 200)}`);
  }
}
```

- [ ] **Step 2: Write the drift detector agent**

Write `supabase/functions/agent-worker/agents/drift_detector.ts`:

```typescript
// Schema-drift detector agent.
//
// For each source:
//   1. Read latest validation summary from validation bucket
//   2. Read stored schema from validation/_schemas/<source>.json (or seed if absent)
//   3. Compare sample rows against stored schema
//   4. If diff: ask Claude to classify (stable / additive_non_breaking / breaking)
//   5. additive_non_breaking → auto-update stored schema, log audit
//      breaking → leave stored schema untouched, return alert event for
//                 the alert_dispatcher to surface
//      stable → no-op
import { SupabaseClient } from "@supabase/supabase-js";
import { classifyDrift } from "../lib/anthropic.ts";
import type { AuditDecision, ValidationSummary } from "../lib/types.ts";

const SOURCES = ["toast_order", "toast_time_entry", "resy_survey",
  "marginedge_invoice", "tripleseat_event", "helixo2_forecast", "sage_budget"];

export interface DriftResult {
  audits: AuditDecision[];
  alerts: { source: string; classification: string; reasoning: string }[];
}

export async function runDriftDetector(supabase: SupabaseClient): Promise<DriftResult> {
  const audits: AuditDecision[] = [];
  const alerts: DriftResult["alerts"] = [];
  const ts = new Date().toISOString();

  for (const source of SOURCES) {
    // Get latest validation summary file for this source
    const { data: files } = await supabase.storage
      .from("validation").list(source, { limit: 100, sortBy: { column: "name", order: "desc" } });
    if (!files || files.length === 0) continue;
    const latestFile = files[0].name;

    const { data: summaryBlob } = await supabase.storage
      .from("validation").download(`${source}/${latestFile}`);
    if (!summaryBlob) continue;
    const summary: ValidationSummary = JSON.parse(await summaryBlob.text());

    // Sample row keys from warnings + errors (these are the actual rows
    // that came through, just with warning flags or errors).
    const sampleKeys = new Set<string>();
    for (const w of summary.warnings_sample) for (const k of w.row_keys) sampleKeys.add(k);
    for (const e of summary.errors_sample)   for (const k of e.row_keys) sampleKeys.add(k);

    // Read stored schema
    const schemaPath = `_schemas/${source}.json`;
    const { data: schemaBlob, error: schemaErr } = await supabase.storage
      .from("validation").download(schemaPath);
    let storedKeys: string[];
    if (schemaErr || !schemaBlob) {
      // Seed from current sample
      storedKeys = [...sampleKeys].sort();
      await supabase.storage.from("validation").upload(
        schemaPath, JSON.stringify({ keys: storedKeys, seeded_at: ts }, null, 2),
        { contentType: "application/json", upsert: true },
      );
      audits.push({
        ts, agent: "drift_detector", source,
        decision: "seeded_initial_schema", details: { keys: storedKeys },
        action_taken: "stored new _schemas/" + source + ".json",
      });
      continue;
    }
    const stored = JSON.parse(await schemaBlob.text());
    storedKeys = stored.keys || [];

    // Quick diff
    const observed = [...sampleKeys].sort();
    const added = observed.filter(k => !storedKeys.includes(k));
    const removed = storedKeys.filter(k => !observed.includes(k));
    if (added.length === 0 && removed.length === 0 && summary.rows_warned === 0) {
      continue;  // stable — no audit, no alert
    }

    // Need an LLM classification to decide if it's safe-additive or breaking.
    const samples = [...summary.warnings_sample.slice(0, 3),
                     ...summary.errors_sample.slice(0, 3)]
      .map(s => Object.fromEntries(s.row_keys.map(k => [k, "..."])));
    const cls = await classifyDrift({
      source, storedSchemaKeys: storedKeys, observedRowSamples: samples,
    });

    if (cls.classification === "additive_non_breaking") {
      const newKeys = [...new Set([...storedKeys, ...added])].sort();
      await supabase.storage.from("validation").upload(
        schemaPath, JSON.stringify({ keys: newKeys, updated_at: ts, prior: stored }, null, 2),
        { contentType: "application/json", upsert: true },
      );
      audits.push({
        ts, agent: "drift_detector", source,
        decision: "additive_non_breaking_auto_applied",
        details: { added, reasoning: cls.reasoning },
        action_taken: `stored schema bumped to include: ${added.join(", ")}`,
      });
    } else if (cls.classification === "breaking") {
      audits.push({
        ts, agent: "drift_detector", source,
        decision: "breaking_drift_detected",
        details: { added, removed, changed_types: cls.changed_types, reasoning: cls.reasoning },
        action_taken: "alert dispatched; stored schema NOT updated",
      });
      alerts.push({ source, classification: "breaking", reasoning: cls.reasoning });
    }
  }

  return { audits, alerts };
}
```

- [ ] **Step 3: Wire into index.ts**

In `supabase/functions/agent-worker/index.ts`, replace the smoke audit block with:

```typescript
import { runDriftDetector } from "./agents/drift_detector.ts";
// ...
const drift = await runDriftDetector(supabase);
await appendAudit(supabase, drift.audits);
result.agents_invoked.push(`drift_detector: ${drift.audits.length} audits, ${drift.alerts.length} alerts`);
// (alerts will be passed to the alert_dispatcher in Task 24)
```

- [ ] **Step 4: Set the ANTHROPIC_API_KEY secret in Supabase**

```bash
npx supabase secrets set ANTHROPIC_API_KEY=<your-key>
```

- [ ] **Step 5: Re-deploy + smoke**

```bash
npx supabase functions deploy agent-worker --no-verify-jwt
curl https://<project>.supabase.co/functions/v1/agent-worker
```
Expected: `drift_detector: N audits, 0 alerts` (N = number of sources seeded). Audit file should grow.

- [ ] **Step 6: Commit**

```bash
git add supabase/functions/agent-worker/lib/anthropic.ts supabase/functions/agent-worker/agents/drift_detector.ts supabase/functions/agent-worker/index.ts
git commit -m "feat(agent-worker): schema-drift detector agent (Anthropic Sonnet classifier)"
```

---

### Task 22: Anomaly detector agent (shadow mode)

**Files:**
- Create: `supabase/functions/agent-worker/agents/anomaly_detector.ts`
- Modify: `supabase/functions/agent-worker/index.ts`

- [ ] **Step 1: Write the agent**

Write `supabase/functions/agent-worker/agents/anomaly_detector.ts`:

```typescript
// Anomaly detector agent.
//
// For each (outlet × metric × DOW) tuple, computes rolling 8-week mean+std
// and flags if today's value lies beyond ±3σ. Phase A.1: SHADOW MODE for
// the first 14 days (logs to audit only; no Slack push). The alert_dispatcher
// honors the shadow flag.
//
// Reads outlet daily history from data/<outlet>.json via GH Pages public URL
// (the dashboard already publishes the same JSON the agent needs).
import type { AuditDecision } from "../lib/types.ts";

const PAGES_BASE = "https://rrmethodco.github.io/method-q1-2026-dashboards/data";
const OUTLETS = ["lsbr", "mulherins", "kampers", "lowland", "vessel",
  "anthology", "rosemary_rose", "hiroki_det", "hiroki_phl", "little_wing", "quoin"];
const METRICS = ["amount", "guests"] as const;
const SHADOW_UNTIL_ISO = "2026-05-18T00:00:00Z";  // 14d shadow window

interface AnomalyAlert {
  outlet: string;
  metric: string;
  date: string;
  value: number;
  expected_mean: number;
  expected_std: number;
  z_score: number;
}

export interface AnomalyResult {
  audits: AuditDecision[];
  alerts: AnomalyAlert[];
  shadowed: boolean;
}

export async function runAnomalyDetector(): Promise<AnomalyResult> {
  const audits: AuditDecision[] = [];
  const alerts: AnomalyAlert[] = [];
  const ts = new Date().toISOString();
  const shadowed = ts < SHADOW_UNTIL_ISO;

  for (const outlet of OUTLETS) {
    let payload: Record<string, unknown>;
    try {
      const r = await fetch(`${PAGES_BASE}/${outlet}.json`, { cache: "no-cache" });
      if (!r.ok) continue;
      payload = await r.json();
    } catch { continue; }

    const od = (payload as any).order_details?.main?.daily;
    if (!Array.isArray(od) || od.length < 60) continue;  // need 8w of data

    // Build per-DOW rolling history (last 8 occurrences of each DOW)
    for (const metric of METRICS) {
      const byDow: Record<number, number[]> = {0:[],1:[],2:[],3:[],4:[],5:[],6:[]};
      for (const row of od) {
        const d = new Date(row.date + "T12:00:00Z");
        const dow = d.getUTCDay();
        const v = Number(row[metric]);
        if (Number.isFinite(v)) byDow[dow].push(v);
      }

      // Yesterday's value (if present) — that's the new observation
      const yesterday = od[od.length - 1];
      const yDate = new Date(yesterday.date + "T12:00:00Z");
      const yDow = yDate.getUTCDay();
      const yVal = Number(yesterday[metric]);
      if (!Number.isFinite(yVal)) continue;

      const history = byDow[yDow].slice(-9, -1);  // 8 prior same-DOW occurrences
      if (history.length < 4) continue;
      const mean = history.reduce((s, v) => s + v, 0) / history.length;
      const variance = history.reduce((s, v) => s + (v - mean) ** 2, 0) / history.length;
      const std = Math.sqrt(variance);
      if (std === 0) continue;
      const z = (yVal - mean) / std;
      if (Math.abs(z) > 3) {
        alerts.push({
          outlet, metric, date: yesterday.date, value: yVal,
          expected_mean: mean, expected_std: std, z_score: z,
        });
        audits.push({
          ts, agent: "anomaly_detector", source: outlet,
          decision: shadowed ? "anomaly_shadow_logged" : "anomaly_alerted",
          details: { metric, value: yVal, mean, std, z, shadowed },
          action_taken: shadowed
            ? `logged only (shadow mode until ${SHADOW_UNTIL_ISO})`
            : "queued for Slack alert",
        });
      }
    }
  }

  return { audits, alerts, shadowed };
}
```

- [ ] **Step 2: Wire into index.ts**

```typescript
import { runAnomalyDetector } from "./agents/anomaly_detector.ts";
// ...
const anomaly = await runAnomalyDetector();
await appendAudit(supabase, anomaly.audits);
result.agents_invoked.push(`anomaly_detector: ${anomaly.audits.length} audits, ${anomaly.alerts.length} alerts (shadowed=${anomaly.shadowed})`);
```

- [ ] **Step 3: Re-deploy + smoke**

```bash
npx supabase functions deploy agent-worker --no-verify-jwt
curl https://<project>.supabase.co/functions/v1/agent-worker
```
Expected: `anomaly_detector: K audits, K alerts (shadowed=true)`. K depends on actual outlet variance.

- [ ] **Step 4: Commit**

```bash
git add supabase/functions/agent-worker/agents/anomaly_detector.ts supabase/functions/agent-worker/index.ts
git commit -m "feat(agent-worker): anomaly detector (per-DOW ±3σ, 14d shadow mode)"
```

---

### Task 23: Self-healing retry/repair agent

**Files:**
- Create: `supabase/functions/agent-worker/lib/github.ts`
- Create: `supabase/functions/agent-worker/agents/retry_repair.ts`
- Modify: `supabase/functions/agent-worker/index.ts`

- [ ] **Step 1: Write the gh-API client**

Write `supabase/functions/agent-worker/lib/github.ts`:

```typescript
const REPO = "rrmethodco/method-q1-2026-dashboards";

function ghToken(): string {
  const t = Deno.env.get("GITHUB_PAT");
  if (!t) throw new Error("GITHUB_PAT secret not set");
  return t;
}

async function ghFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const url = `https://api.github.com${path}`;
  return fetch(url, {
    ...init,
    headers: {
      ...(init.headers || {}),
      "Authorization": `Bearer ${ghToken()}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
}

export interface WorkflowRun {
  id: number;
  name: string;
  status: string;
  conclusion: string | null;
  created_at: string;
  workflow_id: number;
  head_branch: string;
}

export async function listRecentRuns(workflowFile: string, limit = 5): Promise<WorkflowRun[]> {
  const r = await ghFetch(`/repos/${REPO}/actions/workflows/${workflowFile}/runs?per_page=${limit}`);
  if (!r.ok) throw new Error(`gh ${r.status}: ${await r.text()}`);
  const body = await r.json();
  return body.workflow_runs || [];
}

export async function dispatchWorkflow(workflowFile: string, ref = "main", inputs: Record<string,string> = {}): Promise<void> {
  const r = await ghFetch(`/repos/${REPO}/actions/workflows/${workflowFile}/dispatches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ref, inputs }),
  });
  if (!r.ok) throw new Error(`dispatch ${r.status}: ${await r.text()}`);
}
```

- [ ] **Step 2: Write the agent**

Write `supabase/functions/agent-worker/agents/retry_repair.ts`:

```typescript
// Self-healing retry/repair agent.
//
// Polls recent workflow runs. For each that is `cancelled` or `failure`:
//   - If pattern is auto-healable per metric_classes.yml AND we haven't
//     retried it more than 3 times in the past 30 min, dispatch a retry.
//   - Otherwise, queue an alert event.
import { listRecentRuns, dispatchWorkflow } from "../lib/github.ts";
import type { AuditDecision } from "../lib/types.ts";

const WORKFLOWS = [
  "toast-sync.yml", "guest-sync.yml", "budget-sync.yml",
  "marginedge-sync.yml", "tripleseat-sync.yml", "forecast-sync.yml",
];
const RETRY_WINDOW_MS = 30 * 60 * 1000;
const MAX_RETRIES_PER_WINDOW = 3;

// In-memory retry counter — Edge Functions are stateless across cold starts
// but for Phase A.1 this is acceptable (worst case: an extra retry).
const recentRetries = new Map<string, number[]>();

interface RetryResult {
  audits: AuditDecision[];
  alerts: { workflow: string; conclusion: string; reason: string }[];
}

export async function runRetryRepair(): Promise<RetryResult> {
  const audits: AuditDecision[] = [];
  const alerts: RetryResult["alerts"] = [];
  const ts = new Date().toISOString();

  for (const wf of WORKFLOWS) {
    let runs;
    try { runs = await listRecentRuns(wf, 3); } catch (e) {
      audits.push({ ts, agent: "retry_repair", source: wf,
        decision: "list_failed", details: { error: String(e) },
        action_taken: "skipped this workflow this cycle" });
      continue;
    }
    const latest = runs[0];
    if (!latest) continue;
    if (latest.conclusion !== "cancelled" && latest.conclusion !== "failure") continue;
    // Only act on terminal-failed runs that completed in the LAST hour
    const ageMs = Date.now() - new Date(latest.created_at).getTime();
    if (ageMs > 60 * 60 * 1000) continue;

    const retries = (recentRetries.get(wf) || []).filter(t => Date.now() - t < RETRY_WINDOW_MS);
    if (retries.length >= MAX_RETRIES_PER_WINDOW) {
      alerts.push({ workflow: wf, conclusion: latest.conclusion!,
        reason: `exhausted retry budget (${retries.length}/${MAX_RETRIES_PER_WINDOW} in last 30min)` });
      audits.push({ ts, agent: "retry_repair", source: wf,
        decision: "retry_budget_exhausted",
        details: { retries: retries.length, conclusion: latest.conclusion },
        action_taken: "alert dispatched, no auto-heal" });
      continue;
    }

    // Dispatch retry
    try {
      await dispatchWorkflow(wf);
      recentRetries.set(wf, [...retries, Date.now()]);
      audits.push({ ts, agent: "retry_repair", source: wf,
        decision: "auto_retry_dispatched",
        details: { prior_conclusion: latest.conclusion, prior_run_id: latest.id },
        action_taken: `dispatched ${wf} (retry ${retries.length + 1}/${MAX_RETRIES_PER_WINDOW})` });
    } catch (e) {
      alerts.push({ workflow: wf, conclusion: latest.conclusion!,
        reason: `retry dispatch failed: ${String(e)}` });
    }
  }

  return { audits, alerts };
}
```

- [ ] **Step 3: Wire + secret**

In `index.ts`:

```typescript
import { runRetryRepair } from "./agents/retry_repair.ts";
// ...
const retry = await runRetryRepair();
await appendAudit(supabase, retry.audits);
result.agents_invoked.push(`retry_repair: ${retry.audits.length} audits, ${retry.alerts.length} alerts`);
```

Set the GITHUB_PAT secret (needs `actions:write` + `repo` scopes):

```bash
npx supabase secrets set GITHUB_PAT=<your-fine-grained-PAT>
```

- [ ] **Step 4: Re-deploy + smoke**

```bash
npx supabase functions deploy agent-worker --no-verify-jwt
curl https://<project>.supabase.co/functions/v1/agent-worker
```

- [ ] **Step 5: Commit**

```bash
git add supabase/functions/agent-worker/lib/github.ts supabase/functions/agent-worker/agents/retry_repair.ts supabase/functions/agent-worker/index.ts
git commit -m "feat(agent-worker): self-healing retry/repair agent (gh API, 3-retry budget)"
```

---

### Task 24: Alert dispatcher (Slack)

**Files:**
- Create: `supabase/functions/agent-worker/lib/slack.ts`
- Create: `supabase/functions/agent-worker/agents/alert_dispatcher.ts`
- Modify: `supabase/functions/agent-worker/index.ts`

- [ ] **Step 1: Write the Slack client**

Write `supabase/functions/agent-worker/lib/slack.ts`:

```typescript
import { WebClient } from "@slack/web-api";

let _client: WebClient | null = null;
function slack(): WebClient {
  if (_client) return _client;
  const token = Deno.env.get("SLACK_BOT_TOKEN");
  if (!token) throw new Error("SLACK_BOT_TOKEN secret not set");
  _client = new WebClient(token);
  return _client;
}

export async function postAlert(channel: string, text: string, blocks?: unknown[]): Promise<void> {
  await slack().chat.postMessage({ channel, text, blocks });
}
```

- [ ] **Step 2: Write the dispatcher**

Write `supabase/functions/agent-worker/agents/alert_dispatcher.ts`:

```typescript
// Alert dispatcher.
//
// Consumes alert events from drift / anomaly / retry agents, dedups
// (identical event within 60min suppressed), routes to Slack channel
// from SLACK_DASHBOARD_ALERTS_CHANNEL (default C0B1N51L9TN).
import { postAlert } from "../lib/slack.ts";
import type { AuditDecision } from "../lib/types.ts";

interface AlertEvent {
  kind: "drift_breaking" | "anomaly" | "retry_exhausted";
  source: string;
  text: string;
}

const recentAlerts = new Map<string, number>();
const DEDUP_MS = 60 * 60 * 1000;

export async function dispatchAlerts(events: AlertEvent[]): Promise<AuditDecision[]> {
  const channel = Deno.env.get("SLACK_DASHBOARD_ALERTS_CHANNEL") || "C0B1N51L9TN";
  const ts = new Date().toISOString();
  const audits: AuditDecision[] = [];

  for (const ev of events) {
    const key = `${ev.kind}:${ev.source}:${ev.text.slice(0, 80)}`;
    const last = recentAlerts.get(key);
    if (last && Date.now() - last < DEDUP_MS) {
      audits.push({ ts, agent: "alert_dispatcher", source: ev.source,
        decision: "deduplicated", details: { kind: ev.kind, key },
        action_taken: "suppressed (within 60min dedup window)" });
      continue;
    }
    try {
      await postAlert(channel, `[${ev.kind}] ${ev.source}: ${ev.text}`);
      recentAlerts.set(key, Date.now());
      audits.push({ ts, agent: "alert_dispatcher", source: ev.source,
        decision: "slack_posted", details: { kind: ev.kind, channel },
        action_taken: `posted to ${channel}` });
    } catch (e) {
      audits.push({ ts, agent: "alert_dispatcher", source: ev.source,
        decision: "slack_post_failed", details: { error: String(e) },
        action_taken: "alert lost — needs manual triage" });
    }
  }
  return audits;
}
```

- [ ] **Step 3: Wire into index.ts**

```typescript
import { dispatchAlerts } from "./agents/alert_dispatcher.ts";
// ...
// Combine alerts from drift + anomaly (when not shadowed) + retry
const events = [
  ...drift.alerts.map(a => ({ kind: "drift_breaking" as const, source: a.source, text: a.reasoning })),
  ...(!anomaly.shadowed ? anomaly.alerts.map(a => ({
    kind: "anomaly" as const, source: a.outlet,
    text: `${a.metric} on ${a.date} = ${a.value.toFixed(0)} (z=${a.z_score.toFixed(1)}, expected ${a.expected_mean.toFixed(0)} ±${a.expected_std.toFixed(0)})`,
  })) : []),
  ...retry.alerts.map(a => ({ kind: "retry_exhausted" as const, source: a.workflow, text: a.reason })),
];
const dispAudits = await dispatchAlerts(events);
await appendAudit(supabase, dispAudits);
result.agents_invoked.push(`alert_dispatcher: ${dispAudits.length} routed`);
```

Set the secret:

```bash
npx supabase secrets set SLACK_BOT_TOKEN=xoxb-...
npx supabase secrets set SLACK_DASHBOARD_ALERTS_CHANNEL=C0B1N51L9TN
```

- [ ] **Step 4: Re-deploy + smoke**

Force a synthetic alert by setting a test value, deploy, invoke, and verify the Slack channel receives it.

```bash
npx supabase functions deploy agent-worker --no-verify-jwt
curl https://<project>.supabase.co/functions/v1/agent-worker
# Check Slack channel C0B1N51L9TN for an automated message.
```

- [ ] **Step 5: Commit**

```bash
git add supabase/functions/agent-worker/lib/slack.ts supabase/functions/agent-worker/agents/alert_dispatcher.ts supabase/functions/agent-worker/index.ts
git commit -m "feat(agent-worker): alert dispatcher (Slack chat.postMessage with 60min dedup)"
```

---

### Task 25: Banner state writer + dashboard consumer

**Files:**
- Create: `supabase/functions/agent-worker/agents/banner_writer.ts`
- Modify: `supabase/functions/agent-worker/index.ts`
- Modify: `Method_Co_FB_Performance_Dashboard.html` (Banner fetch URL)

- [ ] **Step 1: Write the banner writer**

Write `supabase/functions/agent-worker/agents/banner_writer.ts`:

```typescript
// Banner state writer.
//
// Each invocation, summarize the current state of validation summaries
// per outlet and write a JSON file the dashboard can fetch.
import { SupabaseClient } from "@supabase/supabase-js";
import type { BannerState, ValidationSummary } from "../lib/types.ts";

const SOURCES = ["toast_order", "toast_time_entry", "resy_survey",
  "marginedge_invoice", "tripleseat_event", "helixo2_forecast", "sage_budget"];
const OUTLETS = ["lsbr", "mulherins", "kampers", "lowland", "vessel",
  "anthology", "rosemary_rose", "hiroki_det", "hiroki_phl", "little_wing", "quoin"];

export async function writeBannerStates(supabase: SupabaseClient): Promise<number> {
  const ts = new Date().toISOString();
  // Pre-fetch latest summary per source
  const latestPerSource = new Map<string, ValidationSummary>();
  for (const s of SOURCES) {
    const { data: files } = await supabase.storage.from("validation").list(s, { limit: 1, sortBy: { column: "name", order: "desc" } });
    if (!files || files.length === 0) continue;
    const { data: blob } = await supabase.storage.from("validation").download(`${s}/${files[0].name}`);
    if (!blob) continue;
    latestPerSource.set(s, JSON.parse(await blob.text()));
  }

  let written = 0;
  for (const outlet of OUTLETS) {
    let worst: BannerState["worst_class"] = "ok";
    const issues: string[] = [];
    for (const [src, summary] of latestPerSource) {
      if (!summary.outlets_touched.includes(outlet)) continue;
      const ageHrs = (Date.now() - new Date(summary.ran_at).getTime()) / 3600_000;
      if (summary.rows_invalid > 0) {
        worst = "err";
        issues.push(`${src}: ${summary.rows_invalid} invalid rows`);
      } else if (ageHrs > 26 && worst !== "err") {
        worst = "warn";
        issues.push(`${src}: ${ageHrs.toFixed(1)}h stale`);
      } else if (summary.rows_warned > 0 && worst === "ok") {
        worst = "warn";
      }
    }
    const banner: BannerState = {
      outlet, worst_class: worst,
      message: issues.join("; ") || "All sources current.",
      updated_at: ts,
    };
    await supabase.storage.from("banner").upload(
      `${outlet}.json`, JSON.stringify(banner, null, 2),
      { contentType: "application/json", upsert: true },
    );
    written++;
  }
  return written;
}
```

- [ ] **Step 2: Wire into index.ts**

```typescript
import { writeBannerStates } from "./agents/banner_writer.ts";
// ...
const bannerCount = await writeBannerStates(supabase);
result.agents_invoked.push(`banner_writer: ${bannerCount} outlets`);
```

- [ ] **Step 3: Update dashboard fetch URL**

In the dashboard's `refreshValidationPanel` function (Task 17, Step 3), change the banner fetch URL from `data/_banner/...` to the public Supabase Storage URL:

```javascript
// Replace:
//   const r = await fetch(`data/_banner/${STATE.outlet}.json`, {cache: 'no-cache'});
// With:
const SUPABASE_BANNER_URL = "https://<project>.supabase.co/storage/v1/object/public/banner";
const r = await fetch(`${SUPABASE_BANNER_URL}/${STATE.outlet}.json`, {cache: 'no-cache'});
```

(Replace `<project>` with the actual Supabase project ref.)

- [ ] **Step 4: Re-deploy + smoke**

```bash
npx supabase functions deploy agent-worker --no-verify-jwt
curl https://<project>.supabase.co/functions/v1/agent-worker
# Then fetch the banner directly:
curl https://<project>.supabase.co/storage/v1/object/public/banner/lsbr.json
```
Expected: a JSON banner state object.

- [ ] **Step 5: Commit**

```bash
git add supabase/functions/agent-worker/agents/banner_writer.ts supabase/functions/agent-worker/index.ts Method_Co_FB_Performance_Dashboard.html
git commit -m "feat(agent-worker): banner state writer + dashboard public-URL fetch"
```

---

## Sprint 3 — Polish (Tasks 26-29)

### Task 26: pg_cron schedule for the agent worker

**Files:**
- Create: `supabase/migrations/20260504000001_agent_cron.sql`

- [ ] **Step 1: Write the cron schedule migration**

Write `supabase/migrations/20260504000001_agent_cron.sql`:

```sql
-- Schedule the agent-worker Edge Function to run every 5 minutes.
-- Requires pg_cron + pg_net extensions (Supabase enables both by default).
create extension if not exists pg_cron;
create extension if not exists pg_net;

select cron.schedule(
  'agent-worker-tick',
  '*/5 * * * *',  -- every 5 minutes
  $$
    select net.http_get(
      url := 'https://<project>.supabase.co/functions/v1/agent-worker',
      headers := jsonb_build_object(
        'Authorization', 'Bearer ' || current_setting('app.settings.service_role_key')
      )
    );
  $$
);
```

- [ ] **Step 2: Set the service role key as a Postgres setting**

In the Supabase dashboard SQL editor, run:

```sql
alter database postgres set "app.settings.service_role_key" = 'YOUR_SERVICE_ROLE_KEY';
```

- [ ] **Step 3: Push the migration**

```bash
npx supabase db push
```

- [ ] **Step 4: Verify the cron job is scheduled**

```bash
npx supabase db query "select * from cron.job;"
```
Expected: row for `agent-worker-tick` with schedule `*/5 * * * *`.

- [ ] **Step 5: Wait 5 min + verify it ran**

```bash
sleep 300
npx supabase db query "select * from cron.job_run_details order by start_time desc limit 5;"
```
Expected: at least one successful run.

- [ ] **Step 6: Commit**

```bash
git add supabase/migrations/20260504000001_agent_cron.sql
git commit -m "feat(agent-worker): pg_cron schedule — every 5 min"
```

---

### Task 27: PII redaction utility

**Files:**
- Create: `toast-etl/validation/pii_redact.py`
- Create: `toast-etl/tests/validation/test_pii_redact.py`
- Modify: `toast-etl/validation/runner.py` (call redactor before storing error samples)

- [ ] **Step 1: Write the failing test**

Write `toast-etl/tests/validation/test_pii_redact.py`:

```python
"""Tests for PII redaction."""
from validation.pii_redact import redact_pii


def test_redacts_email():
    row = {"id": 1, "user": {"email": "ross@methodco.com", "name": "Ross"}}
    out = redact_pii(row)
    assert out["user"]["email"] == "[REDACTED:email]"


def test_redacts_phone():
    row = {"reservation": {"contact_phone": "+1-215-555-1212"}}
    out = redact_pii(row)
    assert out["reservation"]["contact_phone"] == "[REDACTED:phone]"


def test_redacts_full_name():
    row = {"user": {"full_name": "Ross Richardson"}}
    out = redact_pii(row)
    assert out["user"]["full_name"] == "[REDACTED:name]"


def test_preserves_non_pii():
    row = {"id": 1, "amount": 50.00, "date": "2026-05-04"}
    out = redact_pii(row)
    assert out == row


def test_handles_nested_lists():
    row = {"items": [{"user": {"email": "a@b.com"}}, {"user": {"email": "c@d.com"}}]}
    out = redact_pii(row)
    assert out["items"][0]["user"]["email"] == "[REDACTED:email]"
    assert out["items"][1]["user"]["email"] == "[REDACTED:email]"
```

- [ ] **Step 2: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/validation/test_pii_redact.py -v
```

- [ ] **Step 3: Implement redactor**

Write `toast-etl/validation/pii_redact.py`:

```python
"""PII redaction for validation error samples.

Intentionally conservative: redacts known sensitive field names regardless
of value content. We don't try to heuristically detect PII in other fields.
"""
from __future__ import annotations

PII_FIELDS = {
    # Field name (lower) → redaction tag
    "email": "email",
    "user_email": "email",
    "contact_email": "email",
    "phone": "phone",
    "contact_phone": "phone",
    "phone_number": "phone",
    "full_name": "name",
    "first_name": "name",
    "last_name": "name",
    "guest_name": "name",
    "address": "address",
    "street": "address",
    "credit_card": "cc",
    "card_number": "cc",
    "ssn": "ssn",
}


def redact_pii(obj):
    """Recursively redact known PII field names. Returns a NEW object."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            tag = PII_FIELDS.get(k.lower())
            if tag is not None:
                out[k] = f"[REDACTED:{tag}]"
            else:
                out[k] = redact_pii(v)
        return out
    if isinstance(obj, list):
        return [redact_pii(x) for x in obj]
    return obj
```

- [ ] **Step 4: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/validation/test_pii_redact.py -v
```

- [ ] **Step 5: Apply in the runner**

Modify `toast-etl/validation/runner.py`. At the top, add:

```python
from .pii_redact import redact_pii
```

In the error-collection block, change:

```python
errors.append({
    "row_offset": i,
    "code": "model_validation_error",
    "message": str(e)[:500],
    "row_keys": sorted(row.keys()) if isinstance(row, dict) else [],
})
```

to:

```python
errors.append({
    "row_offset": i,
    "code": "model_validation_error",
    "message": str(e)[:500],
    "row_keys": sorted(row.keys()) if isinstance(row, dict) else [],
    "row_redacted": redact_pii(row) if isinstance(row, dict) else None,
})
```

- [ ] **Step 6: Commit**

```bash
git add toast-etl/validation/pii_redact.py toast-etl/tests/validation/test_pii_redact.py toast-etl/validation/runner.py
git commit -m "feat(validation): PII redactor + apply in runner error samples"
```

---

### Task 28: Validation file retention pruner

**Files:**
- Create: `toast-etl/validation/retention.py`
- Create: `toast-etl/tests/validation/test_retention.py`
- Add a daily GH Actions cron workflow `.github/workflows/validation-pruner.yml`

- [ ] **Step 1: Write the failing test**

Write `toast-etl/tests/validation/test_retention.py`:

```python
"""Tests for validation file retention pruner."""
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from validation.retention import prune_old_validation_files


def _make_file(p, age_days):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ran_at": "x"}))
    age = datetime.now(timezone.utc) - timedelta(days=age_days)
    import os, time
    ts = age.timestamp()
    os.utime(p, (ts, ts))


def test_prunes_files_older_than_30_days(tmp_path):
    val = tmp_path / "_validation"
    _make_file(val / "src1_old.json", 40)
    _make_file(val / "src2_keep.json", 10)
    _make_file(val / "src3_keep.json", 29)
    removed = prune_old_validation_files(tmp_path, keep_days=30)
    assert removed == 1
    assert not (val / "src1_old.json").exists()
    assert (val / "src2_keep.json").exists()
    assert (val / "src3_keep.json").exists()


def test_idempotent_when_nothing_to_prune(tmp_path):
    val = tmp_path / "_validation"
    _make_file(val / "fresh.json", 1)
    assert prune_old_validation_files(tmp_path, keep_days=30) == 0
```

- [ ] **Step 2: Run tests; verify they fail**

```bash
cd toast-etl && pytest tests/validation/test_retention.py -v
```

- [ ] **Step 3: Implement the pruner**

Write `toast-etl/validation/retention.py`:

```python
"""Retention pruner for data/_validation/ and data/_validation_errors/."""
from __future__ import annotations

import os
import time
from pathlib import Path


def prune_old_validation_files(data_dir: Path, keep_days: int = 30) -> int:
    """Remove files older than keep_days from _validation/ and
    _validation_errors/. Returns count removed."""
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for sub in ("_validation", "_validation_errors"):
        d = data_dir / sub
        if not d.exists():
            continue
        for f in d.glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    return removed


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--keep-days", type=int, default=30)
    args = ap.parse_args(argv)
    n = prune_old_validation_files(Path(args.data_dir), args.keep_days)
    print(f"pruned {n} files older than {args.keep_days} days")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests; verify they pass**

```bash
cd toast-etl && pytest tests/validation/test_retention.py -v
```

- [ ] **Step 5: Add the GH Actions cron**

Write `.github/workflows/validation-pruner.yml`:

```yaml
name: Validation file pruner
on:
  schedule:
    - cron: '0 5 * * *'  # 05:00 UTC daily
  workflow_dispatch:
permissions:
  contents: write
concurrency:
  group: data-sync   # uses the shared queue so it doesn't race other writers
  cancel-in-progress: false
jobs:
  prune:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          token: ${{ secrets.GITHUB_TOKEN }}
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Run pruner
        working-directory: toast-etl
        run: python3 validation/retention.py --data-dir ../data --keep-days 30
      - name: Commit
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/_validation/ data/_validation_errors/ || true
          if git diff --staged --quiet; then
            echo "no files to prune"
            exit 0
          fi
          git commit -m "chore(validation): prune files older than 30 days"
          git push
```

- [ ] **Step 6: Commit**

```bash
git add toast-etl/validation/retention.py toast-etl/tests/validation/test_retention.py .github/workflows/validation-pruner.yml
git commit -m "feat(validation): retention pruner + daily 05:00 UTC cron"
```

---

### Task 29: End-to-end smoke test

**Files:**
- Create: `tests/e2e/test_validation_pipeline.py`

- [ ] **Step 1: Write the e2e test**

Write `tests/e2e/test_validation_pipeline.py`:

```python
"""End-to-end smoke test of the validation pipeline.

Simulates: a sync writes some good and bad rows, validation runner
emits a _validation/ summary file, the dashboard's _validation_index
on the outlet payload reflects the run.

Does NOT exercise the Edge Function (that's covered by manual smoke
in Tasks 18-25). Just verifies the data-side contract end-to-end.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "toast-etl"))

from schemas.toast_order import ToastOrder
from validation.runner import run_validation


def test_full_pipeline(tmp_path):
    # Set up a fake outlet payload
    outlet_dir = tmp_path / "data"
    outlet_dir.mkdir()
    outlet_path = outlet_dir / "smoketown.json"
    outlet_path.write_text(json.dumps({
        "outlet_id": "smoketown",
        "order_details": {"main": {"daily": []}},
    }))

    # Synthesize 3 good orders + 1 bad
    raw_orders = [
        {"guid": f"g-{i}", "voided": False, "deleted": False,
         "openedDate": "2026-05-04T19:00:00Z",
         "closedDate": "2026-05-04T20:00:00Z",
         "numberOfGuests": 2,
         "checks": [{"guid": f"c-{i}", "voided": False, "amount": 50,
                     "tipAmount": 10,
                     "openedDate": "2026-05-04T19:00:00Z",
                     "paidDate": "2026-05-04T20:00:00Z"}]}
        for i in range(3)
    ] + [
        {"guid": "bad", "voided": False, "deleted": False,
         "openedDate": "2026-05-04T19:00:00Z",
         "checks": [{"guid": "bad-c", "voided": False, "amount": -5,  # negative!
                     "tipAmount": 0, "openedDate": "2026-05-04T19:00:00Z"}]}
    ]

    out = run_validation(
        rows=raw_orders, model_cls=ToastOrder, source="toast_order",
        outlets_touched=["smoketown"], data_dir=outlet_dir,
    )

    # Validation summary file exists
    summary_files = list((outlet_dir / "_validation").glob("toast_order_*.json"))
    assert len(summary_files) == 1
    summary = json.loads(summary_files[0].read_text())
    assert summary["rows_in"] == 4
    assert summary["rows_valid"] == 3
    assert summary["rows_invalid"] == 1

    # Validation errors file exists with redacted samples
    err_files = list((outlet_dir / "_validation_errors").glob("toast_order_*.json"))
    assert len(err_files) == 1

    # Outlet payload now has _validation_index
    payload = json.loads(outlet_path.read_text())
    assert "_validation_index" in payload
    idx = payload["_validation_index"]["toast_order"]
    assert idx["rows_valid"] == 3
    assert idx["rows_invalid"] == 1

    # Caller can still use valid_rows
    assert len(out["valid_rows"]) == 3
```

- [ ] **Step 2: Run + verify pass**

```bash
mkdir -p tests/e2e && touch tests/e2e/__init__.py
pytest tests/e2e/test_validation_pipeline.py -v
```
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/__init__.py tests/e2e/test_validation_pipeline.py
git commit -m "test(e2e): full validation pipeline smoke (sync → runner → outlet index)"
```

---

## Self-review checklist (after writing — completed)

**1. Spec coverage:**
- ✅ Goal #1 (wrong financials don't display) → Task 16 (metric_classes.yml hard_fail) + Task 25 (banner)
- ✅ Goal #2 (soft signals annotate) → Task 16 (annotate class) + Task 17 (panel UI)
- ✅ Goal #3 (transient self-heal) → Task 23 (retry agent)
- ✅ Goal #4 (Slack alerts) → Task 24 (alert dispatcher)
- ✅ Goal #5 (validation panel) → Task 17
- ✅ Goal #6 (audit log) → Task 20
- ✅ Component 1 (Pydantic schemas) → Tasks 2-8
- ✅ Component 2 (validation status output) → Task 9
- ✅ Component 3 (drift detector) → Task 21
- ✅ Component 4 (anomaly detector) → Task 22
- ✅ Component 5 (retry/repair) → Task 23
- ✅ Component 6 (alert dispatcher) → Task 24
- ✅ Component 7 (validation panel) → Task 17
- ✅ Component 8 (audit log) → Task 20
- ✅ PII redaction → Task 27
- ✅ Validation file retention → Task 28
- ✅ E2E smoke → Task 29

**2. Placeholder scan:** No "TBD" / "implement later" / "appropriate error handling" / "similar to Task N" found.

**3. Type consistency:**
- `run_validation()` signature consistent in Tasks 9, 10, 11, 12, 13, 14, 15, 17 (added `update_outlet_index`)
- `ValidationSummary` interface in TS lib/types.ts matches the Python writer's output keys
- `BannerState` interface in TS used by both writer (Task 25) and consumer (dashboard JS in Task 17 / 25)
- Slack channel ID `C0B1N51L9TN` consistent across spec and Tasks 24-25
- Method `validate_business_rules()` consistent across all 7 schema models

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-04-trustworthy-reporting-engine-phase-a1.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — Dispatch a fresh subagent per task, review between tasks, fast iteration. Recommended because the plan spans 29 tasks across Python + TypeScript + SQL + YAML — a fresh context per task keeps each subagent focused.

**2. Inline Execution** — Execute tasks in this session using executing-plans skill, batch execution with checkpoints.

Which approach?
