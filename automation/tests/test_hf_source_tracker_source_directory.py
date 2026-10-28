import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPACE = ROOT / "automation" / "hf_source_tracker_space"
APP = SPACE / "app.js"
SNAPSHOT = SPACE / "data" / "tracker_snapshot.json"


def test_source_directory_uses_current_safe_snapshot_positions():
    # The public snapshot stays in the Space checkout and is intentionally not
    # copied into the GitHub code provenance archive.
    if not SNAPSHOT.exists():
        return
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    positions = Counter(row.get("first_pass_state") or "Not inspected" for row in data["sources"])

    assert len(data["sources"]) == 653
    assert positions == Counter(data["summary"]["first_pass_state"])
    assert set(positions) == {
        "Blocked",
        "Complete through target",
        "Inspected; verification pending",
        "Not inspected",
        "Verified through earlier boundary",
    }


def test_source_directory_exposes_position_boundaries_action_and_drawer():
    app = APP.read_text(encoding="utf-8")

    for required in (
        "Every source, one clear position",
        "source-position-band",
        "Where it stands",
        "Recorded Notion",
        "Operational through",
        "First-pass verified",
        "Strict verified",
        "Attention and next action",
        "source-page-size",
        "detail-backdrop",
        "aria-modal",
        'tabindex="0"',
        "Open full details",
    ):
        assert required in app

    assert 'sourceButton = $(\'.tab[data-tab="sources"]\')' in app
    assert 'button.dataset.tab === "sources"' in app
    assert 'panel.id === "tab-sources"' in app


def test_source_position_order_is_deterministic_in_javascript():
    node = shutil.which("node")
    if not node:
        return

    script = f"""
global.window = {{
  TRACKER_DATA: {{sources: [], countries: []}},
  SOURCE_TRACKER_TEST_MODE: true
}};
require({json.dumps(str(APP))});
const api = window.SOURCE_TRACKER_TEST_API;
const rows = [
  {{first_pass_state: 'Complete through target'}},
  {{first_pass_state: 'Not inspected'}},
  {{first_pass_state: 'Verified through earlier boundary'}},
  {{first_pass_state: 'Inspected; verification pending'}},
  {{first_pass_state: 'Blocked'}},
  {{}},
];
process.stdout.write(JSON.stringify({{
  order: api.sourcePositionOrder,
  positions: rows.map(api.sourcePosition),
  ranks: rows.map(api.sourcePositionRank),
  displayed: rows.map(row => api.displaySourcePosition(api.sourcePosition(row))),
}}));
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        text=True,
        capture_output=True,
    )
    result = json.loads(completed.stdout)

    assert result["order"] == [
        "Blocked",
        "Inspected; verification pending",
        "Verified through earlier boundary",
        "Not inspected",
        "Complete through target",
    ]
    assert result["positions"] == [
        "Complete through target",
        "Not inspected",
        "Verified through earlier boundary",
        "Inspected; verification pending",
        "Blocked",
        "Not inspected",
    ]
    assert result["ranks"] == [4, 3, 2, 1, 0, 3]
    assert result["displayed"] == [
        "Complete through target",
        "Not inspected",
        "Verified through earlier boundary",
        "Inspected; agent verification queued",
        "Blocked",
        "Not inspected",
    ]


def test_source_directory_map_cards_open_the_same_details_and_show_next_action():
    app = APP.read_text(encoding="utf-8")

    assert 'class="map-source-row" data-source-index=' in app
    assert "bindSourceOpeners($(\"#map-country-detail\"))" in app
    assert "bindSourceOpeners(root)" in app
    assert "agentQueueAction(row) || conciseSourceState(row)" in app


def test_source_count_series_are_monthly_aggregates_with_truthful_coverage():
    if not SNAPSHOT.exists():
        return
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    series = data["count_series"]

    assert data["meta"]["snapshot_version"] >= 4
    assert len(series) == data["meta"]["count_series_source_count"]
    assert len(series) >= 600
    assert data["meta"]["scrape_count_series_source_count"] >= 200
    assert data["meta"]["tcg_count_series_source_count"] >= 600

    scrape_sources = 0
    tcg_sources = 0
    for key, item in series.items():
        assert re.fullmatch(r"(?:MLP|MLEED)\|[a-z0-9._-]+", key)
        assert 1 <= len(item["points"]) <= 18
        months = [point[0] for point in item["points"]]
        assert months == sorted(set(months))
        assert item["through"] == months[-1]
        for month, scraped, tcg_done in item["points"]:
            assert re.fullmatch(r"\d{4}-\d{2}", month)
            assert scraped is None or isinstance(scraped, int) and scraped >= 0
            assert tcg_done is None or isinstance(tcg_done, int) and tcg_done >= 0
        scrape_sources += any(point[1] is not None for point in item["points"])
        tcg_sources += any(point[2] is not None for point in item["points"])

    assert scrape_sources == data["meta"]["scrape_count_series_source_count"]
    assert tcg_sources == data["meta"]["tcg_count_series_source_count"]


def test_source_drawer_renders_count_chart_or_pending_state_and_agent_queue():
    app = APP.read_text(encoding="utf-8")

    for required in (
        "Monthly source counts",
        "Scraped is the recorded source input volume",
        "TCG done is the downstream filtered article count",
        "Monthly count series pending",
        "Source work continues autonomously while aggregate data is prepared",
        "Agent queue / next action",
        "Agent follow-up",
        "Inspected; agent verification queued",
        "countSeriesChart(r)",
    ):
        assert required in app

    assert '$$("#filter-attention option").forEach' in app
    assert "option.textContent = displayAttentionState(option.value)" in app
    assert '$$("#filter-first-pass option").forEach' in app
    assert "option.textContent = displaySourcePosition(option.value)" in app


def test_count_chart_and_agent_queue_helpers_are_deterministic_in_javascript():
    node = shutil.which("node")
    if not node:
        return

    script = f"""
