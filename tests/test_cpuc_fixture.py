from pathlib import Path
import hashlib
import json
import unittest
from urllib.parse import urlsplit

from backend.diff.pdf import compare_tokens, read_pdf
from backend.docket_store import ensure_prepared_comparison, load_manifest
from backend.storage import docket_file


DATA = Path(__file__).resolve().parents[1] / "data"
FIXTURES = DATA / "seed" / "fixtures" / "cpuc"
DOCKET = DATA / "seed" / "dockets" / "R.25-06-019"


class CpucFixtureTests(unittest.TestCase):
    def test_docket_paths_stay_within_seed_or_local_store(self):
        self.assertEqual(docket_file(DOCKET, 'v1/document.pdf'), DOCKET / 'v1' / 'document.pdf')
        for path in ('../project.json', 'uploads/../manifest.json', '/tmp/other.pdf', 'uploads\\other.pdf'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                docket_file(DOCKET, path)

    def test_revised_proposal_has_two_verified_comparisons(self):
        for old, new in (('v1', 'v1a'), ('v1a', 'v2')):
            ensure_prepared_comparison(DOCKET, old, new)
        manifest = load_manifest(DOCKET)
        for version, meta in manifest['versions'].items():
            self.assertEqual(hashlib.sha256((DOCKET / meta['file']).read_bytes()).hexdigest(), meta['sha256'], version)
            self.assertEqual(meta['original_filename'], Path(urlsplit(meta['source_url']).path).name)
        counts = {}
        for pair in manifest['comparisons']:
            if pair.get('legacy_direct'):
                continue
            report = json.loads(docket_file(DOCKET, pair['changes']).read_text())
            self.assertEqual(report['old']['sha256'], manifest['versions'][pair['old']]['sha256'])
            self.assertEqual(report['new']['sha256'], manifest['versions'][pair['new']]['sha256'])
            counts[(pair['old'], pair['new'])] = len(report['changes'])
        self.assertEqual(counts[('v1', 'v1a')], 271)
        self.assertEqual(counts[('v1a', 'v2')], 3)

    def test_expected_procurement_changes_are_located(self):
        old, old_meta = read_pdf(FIXTURES / "proposed-2026-01-14.pdf")
        new, new_meta = read_pdf(FIXTURES / "final-2026-03-05.pdf")
        self.assertEqual((old_meta["pages"], new_meta["pages"]), (116, 152))
        findings = compare_tokens(old, new)
        self.assertTrue(any(
            item["old"] and item["new"]
            and "4,000 MW NQC" in item["old"]["text"]
            and "2,000 MW NQC" in item["new"]["text"]
            and "2031" in item["new"]["text"]
            for item in findings
        ), "Expected a bounded 2031 procurement change")
        self.assertTrue(any(
            item["new"]
            and "clean firm resources" in item["new"]["text"]
            and "long-duration storage resources" in item["new"]["text"]
            and item["new"]["page_start"] == 146
            for item in findings
        ), "Expected the new resource-mix requirement on final PDF page 146")
        self.assertEqual(compare_tokens(old, old), [])


if __name__ == "__main__":
    unittest.main()
