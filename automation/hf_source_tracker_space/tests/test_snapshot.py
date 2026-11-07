import json, re, subprocess, sys, tempfile, unittest
from datetime import date
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
from export_safe_snapshot import (
    ALLOWED_COUNTRY_KEYS,
    ALLOWED_ROW_KEYS,
    FIRST_PASS_COUNTRY_KEYS,
    FIRST_PASS_ROW_KEYS,
    FORBIDDEN_OUTPUT_PATTERNS,
    REQUIRED_COUNTRY_KEYS,
    REQUIRED_ROW_KEYS,
    apply_public_operations,
    build_country_rows,
    build_source_count_series,
    export_row,
    load_public_operations,
    pipeline_state,
    validate_public_operations,
    validate_export,
)

class SnapshotTests(unittest.TestCase):
    def test_space_metadata_uses_supported_colors(self):
        readme=(ROOT/"README.md").read_text(encoding="utf-8")
        allowed={"red","yellow","green","blue","indigo","purple","pink","gray"}
        frontmatter=readme.split("---",2)[1]
        values={line.split(":",1)[0].strip():line.split(":",1)[1].strip() for line in frontmatter.splitlines() if ":" in line}
        self.assertIn(values.get("colorFrom"),allowed)
        self.assertIn(values.get("colorTo"),allowed)

    @classmethod
    def setUpClass(cls): cls.data=json.loads((ROOT/"data"/"tracker_snapshot.json").read_text(encoding="utf-8"))
    def test_schema_and_counts(self): validate_export(self.data); self.assertEqual(len(self.data["sources"]),self.data["meta"]["source_count"])
    def test_exact_row_allowlist(self):
        self.assertTrue(self.data["sources"])
        self.assertTrue(
            all(
                REQUIRED_ROW_KEYS <= set(row) <= ALLOWED_ROW_KEYS
                and (not (set(row) & FIRST_PASS_ROW_KEYS) or FIRST_PASS_ROW_KEYS <= set(row))
                for row in self.data["sources"]
            )
        )
    def test_exact_country_allowlist(self):
        self.assertTrue(self.data["countries"])
        self.assertTrue(
            all(
                REQUIRED_COUNTRY_KEYS <= set(row) <= ALLOWED_COUNTRY_KEYS
                and (not (set(row) & FIRST_PASS_COUNTRY_KEYS) or FIRST_PASS_COUNTRY_KEYS <= set(row))
                for row in self.data["countries"]
            )
        )
    def test_no_sensitive_patterns(self):
        raw=json.dumps(self.data,ensure_ascii=False)
        self.assertFalse([(p.pattern,p.search(raw).group(0)) for p in FORBIDDEN_OUTPUT_PATTERNS if p.search(raw)])

    def test_all_tracked_text_assets_clear_the_privacy_gate(self):
        tracked=subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-files"], text=True
        ).splitlines()
        findings=[]
        for relative in tracked:
            path=ROOT/relative
            try:
                raw=path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for pattern in FORBIDDEN_OUTPUT_PATTERNS:
                match=pattern.search(raw)
                if match:
                    findings.append((relative, pattern.pattern, match.group(0)))
        self.assertFalse(findings)
    def test_js_matches_json(self):
        raw=(ROOT/"data"/"tracker_snapshot.js").read_text(encoding="utf-8").strip()
        self.assertTrue(raw.startswith("window.TRACKER_DATA=") and raw.endswith(";"))
        self.assertEqual(json.loads(raw[len("window.TRACKER_DATA="):-1]),self.data)
    def test_static_assets_reference_safe_snapshot(self):
        html=(ROOT/"index.html").read_text(encoding="utf-8")
        self.assertIn('data/tracker_snapshot.js',html);self.assertIn('data/world_110m.js',html);self.assertIn('app.js',html);self.assertNotRegex(html,r'https?://')
    def test_attention_copy_is_agent_owned(self):
        html=(ROOT/"index.html").read_text(encoding="utf-8")
        self.assertIn("Agent work queue",html)
        self.assertIn("Blockers and follow-ups",html)
        self.assertNotIn("Decision needed",html)
        self.assertNotIn("Blockers and reviews",html)
    def test_map_geometry_covers_all_national_countries(self):
        raw=(ROOT/"data"/"world_110m.js").read_text(encoding="utf-8").strip()
        self.assertTrue(raw.startswith("window.WORLD_GEOMETRY=") and raw.endswith(";"))
        geometry=json.loads(raw[len("window.WORLD_GEOMETRY="):-1])
        self.assertGreaterEqual(len(geometry),170)
        self.assertTrue(all(set(feature)=={"id","name","type","coordinates"} for feature in geometry))
        geometry_codes={feature["id"] for feature in geometry}
        national_codes={row["country"] for row in self.data["countries"]}-{"INT","REG"}
        self.assertFalse(national_codes-geometry_codes)
    def test_map_ui_is_accessible_and_keeps_shared_sources_separate(self):
        html=(ROOT/"index.html").read_text(encoding="utf-8")
        app=(ROOT/"app.js").read_text(encoding="utf-8")
        self.assertIn('data-tab="map"',html)
        self.assertIn('id="map-country-select"',html)
        self.assertIn('id="shared-source-groups"',html)
        self.assertIn('path.setAttribute("tabindex", "0")',app)
        self.assertIn('event.key === "Enter" || event.key === " "',app)

    def test_operational_boundary_is_primary_and_strict_verification_is_secondary(self):
        html=(ROOT/"index.html").read_text(encoding="utf-8")
        app=(ROOT/"app.js").read_text(encoding="utf-8")
        country_header=html.index('<th data-sort="operational_through">Operational through</th>')
        verified_header=html.index('<th data-sort="verified_through">Strict verified through</th>')
        self.assertLess(country_header, verified_header)
        source_card=app.index('<b>Operational through</b>')
        source_verified=app.index('<b>Verified through</b>')
        self.assertLess(source_card, source_verified)
        self.assertIn("confidence ${esc(operational.countLabel)}", app)

    def test_release_has_basic_records_and_expected_cycle_evidence(self):
        self.assertEqual(self.data["meta"]["source_count"], 653)
        self.assertEqual(self.data["meta"]["national_country_count"], 71)
        self.assertTrue(all(row["scrape_record_through"] for row in self.data["sources"]))
        self.assertTrue(all(row["boundary_check"] for row in self.data["sources"]))
        self.assertGreaterEqual(
            sum(bool(row["tcg_record_through"]) for row in self.data["sources"]),
            631,
        )
        future_tcg=next(
            row for row in self.data["sources"]
            if row["country"]=="SEN" and row["source"]=="iedafrique.org"
        )
        self.assertEqual(future_tcg["tcg_record_through"], "")

        countries={row["country"]: row for row in self.data["countries"]}
        if countries["PHL"]["cycle_coverage_sources"] < countries["PHL"]["cycle_coverage_total"]:
            self.assertEqual(countries["PHL"]["safe_through"], "Open")
        else:
            self.assertGreaterEqual(countries["PHL"]["safe_through"], "2026-08-31")
        self.assertGreaterEqual(countries["MYS"]["safe_through"], "2024-07-08")
        self.assertGreaterEqual(countries["BRA"]["safe_through"], "2026-08-31")
        for code, covered, total in (("PHL", 8, 9), ("ZWE", 11, 12), ("MLI", 7, 9)):
            with self.subTest(country=code):
                self.assertEqual(countries[code]["cycle_coverage_total"], total)
                self.assertGreaterEqual(countries[code]["cycle_coverage_sources"], covered)

    def test_coverage_dates_and_counts_are_well_formed(self):
        def assert_iso(value):
            date.fromisoformat(value)

        for row in self.data["sources"]:
            assert_iso(row["scrape_record_through"])
            for key in ("scrape_verified_through", "tcg_record_through"):
                if row[key]:
                    assert_iso(row[key])

        for row in self.data["countries"]:
            if row["safe_through"] != "Open":
                assert_iso(row["safe_through"])
            self.assertGreaterEqual(row["cycle_coverage_total"], row["cycle_coverage_sources"])
            self.assertEqual(row["cycle_coverage_total"], row["sources"])
            expected=round(100 * row["cycle_coverage_sources"] / row["sources"], 1)
            self.assertEqual(row["cycle_coverage_pct"], expected)
            if row["safe_through"] == "Open":
                self.assertLess(row["coverage_recorded"], row["sources"])

    def test_release_has_privacy_safe_source_count_series(self):
        series=self.data["count_series"]
        self.assertEqual(len(series),self.data["meta"]["count_series_source_count"])
        self.assertGreaterEqual(len(series),600)
        self.assertGreaterEqual(self.data["meta"]["scrape_count_series_source_count"],200)
        self.assertGreaterEqual(self.data["meta"]["tcg_count_series_source_count"],600)
        for key,item in series.items():
            self.assertRegex(key,r"^(?:MLP|MLEED)\|[a-z0-9._-]+$")
            self.assertLessEqual(len(item["points"]),18)
            months=[point[0] for point in item["points"]]
            self.assertEqual(months,sorted(set(months)))
            self.assertEqual(item["through"],months[-1])
            for month,scraped,tcg_done in item["points"]:
                self.assertRegex(month,r"^\d{4}-\d{2}$")
                self.assertTrue(scraped is None or isinstance(scraped,int) and scraped>=0)
                self.assertTrue(tcg_done is None or isinstance(tcg_done,int) and tcg_done>=0)

    def test_count_series_builder_uses_complete_months_and_aggregate_counts_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            civic=root/"civic"/"Country"/"2026_10_15"
            env=root/"env"/"Environmental Country"/"2026_10_15"
            metrics=root/"metrics"
            civic.mkdir(parents=True);env.mkdir(parents=True);metrics.mkdir()
            (civic/"civic.test.csv").write_text(
                "date,total_articles\n2026-08-31,4\n2026-09-30,5\n2026-10-31,99\n",
                encoding="utf-8",
            )
            (env/"env.test.csv").write_text(
                "date,total_articles,total_local_docs\n2026-08-01,3,12\n2026-09-01,4,20\n2026-10-01,99,99\n",
                encoding="utf-8",
            )
            (civic/"shared.test.csv").write_text(
                "date,total_articles\n2026-09-30,2\n", encoding="utf-8"
            )
            (env/"shared.test.csv").write_text(
                "date,total_articles\n2026-09-30,3\n", encoding="utf-8"
            )
            (metrics/"saved_metrics.json").write_text(json.dumps({
                "captured_at_utc":"2026-10-10T00:00:00Z",
                "daily":[
                    {"source":"civic.test","date":"2026-09-01","rows":2},
                    {"source":"civic.test","date":"2026-09-02","rows":5},
                    {"source":"civic.test","date":"2026-10-01","rows":100},
                    {"source":"shared.test","date":"2026-09-01","rows":50},
                ],
            }),encoding="utf-8")
            built=build_source_count_series([
                {"Project":"MLP","Source":"civic.test"},
                {"Project":"MLEED","Source":"env.test"},
                {"Project":"MLP","Source":"shared.test"},
                {"Project":"MLEED","Source":"shared.test"},
            ],civic_root=root/"civic",env_root=root/"env",scrape_metrics_root=metrics)

        self.assertEqual(built["MLP|civic.test"]["points"],[
            ["2026-08",None,4],["2026-09",7,5]
        ])
        self.assertEqual(built["MLEED|env.test"]["points"],[
            ["2026-08",12,3],["2026-09",20,4]
        ])
        self.assertEqual(built["MLP|shared.test"]["points"],[["2026-09",None,2]])
        self.assertEqual(built["MLEED|shared.test"]["points"],[["2026-09",None,3]])
        self.assertNotIn(str(root),json.dumps(built))

    def test_future_legacy_record_is_held_out_without_changing_receipt_date(self):
        row={
            "Country":"TST", "Source":"example.test", "Project":"MLP",
            "Required from":"2026-08-19", "Required through":"2026-08-31",
            "Scrape record through":"2027-08-19",
            "Scrape record state":"Historical complete",
            "Scrape record basis":"Dated legacy source note",
            "Coverage through":"2026-08-31", "Scrape verified through":"2026-08-31",
            "Baseline status":"Done", "Problems identified":"",
            "Needs revisit":"No", "Next action":"", "Patchtools status":"Not assessed",
            "Pre-TCG status":"Not assessed", "Downstream ready":"No",
        }
        exported=export_row(row, as_of=date(2026,9,30))
        self.assertEqual(exported["scrape_record_through"], "2026-08-31")
        self.assertEqual(exported["scrape_verified_through"], "2026-08-31")
        self.assertEqual(exported["scrape_record_state"], "Review")
        self.assertEqual(exported["attention_state"], "Review")
        self.assertIn("future-dated legacy", exported["problem_summary"])

    def test_first_pass_separates_notion_history_from_receipt_verification(self):
        base={
            "Country":"TST", "Source":"example.test", "Project":"MLP",
            "Required from":"2025-04-30", "Required through":"2026-08-31",
            "Baseline status":"Unassessed", "Scraping progress":"Inventory only",
            "Coverage through":"", "Problems identified":"",
            "Needs revisit":"Unassessed", "Next action":"Run first inspection",
            "Patchtools status":"Not assessed", "Pre-TCG status":"Not assessed",
            "Downstream ready":"No", "Method":"Monthly sitemap",
            "Current evidence addressed":"No",
        }
        target=date(2026,9,30)
        untouched=export_row(base, as_of=date(2026,10,5), first_pass_target=target)
        self.assertEqual(untouched["recorded_notion_boundary"], "2025-04-30")
        self.assertEqual(untouched["first_pass_target"], "2026-09-30")
        self.assertEqual(untouched["first_pass_verified_through"], "")
        self.assertEqual(untouched["first_pass_state"], "Not inspected")
        self.assertEqual(untouched["current_month_partial_through"], "")

        inspected=export_row(
            {**base, "Current evidence addressed":"Yes", "Last checked":"2026-10-05"},
            as_of=date(2026,10,5),
            first_pass_target=target,
        )
        self.assertEqual(inspected["first_pass_state"], "Inspected; verification pending")

        earlier=export_row(
            {**base, "Coverage through":"2026-08-31", "Baseline status":"Done"},
            as_of=date(2026,10,5),
            first_pass_target=target,
        )
        self.assertEqual(earlier["first_pass_state"], "Verified through earlier boundary")
        self.assertEqual(earlier["first_pass_verified_through"], "2026-08-31")

        complete=export_row(
            {**base, "Coverage through":"2026-10-04", "Baseline status":"Done"},
            as_of=date(2026,10,5),
            first_pass_target=target,
        )
        self.assertEqual(complete["first_pass_state"], "Complete through target")
        self.assertEqual(complete["first_pass_verified_through"], "2026-10-04")
        self.assertEqual(complete["current_month_partial_through"], "2026-10-04")

    def test_current_month_partial_requires_receipt_backing(self):
        row={
            "Country":"TST", "Source":"example.test", "Project":"MLP",
            "Required from":"2025-04-30", "Required through":"2026-08-31",
            "Baseline status":"Open", "Scraping progress":"Inspected",
            "Coverage through":"2026-09-30",
            "Current-month partial through":"2026-10-05",
            "Problems identified":"", "Needs revisit":"No", "Next action":"",
            "Patchtools status":"Not assessed", "Pre-TCG status":"Not assessed",
            "Downstream ready":"No",
        }
        exported=export_row(
            row, as_of=date(2026,10,5), first_pass_target=date(2026,9,30)
        )
        self.assertEqual(exported["first_pass_state"], "Complete through target")
        self.assertEqual(exported["current_month_partial_through"], "")

    def test_first_pass_note_uses_the_existing_privacy_scrubber(self):
        row={
            "Country":"TST", "Source":"example.test", "Project":"MLP",
            "Required from":"2025-04-30", "Required through":"2026-08-31",
            "Baseline status":"Open", "Scraping progress":"Inspected",
            "Coverage through":"", "Problems identified":"",
            "Needs revisit":"Yes", "Next action":"Review",
            "Patchtools status":"Not assessed", "Pre-TCG status":"Not assessed",
            "Downstream ready":"No", "Method":"API",
            "First-pass note":(
                "password" + "=secret " + "/" + "home/operator/private.log"
            ),
        }
        exported=export_row(
            row, as_of=date(2026,10,5), first_pass_target=date(2026,9,30)
        )
        self.assertNotIn("secret", exported["first_pass_note"])
        self.assertNotIn("/" + "home/", exported["first_pass_note"])
        self.assertIn("removed", exported["first_pass_note"])

    def test_first_pass_note_scrubs_long_internal_hashes(self):
        internal_hash=(
            "3cf14c467cd9a318" + "bd733b74580cd0ac"
            + "b9078654d8322c58" + "010ca209c0b8a8f8"
        )
        row={
            "Country":"TST", "Source":"example.test", "Project":"MLP",
            "Required from":"2025-04-30", "Required through":"2026-08-31",
            "Baseline status":"Open", "Scraping progress":"Inspected",
            "Coverage through":"", "Problems identified":"",
            "Needs revisit":"Yes", "Next action":"Review",
            "Patchtools status":"Not assessed", "Pre-TCG status":"Not assessed",
            "Downstream ready":"No", "Method":"API",
            "First-pass note":f"Exact retry set {internal_hash}",
        }
        exported=export_row(
            row, as_of=date(2026,10,5), first_pass_target=date(2026,9,30)
        )
        self.assertNotIn(internal_hash, exported["first_pass_note"])
        self.assertIn("internal id removed", exported["first_pass_note"])

    def test_release_holds_known_future_legacy_date_for_review(self):
        row=next(item for item in self.data["sources"] if item["country"]=="CMR" and item["source"]=="237online.com")
        self.assertEqual(row["scrape_record_through"], "2026-08-19")
        self.assertEqual(row["scrape_record_state"], "Review")
        self.assertEqual(row["attention_state"], "Review")
        self.assertIn("Future legacy date excluded", row["boundary_check"])

    def test_team_copy_avoids_promotional_and_retired_space_language(self):
        public_copy="\n".join(
            (ROOT/path).read_text(encoding="utf-8")
            for path in ("README.md", "index.html")
        ).lower()
        self.assertNotIn("spreadsheet hunt", public_copy)
        self.assertNotIn("spaces/zr/mlp-mleed-source-tracker", public_copy)

    def test_publisher_is_locked_to_the_zungru_space(self):
        publisher=(ROOT/"publish.sh").read_text(encoding="utf-8")
        self.assertIn(
            'DEFAULT_SPACE_HTTPS="https://huggingface.co/spaces/zungru/mlp-mleed-source-tracker"',
            publisher,
        )
        self.assertIn('REMOTE_NAME="origin"', publisher)
        self.assertNotIn("HF_GIT_REMOTE", publisher)
        self.assertNotIn("spaces/ZR/", publisher)

    def test_country_rollup_keeps_receipt_safe_cycle_and_history_separate(self):
        def row(index, coverage, *, baseline="Done", patch="Complete", pretcg="Complete"):
            return {
                "country": "TST", "source": f"source-{index}",
                "cycle_wave": "1", "required_from": "2026-06-01", "required_through": "2026-08-31",
                "coverage_through": coverage, "baseline_status": baseline,
                "patchtools_status": patch, "pretcg_status": pretcg,
                "downstream_ready": "No", "attention_state": "On track",
                "scrape_record_through": "2026-05-31", "scrape_record_state": "Complete",
                "scrape_record_basis": "Dated legacy source note",
                "tcg_record_through": "2026-04-30", "tcg_record_state": "Recorded complete",
            }

        rows = [row(i, "2026-08-31") for i in range(6)] + [
            row(
                6,
                "2026-06-30",
                baseline="Open",
                patch="Promotion complete; ZWE Patchtools pending",
                pretcg="Translation + geoparsing complete; classifiers blocked",
            )
        ]
        country = build_country_rows(rows)[0]
        self.assertEqual(country["safe_through"], "2026-06-30")
        self.assertEqual(country["later_coverage_from"], "2026-08-01")
        self.assertEqual(country["later_coverage_through"], "2026-08-31")
        self.assertEqual(country["later_coverage_sources"], 6)
        self.assertEqual(country["later_coverage_pct"], 85.7)
        self.assertEqual(country["cycle_coverage_state"], "mostly covered")
        self.assertEqual(country["recorded_scrape_floor"], "2026-05-31")
        self.assertEqual(country["recorded_tcg_floor"], "2026-04-30")
        self.assertEqual(country["patchtools_complete"], 6)
        self.assertEqual(country["patchtools_active"], 1)
        self.assertEqual(country["pretcg_complete"], 6)
        self.assertEqual(country["pretcg_partial"], 1)

    def test_country_rollup_does_not_infer_missing_strict_boundary(self):
        def row(index, coverage):
            return {
                "country": "TST", "source": f"source-{index}", "cycle_wave": "1",
                "required_from": "2026-06-01", "required_through": "2026-08-31",
                "coverage_through": coverage,
                "baseline_status": "Done" if coverage else "Unassessed",
                "patchtools_status": "Not assessed", "pretcg_status": "Not assessed",
                "downstream_ready": "No", "attention_state": "Backlog" if not coverage else "On track",
                "scrape_record_through": "2026-05-31", "scrape_record_state": "Complete",
                "scrape_record_basis": "Dated legacy source note",
                "tcg_record_through": "", "tcg_record_state": "Pending",
            }

        country = build_country_rows([row(i, "2026-08-31" if i < 6 else "") for i in range(7)])[0]
        self.assertEqual(country["safe_through"], "Open")
        self.assertEqual(country["coverage_recorded"], 6)
        self.assertEqual(country["later_coverage_from"], "2026-08-01")
        self.assertEqual(country["later_coverage_through"], "2026-08-31")
        self.assertEqual(country["later_coverage_sources"], 6)
        self.assertEqual(country["later_coverage_pct"], 85.7)
        self.assertEqual(country["cycle_coverage_state"], "mostly covered")
        self.assertEqual(country["recorded_scrape_floor"], "2026-05-31")

    def test_legacy_stage_fills_only_unassessed_pipeline_history(self):
        legacy = {
            "Baseline status": "Unassessed", "Patchtools status": "Not assessed",
            "Pre-TCG status": "Not assessed", "Downstream ready": "No",
            "Legacy record stage": "counts_generated",
        }
        self.assertEqual(pipeline_state(legacy), "Recorded TCG/count complete")
        pending = {
            **legacy,
            "Baseline status": "Done",
            "Patchtools status": "Promotion complete; ZWE Patchtools pending",
            "Legacy record stage": "scraped",
        }
        self.assertEqual(pipeline_state(pending), "Patchtools / promotion")

    def test_public_operations_overlay_has_current_stage_precedence(self):
        operations=load_public_operations(ROOT/"operations_status.public.json")
        validate_public_operations(operations)
        rows=[]
        for source_key in operations["sources"]:
            project,source=source_key.split("|",1)
            rows.append({
                "project":project,
                "source":source,
                "pipeline_state":"Recorded TCG/count complete",
                "attention_state":"On track",
                "next_action":"Historical action",
                "tcg_record_state":"Recorded complete",
                "tcg_record_through":"2026-08-31",
            })
        contracts,summary=apply_public_operations(rows,{},operations)
        by_source={row["source"]:row for row in rows}

        for source in ("newsroompanama.com","eyeradio.org","eagle.co.ug"):
            with self.subTest(source=source):
                row=by_source[source]
                self.assertEqual(row["pipeline_state"],"Exact repair launch failed before source work; agent remediation in progress")
                self.assertEqual(row["historical_pipeline_state"],"Recorded TCG/count complete")
                self.assertEqual(row["historical_tcg_state"],"Recorded complete")
                self.assertEqual(row["scrape_series_availability"],"Pending guarded promotion terminal receipt")
                self.assertEqual(row["tcg_series_availability"],"Pending current-scope TCG receipt")

        self.assertEqual(by_source["sibconline.com.sb"]["pipeline_state"],"Blocked — exact identity ledger unavailable")
        for source in ("7sur7.cd","liberianinvestigator.com","247malawi.com","eluniverso.com","elcomercio.pe"):
            self.assertEqual(by_source[source]["pipeline_state"],"Parked — exact residual ledger unavailable")
        self.assertEqual(by_source["csmonitor.com"]["pipeline_state"],"First-pass complete; production promotion pending")
        self.assertEqual(summary["latest_window"]["state"],"terminal_reconciled")
        self.assertEqual(summary["source_override_count"],10)
        self.assertEqual(set(contracts),{
            "MLP|newsroompanama.com","MLP|eyeradio.org","MLP|eagle.co.ug"
        })

    def test_current_scope_contracts_use_explicit_null_not_zero(self):
        operations=load_public_operations(ROOT/"operations_status.public.json")
        contracts=[
            operation["count_contract"]
            for operation in operations["sources"].values()
            if operation["count_contract"] is not None
        ]
        self.assertEqual(len(contracts),3)
        for contract in contracts:
            with self.subTest(scope=contract["scope_label"], start=contract["range_start"]):
                self.assertEqual(contract["publication_date_field"],"date_publish")
                self.assertRegex(contract["scope_hash"],r"^sha256-base64url:[A-Za-z0-9_-]{43}$")
                self.assertEqual([point[0] for point in contract["points"]],contract["months"])
                self.assertTrue(all(point[1:] == [None,None] for point in contract["points"]))
                self.assertEqual(contract["scrape_series_status"],"pending_promotion_terminal_receipt")
                self.assertEqual(contract["tcg_series_status"],"pending_tcg_receipt")

    def test_public_operations_overlay_allows_unrelated_shared_source_rows(self):
        operations=load_public_operations(ROOT/"operations_status.public.json")
        source_key="MLP|newsroompanama.com"
        operations={**operations,"sources":{source_key:operations["sources"][source_key]}}
        rows=[{
            "project":"MLP",
            "source":source,
            "country":country,
            "pipeline_state":"Recorded TCG/count complete",
            "attention_state":"On track",
            "next_action":"Historical action",
            "tcg_record_state":"Recorded complete",
            "tcg_record_through":"2026-08-31",
        } for country,source in (
            ("INT","elpais.com"),
            ("REG","elpais.com"),
            ("PAN","newsroompanama.com"),
        )]

        contracts,summary=apply_public_operations(rows,{},operations)

        self.assertEqual(summary["source_override_count"],1)
        self.assertEqual(set(contracts),{source_key})
        self.assertTrue(all(
            row["operational_state_source"]=="Canonical tracker"
            for row in rows[:2]
        ))
        self.assertEqual(
            rows[2]["pipeline_state"],
            "Exact repair launch failed before source work; agent remediation in progress",
        )

    def test_public_operations_overlay_rejects_ambiguous_shared_source_key(self):
        operations=load_public_operations(ROOT/"operations_status.public.json")
        operation=operations["sources"]["MLP|newsroompanama.com"]
        operations={**operations,"sources":{"MLP|elpais.com":operation}}
        rows=[{
            "project":"MLP",
            "source":"elpais.com",
            "country":country,
            "pipeline_state":"Recorded TCG/count complete",
            "attention_state":"On track",
            "next_action":"Historical action",
            "tcg_record_state":"Recorded complete",
            "tcg_record_through":"2026-08-31",
        } for country in ("INT","REG")]

        with self.assertRaisesRegex(ValueError,"ambiguous operational source key"):
            apply_public_operations(rows,{},operations)

    def test_ui_separates_operational_and_historical_state(self):
        html=(ROOT/"index.html").read_text(encoding="utf-8")
        app=(ROOT/"app.js").read_text(encoding="utf-8")
        self.assertIn('id="operations-status"',html)
        self.assertIn("Latest scheduled operation",app)
        self.assertIn("Historical pipeline stage",app)
        self.assertIn("Historical TCG record",app)
        self.assertIn("scrape_series_availability",app)
        self.assertIn("tcg_series_availability",app)
        self.assertIn("Every pending value is published as <code>null</code>",app)

    def test_hero_uses_current_operations_time_and_labels_canonical_time(self):
        app=(ROOT/"app.js").read_text(encoding="utf-8")
        self.assertIn(
            "data.operations?.generated_at_utc || data.meta.generated_at_utc",
            app,
        )
        self.assertIn("Canonical tracker:",app)

    def test_release_exposes_latest_operations_without_advancing_verified_dates(self):
        self.assertEqual(self.data["meta"]["snapshot_version"],5)
        self.assertEqual(self.data["operations"]["latest_window"]["state"],"terminal_reconciled")
        self.assertFalse(self.data["operations"]["latest_window"]["work_started"])
        self.assertGreaterEqual(
            self.data["operations"]["generated_at_utc"],
            self.data["operations"]["latest_window"]["evidence_through_utc"],
        )
        self.assertEqual(self.data["meta"]["current_scope_count_contract_count"],3)
        by_source={row["source"]:row for row in self.data["sources"]}
        expected={
            "newsroompanama.com":"Exact repair launch failed before source work; agent remediation in progress",
            "eyeradio.org":"Exact repair launch failed before source work; agent remediation in progress",
            "eagle.co.ug":"Exact repair launch failed before source work; agent remediation in progress",
            "sibconline.com.sb":"Blocked — exact identity ledger unavailable",
            "7sur7.cd":"Parked — exact residual ledger unavailable",
            "liberianinvestigator.com":"Parked — exact residual ledger unavailable",
            "247malawi.com":"Parked — exact residual ledger unavailable",
            "eluniverso.com":"Parked — exact residual ledger unavailable",
            "elcomercio.pe":"Parked — exact residual ledger unavailable",
            "csmonitor.com":"First-pass complete; production promotion pending",
        }
        for source,current_stage in expected.items():
            with self.subTest(source=source):
                row=by_source[source]
                self.assertEqual(row["pipeline_state"],current_stage)
                self.assertEqual(row["operational_state_source"],"Latest scheduled operations")
                self.assertEqual(row["historical_tcg_state"],row["tcg_record_state"])
                self.assertEqual(row["historical_tcg_through"],row["tcg_record_through"])

    def test_mobile_source_table_keeps_current_stage_and_count_series_visible(self):
        app=(ROOT/"app.js").read_text(encoding="utf-8")
        mobile=next(line for line in app.splitlines() if "@media(max-width:720px)" in line)
        self.assertNotIn("nth-child(7)",mobile)
        self.assertNotIn("nth-child(8)",mobile)

    def test_current_scope_contract_does_not_hide_historical_plot(self):
        app=(ROOT/"app.js").read_text(encoding="utf-8")
        self.assertIn('let contractMarkup = ""',app)
        self.assertIn('Historical monthly source counts',app)
        self.assertIn('return `${contractMarkup}<div class="count-chart">',app)

if __name__=="__main__": unittest.main()