global.window = {{
  TRACKER_DATA: {{
    sources: [], countries: [],
    count_series: {{
      'MLEED|example.test': {{
        scrape_as_of: '2026-10-15', tcg_as_of: '2026-10-15',
        scrape_basis: 'Downstream source input snapshot',
        tcg_basis: 'Downstream filtered count snapshot', through: '2026-09',
        points: [['2026-08', 10, 4], ['2026-09', 12, 5]],
      }},
    }},
  }},
  SOURCE_TRACKER_TEST_MODE: true
}};
require({json.dumps(str(APP))});
const api = window.SOURCE_TRACKER_TEST_API;
const row = {{project:'MLEED', source:'example.test', next_action:'Review and verify output', attention_state:'Review'}};
const missing = {{project:'MLP', source:'missing.test', next_action:'Run counts', attention_state:'Backlog'}};
const legacyHumanGate = {{next_action:'Wait for user approval and human check', attention_state:'Review'}};
const legacyNarrative = {{
  first_pass_state:'Inspected; verification pending',
  first_pass_note:'Orchestrator task status was needs_review; verification remains pending',
  attention_state:'Review', needs_revisit:'Needs quick check',
  problem_summary:'Classified for review', next_action:'Wait for user approval',
}};
process.stdout.write(JSON.stringify({{
  key: api.sourceCountKey(row),
  found: api.sourceCountSeries(row).through,
  chart: api.countSeriesChart(row),
  empty: api.countSeriesChart(missing),
  attention: api.displayAttentionState('Review'),
  position: api.displaySourcePosition('Inspected; verification pending'),
  action: api.agentQueueAction(row),
  migratedAction: api.agentQueueAction(legacyHumanGate),
  narrative: api.displayNarrative(legacyNarrative.first_pass_note),
  csv: {{
    firstPass: api.csvExportValue(legacyNarrative, 'first_pass_state'),
    note: api.csvExportValue(legacyNarrative, 'first_pass_note'),
    attention: api.csvExportValue(legacyNarrative, 'attention_state'),
    revisit: api.csvExportValue(legacyNarrative, 'needs_revisit'),
    problem: api.csvExportValue(legacyNarrative, 'problem_summary'),
    action: api.csvExportValue(legacyNarrative, 'next_action'),
  }},
}}));
"""
    completed = subprocess.run(
        [node, "-e", script], check=True, text=True, capture_output=True
    )
    result = json.loads(completed.stdout)

    assert result["key"] == "MLEED|example.test"
    assert result["found"] == "2026-09"
    assert "count-line scraped" in result["chart"]
    assert "count-line tcg" in result["chart"]
    assert "Monthly count series pending" in result["empty"]
    assert result["attention"] == "Agent follow-up"
    assert result["position"] == "Inspected; agent verification queued"
    assert result["action"].startswith("Agent queue ·")
    assert "user" not in result["migratedAction"].lower()
    assert "human" not in result["migratedAction"].lower()
    assert "agent" in result["migratedAction"].lower()
    rendered = json.dumps({"narrative": result["narrative"], "csv": result["csv"]}).lower()
    for legacy in ("needs_review", "verification pending", "verification remains pending", "classified for review", "needs quick check", "user approval"):
        assert legacy not in rendered
    assert result["csv"]["firstPass"] == "Inspected; agent verification queued"
    assert result["csv"]["attention"] == "Agent follow-up"
    assert "agent" in rendered

