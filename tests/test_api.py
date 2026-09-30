import unittest
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.api import app
from backend import workflow
from backend.diff.pdf import read_pdf, compare_tokens
from backend.reviewer.agent import ImpactAssessment, Citation


def small_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, item in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode() + item + b"\nendobj\n")
    start = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(result)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.workflow_dir = TemporaryDirectory()
        self.workflow_patch = patch('backend.workflow.DATA', Path(self.workflow_dir.name))
        self.workflow_patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self.workflow_patch.stop()
        self.workflow_dir.cleanup()

    def test_browse_fixture_and_full_candidate_set(self):
        projects = self.client.get('/api/projects').json()
        self.assertEqual({item['id'] for item in projects}, {'project-1', 'project-2'})
        project = self.client.get('/api/projects/project-1').json()
        self.assertEqual(len(project['obligations']), 3)
        initial = self.client.get('/api/projects/project-1/workflow').json()
        self.assertEqual(initial['visible_version'], 'v1')
        self.assertEqual(initial['obligation_version'], 'v1')
        self.assertEqual(initial['runs'], [])
        self.assertEqual(self.client.get('/api/projects/project-1/changes').status_code, 404)
        self.assertEqual(self.client.get('/api/projects/project-1/sources/v1a').status_code, 404)
        self.assertEqual(self.client.get('/api/projects/project-1/sources/v2').status_code, 404)

        self.assertEqual(self.client.get('/api/projects/project-1/changes?old=v1a&new=v2').status_code, 404)
        proposed_page = self.client.get('/api/projects/project-1/sources/v1/pages/1').json()
        self.assertEqual(proposed_page['page'], 1)
        self.assertGreater(len(proposed_page['lines']), 0)
        with patch('backend.workflow.read_provider_settings', return_value=(None, 'model')):
            triggered = self.client.post('/api/projects/project-1/introduce-change').json()
        self.assertEqual(triggered['visible_version'], 'v1a')
        self.assertEqual(triggered['review']['status'], 'failed')
        self.assertEqual(len(triggered['runs']), 1)
        self.assertEqual(triggered['runs'][0]['trigger'], 'new_version')
        first_page = self.client.get('/api/projects/project-1/changes?page_size=25').json()
        self.assertEqual(first_page['total'], 271)
        self.assertEqual(len(first_page['items']), 25)
        self.assertEqual(len(self.client.get('/api/projects/project-1/changes?page=11&page_size=25').json()['items']), 21)
        matching = self.client.get('/api/projects/project-1/changes?query=change-0248').json()
        self.assertIn('change-0248', {item['id'] for item in matching['items']})
        change = self.client.get('/api/projects/project-1/changes/change-0248').json()
        self.assertEqual(change['new']['page_start'], 145)
        self.assertEqual(change['new']['status'], 'proposed')
        self.assertTrue(any(location['page'] == 145 for location in change['new']['locations']))
        self.assertTrue(all(location['line'] > 0 for location in change['new']['locations']))
        self.assertTrue(any(line['type'] == 'added' for line in change['diff_lines']))
        source = self.client.get('/api/projects/project-1/sources/v1a')
        self.assertEqual(source.status_code, 200)
        self.assertTrue(source.content.startswith(b'%PDF'))
        self.assertIn('inline', source.headers['content-disposition'])
        revised_page = self.client.get('/api/projects/project-1/sources/v1a/pages/145').json()
        self.assertGreater(len(revised_page['lines']), 0)
        self.assertEqual(self.client.get('/api/projects/project-1/sources/v2').status_code, 404)


    def test_prepared_version_generates_comparison_without_seed_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / 'data' / 'seed' / 'projects' / 'project-1'
            project_dir.mkdir(parents=True)
            for name in ('project.json', 'obligations.md'):
                project_dir.joinpath(name).write_bytes(Path('data/seed/projects/project-1', name).read_bytes())
            docket = root / 'data' / 'seed' / 'dockets' / 'R.25-06-019'
            old_file = docket / 'v1' / 'document.pdf'
            new_file = docket / 'v2' / 'document.pdf'
            old_file.parent.mkdir(parents=True)
            new_file.parent.mkdir()
            old_pdf = small_pdf('Procurement schedule in 2030')
            new_pdf = small_pdf('Procurement schedule in 2031')
            old_file.write_bytes(old_pdf)
            new_file.write_bytes(new_pdf)
            (docket / 'manifest.json').write_text(json.dumps({
                'docket_id': 'R.25-06-019', 'agency': 'Test agency', 'title': 'Test decision',
                'versions': {
                    'v1': {'file': 'v1/document.pdf', 'filing_id': 'filing-1', 'status': 'proposed',
                           'date': '2026-01-01', 'source_url': None, 'sha256': hashlib.sha256(old_pdf).hexdigest()},
                    'v2': {'file': 'v2/document.pdf', 'filing_id': 'filing-1', 'status': 'final',
                           'date': '2026-02-01', 'source_url': None, 'sha256': hashlib.sha256(new_pdf).hexdigest()},
                },
                'comparisons': [{'old': 'v1', 'new': 'v2', 'changes': 'changes/v1-v2.json'}],
            }))
            with patch('backend.api.PROJECTS', root / 'data' / 'seed' / 'projects'), \
                 patch('backend.api.DOCKETS', root / 'data' / 'seed' / 'dockets'), \
                 patch('backend.workflow.ROOT', root), \
                 patch('backend.workflow.read_provider_settings', return_value=(None, 'model')):
                initial = self.client.get('/api/projects/project-1/workflow')
                self.assertEqual(initial.status_code, 200)
                self.assertEqual(initial.json()['visible_version'], 'v1')
                self.assertFalse((root / 'data' / 'local' / 'uploads').exists())
                introduced = self.client.post('/api/projects/project-1/introduce-change')
                self.assertEqual(introduced.status_code, 200, introduced.text)
                self.assertEqual(introduced.json()['visible_version'], 'v2')
                changes = self.client.get('/api/projects/project-1/changes')
                self.assertEqual(changes.status_code, 200, changes.text)
                self.assertGreater(changes.json()['total'], 0)
                self.assertFalse((docket / 'changes').exists())
                generated = root / 'data' / 'local' / 'uploads' / 'R.25-06-019'
                overlay = json.loads((generated / 'manifest.json').read_text())
                self.assertTrue(overlay['comparisons'][0]['generated_prepared'])
                self.assertTrue(any(generated.glob('prepared-*.json')))
                self.assertEqual(self.client.post('/api/projects/project-1/reset-demo').status_code, 200)
                with patch('backend.docket_store.compare_tokens', wraps=compare_tokens) as diff:
                    repeated = self.client.post('/api/projects/project-1/introduce-change')
                self.assertEqual(repeated.status_code, 200, repeated.text)
                diff.assert_called_once()
    def test_scope(self):
        project = self.client.get('/api/projects/project-2').json()
        self.assertEqual(project['dockets'], ['field-operations-safety'])
        self.assertEqual(project['docket_details'][0]['docket_id'], 'field-operations-safety')
        visible = self.client.get('/api/projects/project-2/workflow').json()['visible_versions']
        self.assertLessEqual(len(visible), 1)
        self.assertEqual(self.client.get('/api/projects/project-2/changes').status_code, 404)
        self.assertEqual(self.client.get('/api/projects/project-1/changes/unknown').status_code, 404)
        self.assertEqual(self.client.get('/api/projects/project-1/sources/missing').status_code, 404)

    def test_empty_linked_docket_accepts_first_pdf_and_revision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / 'data' / 'seed' / 'projects' / 'project-2'
            project_dir.mkdir(parents=True)
            for name in ('project.json', 'obligations.md'):
                project_dir.joinpath(name).write_bytes(Path('data/seed/projects/project-2', name).read_bytes())
            docket_dir = root / 'data' / 'seed' / 'dockets' / 'field-operations-safety'
            docket_dir.mkdir(parents=True)
            docket_dir.joinpath('manifest.json').write_bytes(
                Path('data/seed/dockets/field-operations-safety/manifest.json').read_bytes())
            with patch('backend.api.PROJECTS', root / 'data' / 'seed' / 'projects'), \
                 patch('backend.api.DOCKETS', root / 'data' / 'seed' / 'dockets'), \
                 patch('backend.workflow.ROOT', root):
                first_pdf = small_pdf('Inspect ladders before field use')
                first = self.client.post(
                    '/api/projects/project-2/filings?title=Field%20safety%20notice&original_filename=field-safety-notice.PDF&issued_on=2026-09-29&status=draft_proposal',
                    content=first_pdf, headers={'content-type': 'application/pdf'})
                self.assertEqual(first.status_code, 200, first.text)
                first_version = first.json()['version']
                self.assertEqual(first.json()['workflow']['visible_versions'], [first_version])
                self.assertEqual(self.client.get('/api/projects/project-2').json()['docket_details'][0]['versions'][first_version]['original_filename'], 'field-safety-notice.PDF')
                self.assertEqual(self.client.get(f'/api/projects/project-2/sources/{first_version}').content, first_pdf)
                self.assertIn('field-safety-notice.PDF', self.client.get(f'/api/projects/project-2/sources/{first_version}').headers['content-disposition'])
                invalid_name = self.client.post(
                    '/api/projects/project-2/filings?title=Bad%20name&original_filename=..%2Fother.pdf&issued_on=2026-09-29',
                    content=small_pdf('Separate filing'), headers={'content-type': 'application/pdf'})
                self.assertEqual(invalid_name.status_code, 422)
                self.assertEqual(self.client.get('/api/projects/project-2/changes').status_code, 404)
                revision_pdf = small_pdf('Inspect ladders and remove damaged equipment before field use')
                with patch('backend.workflow.read_provider_settings', return_value=(None, 'model')):
                    revision = self.client.post(
                        f'/api/projects/project-2/filings?revises={first_version}&title=Revised%20field%20safety%20notice&original_filename=field-safety-revised.pdf&issued_on=2026-09-30&status=final',
                        content=revision_pdf, headers={'content-type': 'application/pdf'})
                self.assertEqual(revision.status_code, 200, revision.text)
                self.assertEqual(len(revision.json()['workflow']['runs']), 1)
                self.assertEqual(self.client.get('/api/projects/project-2/changes').status_code, 200)
                versions = self.client.get('/api/projects/project-2').json()['docket_details'][0]['versions']
                self.assertEqual(versions[first_version]['filing_id'], versions[revision.json()['version']]['filing_id'])
                self.assertEqual(versions[revision.json()['version']]['original_filename'], 'field-safety-revised.pdf')

    def test_adding_obligation_reviews_current_filing_from_first_to_final(self):
        class ImmediateThread:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            project_dir = root / 'data' / 'seed' / 'projects' / 'project-2'
            project_dir.mkdir(parents=True)
            for name in ('project.json', 'obligations.md'):
                project_dir.joinpath(name).write_bytes(Path('data/seed/projects/project-2', name).read_bytes())
            docket_dir = root / 'data' / 'seed' / 'dockets' / 'field-operations-safety'
            docket_dir.mkdir(parents=True)
            docket_dir.joinpath('manifest.json').write_bytes(
                Path('data/seed/dockets/field-operations-safety/manifest.json').read_bytes())

            def reviewer(context, obligation, _agent):
                if obligation.id == 'OBL-101':
                    return ImpactAssessment(project_id='project-2', obligation_id=obligation.id,
                        outcome='not_affected', summary='No ladder impact.', rationale='Unrelated.',
                        proposed_action='No action.', reviewer_role='Safety manager')
                self.assertEqual(context.comparison['old'], first_version)
                self.assertEqual(context.comparison['new'], final_version)
                self.assertEqual(context.manifest['versions'][final_version]['status'], 'final')
                return ImpactAssessment(project_id='project-2', obligation_id=obligation.id,
                    outcome='needs_review', summary='Check the final procurement schedule.',
                    rationale='The current final source changes the baseline schedule.',
                    proposed_action='Ask the planning manager to review.', reviewer_role='Planning manager',
                    citations=[Citation(change_id=context.changes['changes'][0]['id'], side='new')])

            with patch('backend.api.PROJECTS', root / 'data' / 'seed' / 'projects'), \
                 patch('backend.api.DOCKETS', root / 'data' / 'seed' / 'dockets'), \
                 patch('backend.workflow.ROOT', root), \
                 patch('backend.obligation_store.DATA', root / 'data' / 'local' / 'obligations'), \
                 patch('backend.workflow.read_provider_settings', return_value=('test-key', 'test-model')), \
                 patch('backend.workflow.OpenRouterProvider'), patch('backend.workflow.OpenRouterModel'), \
                 patch('backend.workflow.build_agent'), patch('backend.workflow.Thread', ImmediateThread), \
                 patch('backend.workflow.review', side_effect=reviewer) as review_mock:
                def upload(text, *, revises=None, status='proposed'):
                    query = f'?title=Procurement&original_filename=source.pdf&issued_on=2026-09-29&status={status}'
                    if revises:
                        query += f'&revises={revises}'
                    response = self.client.post('/api/projects/project-2/filings' + query,
                                                content=small_pdf(text), headers={'content-type': 'application/pdf'})
                    self.assertEqual(response.status_code, 200, response.text)
                    return response.json()['version']

                first_version = upload('Procurement schedule due in 2030 and 2032')
                middle_version = upload('Procurement schedule due in 2030 2031 and 2032', revises=first_version)
                final_version = upload('Final procurement schedule due in 2030 2031 and 2032',
                                       revises=middle_version, status='final')
                before = self.client.get('/api/projects/project-2/workflow').json()['runs']
                self.assertEqual(len(before), 2)
                self.assertEqual(review_mock.call_count, 2)

                added = self.client.post('/api/projects/project-2/obligations', json={
                    'title': 'Procurement schedule planning',
                    'text': 'Check whether the final 2031 milestone changes our planning schedule.',
                })
                self.assertEqual(added.status_code, 200, added.text)
                state = self.client.get('/api/projects/project-2/workflow').json()
                self.assertEqual(len(state['runs']), 3)
                self.assertEqual(state['runs'][:2], before)
                self.assertEqual(state['runs'][-1]['trigger'], 'obligation_added')
                self.assertEqual(state['runs'][-1]['obligation_ids'], ['OBL-102'])
                self.assertEqual(state['runs'][-1]['comparison']['old'], first_version)
                self.assertEqual(state['runs'][-1]['comparison']['new'], final_version)
                self.assertEqual(state['review']['total'], 1)
                self.assertEqual(state['results'][0]['assessment']['outcome'], 'needs_review')
                self.assertEqual(state['results'][0]['assessment']['citations'][0]['version'], final_version)
                self.assertEqual(state['results'][0]['assessment']['citations'][0]['status'], 'final')
                self.assertEqual(review_mock.call_count, 3)
                detail = self.client.get('/api/projects/project-2').json()['docket_details'][0]
                self.assertTrue(any(item.get('backfill') and item['old'] == first_version
                                    and item['new'] == final_version for item in detail['comparisons']))

                repeat = self.client.post('/api/projects/project-2/obligations/OBL-102/review')
                self.assertEqual(repeat.status_code, 200, repeat.text)
                self.assertEqual(repeat.json()['obligation_version'], 'v2')
                self.assertEqual(len(repeat.json()['runs']), 4)
                self.assertEqual(review_mock.call_count, 4)
                with patch('backend.workflow.read_provider_settings', return_value=(None, 'test-model')):
                    failed = self.client.post('/api/projects/project-2/obligations/OBL-102/review').json()
                self.assertEqual(failed['review']['status'], 'failed')
                self.assertEqual(failed['runs'][-1]['obligation_ids'], ['OBL-102'])
                retried = self.client.post('/api/projects/project-2/introduce-change').json()
                self.assertEqual(retried['runs'][-1]['trigger'], 'retry')
                self.assertEqual(retried['runs'][-1]['obligation_ids'], ['OBL-102'])
                self.assertEqual(self.client.get('/api/projects/project-2/workflow').json()['review']['status'], 'completed')
                self.assertEqual(review_mock.call_count, 5)

    def test_added_obligations_persist_and_enter_next_review(self):
        bundled = Path('data/seed/projects/project-1/obligations.md')
        original = bundled.read_text(encoding='utf-8')
        with TemporaryDirectory() as directory, patch('backend.obligation_store.DATA', Path(directory)):
            first = self.client.post('/api/projects/project-1/obligations', json={
                'title': '  Community   reporting  ',
                'text': 'Track quarterly community reporting commitments.',
            })
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()['obligation']['id'], 'OBL-04')
            self.assertEqual(first.json()['obligation']['title'], 'Community reporting')
            self.assertEqual(first.json()['workflow']['obligation_version'], 'v2')
            second = self.client.post('/api/projects/project-1/obligations', json={
                'title': 'Grid notice', 'text': 'Notify grid operators before energization.',
            })
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(second.json()['obligation']['id'], 'OBL-05')
            self.assertEqual(second.json()['workflow']['obligation_version'], 'v3')
            self.assertEqual([item['id'] for item in self.client.get('/api/projects/project-1').json()['obligations']],
                             ['OBL-01', 'OBL-02', 'OBL-03', 'OBL-04', 'OBL-05'])
            self.assertIn('## OBL-05 - Grid notice', (Path(directory) / 'project-1.md').read_text())
            self.assertEqual(bundled.read_text(encoding='utf-8'), original)
            rejected = self.client.post('/api/projects/project-1/obligations', json={
                'title': 'Bad heading', 'text': '## OBL-99 - injected\ntext',
            })
            self.assertEqual(rejected.status_code, 422)
            with patch('backend.workflow.read_provider_settings', return_value=(None, 'model')):
                state = self.client.post('/api/projects/project-1/introduce-change').json()
            self.assertEqual(state['review']['total'], 5)
            self.assertEqual(state['runs'][-1]['obligation_version'], 'v3')
            reset = self.client.post('/api/projects/project-1/reset-demo').json()
            self.assertEqual(reset['obligation_version'], 'v3')
            self.assertEqual(len(self.client.get('/api/projects/project-1').json()['obligations']), 5)

    def test_manual_pdf_intake_supports_new_filing_and_revision(self):
        with TemporaryDirectory() as directory:
            docket = Path(directory) / 'data' / 'seed' / 'dockets' / 'R.25-06-019'
            (docket / 'v1').mkdir(parents=True)
            (docket / 'v2').mkdir()
            first = small_pdf('Procurement schedule in 2030')
            prepared = small_pdf('Procurement schedule in 2031')
            uploaded = small_pdf('Draft procurement schedule in 2032')
            old_file = docket / 'v1' / 'document.pdf'
            prepared_file = docket / 'v2' / 'document.pdf'
            old_file.write_bytes(first)
            prepared_file.write_bytes(prepared)
            old_tokens, old_meta = read_pdf(old_file)
            prepared_tokens, prepared_meta = read_pdf(prepared_file)
            self.assertTrue(old_tokens)
            (docket / 'changes').mkdir()
            (docket / 'changes' / 'v1-v2.json').write_text(json.dumps({
                'old': old_meta, 'new': prepared_meta,
                'changes': compare_tokens(old_tokens, prepared_tokens),
            }))
            (docket / 'manifest.json').write_text(json.dumps({
                'docket_id': 'R.25-06-019', 'agency': 'Test agency', 'title': 'Test decision',
                'versions': {
                    'v1': {'file': 'v1/document.pdf', 'filing_id': 'filing-1', 'status': 'proposed', 'date': '2026-01-01',
                           'source_url': None, 'sha256': hashlib.sha256(first).hexdigest()},
                    'v2': {'file': 'v2/document.pdf', 'filing_id': 'filing-1', 'status': 'final', 'date': '2026-02-01',
                           'source_url': None, 'sha256': hashlib.sha256(prepared).hexdigest()},
                },
                'comparisons': [{'old': 'v1', 'new': 'v2', 'changes': 'changes/v1-v2.json'}],
            }))
            class ImmediateThread:
                def __init__(self, target, args, daemon):
                    self.target, self.args = target, args

                def start(self):
                    self.target(*self.args)

            def reviewer(context, obligation, _agent):
                self.assertEqual(context.manifest['versions'][context.comparison['new']]['status'],
                                 'draft_proposal')
                return ImpactAssessment(project_id='project-1', obligation_id=obligation.id,
                    outcome='not_affected', summary='No direct relationship found.',
                    rationale='No matching change.', proposed_action='No action.',
                    reviewer_role='Record owner')

            with patch('backend.api.DOCKETS', Path(directory) / 'data' / 'seed' / 'dockets'), \
                 patch('backend.workflow.read_provider_settings', return_value=('test-key', 'test-model')), \
                 patch('backend.workflow.OpenRouterProvider'), patch('backend.workflow.OpenRouterModel'), \
                 patch('backend.workflow.build_agent'), patch('backend.workflow.Thread', ImmediateThread), \
                 patch('backend.workflow.review', side_effect=reviewer) as review_mock:
                query = '?revises=v1&title=Draft%20revision&original_filename=proposed-revision.pdf&issued_on=2026-01-15&status=draft_proposal'
                added = self.client.post('/api/projects/project-1/revisions' + query,
                                         content=uploaded, headers={'content-type': 'application/pdf'})
                self.assertEqual(added.status_code, 200, added.text)
                version = added.json()['version']
                state = added.json()['workflow']
                self.assertEqual(state['visible_versions'], ['v1', version])
                self.assertEqual(state['runs'][0]['trigger'], 'manual_upload')
                self.assertEqual(state['runs'][0]['comparison']['old'], 'v1')
                self.assertEqual(review_mock.call_count, 3)
                self.assertEqual(self.client.get('/api/projects/project-1/workflow').json()['review']['status'], 'completed')
                self.assertEqual(self.client.get('/api/projects/project-1/sources/v2').status_code, 404)
                self.assertEqual(self.client.get(f'/api/projects/project-1/sources/{version}').content, uploaded)
                detail = self.client.get('/api/projects/project-1').json()['docket_details'][0]
                self.assertEqual(detail['versions'][version]['status'], 'draft_proposal')
                self.assertEqual(detail['versions'][version]['original_filename'], 'proposed-revision.pdf')
                self.assertEqual(detail['versions'][version]['revises'], 'v1')
                self.assertEqual(detail['versions'][version]['filing_id'], 'filing-1')
                self.assertEqual(detail['versions'][version]['sha256'], hashlib.sha256(uploaded).hexdigest())
                self.assertEqual(self.client.get('/api/projects/project-1/changes').json()['total'],
                                 len(json.loads((Path(directory) / 'data' / 'local' / 'uploads' / 'R.25-06-019' / f'v1-{version}.json').read_text())['changes']))
                duplicate = self.client.post('/api/projects/project-1/revisions' + query,
                                             content=uploaded, headers={'content-type': 'application/pdf'})
                self.assertEqual(duplicate.status_code, 409)
                self.assertEqual(self.client.post('/api/projects/project-1/reset-demo').status_code, 409)
                self.assertEqual(self.client.post('/api/projects/project-1/revisions' + query,
                                                  content=b'not a PDF').status_code, 422)

                independent = small_pdf('Separate docket filing for grid operations')
                fresh = self.client.post('/api/projects/project-1/filings?title=Grid%20operations&original_filename=grid-operations.pdf&issued_on=2026-01-20&status=final',
                                         content=independent, headers={'content-type': 'application/pdf'})
                self.assertEqual(fresh.status_code, 200, fresh.text)
                fresh_version = fresh.json()['version']
                self.assertEqual(fresh.json()['workflow']['visible_versions'], ['v1', version, fresh_version])
                self.assertEqual(len(fresh.json()['workflow']['runs']), 1)
                self.assertEqual(review_mock.call_count, 3)
                detail = self.client.get('/api/projects/project-1').json()['docket_details'][0]
                self.assertIsNone(detail['versions'][fresh_version]['revises'])
                self.assertEqual(detail['versions'][fresh_version]['original_filename'], 'grid-operations.pdf')
                self.assertNotEqual(detail['versions'][fresh_version]['filing_id'], 'filing-1')
                self.assertEqual(detail['versions'][fresh_version]['sha256'], hashlib.sha256(independent).hexdigest())
                self.assertEqual(self.client.get(f'/api/projects/project-1/sources/{fresh_version}').content, independent)
                self.assertFalse(any(item['new'] == fresh_version for item in detail['comparisons']))

                next_pdf = small_pdf('Revised docket filing for grid operations')
                next_query = f'?revises={fresh_version}&title=Grid%20operations%20revision&original_filename=grid-operations-revised.pdf&issued_on=2026-01-21&status=draft_proposal'
                next_revision = self.client.post('/api/projects/project-1/filings' + next_query,
                                                 content=next_pdf, headers={'content-type': 'application/pdf'})
                self.assertEqual(next_revision.status_code, 200, next_revision.text)
                next_version = next_revision.json()['version']
                self.assertEqual(next_revision.json()['workflow']['runs'][-1]['comparison']['old'], fresh_version)
                self.assertEqual(review_mock.call_count, 6)
                detail = self.client.get('/api/projects/project-1').json()['docket_details'][0]
                self.assertEqual(detail['versions'][next_version]['filing_id'], detail['versions'][fresh_version]['filing_id'])
                self.assertEqual(detail['versions'][next_version]['revises'], fresh_version)

    def test_introduce_runs_reviews_and_attaches_source_changes(self):
        class ImmediateThread:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        def assessment_for(_context, obligation, _agent):
            if _context.comparison['new'] == 'v2':
                return ImpactAssessment(project_id='project-1', obligation_id=obligation.id,
                    outcome='not_affected', summary='No further substantive change.',
                    rationale='Finalization only.', proposed_action='No update proposed.',
                    reviewer_role='Record owner')
            citations = {
                'OBL-01': [Citation(change_id='change-0248', side='new'), Citation(change_id='change-0214', side='new')],
                'OBL-02': [Citation(change_id='change-0249', side='new')],
                'OBL-03': [],
            }[obligation.id]
            return ImpactAssessment(
                project_id='project-1', obligation_id=obligation.id,
                outcome='not_affected' if obligation.id == 'OBL-03' else 'needs_review',
                summary=f'Draft for {obligation.id}', rationale='Inspect the linked source.',
                proposed_action='Ask the record owner to review.', reviewer_role='Record owner',
                open_questions=[] if obligation.id == 'OBL-03' else ['Does the final decision apply?'],
                citations=citations,
            )

        with patch('backend.workflow.read_provider_settings', return_value=('test-key', 'test-model')), \
             patch('backend.workflow.OpenRouterProvider'), \
             patch('backend.workflow.OpenRouterModel'), \
             patch('backend.workflow.build_agent'), \
             patch('backend.workflow.Thread', ImmediateThread), \
             patch('backend.workflow.review', side_effect=assessment_for) as reviewer:
            self.client.post('/api/projects/project-1/introduce-change')
            state = self.client.get('/api/projects/project-1/workflow').json()
            self.assertEqual(state['review']['status'], 'completed')
            self.assertEqual(state['review']['completed'], 3)
            self.assertEqual(len(state['results']), 3)
            self.assertEqual(len(state['results'][0]['assessment']['citations']), 2)
            self.assertEqual(state['results'][2]['assessment']['outcome'], 'not_affected')
            self.assertEqual(len(state['runs']), 1)
            self.assertEqual(state['runs'][0]['review']['status'], 'completed')
            self.assertEqual(state['runs'][0]['results'], state['results'])
            self.client.post('/api/projects/project-1/introduce-change')
            state = self.client.get('/api/projects/project-1/workflow').json()
            self.assertEqual(state['visible_version'], 'v2')
            self.assertEqual(len(state['runs']), 2)
            self.assertEqual(state['runs'][1]['comparison']['old'], 'v1a')
            self.assertEqual(reviewer.call_count, 6)
            self.client.post('/api/projects/project-1/introduce-change')
            self.assertEqual(reviewer.call_count, 6)
        self.assertEqual(self.client.get('/api/projects/project-1/changes/change-0248?old=v1&new=v1a').status_code, 200)
        self.assertEqual(self.client.get('/api/projects/project-1/changes?old=v1a&new=v2').json()['total'], 3)

    def test_retry_preserves_failed_run(self):
        class ImmediateThread:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        with patch('backend.workflow.read_provider_settings', return_value=(None, 'model')):
            failed = self.client.post('/api/projects/project-1/introduce-change').json()
        failed_id = failed['runs'][0]['run_id']
        self.assertEqual(failed['runs'][0]['review']['status'], 'failed')

        def assessment_for(_context, obligation, _agent):
            return ImpactAssessment(
                project_id='project-1', obligation_id=obligation.id, outcome='not_affected',
                summary='No direct relationship found.', rationale='No matching source change.',
                proposed_action='No change proposed.', reviewer_role='Record owner',
            )

        with patch('backend.workflow.read_provider_settings', return_value=('test-key', 'test-model')), \
             patch('backend.workflow.OpenRouterProvider'), patch('backend.workflow.OpenRouterModel'), \
             patch('backend.workflow.build_agent'), patch('backend.workflow.Thread', ImmediateThread), \
             patch('backend.workflow.review', side_effect=assessment_for):
            self.client.post('/api/projects/project-1/introduce-change')
        state = self.client.get('/api/projects/project-1/workflow').json()
        self.assertEqual([run['trigger'] for run in state['runs']], ['new_version', 'retry'])
        self.assertEqual(state['runs'][0]['run_id'], failed_id)
        self.assertEqual(state['runs'][0]['review']['status'], 'failed')
        self.assertEqual(state['runs'][0]['results'], [])
        self.assertEqual(state['runs'][1]['review']['status'], 'completed')
        self.assertEqual(len(state['runs'][1]['results']), 3)

    def test_reset_returns_to_initial_docket_and_replay_starts_new_history(self):
        with patch('backend.workflow.read_provider_settings', return_value=(None, 'model')):
            first = self.client.post('/api/projects/project-1/introduce-change').json()
            reset = self.client.post('/api/projects/project-1/reset-demo').json()
            replay = self.client.post('/api/projects/project-1/introduce-change').json()
        self.assertEqual(reset['visible_version'], 'v1')
        self.assertEqual(reset['obligation_version'], 'v1')
        self.assertEqual(reset['review']['status'], 'idle')
        self.assertEqual(reset['results'], [])
        self.assertEqual(reset['runs'], [])
        self.assertEqual(replay['visible_version'], 'v1a')
        self.assertEqual(len(replay['runs']), 1)
        self.assertEqual(replay['runs'][0]['trigger'], 'new_version')
        self.assertNotEqual(replay['runs'][0]['run_id'], first['runs'][0]['run_id'])
        self.client.post('/api/projects/project-1/reset-demo')
        self.assertEqual(self.client.get('/api/projects/project-1/changes').status_code, 404)
        self.assertEqual(self.client.get('/api/projects/project-1/sources/v2').status_code, 404)
        self.assertEqual(self.client.get('/api/projects/project-1/sources/v1').status_code, 200)

    def test_reset_invalidates_pending_review_worker(self):
        pending = []

        class DeferredThread:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args

            def start(self):
                pending.append(self)

        with patch('backend.workflow.read_provider_settings', return_value=('test-key', 'test-model')), \
             patch('backend.workflow.Thread', DeferredThread), \
             patch('backend.workflow.OpenRouterModel', side_effect=AssertionError('cancelled worker started')):
            first = self.client.post('/api/projects/project-1/introduce-change').json()
            self.client.post('/api/projects/project-1/reset-demo')
            second = self.client.post('/api/projects/project-1/introduce-change').json()
            pending[0].target(*pending[0].args)
            self.assertEqual(workflow._active['project-1'], second['review']['run_id'])
            self.assertNotEqual(first['review']['run_id'], second['review']['run_id'])
            self.client.post('/api/projects/project-1/reset-demo')
        state = self.client.get('/api/projects/project-1/workflow').json()
        self.assertEqual(state['review']['status'], 'idle')
        self.assertEqual(state['runs'], [])

    def test_old_workflow_is_exposed_without_inventing_trigger(self):
        state = self.client.get('/api/projects/project-1/workflow').json()
        state.pop('runs')
        state['visible_version'] = 'v2'
        state['review'].update(status='completed', run_id='legacy-run', started_at='2026-09-29T04:00:00+00:00')
        workflow._write('project-1', state)
        restored = self.client.get('/api/projects/project-1/workflow').json()
        self.assertEqual(len(restored['runs']), 1)
        self.assertEqual(restored['runs'][0]['trigger'], 'unknown')
        self.assertEqual(restored['runs'][0]['run_id'], 'legacy-run')

    def test_manager_decisions_are_recorded_and_can_be_corrected(self):
        state = self.client.get('/api/projects/project-1/workflow').json()
        state.pop('decisions')  # Existing workflow files do not yet have this field.
        state['visible_version'] = 'v2'
        state['visible_versions'] = ['v1', 'v1a', 'v2']
        state['review'].update(status='completed', run_id='run-1', completed=2)
        state['results'] = [
            {'obligation_id': 'OBL-01', 'status': 'complete', 'assessment': {'outcome': 'affected'}},
            {'obligation_id': 'OBL-03', 'status': 'complete', 'assessment': {'outcome': 'not_affected'}},
        ]
        state['runs'] = [{'run_id': 'run-1', 'trigger': 'new_version',
                          'comparison': {'docket_id': 'R.25-06-019', 'old': 'v1a', 'new': 'v2'},
                          'review': state['review'], 'results': state['results']}]
        workflow._write('project-1', state)

        body = {'run_id': 'run-1', 'obligation_id': 'OBL-01', 'action': 'accept'}
        accepted = self.client.post('/api/projects/project-1/decisions', json=body)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()['decisions'][0]['action'], 'accept')
        self.assertEqual(accepted.json()['decisions'][0]['actor_label'], 'Local manager')
        self.assertEqual(accepted.json()['results'], state['results'])
        self.assertEqual(len(self.client.post('/api/projects/project-1/decisions', json=body).json()['decisions']), 1)

        corrected = self.client.post('/api/projects/project-1/decisions', json={**body, 'action': 'route_to_legal'})
        self.assertEqual([item['action'] for item in corrected.json()['decisions']], ['accept', 'route_to_legal'])
        self.assertEqual(len(self.client.get('/api/projects/project-1/workflow').json()['decisions']), 2)
        self.assertEqual(self.client.post('/api/projects/project-1/decisions', json={**body, 'run_id': 'older'}).status_code, 409)
        self.assertEqual(self.client.post('/api/projects/project-1/decisions', json={**body, 'obligation_id': 'OBL-03'}).status_code, 409)
        self.assertEqual(self.client.post('/api/projects/project-1/decisions', json={**body, 'action': 'invalid'}).status_code, 422)
        self.assertEqual(len(self.client.get('/api/projects/project-1/workflow').json()['decisions']), 2)

        state = self.client.get('/api/projects/project-1/workflow').json()
        state['review']['status'] = 'partial'
        state['runs'][-1]['review']['status'] = 'partial'
        workflow._write('project-1', state)
        with patch('backend.workflow.read_provider_settings', return_value=(None, 'model')):
            retried = self.client.post('/api/projects/project-1/introduce-change').json()
        self.assertEqual(len(retried['runs']), 2)
        self.assertEqual(len(retried['decisions']), 2)
        self.assertEqual(self.client.post('/api/projects/project-1/decisions', json=body).status_code, 409)

        reset = self.client.post('/api/projects/project-1/reset-demo').json()
        self.assertEqual(reset['decisions'], [])


if __name__ == '__main__':
    unittest.main()
