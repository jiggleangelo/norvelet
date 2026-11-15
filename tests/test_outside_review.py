import copy
import io
import json
import os
import secrets
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from dataclasses import asdict
from unittest.mock import patch
from urllib.parse import urlsplit

from app.usable import RuleParser, build, draft_dict, editor, import_projects, make_server, new_project, version_record
from app.workspace import NotFound, RateError, WorkspaceStore, token_hash
from tests.test_app import UsableAppTests, form_fields


class StorageReviewTests(unittest.TestCase):
    def test_random_nonexistent_tokens_do_not_allocate_locks_or_rate_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceStore(directory)
            locks = tuple(store.locks)
            for _ in range(2000):
                token = secrets.token_urlsafe(24)
                for operation in (lambda: store.mutate(token, lambda data: None), lambda: store.delete(token), lambda: store.rotate_share(token)):
                    with self.assertRaises(NotFound):
                        operation()
            self.assertEqual(tuple(store.locks), locks)
            self.assertEqual(store.write_windows, {})

    def test_creation_budget_persists_and_existing_workspace_remains_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceStore(directory)
            # Mocking time.time does not change filesystem modification times.
            # Anchor the budget clock to real time so startup does not expire
            # freshly created workspaces against an unrelated future epoch.
            started = time.time()
            with patch('app.workspace.time.time', return_value=started):
                tokens = [store.create(rate_limited=True)[0] for _ in range(60)]
                with self.assertRaises(RateError):
                    store.create(rate_limited=True)
                restarted = WorkspaceStore(directory)
                with self.assertRaises(RateError):
                    restarted.create(rate_limited=True)
                restarted.mutate(tokens[0], lambda data: data['projects'].append({'id': 'saved'}))
                self.assertEqual(restarted.load(tokens[0])['projects'], [{'id': 'saved'}])
            with patch('app.workspace.time.time', return_value=started + 3599):
                with self.assertRaises(RateError):
                    restarted.create(rate_limited=True)
            # Entries are retained only when entry > now - 3600, so they
            # expire exactly at 3600 seconds, well before the 86400-second TTL.
            with patch('app.workspace.time.time', return_value=started + 3600):
                restarted.create(rate_limited=True)
                self.assertEqual(restarted.load(tokens[0])['projects'], [{'id': 'saved'}])

    def test_delete_share_and_sweep_use_byte_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceStore(directory)
            first, _ = store.create()
            second, _ = store.create()
            with patch.object(store, '_disk_total', side_effect=AssertionError('unexpected directory scan')):
                store.rotate_share(first)
                store.rotate_share(first)
                store.delete(first)
                os.utime(store._file(token_hash(second)), (1, 1))
                store.sweep()
            self.assertEqual(store.total, store._disk_total())
            self.assertFalse(store.write_windows)

    def test_atomic_replace_and_tombstone_sync_files_and_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceStore(directory)
            events = []
            real_sync = os.fsync
            real_replace = os.replace
            def sync(fd):
                events.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
                real_sync(fd)
            def replace(source, destination):
                events.append(('replace', destination))
                real_replace(source, destination)
            with patch('app.workspace.os.fsync', side_effect=sync), patch('app.workspace.os.replace', side_effect=replace):
                token, _ = store.create()
                store.mutate(token, lambda data: data.update(sample=True))
                store.delete(token)
            replacements = [i for i, item in enumerate(events) if isinstance(item, tuple)]
            self.assertGreaterEqual(len(replacements), 3)
            for index in replacements:
                self.assertEqual(events[index - 1], 'file')
                self.assertEqual(events[index + 1], 'directory')
            self.assertTrue(any('tombstones' in events[index][1] for index in replacements))


