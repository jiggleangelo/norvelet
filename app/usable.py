import hashlib
import html
import json
import re
import secrets
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler
from threading import Timer
from urllib.parse import parse_qs, urlsplit

from .compliance import RULEPACK_VERSION, blocking, run
from .dieline import blank_size, render
from .hosting import _normalized_authority, _parse_public_base_url
from .intake import CLAIMS, FakeIntake
from .models import BoxDraft, IntakeDraft, TeeDraft
from .proxy_server import ProxySafeBoundedHTTPServer
from .rfq import build_rfq
from .spec import ARTWORK, TEE_AREA, build as core_build
from .web import _draft
from .workspace import CapacityError, NotFound, RateError, WorkspaceStore, now

NOTICE = ('Operated by AI agents. Early version: keep your own copy of anything important. '
          'Do not enter personal data about anyone, payment or bank details, or confidential contracts. Nothing is charged.')
MAX_BODY = 1024 * 1024
SAMPLE = '250 kraft boxes 12x9x4 in with 2-color inside print plus 60 tri-blend heather tees DTG for adults, recyclable'
CHILD_MESSAGE = 'Children’s products are refused. CPSIA testing, tracking labels and certificates are outside this tool.'
EXPIRY_NOTICE = 'Empty workspaces expire 24 hours after creation. Saved workspaces expire 90 days after the last write. Viewing or exporting does not extend this period. Keep your own export.'


def esc(value):
    return html.escape(str(value), quote=True)


def page(title, body):
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            '<title>' + esc(title) + '</title><style>body{font:16px system-ui;max-width:1100px;margin:auto;padding:1rem}'
            'header,mark{background:#fff4cf}header{padding:1rem}label{display:block;margin:.45rem 0}'
            'input,select,textarea{max-width:100%;padding:.35rem}textarea{width:100%;min-height:10rem}'
            'table{border-collapse:collapse;width:100%}td,th{border:1px solid #bbb;padding:.4rem}'
            '.block,.error{background:#ffd8d8;padding:1rem}.stale{color:#a00}.best{background:#ddf7dd}'
            'svg{max-width:100%;height:auto}@media print{.no-print,header{display:none}}</style></head><body>'
            '<header>' + esc(NOTICE) + ' <a href="https://manykiln.com/privacy">Privacy</a></header>' + body + '</body></html>')


def value(fields, name, default=''):
    return fields.get(name, [default])[0].strip()


def text_value(value, maximum, name, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError('Invalid ' + name)
    return value


def integer(value, minimum, maximum, name):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid ' + name)
    return value


def sequence(value, name):
    if not isinstance(value, (tuple, list)):
        raise ValueError('Invalid ' + name)
    return value


def validate_draft(draft):
    claims = sequence(draft.claims, 'claims')
    if any(not isinstance(item, str) or item not in CLAIMS for item in claims):
        raise ValueError('Claims contain an unknown key')
    if draft.audience not in ('adult', 'children', 'unknown'):
        raise ValueError('Invalid audience')
    text_value(draft.raw_text_hash, 64, 'request hash', True)
    for field in sequence(draft.low_confidence, 'confirmation fields'):
        text_value(field, 80, 'confirmation field')
    if draft.box:
        box = draft.box
        integer(box.qty, 1, 10000, 'box quantity')
        for number in (box.L_mil, box.W_mil, box.H_mil):
            integer(number, 250, 60000, 'box dimension')
        text_value(box.board, 80, 'board')
        integer(box.ink_colors, 1, 4, 'ink count')
        if any(not isinstance(item, str) for item in sequence(box.print_sides, 'print sides')):
            raise ValueError('Invalid print sides')
    if draft.tee:
        tee = draft.tee
        integer(tee.qty, 1, 10000, 'tee quantity')
        text_value(tee.color, 80, 'tee color')
        text_value(tee.method, 16, 'print method')
        if not isinstance(tee.sizes, dict) or set(tee.sizes) != {'S', 'M', 'L', 'XL'}:
            raise ValueError('Sizes must contain exactly S, M, L and XL')
        for number in tee.sizes.values():
            integer(number, 0, 10000, 'size quantity')
        for item in sequence(tee.fiber, 'fiber'):
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError('Invalid fiber entry')
            text_value(item[0], 80, 'fiber name')
            integer(item[1], 1, 100, 'fiber percentage')
        if any(not isinstance(item, str) for item in sequence(tee.placements, 'placements')):
            raise ValueError('Invalid placements')
        if not isinstance(tee.origin, dict) or set(tee.origin) != {'blank', 'sewing', 'print'}:
            raise ValueError('Provide blank, sewing and print origins')
        for origin in tee.origin.values():
            text_value(origin, 80, 'origin')


def build(draft, brand, spec_id=None, version=1, origin_user_asserted=False):
    validate_draft(draft)
    text_value(brand, 160, 'brand')
    if draft.audience == 'children':
        raise ValueError(CHILD_MESSAGE)
    if spec_id is not None:
        text_value(spec_id, 128, 'specification ID')
    integer(version, 1, 24, 'specification version')
    if type(origin_user_asserted) is not bool:
        raise ValueError('Origin attestation must be a boolean')
    return core_build(draft, brand, spec_id, version, origin_user_asserted)


class RuleParser:
    def parse(self, text, brand=''):
        draft = FakeIntake().parse(text, brand)
        # The rules do not establish sourcing. Never invent a country of origin.
        if draft.tee:
            draft = replace(draft, tee=replace(draft.tee, origin=dict.fromkeys(('blank', 'sewing', 'print'), 'Unknown')))
        fields = ['claims', 'audience']
        if draft.box:
            fields += ['box quantity', 'box dimensions', 'board', 'print sides', 'ink colors']
        if draft.tee:
            fields += ['tee quantity', 'color', 'fiber', 'method', 'placements', 'S/M/L/XL split', 'blank origin', 'sewing origin', 'print origin']
        return replace(draft, low_confidence=tuple(fields))


def draft_dict(draft):
    return asdict(draft)


def draft_from(data):
    allowed = {'raw_text_hash', 'box', 'tee', 'claims', 'audience', 'low_confidence'}
    if not isinstance(data, dict) or set(data) != allowed:
        raise ValueError('Unknown or missing draft keys')
    products = []
    for key, cls in (('box', BoxDraft), ('tee', TeeDraft)):
        raw = data[key]
        if raw is None:
            products.append(None)
        elif not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__):
            raise ValueError('Unknown or missing ' + key + ' keys')
        else:
            products.append(cls(**raw))
    draft = IntakeDraft(data['raw_text_hash'], *products, data['claims'], data['audience'], data['low_confidence'])
    validate_draft(draft)
    return draft


