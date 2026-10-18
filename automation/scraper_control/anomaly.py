from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class CountAnomaly:
    index: int
    value: int
    baseline: float
    robust_z: float
    direction: str


def robust_count_anomalies(values: Sequence[int], threshold: float = 4.5, minimum_history: int = 6) -> List[CountAnomaly]:
    """Flag a point against prior history using a median absolute deviation score."""
    anomalies: List[CountAnomaly] = []
    for index in range(minimum_history, len(values)):
        history = list(values[max(0, index - 24) : index])
        median = statistics.median(history)
        deviations = [abs(item - median) for item in history]
        mad = statistics.median(deviations)
        if mad == 0:
            # A small stable series should still flag a material jump, without dividing by zero.
            score = 0.0 if values[index] == median else abs(values[index] - median) / max(1.0, median * 0.1)
        else:
            score = 0.6745 * abs(values[index] - median) / mad
        if score >= threshold:
            anomalies.append(CountAnomaly(index, values[index], float(median), float(score), "spike" if values[index] > median else "drop"))
    return anomalies


def concentration_ratio(counts: Sequence[int]) -> float:
    total = sum(counts)
    return max(counts) / total if total else 0.0

