from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts.parallel_observe_pure import observe_parallel


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sealed_parent(
    root: Path, url: str, source_id: str = "mlp:TST:example.test", *, overlap: bool = False,
) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    worklist = root / "worklist.private.txt"
    worklist.write_text(url + "\n", encoding="utf-8")
    preserved = root / "preserved_passes.private.ndjson"
    preserved_urls = [f"https://example.test/pass-{index}" for index in range(7)]
    if overlap:
        preserved_urls[0] = url
    preserved.write_text(''.join(
        json.dumps({"source_id": source_id, "url": value}) + "\n"
        for value in preserved_urls
    ), encoding="utf-8")
    exclusions = root / "verified_exclusions.private.ndjson"
    exclusions.write_text(''.join(
        json.dumps({"source_id": source_id, "url": f"https://example.test/exclusion-{index}"}) + "\n"
        for index in range(3)
    ), encoding="utf-8")
    classified = root / "classified_source_ledger.private.ndjson"
    classified.write_text(json.dumps({"source_id": source_id, "url": url}) + "\n", encoding="utf-8")
    summary = root / "source_summary.private.json"
    summary.write_text(json.dumps({
        "source_id": source_id,
        "classified_count": 1,
        "preserved_passes": {"count": 7, "sha256": sha(preserved)},
        "verified_exclusions": {"count": 3, "sha256": sha(exclusions)},
    }) + "\n", encoding="utf-8")
    seal = root / "classification_seal.private.json"
    seal.write_text(json.dumps({
        "kind": "private_offline_classification_seal",
        "status": "sealed_pending_independent_audit",
        "output_artifacts": [{"sha256": sha(summary)}, {"sha256": sha(classified)}],
    }) + "\n", encoding="utf-8")
    audit = root / "independent_audit.private.json"
    audit.write_text(json.dumps({
        "kind": "private_independent_offline_source_local_classification_audit",
        "result": "approved", "failed_checks": [],
        "check_summary": {"failed": 0, "passed": 3, "total": 3},
        "audited_artifacts": {"classification_seal_sha256": sha(seal)},
        "sources": {"TST": {
            "classified_count": 1, "preserved_pass_count": 7,
            "verified_exclusion_count": 3,
        }},
    }) + "\n", encoding="utf-8")
    handoff = root / "handoff_manifest.private.json"
    url_set_sha = hashlib.sha256((url + "\n").encode("utf-8")).hexdigest()
    handoff.write_text(json.dumps({
        "schema_version": 1,
        "kind": "sealed_classification_exact_handoff",
        "status": "sealed",
        "source_id": source_id,
        "source_name": "example.test",
        "worklist": {
            "count": 1, "sha256": sha(worklist), "url_set_sha256": url_set_sha,
        },
        "classification_seal_sha256": sha(seal),
        "independent_audit_sha256": sha(audit),
        "source_summary_sha256": sha(summary),
        "preserved_passes": {
            "count": 7, "sha256": sha(preserved),
            "url_set_sha256": hashlib.sha256(''.join(
                f"{value}\n" for value in sorted(preserved_urls)
            ).encode()).hexdigest(),
        },
        "verified_exclusions": {
            "count": 3, "sha256": sha(exclusions),
            "url_set_sha256": hashlib.sha256(''.join(
                f"https://example.test/exclusion-{index}\n" for index in range(3)
            ).encode()).hexdigest(),
        },
        "classified_source_ledger": {
            "count": 1, "sha256": sha(classified), "url_set_sha256": url_set_sha,
        },
    }) + "\n", encoding="utf-8")
    return {
        name: {"path": path, "sha256": sha(path)}
        for name, path in {
            "handoff_manifest": handoff,
            "worklist": worklist,
            "classification_seal": seal,
            "independent_audit": audit,
            "source_summary": summary,
            "preserved_passes": preserved,
            "verified_exclusions": exclusions,
            "classified_source_ledger": classified,
        }.items()
    }


