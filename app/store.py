from collections import OrderedDict, deque
from threading import RLock
from time import monotonic

from .audit import AuditLog
from .vault import FakeVault


class Store:
    def __init__(self, max_specs=100, max_intents=500, max_versions=24):
        self.specs = OrderedDict()
        self.quotes = OrderedDict()
        self.intents = OrderedDict()
        self.audit = AuditLog(max_events_per_spec=96)
        self.vault = FakeVault(max_values=max_intents)
        self.lock = RLock()
        self.max_specs = max_specs
        self.max_intents = max_intents
        self.max_versions = max_versions
        self.rate_windows = OrderedDict()
        self.stats = dict.fromkeys(('home', 'intakes', 'specs', 'quotes', 'intents',
                                   'deposit_yes', 'deposit_maybe', 'deposit_no',
                                   'survey_yes', 'survey_no'), 0)

    def put(self, spec):
        with self.lock:
            versions = self.specs.get(spec.id, [])
            if len(versions) >= self.max_versions:
                raise OverflowError('Specification revision limit reached')
            self.specs.setdefault(spec.id, []).append(spec)
            self.specs.move_to_end(spec.id)
            self.quotes.pop(spec.id, None)
            while len(self.specs) > self.max_specs:
                old_id, _ = self.specs.popitem(last=False)
                self.quotes.pop(old_id, None)
                self.intents.pop(old_id, None)
                self.audit.evict(old_id)

    def commit(self, spec, event_type, payload):
        with self.lock:
            if len(self.specs.get(spec.id, ())) >= self.max_versions:
                raise OverflowError('Specification revision limit reached')
            self.audit.append(spec.id, event_type, payload)
            self.put(spec)

    def get(self, spec_id):
        with self.lock:
            versions = self.specs.get(spec_id)
            if not versions:
                return None
            self.specs.move_to_end(spec_id)
            return versions[-1]

    def set_quotes(self, spec_id, quote_values):
        with self.lock:
            if spec_id not in self.specs:
                raise KeyError(spec_id)
            self.quotes[spec_id] = tuple(quote_values)

    def get_quotes(self, spec_id):
        with self.lock:
            return self.quotes.get(spec_id, ())

    def put_intent(self, intent):
        with self.lock:
            if intent.spec_id in self.intents:
                return False
            if len(self.intents) >= self.max_intents:
                raise OverflowError('Order intent limit reached')
            self.intents[intent.spec_id] = intent
            return True

    def has_intent(self, spec_id):
        with self.lock:
            return spec_id in self.intents

    def increment(self, key):
        with self.lock:
            self.stats[key] += 1

    def stats_snapshot(self):
        with self.lock:
            return dict(self.stats)

    def allow_request(self, client, limit=120, window_seconds=60):
        now = monotonic()
        with self.lock:
            window = self.rate_windows.setdefault(client, deque())
            self.rate_windows.move_to_end(client)
            while len(self.rate_windows) > 1000:
                self.rate_windows.popitem(last=False)
            while window and window[0] <= now - window_seconds:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True
