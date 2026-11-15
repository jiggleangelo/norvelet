import unittest
from dataclasses import replace
from xml.etree import ElementTree
from app.audit import AuditLog
from app.compliance import blocking,run
from app.dieline import blank_size,render
from app.intake import FakeIntake,LLMIntake,NotConfigured
from app.pricing import platform_fee
from app.spec import build,origin_label
from app.vault import FakeVault

class CoreTests(unittest.TestCase):
 def setUp(self): self.draft=FakeIntake().parse('100 boxes 10x8x4 in plus 100 black cotton tees eco-friendly made in USA','Fernleaf')
 def test_intake(self):
  self.assertEqual((self.draft.box.L_mil,self.draft.box.W_mil,self.draft.box.H_mil),(10000,8000,4000)); self.assertIn('made_in_usa',self.draft.claims)
  cm=FakeIntake().parse('50 boxes 25.4x20.32x10.16 cm','x'); self.assertEqual(cm.box.L_mil,10000)
  with self.assertRaises(NotConfigured):LLMIntake().parse('x')
 def test_spec_and_rules(self):
  spec=build(self.draft,'Fernleaf'); results=run(spec); self.assertTrue(blocking(results)); self.assertEqual(results[0].severity,'block'); self.assertTrue(all(r.reviewed_by is None for r in results)); self.assertNotEqual(origin_label(spec.tee.origin),'Made in USA')
 def test_dieline(self):
  self.assertEqual(blank_size(10000,8000,4000),(37498,12124)); svg=render(build(self.draft,'x').box); ElementTree.fromstring(svg); self.assertEqual(svg,render(build(self.draft,'x').box)); self.assertIn('Illustrative',svg)
 def test_pricing(self):
  self.assertEqual(platform_fee(1000),500); self.assertEqual(platform_fee(10005),1001)
  with self.assertRaises(ValueError):platform_fee(1000,700)
 def test_audit_and_vault(self):
  a=AuditLog(); event=a.append('abcdefgh','x',{'safe':1}); self.assertEqual(a.verify('abcdefgh'),(True,None)); a.events['abcdefgh'][0]=replace(event,payload={'safe':2}); self.assertEqual(a.verify('abcdefgh'),(False,1))
  v=FakeVault(); self.assertTrue(v.store('email','a@b.co').startswith('vlt_')); self.assertFalse(hasattr(v,'get'))

if __name__=='__main__':unittest.main()
