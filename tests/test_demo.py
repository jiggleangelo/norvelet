import http.client
import re
import threading
import unittest
import urllib.parse

from app.web import BANNER, make_server


class DemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(0, '127.0.0.1')
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data)
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'Content-Length': str(len(body)),
                **(headers or {}),
            }
        try:
            conn.request(method, path, body, headers or {})
            response = conn.getresponse()
            payload = response.read().decode(errors='replace')
            location = response.getheader('Location')
            return response.status, payload, location
        finally:
            conn.close()

    def test_flow(self):
        self.assertEqual(self.request('GET', '/healthz')[:2], (200, 'ok'))
        self.assertIn(BANNER, self.request('GET', '/')[1])
        prompt = '100 boxes 10x8x4 in plus 100 black cotton tees eco-friendly made in USA'
        status, draft, _ = self.request(
            'POST', '/intake',
            {'text': prompt, 'brand': 'Fernleaf <script>alert(1)</script>'},
        )
        self.assertEqual(status, 200)
        self.assertIn('&lt;script&gt;', draft)
        hidden = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', draft))
        fields = {
            **hidden,
            'box_qty': '100',
            'L': '10',
            'W': '8',
            'H': '4',
            'tee_qty': '100',
            'color': 'black',
        }
        status, _, location = self.request('POST', '/spec', fields)
        self.assertEqual(status, 303)
        sid = location.rsplit('/', 1)[-1]
        page = self.request('GET', location)[1]
        self.assertIn('Made in USA claim', page)
        self.assertIn('dieline.svg', page)
        blocked = self.request('GET', f'/spec/{sid}/quotes')[1]
        self.assertIn('Quotes blocked', blocked)
        status, _, location = self.request(
            'POST', f'/spec/{sid}/revise', {'claim': 'eco_friendly'},
        )
        self.assertEqual(status, 303)
        # Removing one claim must not bypass the remaining USA blocker.
        self.assertIn('Quotes blocked', self.request('GET', location)[1])
        status, _, location = self.request(
            'POST', f'/spec/{sid}/revise', {'claim': 'made_in_usa'},
        )
        self.assertEqual(status, 303)
        quote_page = self.request('GET', location)[1]
        self.assertIn('Seller of record', quote_page)
        self.assertIn('Platform fee', quote_page)
        qid = re.search(r'name="quote_id" value="([^"]+)"', quote_page).group(1)
        status, confirmation, _ = self.request(
            'POST', f'/spec/{sid}/order-intent',
            {
                'quote_id': qid,
                'email': 'pilot@example.test',
                'deposit_intent': 'yes',
            },
        )
        self.assertEqual(status, 200)
        self.assertIn('Nothing was charged', confirmation)
        self.assertIn('LOT-', confirmation)
        self.assertIn('Chain verified', self.request('GET', f'/audit/{sid}')[1])
        self.assertNotIn('pilot@example.test', self.request('GET', '/stats')[1])

    def test_intake_round_trip_preserves_tee_sizes_when_size_fields_are_omitted(self):
        prompt = '100 boxes 10x8x4 in plus 100 black cotton tees'
        status, draft, _ = self.request(
            'POST', '/intake', {'text': prompt, 'brand': 'Fernleaf'},
        )
        self.assertEqual(status, 200)
        fields = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', draft))
        for key in tuple(fields):
            if key.startswith('size_'):
                del fields[key]
        fields.update({
            'box_qty': '100',
            'L': '10',
            'W': '8',
            'H': '4',
            'tee_qty': '100',
            'color': 'black',
        })
        status, _, location = self.request('POST', '/spec', fields)
        self.assertEqual(status, 303)
        self.assertRegex(location, r'^/spec/[^/]+$')

    def test_errors(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            # request(..., body=None) inserts Content-Length: 0 for POST.
            # Use the low-level API to actually omit the header.
            conn.putrequest('POST', '/intake')
            conn.putheader('Content-Type', 'application/x-www-form-urlencoded')
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 411)
            self.assertIn(BANNER, response.read().decode())
        finally:
            conn.close()

        # An explicitly empty form has framing but lacks required fields.
        self.assertEqual(self.request('POST', '/intake')[0], 400)

        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            conn.putrequest('POST', '/intake')
            conn.putheader('Content-Type', 'application/x-www-form-urlencoded')
            conn.putheader('Content-Length', '70000')
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 413)
            response.read()
        finally:
            conn.close()
        self.assertEqual(self.request('GET', '/spec/not-known')[0], 404)


if __name__ == '__main__':
    unittest.main()
