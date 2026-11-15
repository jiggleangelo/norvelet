"""Exercise real HTTP parsing and routing using in-memory sockets, without network."""
import html
import io
import json
import re
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from http.client import HTTPResponse
from urllib.parse import urlencode

from app.spec import revise
from app.store import Store
from app.web import BANNER, SAMPLES, make_server


class FakeClock:
    def now(self):
        return datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


class MemorySocket:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()
        self.timeout = None

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, value):
        self.timeout = value

    def shutdown(self, how):
        pass


class MemoryServer:
    def __init__(self, address, handler, **kwargs):
        self.server_name, self.server_port = address
        self.RequestHandlerClass = handler


class EndToEndDemoTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.server = make_server(store=self.store, clock=FakeClock(), server_class=MemoryServer)

    def request(self, method, path, fields=None):
        body = urlencode(fields or {}, doseq=True).encode()
        raw = (f'{method} {path} HTTP/1.0\r\nHost: demo.test\r\nContent-Length: {len(body)}\r\nContent-Type: application/x-www-form-urlencoded\r\n\r\n').encode() + body
        sock = MemorySocket(raw)
        self.server.RequestHandlerClass(sock, ('127.0.0.1', 12345), self.server)
        response = HTTPResponse(MemorySocket(bytes(sock.output)))
        response.begin()
        return response.status, response.read().decode(), dict(response.getheaders())

    def create(self, prompt=SAMPLES[0], brand='Fernleaf &copy; <demo>'):
        status, page, _ = self.request('POST', '/intake', {'text': prompt, 'brand': brand})
        self.assertEqual(status, 200)
        self.assertIn('Please confirm', page)
        fields = {k: html.unescape(v) for k, v in re.findall(r'<input(?: type="hidden")? name="([^"]+)" value="([^"]*)"', page)}
        status, _, headers = self.request('POST', '/spec', fields)
        self.assertEqual(status, 303)
        return headers['Location']

    def test_full_core_flow_for_all_deposit_answers(self):
        status, home, headers = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(BANNER, home)
        self.assertEqual(home.count('Try this:'), 3)
        self.assertIn('Content-Security-Policy', headers)
        for answer in ('yes', 'maybe', 'no'):
            location = self.create()
            sid = location.split('/')[-1]
            spec = self.store.get(sid)
            self.assertEqual(spec.brand, 'Fernleaf &copy; <demo>')
            page = self.request('GET', location)[1]
            for text in ('Blank size', 'bleed', 'safe zone', 'CMYK', 'Care:', 'Tee print area', 'reviewed_by: null', 'pending legal review', 'checklist — not legal advice — pending counsel review'):
                self.assertIn(text, page)
            self.assertIn('<svg', self.request('GET', location + '/dieline.svg')[1])
            blocked = self.request('GET', location + '/quotes')[1]
            self.assertIn('Quotes blocked', blocked)
            self.assertIn('CLM-USA', blocked)
            self.assertIn('Revise specification', blocked)
            self.assertEqual(self.request('POST', location + '/revise', {'claim': 'made_in_usa'})[0], 303)
            status, quotes, _ = self.request('GET', location + '/quotes')
            self.assertEqual(status, 200)
            self.assertEqual(quotes.count('Seller of record:'), 3)
            for text in ('Platform fee:', 'Warranty:', 'Refund terms:', 'Valid until:', 'Import duties: manufacturer to confirm', 'tolerance', 'refundable $50', 'value="maybe"', 'value="no"'):
                self.assertIn(text, quotes)
            qid = re.search(r'name="quote_id" value="([^"]+)"', quotes)[1]
            fields = {'quote_id': qid, 'email': 'fictional@example.test', 'deposit_intent': answer}
            status, confirmation, _ = self.request('POST', location + '/order-intent', fields)
            self.assertEqual(status, 200)
            self.assertIn('oi_', confirmation)
            self.assertIn('LOT-', confirmation)
            self.assertIn('Nothing was charged', confirmation)
            self.assertEqual(self.request('POST', location + '/order-intent', fields)[0], 409)
            self.assertTrue(self.store.has_intent(sid))
            audit = self.request('GET', '/audit/' + sid)[1]
            for text in ('Chain verified', 'head hash', 'spec_sha256', 'checks', 'quotes_shown', 'order_intent'):
                self.assertIn(text, audit)
            self.assertNotIn('fictional@example.test', audit)
            self.assertNotIn('fictional@example.test', json.dumps(self.store.stats_snapshot()))
        stats = self.store.stats_snapshot()
        self.assertEqual(stats['intents'], 3)
        self.assertEqual([stats['deposit_' + a] for a in ('yes', 'maybe', 'no')], [1, 1, 1])
        self.assertEqual(self.request('POST', '/survey', {'answer': 'yes'})[0], 200)
        self.assertEqual(self.store.stats_snapshot()['survey_yes'], 1)

    def test_origin_attestation_and_polyester_quote(self):
        location = self.create('100 polyester tees made in USA')
        self.assertEqual(self.request('POST', location + '/revise', {'origin_blank': 'USA', 'origin_sewing': 'USA', 'origin_print': 'USA'})[0], 303)
        self.assertIn('Quotes blocked', self.request('GET', location + '/quotes')[1])
        self.request('POST', location + '/revise', {'origin_user_asserted': 'yes'})
        quotes = self.request('GET', location + '/quotes')[1]
        self.assertIn('100% Polyester', quotes)
        self.assertNotIn('Printed cotton tees', quotes)

    def test_atomic_capacity_and_content_tampering(self):
        location = self.create('100 boxes')
        sid = location.split('/')[-1]
        original = self.store.get(sid)
        self.store.audit.max_events_per_spec = len(self.store.audit.snapshot(sid))
        self.assertEqual(self.request('POST', location + '/revise', {})[0], 400)
        self.assertEqual(self.store.get(sid), original)
        self.store.audit.max_events_per_spec = 96
        self.store.max_versions = 1
        self.assertEqual(self.request('POST', location + '/revise', {})[0], 400)
        self.assertEqual(len(self.store.audit.snapshot(sid)), 1)
        with self.assertRaises(OverflowError):
            self.store.put(revise(original))
        self.store.specs[sid][-1] = replace(original, brand='tampered')
        self.assertIn('Chain failed', self.request('GET', '/audit/' + sid)[1])

    def test_invalid_quantity_health_and_escaping(self):
        self.assertEqual(self.request('POST', '/spec', {'text': '100 boxes', 'brand': 'x', 'box_qty': '0'})[0], 400)
        self.store.allow_request = lambda client: False
        self.assertEqual(self.request('GET', '/healthz')[:2], (200, 'ok'))
        self.assertEqual(self.request('POST', '/intake', {'text': '100 boxes', 'brand': 'x'})[0], 429)


if __name__ == '__main__':
    unittest.main()
