"""Deterministic fictional suppliers behind a replaceable QuoteSource interface."""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from .pricing import platform_fee


@dataclass(frozen=True)
class Quote:
    quote_id: str
    spec_id: str
    spec_version: int
    created_at: object
    seller_of_record: str
    items: tuple
    goods_cents: int
    shipping_cents: int
    platform_fee_cents: int
    total_cents: int
    lead_time_days: int
    warranty: str
    refund_terms: str
    valid_until: str
    notes: tuple
    manufacturer_id: str


class QuoteSource(Protocol):
    manufacturer_id: str
    def can_make(self, spec): ...
    def quote(self, spec, now): ...
    def assign_lot(self, spec, quote_id): ...


_MANUFACTURERS = (
    ('Cedar Packaging & Apparel (fictional)', 240, 950, 1800, 14,
     'Manufacturing defects covered for 30 days after delivery.',
     'Full refund before production; defective goods replaced or refunded.'),
    ('Harbor Custom Manufacturing (fictional)', 275, 1025, 1200, 10,
     'Manufacturing defects covered for 60 days after delivery.',
     'Full refund before artwork approval; defects refunded after inspection.'),
    ('Maple Print Works (fictional)', 220, 900, 2400, 21,
     'Manufacturing defects covered for 45 days after delivery.',
     'Full refund before production; custom goods otherwise nonreturnable except for defects.'),
)


class FakeManufacturer:
    def __init__(self, index):
        self.terms = _MANUFACTURERS[index]
        self.manufacturer_id = ('cedar', 'harbor', 'maple')[index]

    def can_make(self, spec):
        return bool((spec.box and spec.box.qty > 0) or (spec.tee and spec.tee.qty > 0))

    def quote(self, spec, now):
        if not self.can_make(spec):
            raise ValueError('A quote requires at least one positive quantity')
        seller, box_price, tee_price, shipping, days, warranty, refunds = self.terms
        items = []
        notes = ['Box tolerance: ±62 mil; tee placement tolerance: ±500 mil.']
        if spec.box and spec.box.qty:
            items.append(('Printed boxes', spec.box.qty, box_price))
        if spec.tee and spec.tee.qty:
            fiber = ', '.join(f'{pct}% {name}' for name, pct in spec.tee.fiber)
            items.append(('Printed tees — ' + fiber, spec.tee.qty, tee_price))
            if spec.tee.origin['blank'] != 'USA':
                notes.append('Import duties: manufacturer to confirm')
        goods = sum(quantity * unit for _, quantity, unit in items)
        fee = platform_fee(goods + shipping)
        return Quote('q_' + secrets.token_urlsafe(18), spec.id, spec.version, now,
                     seller, tuple(items), goods, shipping, fee, goods + shipping + fee,
                     days, warranty, refunds, (now + timedelta(days=7)).isoformat(),
                     tuple(notes), self.manufacturer_id)

    def assign_lot(self, spec, quote_id):
        identity = '{}:{}:{}'.format(spec.id, spec.version, quote_id)
        return 'LOT-' + hashlib.sha256(identity.encode()).hexdigest()[:24].upper()


def make_quotes(spec, now):
    return tuple(FakeManufacturer(i).quote(spec, now) for i in range(3))


def assign_lot(spec, quote):
    if quote.spec_id != spec.id or quote.spec_version != spec.version:
        raise ValueError('Quote does not belong to the current specification')
    source = next(FakeManufacturer(i) for i in range(3)
                  if FakeManufacturer(i).manufacturer_id == quote.manufacturer_id)
    return source.assign_lot(spec, quote.quote_id)
