import html
import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

from app.usable import NOTICE, make_server


class FormFields(HTMLParser):
    """Read successful controls from one form, including selected options."""
    def __init__(self, action):
        super().__init__(convert_charrefs=True)
        self.action = action
        self.active = False
        self.fields = {}
        self.select = None
        self.textarea = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.active = attrs.get('action') == self.action
        if not self.active:
            return
        name = attrs.get('name')
        if tag == 'input' and name and 'disabled' not in attrs:
            kind = attrs.get('type', 'text')
            if kind not in ('submit', 'button', 'file') and (kind not in ('checkbox', 'radio') or 'checked' in attrs):
                self.fields[name] = attrs.get('value', '')
        elif tag == 'select' and name:
            self.select = name
        elif tag == 'option' and self.select:
            if self.select not in self.fields or 'selected' in attrs:
                self.fields[self.select] = attrs.get('value', '')
        elif tag == 'textarea' and name:
            self.textarea = name
            self.fields[name] = ''

    def handle_data(self, data):
        if self.active and self.textarea:
            self.fields[self.textarea] += data

    def handle_endtag(self, tag):
        if tag == 'form':
            self.active = False
        elif tag == 'select':
            self.select = None
        elif tag == 'textarea':
            self.textarea = None


def form_fields(page, action):
    parser = FormFields(action)
    parser.feed(page)
    if not parser.fields:
        raise AssertionError('No form controls found for ' + action)
    return parser.fields


class UsableAppTests(unittest.TestCase):
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

    def request(self, method, path, fields=None, origin=True, host='app-norvelet.manykiln.com'):
        body = None
        headers = {'Host': host}
        if fields is not None:
            body = urlencode(fields, doseq=True)
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            headers['Content-Length'] = str(len(body.encode()))
        if method == 'POST' and origin:
            headers['Origin'] = self.origin
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            payload = response.read().decode(errors='replace')
            return response.status, payload, response.getheader('Location')
        finally:
            connection.close()

    def local_path(self, location):
        return urlsplit(location).path

    def workspace(self):
        status, _, location = self.request('POST', '/w', {})
        self.assertEqual(status, 303)
        return self.local_path(location)

    def project(self, workspace, prompt='250 white mailer boxes 30x22x8 cm 2-color inside print plus 60 tri-blend heather tees DTG, recyclable'):
        status, _, location = self.request('POST', workspace + '/p', {'brand': 'Fernleaf', 'text': prompt})
        self.assertEqual(status, 303)
        review = self.local_path(location)
        page = self.request('GET', review)[1]
        if 'Request refused' in page:
            return review
        fields = form_fields(page, review + '/spec')
        fields.update({'size_S': '15', 'size_M': '15', 'size_L': '15', 'size_XL': '15'})
        status, _, location = self.request('POST', review + '/spec', fields)
        self.assertEqual(status, 303)
        return self.local_path(location)

    def test_core_flow_export_persistence_and_notice(self):
        self.assertEqual(self.request('GET', '/healthz')[:2], (200, 'ok'))
        home = self.request('GET', '/')[1]
        self.assertIn(NOTICE, home)
        self.assertIn('manykiln.com/privacy', home)
        workspace = self.workspace()
        project = self.project(workspace)
        sheet = self.request('GET', project)[1]
        for text in ('50% Cotton', 'Blank size:', 'Recyclable claim', 'Request supplier declarations', 'RFQ'):
            self.assertIn(text, sheet)
        status, payload, _ = self.request('GET', workspace + '/export')
        self.assertEqual(status, 200)
        exported = json.loads(payload)
        self.assertEqual(len(exported['projects']), 1)
        spec = exported['projects'][0]['versions'][0]['spec']
        self.assertEqual(spec['box']['L_mil'], 11811)
        self.assertEqual(spec['tee']['method'], 'dtg')
        self.assertFalse(spec['origin_user_asserted'])
        token = workspace.split('/')[-1]
        for root, dirs, files in os.walk(self.temp.name):
            for name in files:
                with open(os.path.join(root, name), 'rb') as handle:
                    self.assertNotIn(token.encode(), handle.read())
        self.assertNotIn('250 white mailer', payload)

    def test_revision_quotes_children_share_and_security(self):
        workspace = self.workspace()
        project = self.project(workspace)
        status, _, location = self.request('POST', project + '/quotes', {
            'supplier': 'One Box Co', 'desc_0': 'Boxes', 'qty_0': '250', 'unit_0': '1.25',
            'desc_1': 'Tees', 'qty_1': '60', 'unit_1': '8.50', 'shipping': '40.00',
            'lead_time': '14', 'terms': 'Net 30',
        })
        self.assertEqual(status, 303)
        self.assertIn('$862.50', self.request('GET', project)[1])
        self.assertEqual(self.request('POST', '/w', {}, host='wrong.example')[0], 400)
        self.assertEqual(self.request('POST', '/w', {}, origin=False)[0], 403)
        self.assertEqual(self.request('POST', '/w', {}, origin=True)[0], 303)
        status, share_page, _ = self.request('POST', workspace + '/share', {})
        self.assertEqual(status, 200)
        share_match = re.search(r'href="https://app-norvelet\.manykiln\.com([^"]+)"', share_page)
        self.assertIsNotNone(share_match)
        share_path = share_match.group(1)
        self.assertIn('Read-only', self.request('GET', share_path)[1])
        self.assertEqual(self.request('POST', share_path, {})[0], 404)
        child_workspace = self.workspace()
        child_project = self.project(child_workspace, '60 children cotton tees DTG')
        child = self.request('GET', child_project)[1]
        self.assertIn('CPSIA', child)
        self.assertNotIn('Label draft:', child)
        self.assertNotIn('copy and send', child)

    def test_delete(self):
        workspace = self.workspace()
        self.request('POST', workspace + '/delete', {})
        self.assertEqual(self.request('GET', workspace)[0], 404)

    def test_form_helper_reads_entities_selects_and_checked_controls(self):
        fields = form_fields('<form action="/save"><input name="brand" value="A &amp; B"><input type="checkbox" name="unchecked" value="yes"><input type="checkbox" name="checked" value="yes" checked><select name="unit"><option value="in">in</option><option value="cm" selected>cm</option></select><textarea name="terms">Net &amp; 30</textarea></form>', '/save')
        self.assertEqual(fields, {'brand': 'A & B', 'checked': 'yes', 'unit': 'cm', 'terms': 'Net & 30'})


if __name__ == '__main__':
    unittest.main()
