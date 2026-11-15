import hashlib
import json
from datetime import datetime, timezone
from threading import RLock

from .models import AuditEvent


class AuditLog:
    def __init__(self, clock=None, max_events_per_spec=2000):
        self.events = {}
        self.clock = clock
        self.max_events_per_spec = max_events_per_spec
        self.lock = RLock()

    def _now(self):
        value = self.clock.now() if self.clock else datetime.now(timezone.utc)
        return value.isoformat()

    def append(self, spec_id, event_type, payload):
        with self.lock:
            chain = self.events.setdefault(spec_id, [])
            if len(chain) >= self.max_events_per_spec:
                raise OverflowError('audit event limit reached')
            seq = len(chain) + 1
            prev = chain[-1].hash if chain else '0' * 64
            timestamp = self._now()
            safe_payload = json.loads(json.dumps(payload, sort_keys=True))
            raw = json.dumps(
                {'seq': seq, 'ts': timestamp, 'type': event_type, 'payload': safe_payload},
                sort_keys=True,
                separators=(',', ':'),
            )
            digest = hashlib.sha256((prev + '|' + raw).encode()).hexdigest()
            event = AuditEvent(seq, timestamp, event_type, safe_payload, prev, digest)
            chain.append(event)
            return event

    def verify(self, spec_id):
        with self.lock:
            prev = '0' * 64
            for event in self.events.get(spec_id, []):
                raw = json.dumps(
                    {'seq': event.seq, 'ts': event.ts, 'type': event.type, 'payload': event.payload},
                    sort_keys=True,
                    separators=(',', ':'),
                )
                digest = hashlib.sha256((prev + '|' + raw).encode()).hexdigest()
                if event.prev_hash != prev or event.hash != digest:
                    return False, event.seq
                prev = event.hash
            return True, None

    def snapshot(self, spec_id):
        with self.lock:
            return tuple(self.events.get(spec_id, ()))

    def head(self, spec_id):
        with self.lock:
            chain = self.events.get(spec_id, ())
            return chain[-1].hash if chain else '0' * 64

    def evict(self, spec_id):
        with self.lock:
            self.events.pop(spec_id, None)
