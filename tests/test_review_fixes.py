import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from urllib.parse import urlencode, urlsplit

from app.proxy_server import ProxySafeBoundedHTTPServer
from app.usable import make_server
from app.workspace import WorkspaceStore, token_hash


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.origin = 'https://app-norvelet.manykiln.com'
        self.server = make_server(0, '127.0.0.1', self.origin, self.temp.name)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, fields=None):
        body = None
        headers = {'Host': 'app-norvelet.manykiln.com'}
        if fields is not None:
            body = urlencode(fields, doseq=True)
            headers.update({'Origin': self.origin, 'Content-Type': 'application/x-www-form-urlencoded',
                            'Content-Length': str(len(body.encode()))})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, response.read().decode(errors='replace'), response.getheader('Location')
        finally:
            connection.close()

    def workspace(self):
        status, body, location = self.request('POST', '/w', {})
        self.assertEqual(status, 303)
        return urlsplit(location).path

    def project(self, workspace):
        status, body, location = self.request('POST', workspace + '/p', {
            'brand': 'Test', 'text': '10 boxes 10x8x4 in plus 24 cotton tees DTG',
        })
        review = urlsplit(location).path
        page = self.request('GET', review)[1]
        fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', page))
        fields.update({'size_S': '6', 'size_M': '6', 'size_L': '6', 'size_XL': '6'})
        status, body, location = self.request('POST', review + '/spec', fields)
        self.assertEqual(status, 303)
        return urlsplit(location).path

    def export(self, workspace):
        return json.loads(self.request('GET', workspace + '/export')[1])

    def test_import_route_is_reachable_and_round_trips(self):
        source = self.workspace()
        self.project(source)
        target = self.workspace()
        status, body, location = self.request('POST', target + '/p/import', {'json': json.dumps(self.export(source))})
        self.assertEqual(status, 303)
        self.assertIn('specification v1', self.request('GET', urlsplit(location).path)[1])

    def test_imported_quote_is_validated_escaped_and_capped(self):
        source = self.workspace()
        project = self.project(source)
        self.request('POST', project + '/quotes', {
            'supplier': 'Safe', 'desc_0': 'Boxes', 'unit_0': '1', 'shipping': '0',
            'lead_time': '10', 'terms': 'ok',
        })
        exported = self.export(source)
        exported['projects'][0]['quotes'][0]['lead_time_days'] = '<a href="https://evil.test">x</a>'
        target = self.workspace()
        self.assertEqual(self.request('POST', target + '/p/import', {'json': json.dumps(exported)})[0], 400)
        exported = self.export(source)
        exported['projects'][0]['quotes'] *= 21
        self.assertEqual(self.request('POST', target + '/p/import', {'json': json.dumps(exported)})[0], 400)

    def test_quote_remove_action_is_absolute_and_works(self):
        workspace = self.workspace()
        project = self.project(workspace)
        self.request('POST', project + '/quotes', {'supplier': 'Mistake', 'desc_0': 'Boxes', 'unit_0': '1',
                                                   'shipping': '0', 'lead_time': '1', 'terms': ''})
        page = self.request('GET', project)[1]
        self.assertIn('action="' + project + '/quotes"', page)
        quote_id = self.export(workspace)['projects'][0]['quotes'][0]['id']
        self.assertEqual(self.request('POST', project + '/quotes', {'remove': quote_id})[0], 303)
        self.assertNotIn('Mistake', self.request('GET', project)[1])

    def test_private_and_shared_dieline_download_links_work(self):
        workspace = self.workspace()
        project = self.project(workspace)
        page = self.request('GET', project)[1]
        self.assertIn('href="' + project + '/dieline.svg"', page)
        self.assertEqual(self.request('GET', project + '/dieline.svg')[0], 200)
        share_page = self.request('POST', workspace + '/share', {})[1]
        share = re.search(r'href="https://app-norvelet.manykiln.com(/s/[^"]+)', share_page).group(1)
        shared = self.request('GET', share)[1]
        link = re.search(r'href="(/s/[^"]+/p/[^"]+/dieline\.svg)"', shared).group(1)
        self.assertEqual(self.request('GET', link)[0], 200)

    def test_server_has_only_a_global_worker_bound(self):
        self.assertIsInstance(self.server, ProxySafeBoundedHTTPServer)
        self.assertFalse(hasattr(self.server, '_peer_limit'))
        self.assertFalse(hasattr(self.server, '_peers'))

    def test_workspace_creation_has_no_global_sixty_request_kill_switch(self):
        store = WorkspaceStore(self.temp.name, max_workspaces=70)
        tokens = [store.create()[0] for unused in range(61)]
        self.assertEqual(len(tokens), 61)

    def test_centimetres_are_accepted_by_edit_form(self):
        workspace = self.workspace()
        status, body, location = self.request('POST', workspace + '/p', {'brand': 'Metric', 'text': '10 boxes 10x8x4 in'})
        review = urlsplit(location).path
        fields = {'box_qty': '10', 'dimension_unit': 'cm', 'L': '25.4', 'W': '20.32', 'H': '10.16',
                  'board': '32ECT-B-kraft', 'print_sides': 'top', 'ink_colors': '1',
                  'claims': '', 'audience': 'adult'}
        self.assertEqual(self.request('POST', review + '/spec', fields)[0], 303)
        spec = self.export(workspace)['projects'][0]['versions'][0]['spec']
        self.assertEqual((spec['box']['L_mil'], spec['box']['W_mil'], spec['box']['H_mil']), (10000, 8000, 4000))

    def test_quote_has_four_lines_and_ignores_posted_quantities(self):
        workspace = self.workspace()
        project = self.project(workspace)
        page = self.request('GET', project)[1]
        self.assertEqual(page.count('<legend>Price line '), 4)
        self.request('POST', project + '/quotes', {'supplier': 'Quoted', 'item_0': 'boxes', 'desc_0': 'Boxes',
                                                   'qty_0': '999999', 'unit_0': '2', 'shipping': '0',
                                                   'lead_time': '4', 'terms': ''})
        quote = self.export(workspace)['projects'][0]['quotes'][0]
        self.assertEqual(quote['lines'][0]['quantity'], 10)
        self.assertEqual(quote['goods_cents'], 2000)

    def test_validation_error_preserves_typed_values_and_explains_problem(self):
        workspace = self.workspace()
        project = self.project(workspace)
        status, page, location = self.request('POST', project + '/quotes', {
            'supplier': 'Keep Me', 'item_0': 'boxes', 'unit_0': 'bad-price',
            'shipping': '0', 'lead_time': '7', 'terms': 'Keep these terms',
        })
        self.assertEqual(status, 400)
        self.assertIn('Prices require dollars', page)
        self.assertIn('value="Keep Me"', page)
        self.assertIn('Keep these terms', page)
        status, page, location = self.request('POST', project + '/spec', {
            'box_qty': '10', 'L': '10', 'W': '8', 'H': '4', 'board': '32ECT-B-kraft',
            'print_sides': 'top', 'ink_colors': '1', 'tee_qty': '24', 'color': 'typed-purple',
            'fiber': 'Cotton:99', 'method': 'dtg', 'placements': 'front', 'size_S': '6',
            'size_M': '6', 'size_L': '6', 'size_XL': '6', 'origin_blank': 'USA',
            'origin_sewing': 'USA', 'origin_print': 'USA', 'claims': '', 'audience': 'adult',
        })
        self.assertEqual(status, 400)
        self.assertIn('Fiber percentages must total 100', page)
        self.assertIn('value="typed-purple"', page)

    def test_sweep_removes_share_index(self):
        store = WorkspaceStore(self.temp.name)
        token, data = store.create()
        share = store.rotate_share(token)
        digest = token_hash(token)
        old = 1
        os.utime(store._file(digest), (old, old))
        store.sweep()
        self.assertFalse(os.path.exists(os.path.join(store.share_root, token_hash(share))))

    def test_lock_objects_are_not_evicted_while_workspaces_exist(self):
        store = WorkspaceStore(self.temp.name, max_workspaces=2)
        first = store._lock('a' * 64)
        first.acquire()
        try:
            store._lock('b' * 64)
            store._lock('c' * 64)
            self.assertIs(store._lock('a' * 64), first)
        finally:
            first.release()

    def test_malformed_stored_record_returns_http_error(self):
        workspace = self.workspace()
        digest = token_hash(workspace.split('/')[-1])
        path = self.server.workspace_store._file(digest)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write('{"projects":[{}]}')
        status, body, location = self.request('GET', workspace)
        self.assertIn(status, (400, 500))
        self.assertTrue(body)

    def test_stale_quotes_are_not_marked_cheapest_or_fastest(self):
        workspace = self.workspace()
        project = self.project(workspace)
        self.request('POST', project + '/quotes', {'supplier': 'Old', 'desc_0': 'Boxes', 'unit_0': '0.01',
                                                   'shipping': '0', 'lead_time': '1', 'terms': ''})
        page = self.request('GET', project)[1]
        fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', page))
        fields.update({'size_S': '6', 'size_M': '6', 'size_L': '6', 'size_XL': '6'})
        self.request('POST', project + '/spec', fields)
        self.request('POST', project + '/quotes', {'supplier': 'Current', 'desc_0': 'Boxes', 'unit_0': '2',
                                                   'shipping': '0', 'lead_time': '10', 'terms': ''})
        page = self.request('GET', project)[1]
        old_row = re.search(r'<tr class="([^"]*)"><td>Old</td>.*?</tr>', page).group(1)
        current_row = re.search(r'<tr class="([^"]*)"><td>Current</td>.*?</tr>', page).group(1)
        self.assertNotIn('best', old_row)
        self.assertIn('best', current_row)

    def test_child_synonyms_are_normalized_and_audience_is_constrained(self):
        workspace = self.workspace()
        status, body, location = self.request('POST', workspace + '/p', {'brand': 'Child', 'text': '24 adult cotton tees DTG'})
        review = urlsplit(location).path
        page = self.request('GET', review)[1]
        fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', page))
        fields.update({'audience': 'toddler', 'size_S': '6', 'size_M': '6', 'size_L': '6', 'size_XL': '6'})
        status, body, location = self.request('POST', review + '/spec', fields)
        self.assertEqual(status, 303)
        self.assertIn('CPSIA', self.request('GET', urlsplit(location).path)[1])
        fields['audience'] = 'anything goes'
        self.assertEqual(self.request('POST', review + '/spec', fields)[0], 400)


if __name__ == '__main__':
    unittest.main()
