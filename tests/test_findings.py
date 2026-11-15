import html
import http.client
import re
import socket
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlencode

from app.intake import FakeIntake
from app.pricing import platform_fee
from app.quotes import assign_lot, make_quotes
from app.spec import build
from app.store import Store
from app.web import make_server


class FindingTests(unittest.TestCase):
    def setUp(self):
        self.server = make_server(0, '127.0.0.1')
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, data=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            body = None if data is None else urlencode(data)
            conn.request(method, path, body, {
                'Content-Type': 'application/x-www-form-urlencoded',
            })
            response = conn.getresponse()
            return response.status, response.read().decode(), response.getheader('Location')
        finally:
            conn.close()

    def create_spec(self, quantity=100):
        status, _, location = self.request('POST', '/spec', {
            'text': '{} boxes 10x8x4 in'.format(quantity), 'brand': 'Regression',
        })
        self.assertEqual(status, 303)
        return location

    def test_f1_stalled_body_does_not_block_health_and_socket_has_timeout(self):
        self.assertIsInstance(self.server, ThreadingHTTPServer)
        self.assertTrue(self.server.daemon_threads)
        self.assertEqual(self.server.RequestHandlerClass.timeout, 10)
        entered = threading.Event()
        observed = []
        handler = self.server.RequestHandlerClass
        original_form = handler._form

        def observe_form(instance):
            observed.append(instance.connection.gettimeout())
            entered.set()
            return original_form(instance)

        with patch.object(handler, '_form', observe_form):
            with socket.create_connection(('127.0.0.1', self.port), timeout=3) as slow:
                slow.sendall(b'POST /intake HTTP/1.0\r\nContent-Length: 60000\r\n\r\n')
                self.assertTrue(entered.wait(2))
                self.assertEqual(observed, [10])
                self.assertEqual(self.request('GET', '/healthz')[:2], (200, 'ok'))
                slow.shutdown(socket.SHUT_WR)
                self.assertIn(b'400', slow.recv(4096).split(b'\r\n', 1)[0])

    def test_f1_socket_timeout_terminates_stalled_body(self):
        # Exercise the real timeout path without making the suite wait 10 seconds.
        with patch.object(self.server.RequestHandlerClass, 'timeout', 0.1):
            conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
            try:
                conn.putrequest('POST', '/intake')
                conn.putheader('Content-Length', '60000')
                conn.endheaders()
                response = conn.getresponse()
                self.assertEqual(response.status, 408)
                self.assertIn('timed out', response.read().decode())
            finally:
                conn.close()
        self.assertEqual(self.request('GET', '/healthz')[:2], (200, 'ok'))

    def test_f1_rate_limits_application_requests_but_not_health(self):
        with patch.object(Store, 'allow_request', return_value=False) as allow:
            self.assertEqual(self.request('GET', '/healthz')[:2], (200, 'ok'))
            self.assertEqual(self.request('POST', '/intake', {
                'text': '100 boxes', 'brand': 'x',
            })[0], 429)
            allow.assert_called_once_with('127.0.0.1')
        self.assertEqual(self.request('GET', '/healthz')[:2], (200, 'ok'))

    def test_f1_worker_bound_rejects_excess_and_releases_slots(self):
        server = make_server(0, '127.0.0.1', max_threads=1)
        try:
            self.assertTrue(server._slots.acquire(blocking=False))
            with patch.object(server, 'shutdown_request') as shutdown:
                with patch.object(ThreadingHTTPServer, 'process_request') as start:
                    request = object()
                    server.process_request(request, ('127.0.0.1', 1234))
                    shutdown.assert_called_once_with(request)
                    start.assert_not_called()
            server._slots.release()
            with patch.object(ThreadingHTTPServer, 'process_request', side_effect=RuntimeError('start failed')):
                with self.assertRaises(RuntimeError):
                    server.process_request(object(), ('127.0.0.1', 1234))
            self.assertTrue(server._slots.acquire(blocking=False))
            with patch.object(ThreadingHTTPServer, 'process_request_thread', side_effect=RuntimeError('worker failed')):
                with self.assertRaises(RuntimeError):
                    server.process_request_thread(object(), ('127.0.0.1', 1234))
            self.assertTrue(server._slots.acquire(blocking=False))
            server._slots.release()
        finally:
            server.server_close()

    def test_f2_quotes_render_and_store_every_disclosure_and_derive_lot(self):
        location = self.create_spec()
        captured = []

        def generate(spec, now):
            result = make_quotes(spec, now)
            captured.append((spec, result))
            return result

        with patch('app.web.make_quotes', side_effect=generate) as generate_mock:
            status, page, _ = self.request('GET', location + '/quotes')
            self.assertEqual(status, 200)
            self.assertEqual(generate_mock.call_count, 1)
            spec, quotes = captured[0]
            self.assertIn(len(quotes), (2, 3))
            self.assertEqual(len({q.seller_of_record for q in quotes}), len(quotes))
            self.assertEqual(len({q.quote_id for q in quotes}), len(quotes))
            for quote in quotes:
                self.assertIn('Seller of record: ' + html.escape(quote.seller_of_record), page)
                self.assertIn('Warranty: ' + html.escape(quote.warranty), page)
                self.assertIn('Refund terms: ' + html.escape(quote.refund_terms), page)
                self.assertIn('Lead time: {} days'.format(quote.lead_time_days), page)
                for label, amount in (
                    ('Goods', quote.goods_cents), ('Shipping', quote.shipping_cents),
                    ('Platform fee', quote.platform_fee_cents), ('Total', quote.total_cents),
                ):
                    self.assertIn('{}: ${:.2f}'.format(label, amount / 100), page)
                self.assertIn('name="quote_id" value="{}"'.format(quote.quote_id), page)
                for name, quantity, unit in quote.items:
                    self.assertIn(html.escape(name), page)
                    self.assertIn('<td>{}</td>'.format(quantity), page)
                    self.assertIn('${:.2f}'.format(unit / 100), page)
                    self.assertIn('${:.2f}'.format(quantity * unit / 100), page)
                self.assertEqual(
                    quote.platform_fee_cents,
                    platform_fee(quote.goods_cents + quote.shipping_cents),
                )
                self.assertEqual(
                    quote.total_cents,
                    quote.goods_cents + quote.shipping_cents + quote.platform_fee_cents,
                )

            selected = quotes[0]
            status, confirmation, _ = self.request('POST', location + '/order-intent', {
                'quote_id': selected.quote_id,
                'email': 'private@example.test',
                'deposit_intent': 'yes',
                'total_cents': '1',
            })
            self.assertEqual(status, 200)
            self.assertIn(assign_lot(spec, selected), confirmation)
            self.assertIn('Nothing was charged', confirmation)
            self.assertEqual(self.request('GET', location + '/quotes')[1], page)
            self.assertEqual(generate_mock.call_count, 1)
        self.assertIn('Chain verified', self.request('GET', '/audit/' + spec.id)[1])
        self.assertNotIn('private@example.test', self.request('GET', '/stats')[1])

    def test_f2_rejects_unknown_cross_spec_and_stale_quotes(self):
        first = self.create_spec()
        second = self.create_spec()
        page = self.request('GET', first + '/quotes')[1]
        quote_id = re.search(r'name="quote_id" value="([^"]+)"', page).group(1)
        fields = {'quote_id': quote_id, 'email': 'x@example.test', 'deposit_intent': 'yes'}
        self.assertEqual(self.request('POST', second + '/order-intent', fields)[0], 400)
        self.request('GET', second + '/quotes')
        self.assertEqual(self.request('POST', second + '/order-intent', fields)[0], 400)
        self.assertEqual(self.request('POST', first + '/order-intent', {**fields, 'quote_id': 'invented'})[0], 400)
        self.assertEqual(self.request('POST', first + '/revise', {})[0], 303)
        self.assertEqual(self.request('POST', first + '/order-intent', fields)[0], 400)
        new_page = self.request('GET', first + '/quotes')[1]
        self.assertNotIn(quote_id, new_page)
        self.assertEqual(self.request('POST', first + '/order-intent', fields)[0], 400)

    def test_f2_prices_and_fees_depend_on_order_quantity(self):
        now = datetime.now(timezone.utc)
        small = build(FakeIntake().parse('100 boxes 10x8x4 in', 'x'), 'x')
        large = build(FakeIntake().parse('200 boxes 10x8x4 in', 'x'), 'x')
        small_quotes = make_quotes(small, now)
        large_quotes = make_quotes(large, now)
        for first, second in zip(small_quotes, large_quotes):
            self.assertEqual(second.goods_cents, 2 * first.goods_cents)
            self.assertGreater(second.platform_fee_cents, first.platform_fee_cents)
            self.assertGreater(second.total_cents, first.total_cents)
        first_page = self.request('GET', self.create_spec(100) + '/quotes')[1]
        second_page = self.request('GET', self.create_spec(200) + '/quotes')[1]
        first_fees = re.findall(r'Platform fee: (\$[0-9.]+)', first_page)
        second_fees = re.findall(r'Platform fee: (\$[0-9.]+)', second_page)
        self.assertEqual(len(first_fees), 3)
        self.assertEqual(len(second_fees), 3)
        self.assertNotEqual(first_fees, second_fees)


if __name__ == '__main__':
    unittest.main()
