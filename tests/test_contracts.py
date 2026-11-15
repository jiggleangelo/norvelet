import ast
import pathlib
import sys
import unittest
from dataclasses import replace

from app.compliance import blocking, run
from app.intake import FakeIntake
from app.spec import build, revise


class ContractTests(unittest.TestCase):
    def test_featured_prompt_quantities_and_false_positive_contexts(self):
        prompt = '100 kraft shipping boxes 10x8x4 in plus 80 black cotton tees sizes S–XL'
        draft = FakeIntake().parse(prompt, 'Organic Valley Brand')
        self.assertEqual(draft.box.qty, 100)
        self.assertEqual(draft.tee.qty, 80)
        self.assertNotIn('organic', draft.claims)
        self.assertNotIn('eco_friendly', draft.claims)
        self.assertEqual(draft.audience, 'unknown')
        self.assertEqual(FakeIntake().parse('100 green tees', 'x').claims, ())
        self.assertNotEqual(FakeIntake().parse('baby blue tees', 'x').audience, 'children')
        self.assertNotEqual(FakeIntake().parse('youth soccer club logo on tees', 'x').audience, 'children')

    def test_compliance_categories_and_revision(self):
        draft = FakeIntake().parse('100 cotton tees made in USA compostable organic', 'x')
        spec = build(draft, 'x')
        ids = {result.rule_id for result in run(spec)}
        self.assertIn('CLM-USA', ids)
        self.assertIn('CLM-DEGRADE', ids)
        self.assertIn('CLM-ORGANIC', ids)
        updated = revise(spec, ())
        self.assertEqual(updated.version, 2)
        self.assertFalse(blocking(run(updated)))

    def test_children_block(self):
        spec = build(FakeIntake().parse('100 kids cotton tees', 'x'), 'x')
        self.assertIn('CPSIA-CHILD', {result.rule_id for result in run(spec)})

    def test_size_sum_and_screen_minimum(self):
        draft = FakeIntake().parse('25 cotton tees', 'x')
        bad_sizes = replace(draft.tee, sizes={'S': 1, 'M': 1, 'L': 1, 'XL': 1})
        with self.assertRaises(ValueError):
            build(replace(draft, tee=bad_sizes), 'x')
        small = replace(draft.tee, qty=23, sizes={'S': 5, 'M': 6, 'L': 6, 'XL': 6})
        with self.assertRaises(ValueError):
            build(replace(draft, tee=small), 'x')

    def test_application_imports_are_stdlib_or_local(self):
        root = pathlib.Path(__file__).parents[1]
        # Use Python's standard-library inventory rather than a partial list
        # that incorrectly rejects filesystem modules such as os and tempfile.
        allowed = sys.stdlib_module_names | {'app'}
        for path in (root / 'app').glob('*.py'):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue
                    self.assertIn(node.module.split('.')[0], allowed,
                                  f'non-stdlib import in {path}: {node.module}')
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split('.')[0], allowed,
                                      f'non-stdlib import in {path}: {alias.name}')


if __name__ == '__main__':
    unittest.main()
