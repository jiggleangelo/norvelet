import concurrent.futures
import unittest
from dataclasses import replace

from app.audit import AuditLog
from app.intake import FakeIntake
from app.spec import build, revise
from app.store import Store
from app.vault import FakeVault


class SecurityTests(unittest.TestCase):
    def test_capability_ids_are_long_and_revisions_preserve_them(self):
        draft = FakeIntake().parse('100 boxes 10x8x4 in', 'x')
        original = build(draft, 'x')
        updated = revise(original, ())
        self.assertGreaterEqual(len(original.id), 22)
        self.assertEqual(original.id, updated.id)
        self.assertEqual(updated.version, 2)

    def test_audit_rejects_overflow_without_breaking_chain(self):
        audit = AuditLog(max_events_per_spec=2)
        audit.append('spec', 'one', {'safe': 1})
        audit.append('spec', 'two', {'safe': 2})
        with self.assertRaises(OverflowError):
            audit.append('spec', 'three', {'safe': 3})
        self.assertEqual(audit.verify('spec'), (True, None))

    def test_concurrent_audit_appends_form_one_chain(self):
        audit = AuditLog(max_events_per_spec=100)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda value: audit.append('spec', 'event', {'value': value}), range(50)))
        self.assertEqual(len(audit.snapshot('spec')), 50)
        self.assertEqual(audit.verify('spec'), (True, None))
        self.assertEqual([event.seq for event in audit.snapshot('spec')], list(range(1, 51)))

    def test_store_evicts_related_state(self):
        store = Store(max_specs=1)
        first = build(FakeIntake().parse('100 boxes', 'x'), 'x')
        second = build(FakeIntake().parse('100 boxes', 'x'), 'x')
        store.put(first)
        store.audit.append(first.id, 'created', {})
        store.set_quotes(first.id, ('quote',))
        store.put(second)
        self.assertIsNone(store.get(first.id))
        self.assertEqual(store.audit.snapshot(first.id), ())
        self.assertEqual(store.get_quotes(first.id), ())

    def test_vault_is_bounded_and_has_no_read_interface(self):
        vault = FakeVault(max_values=2)
        for value in ('a@example.test', 'b@example.test', 'c@example.test'):
            vault.store('email', value)
        self.assertEqual(vault.count(), 2)
        self.assertFalse(hasattr(vault, 'get'))


if __name__ == '__main__':
    unittest.main()
