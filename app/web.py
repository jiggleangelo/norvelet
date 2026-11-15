import hashlib
import html
import json
import re
import secrets
from dataclasses import asdict, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore, RLock, Timer
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from .compliance import RULEPACK_VERSION, blocking, run
from .dieline import blank_size, render
from .intake import CLAIMS, FakeIntake
from .models import BoxDraft, IntakeDraft, OrderIntent, TeeDraft
from .quotes import assign_lot, make_quotes
from .spec import ARTWORK, TEE_AREA, build
from .store import Store

BANNER = 'PackPilot Demo — fictional data. Nothing is charged. Do not enter real personal data.'
_MAX_BODY = 65536
_MAX_THREADS = 16
BRAND = 'Fernleaf Candle Co. (fictional)'
SAMPLES = ('100 kraft shipping boxes 10x8x4 in with our logo on top, plus 100 black cotton tees, logo front, sizes S–XL, eco-friendly and made in USA',
           '100 kraft boxes 10x8x4 in with our logo on top', '100 black polyester tees for adults, logo front')


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)


def esc(value):
    return html.escape(str(value), quote=True)


def _page(title, body):
    return '<!doctype html><html><head><meta charset="utf-8"><title>' + esc(title) + '</title></head><body><header><strong>' + esc(BANNER) + '</strong></header>' + body + '</body></html>'


def _hidden(name, value):
    return '<input type="hidden" name="{}" value="{}">'.format(esc(name), esc(value))


def _input(name, value, label=None):
    return '<label>{} <input name="{}" value="{}"></label><br>'.format(esc(label or name), esc(name), esc(value))