class SchemaReviewTests(unittest.TestCase):
    def exported(self):
        draft = RuleParser().parse('24 cotton tees DTG plus 10 boxes', 'Brand')
        spec = build(draft, 'Brand')
        project = new_project(draft, 'Brand')
        project['versions'] = [version_record(spec)]
        return project

    def test_import_rejects_bad_draft_claims_sizes_and_color_before_storage(self):
        mutations = [
            lambda p: p['draft'].update(extra=True),
            lambda p: p['draft']['tee'].update(color=[]),
            lambda p: p['versions'][0]['spec'].update(claims=['invented']),
            lambda p: p['versions'][0]['spec']['tee'].update(color=[]),
            lambda p: p['versions'][0]['spec']['tee'].update(sizes={'XXL': 24}),
            lambda p: p['versions'][0]['spec']['tee'].update(qty=True),
            lambda p: p['versions'][0]['spec'].update(origin_user_asserted='false'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                project = self.exported()
                mutate(project)
                with self.assertRaises(ValueError):
                    import_projects(project)
        normalized = import_projects(self.exported())[0]
        self.assertEqual(normalized['draft']['tee']['origin']['blank'], 'Unknown')
        self.assertEqual(normalized['versions'][0]['spec']['tee']['sizes'], {'S': 6, 'M': 6, 'L': 6, 'XL': 6})

    def test_all_quick_fill_fields_require_confirmation_and_origins_are_unknown(self):
        draft = RuleParser().parse('24 tees plus 10 boxes')
        self.assertEqual(set(draft.tee.origin.values()), {'Unknown'})
        for field in ('board', 'color', 'method', 'S/M/L/XL split', 'blank origin', 'sewing origin', 'print origin', 'placements', 'print sides', 'ink colors'):
            self.assertIn(field, draft.low_confidence)
        markup = editor(draft)
        self.assertIn('<mark>Please confirm</mark> Board', markup)
        self.assertIn('<mark>Please confirm</mark> blank origin', markup)
        self.assertNotIn('Honduras', markup)

    def test_child_build_and_import_are_refused(self):
        draft = RuleParser().parse('24 children cotton tees plus 10 boxes')
        with self.assertRaisesRegex(ValueError, 'CPSIA'):
            build(draft, 'Brand')
        project = self.exported()
        project['versions'][0]['spec']['audience'] = 'children'
        with self.assertRaisesRegex(ValueError, 'CPSIA'):
            import_projects(project)


class HTTPReviewTests(UsableAppTests):
    def test_http_creation_uses_budget_for_normal_and_sample_routes(self):
        with patch.object(self.server.workspace_store, '_creation_budget', side_effect=RateError('limited')):
            self.assertEqual(self.request('POST', '/w', {})[0], 429)
            self.assertEqual(self.request('POST', '/w/sample', {})[0], 429)
        self.assertEqual(self.request('GET', '/healthz')[:2], (200, 'ok'))

    def test_expiry_is_explicit_and_reads_do_not_extend_it(self):
        workspace = self.workspace()
        path = self.server.workspace_store._file(token_hash(workspace.split('/')[-1]))
        original = os.stat(path).st_mtime_ns
        self.assertIn('Viewing or exporting does not extend', self.request('GET', workspace)[1])
        self.request('GET', workspace + '/export')
        self.assertEqual(os.stat(path).st_mtime_ns, original)

    def test_error_rendering_handles_disappearing_workspace_without_logging(self):
        workspace = self.workspace()
        project = self.project(workspace)
        stderr = io.StringIO()
        with patch.object(self.server.workspace_store, 'load', side_effect=NotFound('gone')), redirect_stderr(stderr):
            status, body, _ = self.request('POST', project + '/quotes', {'supplier': 'Test', 'desc_0': 'Boxes', 'unit_0': 'invalid'})
        self.assertEqual(status, 404)
        self.assertIn('no longer exists', body)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(self.request('GET', '/healthz')[:2], (200, 'ok'))

    def test_child_refusal_has_no_generated_outputs_or_exported_label(self):
        workspace = self.workspace()
        status, _, location = self.request('POST', workspace + '/p', {'brand': 'Child', 'text': '24 children cotton tees plus 10 boxes'})
        self.assertEqual(status, 303)
        project = urlsplit(location).path
        page = self.request('GET', project)[1]
        self.assertIn('CPSIA', page)
        for forbidden in ('Blank size:', 'Label draft:', 'RFQ', '<svg', 'Claims and labeling checklist'):
            self.assertNotIn(forbidden, page)
        self.assertEqual(self.request('GET', project + '/dieline.svg')[0], 404)
        exported = self.request('GET', workspace + '/export')[1]
        self.assertNotIn('label_content', exported)
        self.assertEqual(json.loads(exported)['projects'][0]['versions'], [])

    def test_add_missing_product_in_both_directions(self):
        for prompt, missing in [('10 boxes 10x8x4 in', 'tee'), ('24 cotton tees DTG', 'box')]:
            with self.subTest(missing=missing):
                workspace = self.workspace()
                status, _, location = self.request('POST', workspace + '/p', {'brand': 'Expand', 'text': prompt})
                self.assertEqual(status, 303)
                project = urlsplit(location).path
                fields = form_fields(self.request('GET', project)[1], project + '/spec')
                self.assertEqual(fields['include_' + missing], 'no')
                self.assertEqual(self.request('POST', project + '/spec', fields)[0], 303)
                fields = form_fields(self.request('GET', project)[1], project + '/spec')
                fields['include_' + missing] = 'yes'
                self.assertEqual(self.request('POST', project + '/spec', fields)[0], 303)
                exported = json.loads(self.request('GET', workspace + '/export')[1])
                spec = exported['projects'][0]['versions'][-1]['spec']
                self.assertIsNotNone(spec['box'])
                self.assertIsNotNone(spec['tee'])
                self.assertEqual(spec['version'], 2)

    def test_real_form_values_survive_review_round_trip(self):
        workspace = self.workspace()
        _, _, location = self.request('POST', workspace + '/p', {'brand': 'A & B', 'text': '24 cotton tees DTG plus 10 boxes'})
        project = urlsplit(location).path
        fields = form_fields(self.request('GET', project)[1], project + '/spec')
        fields.update(color='purple & gold', origin_blank='Canada', origin_sewing='Canada', origin_print='Canada', dimension_unit='cm', L='25.4', W='20.32', H='10.16')
        self.assertNotIn('origin_user_asserted', fields)
        self.assertEqual(self.request('POST', project + '/spec', fields)[0], 303)
        exported = json.loads(self.request('GET', workspace + '/export')[1])
        spec = exported['projects'][0]['versions'][0]['spec']
        self.assertEqual(spec['tee']['color'], 'purple & gold')
        self.assertEqual(spec['box']['L_mil'], 10000)
        self.assertFalse(spec['origin_user_asserted'])
        self.assertEqual(spec['brand'], 'A & B')


class HTTPSReviewTests(unittest.TestCase):
    def test_https_termination_requires_https_and_enables_hsts(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'HTTPS'):
                make_server(public_base_url='http://localhost', data_dir=directory, https_terminated=True)
            server = make_server(data_dir=directory, https_terminated=True)
            try:
                handler = object.__new__(server.RequestHandlerClass)
                headers = {}
                handler.send_response = lambda status: None
                handler.send_header = lambda key, value: headers.update({key: value})
                handler.end_headers = lambda: None
                handler.wfile = io.BytesIO()
                handler._send(200, 'ok')
                self.assertEqual(headers['Strict-Transport-Security'], 'max-age=31536000')
            finally:
                server.server_close()


if __name__ == '__main__':
    unittest.main()
