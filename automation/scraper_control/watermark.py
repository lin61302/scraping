from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class SourceGate:
    source_id: str
    requested_start: date
    requested_end: date
    discovery_through: Optional[date]
    parsed_through: Optional[date]
    validated_through: Optional[date]
    committed_through: Optional[date]
    postcheck_through: Optional[date]
    coverage_bounded: bool
    blocking_issues: int
    quarantined_in_window: int
    anomaly_gate_passed: bool
    required: bool = True


@dataclass(frozen=True)
class Watermark:
    safe_through: Optional[date]
    confidence: str
    reasons: List[str]


def source_watermark(gate: SourceGate) -> Watermark:
    reasons: List[str] = []
    milestones = (
        gate.discovery_through,
        gate.parsed_through,
        gate.validated_through,
        gate.committed_through,
        gate.postcheck_through,
    )
    if any(value is None for value in milestones):
        reasons.append("incomplete_stage_watermark")
        return Watermark(None, "unknown", reasons)
    safe = min(value for value in milestones if value is not None)
    if not gate.coverage_bounded:
        reasons.append("coverage_unbounded")
    if gate.blocking_issues:
        reasons.append("blocking_issues")
    if gate.quarantined_in_window:
        reasons.append("quarantined_articles")
    if not gate.anomaly_gate_passed:
        reasons.append("anomaly_gate_failed")
    if safe < gate.requested_end:
        reasons.append("requested_end_not_reached")
    if reasons:
        confidence = "low" if safe >= gate.requested_start else "unknown"
    else:
        confidence = "high"
    return Watermark(safe, confidence, reasons)


def country_watermark(gates: Iterable[SourceGate]) -> Watermark:
    required = [source_watermark(gate) for gate in gates if gate.required]
    if not required:
        return Watermark(None, "unknown", ["no_required_sources"])
    if any(item.safe_through is None for item in required):
        return Watermark(None, "unknown", ["required_source_unknown"])
    safe = min(item.safe_through for item in required if item.safe_through is not None)
    reasons = sorted({reason for item in required for reason in item.reasons})
    confidence = "high" if all(item.confidence == "high" for item in required) else "low"
    return Watermark(safe, confidence, reasons)