def spec_draft(data):
    allowed = {'id', 'version', 'brand', 'box', 'tee', 'claims', 'audience', 'origin_user_asserted'}
    if not isinstance(data, dict) or set(data) != allowed:
        raise ValueError('Unknown or missing specification keys')
    products = []
    for key, cls, generated in (('box', BoxDraft, {'tolerance_mil'}), ('tee', TeeDraft, {'label_content', 'care'})):
        raw = data[key]
        if raw is None:
            products.append(None)
            continue
        keys = set(cls.__dataclass_fields__)
        if not isinstance(raw, dict) or not keys <= set(raw) or set(raw) - keys - generated:
            raise ValueError('Unknown or missing ' + key + ' keys')
        if key == 'box' and 'tolerance_mil' in raw:
            integer(raw['tolerance_mil'], 62, 62, 'tolerance')
        if key == 'tee':
            for name in generated & set(raw):
                text_value(raw[name], 1000, name, True)
        products.append(cls(**{name: raw[name] for name in keys}))
    draft = IntakeDraft('', *products, data['claims'], data['audience'], ())
    return build(draft, data['brand'], data['id'], data['version'], data['origin_user_asserted'])


def version_record(spec, previous='0' * 64):
    digest = hashlib.sha256(spec.canonical_json().encode()).hexdigest()
    stamp = now()
    raw = json.dumps({'ts': stamp, 'spec_sha256': digest, 'version': spec.version}, sort_keys=True, separators=(',', ':'))
    return {'version': spec.version, 'created_at': stamp, 'spec': asdict(spec), 'spec_sha256': digest,
            'rulepack': RULEPACK_VERSION, 'checks': [asdict(item) for item in run(spec)],
            'prev_hash': previous, 'hash': hashlib.sha256((previous + '|' + raw).encode()).hexdigest()}


def latest(project):
    return spec_draft(project['versions'][-1]['spec']) if project.get('versions') else None


def project_by_id(data, pid):
    project = next((item for item in data['projects'] if item.get('id') == pid), None)
    if project is None:
        raise NotFound('Project not found')
    return project


