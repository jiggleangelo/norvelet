from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True)
class BoxDraft:
    qty: int
    L_mil: int
    W_mil: int
    H_mil: int
    board: str
    print_sides: tuple[str, ...]
    ink_colors: int


@dataclass(frozen=True)
class TeeDraft:
    qty: int
    color: str
    fiber: tuple[tuple[str, int], ...]
    sizes: dict[str, int]
    method: str
    placements: tuple[str, ...]
    origin: dict[str, str]


@dataclass(frozen=True)
class IntakeDraft:
    raw_text_hash: str
    box: BoxDraft | None
    tee: TeeDraft | None
    claims: tuple[str, ...]
    audience: str
    low_confidence: tuple[str, ...]


@dataclass(frozen=True)
class BoxSpec:
    qty: int
    L_mil: int
    W_mil: int
    H_mil: int
    board: str
    print_sides: tuple[str, ...]
    ink_colors: int
    tolerance_mil: int = 62


@dataclass(frozen=True)
class TeeSpec:
    qty: int
    color: str
    fiber: tuple[tuple[str, int], ...]
    sizes: dict[str, int]
    method: str
    placements: tuple[str, ...]
    origin: dict[str, str]
    label_content: str
    care: str


@dataclass(frozen=True)
class Spec:
    id: str
    version: int
    brand: str
    box: BoxSpec | None
    tee: TeeSpec | None
    claims: tuple[str, ...]
    audience: str
    origin_user_asserted: bool = False

    def canonical_json(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))


@dataclass(frozen=True)
class CheckResult:
    rule_id: str
    severity: str
    title: str
    detail: str
    action: str
    reviewed_by: str | None = None


@dataclass(frozen=True)
class QuoteLine:
    description: str
    cents: int


@dataclass(frozen=True)
class Quote:
    quote_id: str
    manufacturer_id: str
    seller_of_record: str
    sku_lines: tuple[QuoteLine, ...]
    goods_cents: int
    shipping_cents: int
    platform_fee_cents: int
    total_cents: int
    lead_time_days: tuple[int, int]
    warranty: str
    refund_terms: str
    valid_until: str
    notes: tuple[str, ...]


@dataclass(frozen=True)
class OrderIntent:
    id: str
    spec_id: str
    spec_version: int
    quote_id: str
    contact_token: str
    deposit_intent: str
    lot_id: str


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    ts: str
    type: str
    payload: dict
    prev_hash: str
    hash: str
