"""Admission invariants for the bounded local ten-country observation wave."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

import pytest

AUTOMATION_ROOT = Path(__file__).resolve().parents[1]
if str(AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_ROOT))

from adapters.COD import sevensurseven
from adapters.ECU import eluniverso
from adapters.LBR import liberianinvestigator
from adapters.MWI import malawi247
from adapters.NGA import guardian
from adapters.PAN import newsroompanama
from adapters.PER import elcomercio
from adapters.SLB import sibconline
from adapters.SSD import eyeradio
from adapters.UGA import eagle


@dataclass(frozen=True)
class Case:
    source_id: str
    module: object


CASES = (
    Case("mlp:LBR:liberianinvestigator.com", liberianinvestigator),
    Case("mlp:MWI:247malawi.com", malawi247),
    Case("mlp:PAN:newsroompanama.com", newsroompanama),
    Case("mlp:SLB:sibconline.com.sb", sibconline),
    Case("mlp:SSD:eyeradio.org", eyeradio),
    Case("mlp:UGA:eagle.co.ug", eagle),
    Case("mlp:COD:7sur7.cd", sevensurseven),
    Case("mlp:ECU:eluniverso.com", eluniverso),
    Case("mlp:NGA:guardian.ng", guardian),
    Case("mlp:PER:elcomercio.pe", elcomercio),
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.source_id)
def test_bounded_observation_is_admitted_only_with_controller_opt_in(monkeypatch, case):
    module = case.module
    monkeypatch.setenv("SCRAPER_BOUNDED_OBSERVATION", "1")
    assert module.Config.observation_ready is True
    assert module.OBSERVATION_READY is True
    assert module.Config.launch_ready is False
    assert module.Config.production_writes is False
    assert module.PRODUCTION_WRITES is False
    assert module.BOUNDED_ROUTE_INVENTORY is True
    assert all(module._observation_gates().values())
    assert not all(module._gates().values())

    evidence = {}
    module._require_live_gates(evidence)

    assert evidence["admission"] == "bounded_pure_observation"
    assert evidence["observation_ready"] is True
    assert evidence["launch_ready"] is False
    assert evidence["promotion_eligible"] is False
    assert evidence["production_gates_satisfied"] is False
    assert evidence["production_writes"] is False
    assert evidence["first_pass_verified_through"] is None
    assert evidence["strict_verified_through"] is None


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.source_id)
def test_observation_remains_fail_closed_without_controller_opt_in(case):
    with pytest.raises(case.module.RouteProfileUnverifiedError):
        case.module._require_live_gates({})


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.source_id)
@pytest.mark.parametrize(
    "field",
    (
        "OBSERVATION_READY",
        "BOUNDED_ROUTE_INVENTORY",
        "ARTICLE_PROFILE_APPROVED",
        "ROBOTS_POLICY_REVIEWED",
        "PRODUCTION_WRITES",
    ),
)
def test_observation_admission_fails_closed_when_an_invariant_changes(
    monkeypatch, case, field
):
    module = case.module
    monkeypatch.setenv("SCRAPER_BOUNDED_OBSERVATION", "1")
    value = True if field == "PRODUCTION_WRITES" else False
    monkeypatch.setattr(module, field, value)
    evidence = {}

    with pytest.raises(module.RouteProfileUnverifiedError):
        module._require_live_gates(evidence)

    assert evidence["mode"] == "blocked_prelaunch_no_production_writes"
    assert evidence["coverage_complete"] is False


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.source_id)
def test_config_write_flag_also_closes_observation_admission(monkeypatch, case):
    module = case.module
    monkeypatch.setenv("SCRAPER_BOUNDED_OBSERVATION", "1")
    monkeypatch.setattr(module.Config, "production_writes", True)

    with pytest.raises(module.RouteProfileUnverifiedError):
        module._require_live_gates({})