def money(amount):
    return '${}.{:02d}'.format(amount // 100, amount % 100)


def cents(raw):
    if not re.fullmatch(r'\d{1,7}(?:\.\d{1,2})?', raw):
        raise ValueError('Prices require dollars with at most two decimals')
    whole, _, fraction = raw.partition('.')
    return int(whole) * 100 + int(fraction.ljust(2, '0') or '0')


def dimension(raw, unit):
    if not re.fullmatch(r'\d+(?:\.\d{1,3})?', raw):
        raise ValueError('Dimensions require a positive number with at most three decimals')
    return round(float(raw) * (393.700787 if unit == 'cm' else 1000))


def audience_value(raw):
    normalized = raw.strip().casefold()
    if normalized in ('child', 'children', 'kid', 'kids', 'toddler', 'toddlers', 'baby', 'babies', 'youth'):
        return 'children'
    if normalized in ('adult', 'adults'):
        return 'adult'
    if normalized in ('', 'unknown'):
        return 'unknown'
    raise ValueError('Audience must be adult, children, or unknown')


def product_defaults():
    return RuleParser().parse('100 boxes plus 100 tees')


def apply_form(draft, fields):
    defaults = product_defaults()
    products = []
    for key in ('box', 'tee'):
        current = getattr(draft, key)
        selection = value(fields, 'include_' + key, 'yes' if current else 'no')
        if selection not in ('yes', 'no'):
            raise ValueError('Invalid product selection')
        products.append((current or getattr(defaults, key)) if selection == 'yes' else None)
    box, tee = products
    if box:
        unit = value(fields, 'dimension_unit', 'in').casefold()
        if unit not in ('in', 'cm'):
            raise ValueError('Dimension unit must be inches or centimetres')
        dimensions = {key + '_mil': dimension(value(fields, key, str(getattr(box, key + '_mil') / 1000)), unit) for key in ('L', 'W', 'H')}
        box = replace(box, qty=int(value(fields, 'box_qty', str(box.qty))), board=value(fields, 'board', box.board),
                      ink_colors=int(value(fields, 'ink_colors', str(box.ink_colors))),
                      print_sides=tuple(x.strip() for x in value(fields, 'print_sides', ','.join(box.print_sides)).split(',') if x.strip()), **dimensions)
    if tee:
        try:
            fiber = tuple((name.strip(), int(percent)) for name, percent in
                          (item.split(':', 1) for item in value(fields, 'fiber', ','.join('%s:%s' % tuple(item) for item in tee.fiber)).split(',')))
        except (ValueError, TypeError):
            raise ValueError('Fiber must use name:percent entries') from None
        tee = replace(tee, qty=int(value(fields, 'tee_qty', str(tee.qty))), fiber=fiber,
                      sizes={name: int(value(fields, 'size_' + name, str(tee.sizes[name]))) for name in ('S', 'M', 'L', 'XL')},
                      color=value(fields, 'color', tee.color), method=value(fields, 'method', tee.method),
                      placements=tuple(x.strip() for x in value(fields, 'placements', ','.join(tee.placements)).split(',') if x.strip()),
                      origin={key: value(fields, 'origin_' + key, tee.origin[key]) for key in ('blank', 'sewing', 'print')})
    claims = tuple(x.strip() for x in value(fields, 'claims', ','.join(draft.claims)).split(',') if x.strip() and x.strip() not in fields.get('claim', ()))
    updated = replace(draft, box=box, tee=tee, claims=claims, audience=audience_value(value(fields, 'audience', draft.audience)))
    validate_draft(updated)
    return updated


def input_control(name, shown, label, overrides):
    current = value(overrides, name, str(shown))
    return '<label><mark>Please confirm</mark> ' + esc(label) + ' <input name="' + esc(name) + '" value="' + esc(current) + '"></label>'


def select_control(name, selected, choices, label):
    return '<label>' + esc(label) + ' <select name="' + name + '">' + ''.join('<option value="' + esc(key) + '"' + (' selected' if key == selected else '') + '>' + esc(title) + '</option>' for key, title in choices) + '</select></label>'


def editor(draft, asserted=False, overrides=None, error=''):
    overrides = overrides or {}
    defaults = product_defaults()
    out = ['<p><mark>Please confirm: ' + esc(', '.join(draft.low_confidence) or 'all proposed fields') + '</mark></p>']
    if error:
        out.append('<p class="error" role="alert">Please correct this form: ' + esc(error) + '</p>')
    for key in ('box', 'tee'):
        out.append(select_control('include_' + key, value(overrides, 'include_' + key, 'yes' if getattr(draft, key) else 'no'), [('yes', 'Include'), ('no', 'Do not include')], key.title() + ' product'))
        out.append('<fieldset><legend>' + key.title() + ' fields — used only when included</legend>')
        product = getattr(draft, key) or getattr(defaults, key)
        if key == 'box':
            out.append(input_control('box_qty', product.qty, 'Boxes', overrides))
            out.append(select_control('dimension_unit', value(overrides, 'dimension_unit', 'in'), [('in', 'inches'), ('cm', 'cm')], 'Dimension unit'))
            for name in ('L', 'W', 'H'):
                out.append(input_control(name, '{:.3f}'.format(getattr(product, name + '_mil') / 1000).rstrip('0').rstrip('.'), name, overrides))
            for name, shown, label in [('board', product.board, 'Board'), ('print_sides', ','.join(product.print_sides), 'Print sides'), ('ink_colors', product.ink_colors, 'Ink colors')]:
                out.append(input_control(name, shown, label, overrides))
        else:
            for name, shown, label in [('tee_qty', product.qty, 'Tees'), ('color', product.color, 'Color'), ('fiber', ','.join('%s:%s' % tuple(item) for item in product.fiber), 'Fiber (name:percent)'), ('method', product.method, 'Method'), ('placements', ','.join(product.placements), 'Placements')]:
                out.append(input_control(name, shown, label, overrides))
            for name in ('S', 'M', 'L', 'XL'):
                out.append(input_control('size_' + name, product.sizes[name], name, overrides))
            for name in ('blank', 'sewing', 'print'):
                out.append(input_control('origin_' + name, product.origin[name], name + ' origin', overrides))
            checked = value(overrides, 'origin_user_asserted') == 'yes' if overrides else asserted
            out.append('<label><input type="checkbox" name="origin_user_asserted" value="yes"' + (' checked' if checked else '') + '> I confirm blank, sewing and printing origins from supplier information.</label>')
        out.append('</fieldset>')
    out.append(input_control('claims', ','.join(draft.claims), 'Claims (comma-separated keys)', overrides))
    out.append(select_control('audience', value(overrides, 'audience', draft.audience), [(x, x) for x in ('unknown', 'adult', 'children')], 'Audience — please confirm'))
    return ''.join(out)


def checks_html(spec):
    checks = run(spec)
    banner = '<div class="block">Resolve blockers before sending this RFQ.</div>' if blocking(checks) else ''
    rows = ''.join('<tr>' + ''.join('<td>' + esc(cell) + '</td>' for cell in (item.severity, item.rule_id, item.title, item.detail, item.action)) + '</tr>' for item in checks)
    return banner + '<h2>Claims and labeling checklist — not legal advice</h2><table><tr><th>Severity</th><th>Rule</th><th>Issue</th><th>Detail</th><th>Action</th></tr>' + rows + '</table>'


def quote_table(project, spec, remove_action=None):
    quotes = project.get('quotes', [])
    if not quotes:
        return '<p>No supplier quotes entered yet.</p>'
    current = [item for item in quotes if item['spec_version'] == spec.version]
    cheapest = min((item['total_cents'] for item in current), default=None)
    fastest = min((item['lead_time_days'] for item in current), default=None)
    rows = []
    for item in quotes:
        notes = ['stale'] if item['spec_version'] != spec.version else []
        if not notes:
            if item['total_cents'] == cheapest:
                notes.append('cheapest')
            if item['lead_time_days'] == fastest:
                notes.append('fastest')
        css = 'stale' if 'stale' in notes else ('best' if notes else '')
        remove = ''
        if remove_action:
            remove = '<form method="post" action="' + esc(remove_action) + '"><input type="hidden" name="remove" value="' + esc(item['id']) + '"><button>Remove</button></form>'
        cells = (item['supplier'], money(item['goods_cents']), money(item['shipping_cents']), money(item['total_cents']), item['lead_time_days'], item['terms'], ', '.join(notes))
        rows.append('<tr class="' + css + '">' + ''.join('<td>' + esc(cell) + '</td>' for cell in cells) + '<td>' + remove + '</td></tr>')
    return '<table><tr><th>Supplier</th><th>Goods</th><th>Shipping</th><th>Total</th><th>Days</th><th>Terms</th><th>Status</th><th></th></tr>' + ''.join(rows) + '</table>'


def print_body(project, spec, dieline_href=None, remove_action=None):
    if spec.audience == 'children':
        return '<div class="block">' + CHILD_MESSAGE + '</div>'
    body = '<h1>' + esc(spec.brand) + ' — specification v' + str(spec.version) + '</h1>'
    if spec.box:
        box = spec.box
        blank = blank_size(box.L_mil, box.W_mil, box.H_mil)
        body += '<h2>RSC box</h2><p>Blank size: {} × {} mil ({:.3f} × {:.3f} in).</p><p>Inside dimension tolerance: ±{} mil.</p><p>{}</p>'.format(*blank, blank[0] / 1000, blank[1] / 1000, box.tolerance_mil, esc(ARTWORK)) + render(box)
        if dieline_href:
            body += '<p><a href="' + esc(dieline_href) + '">Download SVG dieline</a></p>'
    if spec.tee:
        body += '<h2>Tee</h2><p><strong>Label draft:</strong> ' + esc(spec.tee.label_content) + '</p><p><strong>Care draft:</strong> ' + esc(spec.tee.care) + '</p><p>' + esc(TEE_AREA) + '</p><svg viewBox="0 0 160 200" width="240" role="img" aria-label="Tee print area"><rect x="20" y="20" width="120" height="160" fill="none" stroke="black"/><text x="30" y="100" font-size="8">12 × 16 in print area</text></svg>'
    body += checks_html(spec) + '<h2>RFQ — copy and send it yourself</h2><textarea readonly>' + esc(build_rfq(spec)) + '</textarea>'
    body += '<h2>Quote comparison</h2>' + quote_table(project, spec, remove_action)
    body += '<h2>Version history</h2><p>Hash-chained within this workspace; not externally anchored.</p><ol>' + ''.join('<li>Version ' + str(item['version']) + ' — spec_sha256 ' + esc(item['spec_sha256']) + ' — chain ' + esc(item['hash']) + '</li>' for item in project['versions']) + '</ol>'
    return body


def quote_form(spec, action, fields=None, error=''):
    fields = fields or {}
    body = '<h2>Add your supplier quote</h2>'
    if error:
        body += '<p class="error" role="alert">Please correct the quote: ' + esc(error) + '</p>'
    body += '<form method="post" action="' + esc(action) + '"><label>Supplier business name <input name="supplier" maxlength="80" required value="' + esc(value(fields, 'supplier')) + '"></label>'
    products = []
    if spec.box:
        products.append(('boxes', 'Boxes', spec.box.qty))
    if spec.tee:
        products.append(('tees', 'Tees', spec.tee.qty))
    for index in range(4):
        suffix = str(index)
        selected = value(fields, 'item_' + suffix, products[index][0] if index < len(products) else '')
        choices = [('', 'unused')] + [(key, label + ' — quantity ' + str(qty)) for key, label, qty in products]
        body += '<fieldset><legend>Price line ' + str(index + 1) + '</legend>' + select_control('item_' + suffix, selected, choices, 'Product')
        body += '<label>Description <input name="desc_' + suffix + '" maxlength="80" value="' + esc(value(fields, 'desc_' + suffix, products[index][1] if index < len(products) else '')) + '"></label><label>Unit price $ <input name="unit_' + suffix + '" value="' + esc(value(fields, 'unit_' + suffix)) + '"></label></fieldset>'
    body += '<label>Shipping $ <input name="shipping" value="' + esc(value(fields, 'shipping', '0')) + '"></label><label>Lead-time days <input name="lead_time" required value="' + esc(value(fields, 'lead_time')) + '"></label><label>Terms <textarea name="terms" maxlength="500">' + esc(value(fields, 'terms')) + '</textarea></label><button>Add quote</button></form>'
    return body


def normalized_quote(source, spec, renew_id=False):
    allowed = {'id', 'supplier', 'lines', 'goods_cents', 'shipping_cents', 'total_cents', 'lead_time_days', 'terms', 'spec_version', 'created_at'}
    if not isinstance(source, dict) or set(source) != allowed:
        raise ValueError('Unknown or missing quote keys')
    supplier = text_value(source['supplier'], 80, 'supplier')
    terms = text_value(source['terms'], 500, 'terms', True)
    text_value(source['id'], 128, 'quote ID')
    text_value(source['created_at'], 80, 'quote timestamp')
    if not isinstance(source['lines'], list) or not 1 <= len(source['lines']) <= 4:
        raise ValueError('An imported quote needs one to four lines')
    lines = []
    for item in source['lines']:
        if not isinstance(item, dict) or set(item) != {'description', 'quantity', 'unit_cents', 'total_cents'}:
            raise ValueError('Unknown or missing quote-line keys')
        description = text_value(item['description'], 80, 'quote description')
        quantity = integer(item['quantity'], 1, 10000, 'quote quantity')
        unit = integer(item['unit_cents'], 0, 999999999, 'unit cents')
        integer(item['total_cents'], quantity * unit, quantity * unit, 'line total')
        lines.append({'description': description, 'quantity': quantity, 'unit_cents': unit, 'total_cents': quantity * unit})
    shipping = integer(source['shipping_cents'], 0, 999999999, 'shipping')
    lead = integer(source['lead_time_days'], 0, 3650, 'lead time')
    version = integer(source['spec_version'], 1, 24, 'quote version')
    goods = sum(item['total_cents'] for item in lines)
    integer(source['goods_cents'], goods, goods, 'goods total')
    integer(source['total_cents'], goods + shipping, goods + shipping, 'quote total')
    return {**source, 'id': secrets.token_urlsafe(12) if renew_id else source['id'], 'supplier': supplier,
            'terms': terms, 'lines': lines, 'shipping_cents': shipping, 'lead_time_days': lead, 'spec_version': version}


def quote_from_fields(fields, spec):
    if spec is None:
        raise ValueError('Build a specification before adding quotes')
    supplier = text_value(value(fields, 'supplier'), 80, 'supplier business name')
    terms = text_value(value(fields, 'terms'), 500, 'terms', True)
    products = {}
    if spec.box:
        products['boxes'] = ('Boxes', spec.box.qty)
    if spec.tee:
        products['tees'] = ('Tees', spec.tee.qty)
    lines = []
    for index in range(4):
        suffix = str(index)
        unit = value(fields, 'unit_' + suffix)
        if not unit:
            continue
        key = value(fields, 'item_' + suffix)
        description = value(fields, 'desc_' + suffix)
        if not key:
            lower = description.casefold()
            key = 'boxes' if 'box' in lower else ('tees' if 'tee' in lower or 'shirt' in lower else '')
        if key not in products:
            raise ValueError('Each priced line must select a product from this specification')
        default, quantity = products[key]
        description = text_value(description or default, 80, 'quote description')
        unit_cents = cents(unit)
        lines.append({'description': description, 'quantity': quantity, 'unit_cents': unit_cents, 'total_cents': quantity * unit_cents})
    if not lines:
        raise ValueError('At least one unit price is required')
    shipping = cents(value(fields, 'shipping', '0'))
    lead = integer(int(value(fields, 'lead_time')), 0, 3650, 'lead time')
    goods = sum(item['total_cents'] for item in lines)
    return {'id': secrets.token_urlsafe(12), 'supplier': supplier, 'lines': lines, 'goods_cents': goods,
            'shipping_cents': shipping, 'total_cents': goods + shipping, 'lead_time_days': lead,
            'terms': terms, 'spec_version': spec.version, 'created_at': now()}


def new_project(draft, brand):
    return {'id': secrets.token_urlsafe(12), 'brand': brand, 'raw_text_hash': draft.raw_text_hash,
            'draft': draft_dict(draft), 'versions': [], 'quotes': [], 'created_at': now(), 'updated_at': now()}


def refused(project):
    return not project.get('versions') and project.get('draft', {}).get('audience') == 'children'


def import_projects(imported):
    top_allowed = {'schema', 'created_at', 'updated_at', 'written', 'sample', 'share_hash', 'projects'}
    if isinstance(imported, dict) and 'projects' in imported:
        if set(imported) - top_allowed or imported.get('schema') != 1:
            raise ValueError('Unknown export schema or keys')
        candidates = imported['projects']
    else:
        candidates = [imported]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 50:
        raise ValueError('Import must contain one to fifty projects')
    made = []
    project_allowed = {'id', 'brand', 'raw_text_hash', 'draft', 'versions', 'quotes', 'created_at', 'updated_at'}
    version_allowed = {'version', 'created_at', 'spec', 'spec_sha256', 'rulepack', 'checks', 'prev_hash', 'hash'}
    for source in candidates:
        if not isinstance(source, dict) or set(source) != project_allowed:
            raise ValueError('Unknown or missing project keys')
        text_value(source['brand'], 160, 'brand')
        text_value(source['raw_text_hash'], 64, 'request hash', True)
        for key in ('id', 'created_at', 'updated_at'):
            text_value(source[key], 128, key)
        raw_versions = source['versions']
        if not isinstance(raw_versions, list) or not 1 <= len(raw_versions) <= 24:
            raise ValueError('Import requires one to twenty-four versions')
        # Validate the supplied draft even though the final draft is regenerated.
        if source['draft'] is not None:
            imported_draft = draft_from(source['draft'])
            build(imported_draft, source['brand'])
        versions, specs, previous = [], [], '0' * 64
        for entry in raw_versions:
            if not isinstance(entry, dict) or set(entry) != version_allowed:
                raise ValueError('Unknown or missing version keys')
            spec = spec_draft(entry['spec'])
            if spec.version != len(specs) + 1 or entry['version'] != spec.version or (specs and spec.id != specs[-1].id):
                raise ValueError('Imported versions must start at one and be sequential')
            record = version_record(spec, previous)
            previous = record['hash']
            versions.append(record)
            specs.append(spec)
        raw_quotes = source['quotes']
        if not isinstance(raw_quotes, list) or len(raw_quotes) > 20:
            raise ValueError('A project can hold at most 20 quotes')
        quotes = [normalized_quote(item, specs[-1], True) for item in raw_quotes]
        if any(item['spec_version'] not in {spec.version for spec in specs} for item in quotes):
            raise ValueError('Quote refers to an unknown specification version')
        project = new_project(_draft(specs[-1]), specs[-1].brand)
        project.update(versions=versions, quotes=quotes, raw_text_hash=source['raw_text_hash'])
        made.append(project)
    return made


def make_server(port=0, host='127.0.0.1', public_base_url='https://app-norvelet.manykiln.com',
                data_dir='/tmp/norvelet-data', data_cap_mb=200, https_terminated=False,
                store=None, server_class=ProxySafeBoundedHTTPServer):
    public_base, scheme, authority = _parse_public_base_url(public_base_url)
    if https_terminated and scheme != 'https':
        raise ValueError('APP_HTTPS_TERMINATED requires an HTTPS PUBLIC_BASE_URL')
    storage = store or WorkspaceStore(data_dir, data_cap_mb)
    parser = RuleParser()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'Norvelet/1'
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
            except (ConnectionError, TimeoutError, OSError):
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
            encoded = payload.encode() if isinstance(payload, str) else payload
            self.send_response(status)
            response_headers = {'Content-Type': content_type, 'Content-Length': str(len(encoded)), 'Connection': 'close',
                                'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin',
                                'Content-Security-Policy': "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}
            if https_terminated:
                response_headers['Strict-Transport-Security'] = 'max-age=31536000'
            response_headers.update(headers or {})
            for key, item in response_headers.items():
                self.send_header(key, item)
            self.end_headers()
            self.wfile.write(encoded)

        def _error(self, status, message):
            self._send(status, page('Error', '<h1>' + esc(message) + '</h1>'))

        def _redirect(self, location):
            self._send(303, b'', headers={'Location': (public_base or '') + location})

        def _host_ok(self):
            values = self.headers.get_all('Host', [])
            return authority is None or (len(values) == 1 and _normalized_authority(values[0], scheme) == authority)

        def _origin_ok(self):
            origins = self.headers.get_all('Origin', [])
            if origins:
                return len(origins) == 1 and origins[0] != 'null' and origins[0].rstrip('/') == public_base
            referer = self.headers.get('Referer', '')
            return bool(public_base and (referer == public_base or referer.startswith(public_base + '/')))

        def _fields(self):
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1 or not re.fullmatch(r'\d{1,8}', lengths[0]):
                raise ValueError('A valid Content-Length is required')
            size = int(lengths[0])
            if size > MAX_BODY:
                raise OverflowError('Request body exceeds 1 MB')
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError('Incomplete request body')
            content_type = self.headers.get('Content-Type', '')
            if content_type.startswith('multipart/form-data'):
                match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
                if not match:
                    raise ValueError('Multipart boundary missing')
                boundary = ('--' + next(item for item in match.groups() if item).strip()).encode()
                fields = {}
                for part in raw.split(boundary)[1:-1]:
                    head, marker, body = part.lstrip(b'\r\n').partition(b'\r\n\r\n')
                    name = re.search(br'name="([^"]+)"', head)
                    if marker and name:
                        fields.setdefault(name.group(1).decode(), []).append(body.removesuffix(b'\r\n').decode('utf-8'))
            else:
                fields = parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=128)
            if sum(len(items) for items in fields.values()) > 128:
                raise ValueError('Too many fields')
            for key, items in fields.items():
                maximum = 4000 if key == 'text' else (MAX_BODY if key in ('json', 'file') else 512)
                if any(len(item) > maximum for item in items):
                    raise ValueError('Field exceeds its limit')
            return fields

        def do_GET(self):
            if not self._host_ok():
                self._error(400, 'Invalid Host header')
                return
            try:
                self._get(urlsplit(self.path).path)
            except NotFound as error:
                self._error(404, str(error))
            except (ValueError, TypeError, KeyError, AttributeError):
                self._error(400, 'Invalid stored data')
            except Exception:
                self._error(500, 'The request could not be completed')

        def do_POST(self):
            if not self._host_ok():
                self._error(400, 'Invalid Host header')
                return
            if not self._origin_ok():
                self._error(403, 'Same-origin form submission required')
                return
            fields, path = {}, ''
            try:
                path = urlsplit(self.path).path
                fields = self._fields()
                self._post(path, fields)
            except OverflowError as error:
                self._error(413, str(error))
            except CapacityError as error:
                self._error(507, str(error))
            except RateError as error:
                self._error(429, str(error))
            except NotFound as error:
                self._error(404, str(error))
            except (ValueError, TypeError, KeyError) as error:
                # Recovery rendering can race deletion or encounter damaged data.
                try:
                    handled = self._form_error(path, fields, str(error))
                except NotFound:
                    self._error(404, 'Workspace or project no longer exists')
                    return
                except Exception:
                    self._error(400, 'Invalid form or stored data')
                    return
                if not handled:
                    self._error(400, str(error) or 'Invalid form data')
            except Exception:
                self._error(500, 'The request could not be completed')

        def _form_error(self, path, fields, message):
            match = re.fullmatch(r'/w/([^/]+)/p/([^/]+)/(spec|quotes)', path)
            if not match:
                return False
            token, pid, action = match.groups()
            project = project_by_id(storage.load(token), pid)
            spec = latest(project)
            if action == 'spec':
                base = _draft(spec) if spec else draft_from(project['draft'])
                body = '<h1>Review specification</h1><form method="post" action="' + esc(path) + '">' + editor(base, bool(spec and spec.origin_user_asserted), fields, message) + '<button>Save specification</button></form>'
            elif spec:
                body = print_body(project, spec) + quote_form(spec, path, fields, message)
            else:
                return False
            self._send(400, page('Correct form', body))
            return True

        def _get(self, path):
            if path == '/healthz':
                self._send(200, 'ok', 'text/plain; charset=utf-8')
                return
            if path == '/':
                self._send(200, page('Norvelet', '<h1>Norvelet spec checker</h1><p>Describe boxes and tees, check a specification, prepare an RFQ and compare your supplier quotes.</p><p><strong>Rule-based quick-fill, not AI: check every field.</strong> Nothing is sent to suppliers.</p><form method="post" action="/w"><button>Start a workspace</button></form><form method="post" action="/w/sample"><button>Try with sample data</button></form>'))
                return
            shared = re.fullmatch(r'/s/([^/]+)(?:/p/([^/]+)/dieline\.svg)?', path)
            if shared:
                share, pid = shared.groups()
                data = storage.load_share(share)
                if pid:
                    self._dieline(latest(project_by_id(data, pid)))
                    return
                body = '<h1>Read-only shared specifications</h1>'
                for project in data['projects']:
                    if refused(project):
                        body += '<p class="block">' + CHILD_MESSAGE + '</p>'
                    else:
                        spec = latest(project)
                        if spec:
                            body += print_body(project, spec, '/s/' + share + '/p/' + project['id'] + '/dieline.svg')
                self._send(200, page('Shared specifications', body))
                return
            match = re.fullmatch(r'/w/([^/]+)(?:/p/([^/]+)(?:/(dieline\.svg|print))?|/export)?', path)
            if not match:
                raise NotFound('Not found')
            token, pid, tail = match.groups()
            data = storage.load(token)
            base = '/w/' + token
            if path.endswith('/export'):
                self._send(200, json.dumps(data, indent=2, sort_keys=True), 'application/json; charset=utf-8', {'Content-Disposition': 'attachment; filename=norvelet-workspace.json'})
                return
            if pid is None:
                projects = ''.join('<li><a href="' + esc(base + '/p/' + item['id']) + '">' + esc(item['brand']) + '</a></li>' for item in data['projects']) or '<li>No projects yet.</li>'
                body = '<h1>Private workspace</h1><p><strong>Bookmark this link: it is the only key and cannot be recovered.</strong></p><p>' + EXPIRY_NOTICE + '</p><h2>Projects</h2><ul>' + projects + '</ul><h2>New project</h2><form method="post" action="' + esc(base + '/p') + '"><label>Brand name <input name="brand" maxlength="160" required></label><label>Describe boxes and tees <textarea name="text" maxlength="4000" required></textarea></label><button>Rule-based quick-fill</button></form><h2>Import project JSON</h2><form method="post" enctype="multipart/form-data" action="' + esc(base + '/p/import') + '"><input type="file" name="file" accept="application/json"><label>or paste JSON<textarea name="json"></textarea></label><button>Import</button></form><p>' + ('A read-only share link is active.' if data.get('share_hash') else 'No read-only share link yet.') + '</p><form method="post" action="' + esc(base + '/share') + '"><button>Create or rotate read-only share link</button></form><p><a href="' + esc(base + '/export') + '">Export everything</a></p><form method="post" action="' + esc(base + '/delete') + '"><button>Delete this workspace now</button></form>'
                self._send(200, page('Workspace', body))
                return
            project = project_by_id(data, pid)
            project_path = base + '/p/' + pid
            if refused(project):
                if tail == 'dieline.svg':
                    raise NotFound('Dieline unavailable')
                self._send(200, page('Request refused', '<h1>Request refused</h1><p class="block">' + CHILD_MESSAGE + '</p>'))
                return
            spec = latest(project)
            if tail == 'dieline.svg':
                self._dieline(spec)
                return
            if spec is None:
                body = '<h1>Review rule-based quick-fill</h1><form method="post" action="' + esc(project_path + '/spec') + '">' + editor(draft_from(project['draft'])) + '<button>Build specification</button></form>'
            else:
                body = print_body(project, spec, project_path + '/dieline.svg', None if tail == 'print' else project_path + '/quotes')
                if tail != 'print':
                    remove = ''.join('<label><input type="checkbox" name="claim" value="' + esc(claim) + '"> Remove ' + esc(claim.replace('_', ' ')) + '</label>' for claim in spec.claims)
                    body += '<section class="no-print"><h2>Revise</h2><form method="post" action="' + esc(project_path + '/spec') + '">' + editor(_draft(spec), spec.origin_user_asserted) + remove + '<button>Create next version</button></form>' + quote_form(spec, project_path + '/quotes') + '<p><a href="' + esc(project_path + '/print') + '">Printable page</a></p><form method="post" action="' + esc(project_path + '/delete') + '"><button>Delete project</button></form></section>'
            self._send(200, page('Specification', body))

        def _dieline(self, spec):
            if spec is None or spec.audience == 'children' or not spec.box:
                raise NotFound('Dieline unavailable')
            self._send(200, render(spec.box), 'image/svg+xml; charset=utf-8', {'Content-Disposition': 'attachment; filename=dieline.svg'})

        def _post(self, path, fields):
            if path in ('/w', '/w/sample'):
                token, _ = storage.create(path.endswith('/sample'), rate_limited=True)
                if path.endswith('/sample'):
                    draft = parser.parse(SAMPLE, 'Fernleaf Goods (fictional)')
                    project = new_project(draft, 'Fernleaf Goods (fictional)')
                    project['versions'].append(version_record(build(draft, project['brand'])))
                    storage.mutate(token, lambda workspace: workspace['projects'].append(project))
                self._redirect('/w/' + token)
                return
            match = re.fullmatch(r'/w/([^/]+)/(delete|share)', path)
            if match:
                token, action = match.groups()
                if action == 'delete':
                    storage.delete(token)
                    self._redirect('/')
                else:
                    share = storage.rotate_share(token)
                    link = (public_base or '') + '/s/' + share
                    self._send(200, page('Share link', '<h1>Read-only share link</h1><p><a href="' + esc(link) + '">' + esc(link) + '</a></p><p>Rotating it invalidates the previous link.</p>'))
                return
            match = re.fullmatch(r'/w/([^/]+)/p/import', path)
            if match:
                token = match.group(1)
                made = import_projects(json.loads(value(fields, 'file') or value(fields, 'json')))
                def add_import(workspace):
                    if len(workspace['projects']) + len(made) > 50:
                        raise CapacityError('A workspace can hold at most 50 projects')
                    workspace['projects'].extend(made)
                storage.mutate(token, add_import)
                self._redirect('/w/' + token + '/p/' + made[0]['id'])
                return
            match = re.fullmatch(r'/w/([^/]+)/p(?:/([^/]+)/(spec|quotes|delete))?', path)
            if not match:
                raise NotFound('Not found')
            token, pid, action = match.groups()
            if pid is None:
                text = text_value(value(fields, 'text'), 4000, 'request')
                brand = text_value(value(fields, 'brand'), 160, 'brand')
                project = new_project(parser.parse(text, brand), brand)
                def add(workspace):
                    if len(workspace['projects']) >= 50:
                        raise CapacityError('A workspace can hold at most 50 projects')
                    workspace['projects'].append(project)
                storage.mutate(token, add)
                self._redirect('/w/' + token + '/p/' + project['id'])
                return
            if action == 'delete':
                storage.mutate(token, lambda workspace: workspace['projects'].remove(project_by_id(workspace, pid)))
                self._redirect('/w/' + token)
                return
            def change(workspace):
                project = project_by_id(workspace, pid)
                current = latest(project)
                if action == 'spec':
                    base = _draft(current) if current else draft_from(project['draft'])
                    updated = apply_form(base, fields)
                    if updated.audience == 'children':
                        # Keep only the unbuilt request. No child specification,
                        # label, care text, checklist or RFQ is ever generated.
                        if current:
                            raise ValueError(CHILD_MESSAGE)
                        project['draft'] = draft_dict(updated)
                        project['quotes'] = []
                        return
                    if len(project['versions']) >= 24:
                        raise CapacityError('A project can have at most 24 versions')
                    spec = build(updated, project['brand'], current.id if current else None,
                                 current.version + 1 if current else 1, value(fields, 'origin_user_asserted') == 'yes')
                    previous = project['versions'][-1]['hash'] if project['versions'] else '0' * 64
                    project['versions'].append(version_record(spec, previous))
                    project['draft'] = draft_dict(updated)
                    project['updated_at'] = now()
                elif action == 'quotes':
                    if refused(project):
                        raise ValueError(CHILD_MESSAGE)
                    remove = value(fields, 'remove')
                    if remove:
                        project['quotes'] = [item for item in project['quotes'] if item['id'] != remove]
                    else:
                        if len(project['quotes']) >= 20:
                            raise CapacityError('A project can hold at most 20 quotes')
                        project['quotes'].append(quote_from_fields(fields, current))
            storage.mutate(token, change)
            self._redirect('/w/' + token + '/p/' + pid)

    server = server_class((host, port), Handler, max_threads=16)
    server.workspace_store = storage
    server.public_base_url = public_base
    server.https_terminated = bool(https_terminated)
    return server
