import secrets

from .models import BoxSpec, Spec, TeeSpec


ARTWORK = 'Artwork: 125 mil bleed; 125 mil safe zone; CMYK; vector PDF or 300 dpi raster at final size.'
TEE_AREA = 'Tee print area: maximum 12000 × 16000 mil; placement tolerance ±500 mil.'


def care_for(fiber, method):
    base = 'Machine wash cold inside out, tumble dry low'
    return base + ', do not iron print' if method in ('screen', 'dtg') else base


def origin_label(origin):
    values = [origin.get(key, 'Unknown') for key in ('blank', 'sewing', 'print')]
    if all(value == 'USA' for value in values):
        return 'Made in USA'
    first = values[0] if values[0] != 'Unknown' else values[1]
    return f'Made in {first}. Printed in {values[2]}'


def build(draft, brand, spec_id=None, version=1, origin_user_asserted=False):
    if not brand or len(brand) > 160 or not (draft.box or draft.tee):
        raise ValueError('Brand and at least one product are required')
    if draft.audience not in ('adult', 'children', 'unknown'):
        raise ValueError('Invalid audience')
    box = BoxSpec(**draft.box.__dict__) if draft.box else None
    if box:
        if not 1 <= box.qty <= 10000 or not all(250 <= n <= 60000 for n in (box.L_mil, box.W_mil, box.H_mil)):
            raise ValueError('Box quantity or dimensions are outside demo limits')
        if box.board not in ('32ECT-B-kraft', '32ECT-B-white') or not 1 <= box.ink_colors <= 4:
            raise ValueError('Unsupported board or ink count')
        if not box.print_sides or not set(box.print_sides) <= {'top', 'inside', 'outside'}:
            raise ValueError('Unsupported box print sides')
    tee = None
    if draft.tee:
        data = draft.tee
        if not 1 <= data.qty <= 10000 or sum(data.sizes.values()) != data.qty or any(n < 0 for n in data.sizes.values()):
            raise ValueError('Size quantities must sum to a positive tee quantity')
        if data.method not in ('screen', 'dtg') or (data.method == 'screen' and data.qty < 24):
            raise ValueError('Screen printing requires at least 24 tees; method must be screen or dtg')
        if not data.fiber or sum(pct for _, pct in data.fiber) != 100 or any(name not in ('Cotton', 'Polyester', 'Rayon') or pct <= 0 for name, pct in data.fiber):
            raise ValueError('Fiber percentages must total 100')
        if set(data.origin) != {'blank', 'sewing', 'print'} or any(not v or len(v) > 80 for v in data.origin.values()):
            raise ValueError('Provide blank, sewing and print origins')
        if not data.placements or not set(data.placements) <= {'front', 'back'} or len(data.color) > 80:
            raise ValueError('Unsupported tee placement or color')
        fiber = ', '.join(f'{pct}% {name}' for name, pct in data.fiber)
        label = f'{fiber} | {origin_label(data.origin)} | {brand}'
        tee = TeeSpec(**data.__dict__, label_content=label, care=care_for(data.fiber, data.method))
    return Spec(spec_id or secrets.token_urlsafe(24), version, brand, box, tee,
                draft.claims, draft.audience, origin_user_asserted)


def revise(spec, claims=None):
    return Spec(spec.id, spec.version + 1, spec.brand, spec.box, spec.tee,
                tuple(spec.claims if claims is None else claims), spec.audience,
                spec.origin_user_asserted)