class ParallelPureObserverTests(unittest.TestCase):
    def test_writes_complete_deterministic_promotable_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = root / "adapter.py"
            adapter.write_text(
                """from datetime import datetime
class Config:
 source_name='example.test'; country_code='TST'; is_env=False
def discover_urls(start,end,evidence):
 evidence.update({'coverage_complete':True,'strategy':'fixture'}); return ['https://example.test/b','https://example.test/a']
def scrape_url(url):
 title='Complete title '+url[-1]; body=' '.join(['publisher']*30)
 return {'url':url,'title':title,'date_publish':datetime(2026,9,16,12,0),'maintext':body,'field_evidence':{'titles':[{'raw':title}],'dates':[{'kind':'published','raw':'2026-09-16T12:00:00'}],'bodies':[{'text':body}],'http_status':200}}
""",
                encoding="utf-8",
            )
            run = observe_parallel(
                Path(__file__).resolve().parents[1], adapter,
                date(2026, 9, 16), date(2026, 9, 16), root / "runs", workers=2,
            )
            manifest = json.loads((run / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "observed")
            self.assertEqual(manifest["counts"]["passed"], 2)
            self.assertEqual(manifest["counts"]["exceptions"], 0)
            urls = [json.loads(line)["url"] for line in (run / "urls.jsonl").read_text().splitlines()]
            articles = [json.loads(line)["url"] for line in (run / "articles.jsonl").read_text().splitlines()]
            self.assertEqual(urls, ["https://example.test/b", "https://example.test/a"])
            self.assertEqual(articles, urls)

    def test_exact_url_mode_records_targeted_repair_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = root / "adapter.py"
            adapter.write_text(
                """from datetime import datetime
class Config:
 source_name='example.test'; country_code='TST'; is_env=False
def discover_urls(start,end,evidence): raise AssertionError('must not run discovery')
def scrape_url(url):
 body=' '.join(['complete']*30); title='Exact repair title'
 return {'url':url,'title':title,'date_publish':datetime(2026,3,12),'maintext':body,'field_evidence':{'titles':[{'raw':title}],'dates':[{'kind':'published','raw':'2026-03-12'}],'bodies':[{'text':body}],'http_status':200}}
""",
                encoding="utf-8",
            )
            run = observe_parallel(
                Path(__file__).resolve().parents[1], adapter,
                date(2026, 3, 12), date(2026, 3, 12), root / "runs", 2,
                ["https://example.test/legacy"],
            )
            manifest = json.loads((run / "manifest.json").read_text())
            self.assertEqual(manifest["counts"]["passed"], 1)
            self.assertEqual(manifest["discovery_evidence"]["strategy"], "exact_reviewed_url_set")
            self.assertIn("not source-wide", manifest["discovery_evidence"]["scope"])

    def test_exact_url_reprobe_binds_immutable_parent_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = root / "adapter.py"
            adapter.write_text(
                """from datetime import datetime
class Config:
 source_name='example.test'; country_code='TST'; is_env=False
def discover_urls(start,end,evidence): raise AssertionError('must not run discovery')
def scrape_url(url):
 body=' '.join(['complete']*30); title='Repaired template title'
 return {'url':url,'title':title,'date_publish':datetime(2026,7,26),'maintext':body,'field_evidence':{'titles':[{'raw':title}],'dates':[{'kind':'published','raw':'2026-07-26'}],'bodies':[{'text':body}],'http_status':200}}
""",
                encoding="utf-8",
            )
            parent = root / "parent"
            parent.mkdir()
            (parent / "manifest.json").write_text(json.dumps({
                "run_id": "parent-observation", "status": "observed",
                "source": "example.test", "source_sha256": "old-source",
                "source_bundle_sha256": "old-bundle",
            }))
            for name in ("urls.jsonl", "field_checks.jsonl", "issues.jsonl"):
                (parent / name).write_text('{"url":"https://example.test/template"}\n')
            run = observe_parallel(
                Path(__file__).resolve().parents[1], adapter,
                date(2026, 7, 26), date(2026, 7, 26), root / "runs", 2,
                ["https://example.test/template"], parent, "template_selector_repair",
            )
            manifest = json.loads((run / "manifest.json").read_text())
            provenance = manifest["derived_from"]
            self.assertEqual(provenance["parent_run_id"], "parent-observation")
            self.assertEqual(provenance["parent_source_sha256"], "old-source")
            self.assertEqual(provenance["target_url_count"], 1)
            self.assertEqual(provenance["reason"], "template_selector_repair")
            self.assertEqual(
                set(provenance["parent_artifact_sha256"]),
                {"manifest.json", "urls.jsonl", "field_checks.jsonl", "issues.jsonl"},
            )

    def test_sealed_classification_parent_is_verified_and_recorded_without_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            url = "https://example.test/sealed-repair"
            adapter = root / "adapter.py"
            adapter.write_text(
                """from datetime import datetime
class Config:
 source_name='example.test'; country_code='TST'; is_env=False
def discover_urls(start,end,evidence): raise AssertionError('must not run discovery')
def scrape_url(url):
 body=' '.join(['complete']*30); title='Sealed repair title'
 return {'url':url,'title':title,'date_publish':datetime(2026,9,20),'maintext':body,'field_evidence':{'titles':[{'raw':title}],'dates':[{'kind':'published','raw':'2026-09-20'}],'bodies':[{'text':body}],'http_status':200}}
""",
                encoding="utf-8",
            )
            contract = sealed_parent(root, url)
            run = observe_parallel(
                Path(__file__).resolve().parents[1], adapter,
                date(2026, 9, 1), date(2026, 9, 30), root / "runs", 1,
                [url], None, "sealed_exact_repair", contract,
            )
            provenance = json.loads((run / "manifest.json").read_text())["derived_from"]
            self.assertEqual(provenance["kind"], "sealed_classification_parent")
            self.assertEqual(provenance["target_url_count"], 1)
            self.assertEqual(provenance["preserved_pass_count"], 7)
            self.assertEqual(provenance["verified_exclusion_count"], 3)
            self.assertFalse(any("path" in key or "run_dir" in key for key in provenance))

            overlap_root = root / "overlap"
            overlap_contract = sealed_parent(overlap_root, url, overlap=True)
            with self.assertRaisesRegex(ValueError, "partitions overlap"):
                observe_parallel(
                    Path(__file__).resolve().parents[1], adapter,
                    date(2026, 9, 1), date(2026, 9, 30), overlap_root / "runs", 1,
                    [url], None, "sealed_exact_repair", overlap_contract,
                )

            Path(contract["source_summary"]["path"]).write_text(
                '{"drift":true}\n', encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "source_summary SHA-256 mismatch"):
                observe_parallel(
                    Path(__file__).resolve().parents[1], adapter,
                    date(2026, 9, 1), date(2026, 9, 30), root / "drift-runs", 1,
                    [url], None, "sealed_exact_repair", contract,
                )

    def test_parallel_observer_emits_serial_parity_adapter_parser_error_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = root / "adapter.py"
            adapter.write_text(
                """from datetime import datetime
class Config:
 source_name='example.test'; country_code='TST'; is_env=False
def discover_urls(start,end,evidence): return ['https://example.test/parser-error']
def scrape_url(url):
 body=' '.join(['complete']*30); title='Parser error title'
 return {'url':url,'title':title,'date_publish':datetime(2026,9,20),'maintext':body,'parser_error':'target token remained','field_sources':{'title':'adapter'},'field_evidence':{'titles':[{'raw':title}],'dates':[{'kind':'published','raw':'2026-09-20'}],'bodies':[{'text':body}],'http_status':200}}
""",
                encoding="utf-8",
            )
            run = observe_parallel(
                Path(__file__).resolve().parents[1], adapter,
                date(2026, 9, 1), date(2026, 9, 30), root / "runs", 1,
            )
            issues = [json.loads(line) for line in (run / "issues.jsonl").read_text().splitlines()]
            parser_issues = [item for item in issues if item["code"] == "adapter_parser_error"]
            self.assertEqual(len(parser_issues), 1)
            self.assertEqual(parser_issues[0]["stage"], "parse")
            self.assertEqual(parser_issues[0]["category"], "content")
            self.assertEqual(parser_issues[0]["observed"]["parser_error"], "target token remained")
            self.assertEqual(parser_issues[0]["observed"]["field_sources"], {"title": "adapter"})
            manifest = json.loads((run / "manifest.json").read_text())
            self.assertEqual(manifest["counts"]["quarantined"], 1)


if __name__ == "__main__":
    unittest.main()
