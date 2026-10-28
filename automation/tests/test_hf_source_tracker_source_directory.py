import json
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


def test_source_directory_map_cards_open_the_same_details_and_show_next_action():
    app = APP.read_text(encoding="utf-8")

    assert 'class="map-source-row" data-source-index=' in app
    assert "bindSourceOpeners($(\"#map-country-detail\"))" in app
    assert "bindSourceOpeners(root)" in app
    assert "row.next_action || conciseSourceState(row)" in app