def _money(cents):
    return '${}.{:02d}'.format(cents // 100, cents % 100)


def _value(fields, name, default=''):
    return fields.get(name, [default])[0].strip()


def _dimension(raw):
    if not re.fullmatch(r'\d+(?:\.\d{1,3})?', raw):
        raise ValueError('Dimensions require inches with at most three decimal places')
    whole, _, fraction = raw.partition('.')
    return int(whole) * 1000 + int(fraction.ljust(3, '0'))


def _apply_form(draft, fields):
    b, t = draft.box, draft.tee
    if b:
        b = replace(b, qty=int(_value(fields, 'box_qty', str(b.qty))),
                    **{key + '_mil': _dimension(_value(fields, key, str(getattr(b, key + '_mil') // 1000) + '.' + str(getattr(b, key + '_mil') % 1000).zfill(3))) for key in ('L', 'W', 'H')},
                    board=_value(fields, 'board', b.board), ink_colors=int(_value(fields, 'ink_colors', str(b.ink_colors))),
                    print_sides=tuple(_value(fields, 'print_sides', ','.join(b.print_sides)).split(',')))
    if t:
        qty = int(_value(fields, 'tee_qty', str(t.qty)))
        sizes = {s: int(_value(fields, 'size_' + s, '0')) for s in ('S', 'M', 'L', 'XL')} if any('size_' + s in fields for s in ('S', 'M', 'L', 'XL')) else {s: qty // 4 + (i < qty % 4) for i, s in enumerate(('S', 'M', 'L', 'XL'))}
        fiber = tuple((name.strip(), int(pct)) for name, pct in (part.split(':') for part in _value(fields, 'fiber', ','.join(f'{n}:{p}' for n, p in t.fiber)).split(',')))
        t = replace(t, qty=qty, sizes=sizes, fiber=fiber, color=_value(fields, 'color', t.color), method=_value(fields, 'method', t.method),
                    placements=tuple(_value(fields, 'placements', ','.join(t.placements)).split(',')),
                    origin={k: _value(fields, 'origin_' + k, v) for k, v in t.origin.items()})
    claims = tuple(c for c in _value(fields, 'claims', ','.join(draft.claims)).split(',') if c and c not in fields.get('claim', ()))
    if not set(claims) <= set(CLAIMS):
        raise ValueError('Unknown claim')
    return replace(draft, box=b, tee=t, claims=claims, audience=_value(fields, 'audience', draft.audience))


def _editor(draft, asserted=False):
    controls = ['<p><mark>Please confirm: ' + esc(', '.join(draft.low_confidence) or 'all proposed fields') + '</mark></p>']
    if draft.box:
        b = draft.box
        controls += [_input('box_qty', b.qty, 'Boxes'), *[_input(k, f'{getattr(b, k + "_mil") // 1000}.{getattr(b, k + "_mil") % 1000:03d}', k + ' (inches)') for k in ('L', 'W', 'H')], _input('board', b.board), _input('print_sides', ','.join(b.print_sides)), _input('ink_colors', b.ink_colors)]
    if draft.tee:
        t = draft.tee
        controls += [_input('tee_qty', t.qty), _input('color', t.color), _input('fiber', ','.join(f'{n}:{p}' for n, p in t.fiber), 'Fiber (name:percent)'), _input('method', t.method), _input('placements', ','.join(t.placements)), '<p>' + TEE_AREA + '</p>']
        controls += [_input('size_' + k, v) for k, v in t.sizes.items()]
        controls += [_input('origin_' + k, v, k + ' origin') for k, v in t.origin.items()]
        controls.append('<label><input type="checkbox" name="origin_user_asserted" value="yes"' + (' checked' if asserted else '') + '> I confirm blank, sewing and printing origins from supplier information (demo attestation only).</label>')
    controls += [_input('claims', ','.join(draft.claims), 'Detected claims (comma-separated keys)'), _input('audience', draft.audience)]
    return ''.join(controls)


def _draft(spec):
    return IntakeDraft('', BoxDraft(**{k: getattr(spec.box, k) for k in BoxDraft.__dataclass_fields__}) if spec.box else None,
                       TeeDraft(**{k: getattr(spec.tee, k) for k in TeeDraft.__dataclass_fields__}) if spec.tee else None,
                       spec.claims, spec.audience, ())


def _checks(spec):
    return '<h2>checklist — not legal advice — pending counsel review</h2><ul>' + ''.join('<li><strong>{}: {} — {}</strong><p>{}</p><p>Action: {}</p><p>reviewed_by: {}; {}</p></li>'.format(esc(r.severity), esc(r.rule_id), esc(r.title), esc(r.detail), esc(r.action), esc(r.reviewed_by if r.reviewed_by else 'null'), esc('pending legal review' if not r.reviewed_by else 'reviewed')) for r in run(spec)) + '</ul>'


def _spec_payload(spec):
    return {'version': spec.version, 'spec_sha256': hashlib.sha256(spec.canonical_json().encode()).hexdigest(), 'rulepack': RULEPACK_VERSION, 'checks': [asdict(r) for r in run(spec)]}


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, handler, max_threads=_MAX_THREADS):
        if max_threads < 1:
            raise ValueError('max_threads must be positive')
        self._slots = BoundedSemaphore(max_threads)
        self._peers = {}
        self._peer_lock = RLock()
        self._peer_limit = max(1, max_threads // 4)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        peer = client_address[0]
        with self._peer_lock:
            if self._peers.get(peer, 0) >= self._peer_limit or not self._slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            self._peers[peer] = self._peers.get(peer, 0) + 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(peer)
            raise

    def _release(self, peer):
        with self._peer_lock:
            count = self._peers.get(peer, 1) - 1
            if count:
                self._peers[peer] = count
            else:
                self._peers.pop(peer, None)
            self._slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0])


def make_server(port=0, host='127.0.0.1', max_threads=_MAX_THREADS, store=None, clock=None, intake=None, quote_source=None, server_class=BoundedHTTPServer):
    store, clock, intake = store or Store(), clock or SystemClock(), intake or FakeIntake()
    state_lock = store.lock
    cookie_secret = secrets.token_hex(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'PackPilot/1'
        timeout = 10
        header_deadline = 2
        request_deadline = 10
        def log_message(self, format, *args):
            pass

        def handle(self):
            def expire():
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
            self._header_timer = Timer(self.header_deadline, expire)
            total = Timer(self.request_deadline, expire)
            self._header_timer.daemon = total.daemon = True
            self._header_timer.start()
            total.start()
            try:
                super().handle()
            except (ConnectionError, TimeoutError):
                pass
            finally:
                self._header_timer.cancel()
                total.cancel()

        def parse_request(self):
            result = super().parse_request()
            self._header_timer.cancel()
            self.close_connection = True
            return result

        def _send(self, status, payload, content_type='text/html; charset=utf-8', headers=None):
            encoded = payload.encode()
            self.send_response(status)
            for key, value in {'Content-Type': content_type, 'Content-Length': str(len(encoded)), 'Connection': 'close', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'", **(headers or {})}.items():
                self.send_header(key, value)
            if getattr(self, '_new_cookie', None):
                self.send_header('Set-Cookie', 'demo_client=' + self._new_cookie + '; HttpOnly; SameSite=Strict; Path=/')
            self.end_headers()
            self.wfile.write(encoded)

        def _error(self, status, message):
            self._send(status, _page('Error', '<h1>' + esc(message) + '</h1>'))

        def _redirect(self, location):
            self._send(303, '', headers={'Location': location})

        def _allow_request(self):
            match = re.search(r'(?:^|;\s*)demo_client=([a-f0-9]{32})\.([a-f0-9]{64})(?:;|$)', self.headers.get('Cookie', ''))
            client = self.client_address[0]
            if match and secrets.compare_digest(match[2], hashlib.sha256((cookie_secret + match[1]).encode()).hexdigest()):
                client = 'session:' + match[1]
            else:
                nonce = secrets.token_hex(16)
                self._new_cookie = nonce + '.' + hashlib.sha256((cookie_secret + nonce).encode()).hexdigest()
                # Browser sessions behind an ingress get independent bounded windows.
                if self.command == 'GET' and urlsplit(self.path).path == '/':
                    return True
            if store.allow_request(client):
                return True
            self._error(429, 'Too many requests')
            return False

        def _form(self):
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') is not None:
                self._error(400, 'Transfer-Encoding is not supported')
                return
            if not lengths:
                self._error(411, 'Content-Length required')
                return
            if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,8}', lengths[0]):
                raise ValueError('Invalid Content-Length')
            size = int(lengths[0])
            if size > _MAX_BODY:
                self._error(413, 'Request body too large')
                return
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError('Incomplete request body')
            fields = parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=128)
            if any(len(v) > (4000 if k == 'text' else 512) for k, values in fields.items() for v in values):
                raise ValueError('Field exceeds demo limits')
            return fields

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/healthz':
                self._send(200, 'ok', 'text/plain; charset=utf-8')
                return
            if not self._allow_request():
                return
            try:
                self._get(path)
            except (ValueError, OverflowError) as error:
                self._error(400, str(error))

        def _get(self, path):
            if path == '/':
                store.increment('home')
                body = '<h1>Describe your boxes and tees</h1><p>Review a draft, check artwork and claims, compare named fictional sellers, then record a pilot preference. About three minutes; no payment or signup.</p><form method="post" action="/intake"><textarea name="text">' + esc(SAMPLES[0]) + '</textarea>' + _input('brand', BRAND) + '<button>Start</button></form>'
                body += ''.join('<form method="post" action="/intake">' + _hidden('text', text) + _hidden('brand', BRAND) + '<button>Try this: ' + esc(text) + '</button></form>' for text in SAMPLES)
                self._send(200, _page('Home', body))
                return
            if path in ('/stats', '/survey'):
                body = '<pre>' + esc(json.dumps(store.stats_snapshot(), sort_keys=True)) + '</pre>' if path == '/stats' else '<form method="post" action="/survey"><p>Is the disclosed 10% software/referral fee (minimum $5, on goods plus shipping) acceptable?</p><button name="answer" value="yes">Yes</button><button name="answer" value="no">No</button></form>'
                self._send(200, _page(path[1:], body))
                return
            match = re.fullmatch(r'/(spec|audit)/([^/]+)(?:/(quotes|dieline\.svg))?', path)
            if not match:
                self._error(404, 'Not found')
                return
            with state_lock:
                spec = store.get(match[2])
                if spec is None:
                    self._error(404, 'Not found')
                elif match[1] == 'audit':
                    events = store.audit.snapshot(spec.id)
                    valid, seq = store.audit.verify(spec.id)
                    latest = next((ev for ev in reversed(events) if ev.type in ('created', 'revised')), None)
                    content_valid = latest is not None and latest.payload['spec_sha256'] == _spec_payload(spec)['spec_sha256']
                    title = 'Chain verified ✓' if valid and content_valid else 'Chain failed: event {} or stored spec content changed'.format(seq)
                    self._send(200, _page('Audit', '<h1>' + esc(title) + '</h1><p>Process-local head hash: ' + store.audit.head(spec.id) + '</p><p>Not externally signed or anchored.</p><pre>' + esc(json.dumps([asdict(ev) for ev in events], indent=2)) + '</pre>'))
                elif match[3] == 'dieline.svg':
                    self._send(200, render(spec.box), 'image/svg+xml; charset=utf-8') if spec.box else self._error(404, 'No box')
                elif match[3] == 'quotes':
                    self._show_quotes(spec)
                else:
                    self._show_spec(spec)

        def do_POST(self):
            if not self._allow_request():
                return
            try:
                fields = self._form()
                if fields is not None:
                    with state_lock:
                        self._post(urlsplit(self.path).path, fields)
            except TimeoutError:
                self._error(408, 'Request body timed out')
            except (TypeError, ValueError, OverflowError) as error:
                self._error(400, str(error))

        def _post(self, path, fields):
            if path == '/survey':
                answer = _value(fields, 'answer')
                if answer not in ('yes', 'no'):
                    raise ValueError('Answer yes or no')
                store.increment('survey_' + answer)
                self._send(200, _page('Survey', '<p>Preference recorded. These demo counters are exploratory.</p>'))
                return
            if path in ('/intake', '/spec'):
                text, brand = _value(fields, 'text'), _value(fields, 'brand')
                if not text or not brand or len(brand) > 160:
                    raise ValueError('Request and brand are required; brand maximum 160 characters')
                draft = intake.parse(text, brand)
                if path == '/intake':
                    store.increment('intakes')
                    body = '<h1>Review intake for ' + esc(brand) + '</h1><form method="post" action="/spec">' + _hidden('text', text) + _hidden('brand', brand) + _editor(draft) + '<button>Create specification</button></form>'
                    self._send(200, _page('Review intake', body))
                else:
                    spec = build(_apply_form(draft, fields), brand, origin_user_asserted=_value(fields, 'origin_user_asserted') == 'yes')
                    store.commit(spec, 'created', {**_spec_payload(spec), 'raw_text_hash': draft.raw_text_hash})
                    store.increment('specs')
                    self._redirect('/spec/' + spec.id)
                return
            match = re.fullmatch(r'/spec/([^/]+)/(revise|order-intent)', path)
            spec = store.get(match[1]) if match else None
            if spec is None:
                self._error(404, 'Not found')
                return
            if match[2] == 'order-intent':
                self._order_intent(spec, fields)
                return
            if store.has_intent(spec.id):
                self._error(409, 'An order intent already exists; start a new request')
                return
            updated = build(_apply_form(_draft(spec), fields), spec.brand, spec.id, spec.version + 1,
                            _value(fields, 'origin_user_asserted') == 'yes')
            store.commit(updated, 'revised', _spec_payload(updated))
            self._redirect('/spec/' + spec.id + '/quotes')

        def _show_spec(self, spec):
            body = '<h1>Specification ' + esc(spec.brand) + '</h1><p>Version ' + str(spec.version) + '</p>'
            if spec.box:
                b = spec.box
                body += '<h2>Box</h2><p>Blank size: {} × {} mil. Dimension tolerance: ±{} mil.</p><p>{}</p>'.format(*blank_size(b.L_mil, b.W_mil, b.H_mil), b.tolerance_mil, ARTWORK) + render(b) + '<a href="/spec/' + spec.id + '/dieline.svg">Download dieline.svg</a>'
            if spec.tee:
                body += '<h2>Tee</h2><p>' + esc(spec.tee.label_content) + '</p><p>Care: ' + esc(spec.tee.care) + '</p><p>' + TEE_AREA + '</p><p>' + ARTWORK + '</p><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 200" width="240" role="img" aria-label="Tee print area"><rect x="20" y="20" width="120" height="160" fill="none" stroke="black"/><text x="25" y="100" font-size="8">12 × 16 in print area</text></svg>'
            remove = ''.join('<label><input type="checkbox" name="claim" value="' + esc(c) + '"> Remove ' + esc(c.replace('_', ' ')) + '</label>' for c in spec.claims)
            body += _checks(spec) + '<form method="post" action="/spec/' + spec.id + '/revise">' + _editor(_draft(spec), spec.origin_user_asserted) + remove + '<button>Revise</button></form><a href="/spec/' + spec.id + '/quotes">Quotes</a> <a href="/audit/' + spec.id + '">Audit</a>'
            self._send(200, _page('Specification', body))

        def _show_quotes(self, spec):
            if blocking(run(spec)):
                body = '<h1>Quotes blocked</h1>' + _checks(spec) + '<a href="/spec/' + spec.id + '">Revise specification</a>'
                self._send(200, _page('Quotes blocked', body))
                return
            quotes = store.get_quotes(spec.id)
            if not quotes:
                quotes = (quote_source or make_quotes)(spec, clock.now())
                snapshots = [{**asdict(q), 'created_at': q.created_at.isoformat()} for q in quotes]
                store.audit.append(spec.id, 'quotes_shown', {**_spec_payload(spec), 'quotes': snapshots})
                store.set_quotes(spec.id, quotes)
                store.increment('quotes')
            cards = []
            for q in quotes:
                rows = ''.join('<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'.format(esc(n), qty, _money(unit), _money(qty * unit)) for n, qty, unit in q.items)
                cards.append('<section><h2>Seller of record: ' + esc(q.seller_of_record) + '</h2><table><tr><th>Item</th><th>Quantity</th><th>Unit price</th><th>Line total</th></tr>' + rows + '</table>' + ''.join('<p>' + label + ': ' + _money(amount) + '</p>' for label, amount in (('Goods', q.goods_cents), ('Shipping', q.shipping_cents), ('Platform fee', q.platform_fee_cents), ('Total', q.total_cents))) + '<p>Lead time: {} days</p><p>Warranty: {}</p><p>Refund terms: {}</p><p>Valid until: {}</p><p>{}</p>'.format(q.lead_time_days, esc(q.warranty), esc(q.refund_terms), esc(q.valid_until), esc(' '.join(q.notes))) + '<form method="post" action="/spec/' + spec.id + '/order-intent">' + _hidden('quote_id', q.quote_id) + '<label>Fictional business email <input name="email" type="email" required></label><fieldset><legend>Would you place a refundable $50 deposit for a pilot order?</legend>' + ''.join('<label><input type="radio" name="deposit_intent" value="' + a + '" required>' + a + '</label>' for a in ('yes', 'maybe', 'no')) + '</fieldset><button>Select quote and confirm intent</button></form></section>')
            self._send(200, _page('Quotes', '<h1>Quotes</h1><p>Fictional, nonbinding offers. Manufacturers are sellers of record. Software/referral fee: 10% of goods plus shipping, minimum $5. No deposit is collected.</p>' + ''.join(cards)))

        def _order_intent(self, spec, fields):
            if store.has_intent(spec.id):
                self._error(409, 'An order intent already exists for this specification')
                return
            qid, email, answer = (_value(fields, k) for k in ('quote_id', 'email', 'deposit_intent'))
            quote = next((q for q in store.get_quotes(spec.id) if q.quote_id == qid and q.spec_version == spec.version), None)
            if blocking(run(spec)) or quote is None:
                raise ValueError('Resolve blockers and select a current quote')
            if clock.now() >= datetime.fromisoformat(quote.valid_until):
                raise ValueError('Quote expired; revise to request fresh quotes')
            if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', email) or answer not in ('yes', 'maybe', 'no'):
                raise ValueError('Fictional email and yes/maybe/no deposit answer are required')
            if len(store.intents) >= store.max_intents or len(store.audit.snapshot(spec.id)) >= store.audit.max_events_per_spec:
                raise OverflowError('Demo recording capacity reached')
            intent = OrderIntent('oi_' + secrets.token_urlsafe(18), spec.id, spec.version, quote.quote_id,
                                 store.vault.store('email', email), answer, assign_lot(spec, quote))
            store.audit.append(spec.id, 'order_intent', asdict(intent))
            store.put_intent(intent)
            store.increment('intents')
            store.increment('deposit_' + answer)
            body = '<h1>Order intent recorded</h1><p>Order-intent ID: ' + esc(intent.id) + '</p><p>Lot ID: ' + esc(intent.lot_id) + '</p><p>Deposit answer: ' + esc(answer) + '</p><p>Nothing was charged.</p><a href="/audit/' + spec.id + '">Audit</a> <a href="/survey">Fee survey</a>'
            self._send(200, _page('Order intent', body))

    server = server_class((host, port), Handler, max_threads=max_threads)
    server.store = store
    return server
