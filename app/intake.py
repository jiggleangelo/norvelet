import hashlib
import re
from typing import Protocol

from .models import BoxDraft, IntakeDraft, TeeDraft


class NotConfigured(RuntimeError):
    pass


class IntakeModel(Protocol):
    def parse(self, text: str, brand: str) -> IntakeDraft: ...


CLAIMS = {
    'eco_friendly': re.compile(r'\b(?:eco[- ]friendly|environmentally friendly|sustainable)\b'),
    'recyclable': re.compile(r'\brecyclable\b'),
    'degradable': re.compile(r'\b(?:compostable|biodegradable|degradable)\b'),
    'organic': re.compile(r'\borganic\b'),
    'made_in_usa': re.compile(r'\b(?:made in (?:the )?u\.?s\.?a\.?|american[- ]made)\b'),
    'non_toxic': re.compile(r'\bnon[- ]toxic\b'),
}


class FakeIntake:
    def parse(self, text, brand=''):
        low = []
        lower = text.lower()
        dimension = re.search(
            r'\b(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(cm|in(?:ch(?:es)?)?|\")?\b',
            lower,
        )
        if dimension:
            factor = 393.700787 if dimension.group(4) == 'cm' else 1000
            dims = [round(float(dimension.group(i)) * factor) for i in range(1, 4)]
        else:
            dims = [10000, 8000, 4000]
            low.append('box dimensions')

        def qty_for(words, default):
            targets = '|'.join(re.escape(word) for word in words)
            pattern = rf'\b(\d+)\b(?:\s+(?:pcs|pieces))?(?:\s+[a-z][a-z-]*){{0,3}}\s+(?:{targets})\b'
            match = re.search(pattern, lower)
            if match:
                return int(match.group(1))
            low.append(words[0] + ' quantity')
            return default

        has_box = bool(re.search(r'\b(?:box(?:es)?|packaging|mailer(?:s)?)\b', lower))
        has_tee = bool(re.search(r'\b(?:tee(?:s)?|shirt(?:s)?|apparel)\b', lower))
        if not (has_box or has_tee):
            has_box = has_tee = True
        bqty = qty_for(('boxes', 'box'), 100) if has_box else 0
        tqty = qty_for(('tees', 'shirts', 'tee', 'shirt'), 100) if has_tee else 0
        box = BoxDraft(
            bqty,
            *dims,
            '32ECT-B-white' if 'white box' in lower else '32ECT-B-kraft',
            ('top', 'inside') if re.search(r'\binside\b', lower) else ('top',),
            2 if re.search(r'\b2[- ]color\b', lower) else 1,
        ) if has_box else None
        if '50/50' in lower:
            fiber = (('Cotton', 50), ('Polyester', 50))
        elif re.search(r'\bpolyester\b', lower):
            fiber = (('Polyester', 100),)
        elif re.search(r'\btri[- ]blend\b', lower):
            fiber = (('Cotton', 50), ('Polyester', 25), ('Rayon', 25))
        else:
            fiber = (('Cotton', 100),)
            if not re.search(r'\bcotton\b', lower):
                low.append('tee fiber')
        sizes = {'S': tqty // 4, 'M': tqty // 4, 'L': tqty // 4, 'XL': tqty - 3 * (tqty // 4)}
        tee = TeeDraft(
            tqty,
            'black' if re.search(r'\bblack\b', lower) else 'white',
            fiber,
            sizes,
            'dtg' if re.search(r'\bdtg\b', lower) else 'screen',
            ('front',),
            {'blank': 'Honduras', 'sewing': 'Honduras', 'print': 'USA'},
        ) if has_tee else None
        claims = tuple(key for key, pattern in CLAIMS.items() if pattern.search(lower))
        audience_text = re.sub(r'\bbaby blue\b|\byouth soccer club(?: logo)?\b', '', lower)
        if re.search(r'\b(?:kid|kids|child|children|toddler|toddlers|baby|babies|youth)\b', audience_text):
            audience = 'children'
        elif re.search(r'\b(?:adult|adults|men|mens|women|womens)\b', audience_text):
            audience = 'adult'
        else:
            audience = 'unknown'
        return IntakeDraft(hashlib.sha256(text.encode()).hexdigest(), box, tee, claims, audience, tuple(low))


class LLMIntake:
    def __init__(self, transport=None):
        self.transport = transport

    def parse(self, text, brand=''):
        if self.transport is None:
            raise NotConfigured('LLM transport is not configured')
        raise NotConfigured('Real LLM adapter is outside this pilot')
