from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from analysis_layer.tip_quality import aggregate_tip_quality_scores
from workflow_layer.tip_reference_verification import rate_tip_quality_card


class TipQualityRatingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        evidence = self.root / 'operator-label.json'
        evidence.write_text('{"kind":"test_review_evidence"}', encoding='utf-8')
        self.card = {
            'material':'Au(111)', 'purpose':'imaging_and_sts',
            'conditions':'Test fixture at explicitly compared conditions; not hardware evidence.',
            'comparison_conditions_verified':True,
            'historical_operator_verdict':'fixture_only',
            'evidence':[{'id':'label','path':evidence.name,
                         'sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}],
            'axes':{name:{'score':score,'reason':'Explicit fixture grade.',
                          'evidence_ids':['label'],'source_kind':'operator_review',
                          'reviewer':'fixture reviewer'}
                    for name,score in [('signal_stability',1),('imaging_usability',3),
                                       ('spectroscopy_usability',2)]},
        }

    def test_strong_image_does_not_compensate_for_weaker_signal(self):
        result = rate_tip_quality_card(self.card, evidence_root=self.root)
        self.assertEqual(result['score'],1)
        self.assertEqual(result['grade'],'usable_not_optimal')
        self.assertFalse(result['authorizes_live_action'])
        self.assertFalse(result['automatic_acceptance'])
        self.assertFalse(result['reviewer_identity_authenticated'])

    def test_missing_required_axis_preserves_unknown_and_historical_verdict(self):
        self.card['axes']['signal_stability']['score'] = None
        result = rate_tip_quality_card(self.card, evidence_root=self.root)
        self.assertIsNone(result['score'])
        self.assertEqual(result['missing_axes'],['signal_stability'])
        self.assertEqual(result['historical_operator_verdict'],'fixture_only')

    def test_unused_axis_is_not_a_required_measurement(self):
        self.card['purpose'] = 'imaging'
        self.card['axes']['spectroscopy_usability']['score'] = None
        result = rate_tip_quality_card(self.card, evidence_root=self.root)
        self.assertEqual(result['score'],1)
        self.assertEqual(result['missing_axes'],[])

    def test_ag100_can_be_reviewed_without_inheriting_ag111_reference_grade(self):
        self.card['material'] = 'Ag(100)'
        self.card['historical_operator_verdict'] = None
        for axis in self.card['axes'].values():
            axis['score'] = None
        result = rate_tip_quality_card(self.card, evidence_root=self.root)
        self.assertEqual(result['material'], 'Ag(100)')
        self.assertIsNone(result['score'])
        self.assertIsNone(result['historical_operator_verdict'])

        self.card['purpose'] = 'imaging'
        for name in ('signal_stability', 'imaging_usability'):
            self.card['axes'][name]['score'] = 1
            self.card['axes'][name]['source_kind'] = 'analyst_assessment'
        result = rate_tip_quality_card(self.card, evidence_root=self.root)
        self.assertEqual(result['score'], 1)
        self.assertEqual(result['status'], 'provisional_analyst_review')
        self.assertIsNone(result['axes']['spectroscopy_usability']['score'])
        self.assertFalse(result['automatic_acceptance'])

    def test_conditions_or_analyst_review_cannot_become_operator_acceptance(self):
        for value in (None,False):
            with self.subTest(comparison_conditions_verified=value):
                card=deepcopy(self.card);card['comparison_conditions_verified']=value
                self.assertIsNone(rate_tip_quality_card(card,evidence_root=self.root)['score'])
        self.card['axes']['imaging_usability']['source_kind']='analyst_assessment'
        result=rate_tip_quality_card(self.card,evidence_root=self.root)
        self.assertEqual(result['status'],'provisional_analyst_review')
        self.assertFalse(result['automatic_acceptance'])

    def test_changed_or_outside_evidence_is_rejected(self):
        self.card['evidence'][0]['sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            rate_tip_quality_card(self.card,evidence_root=self.root)
        self.card['evidence'][0]['path']='../outside.json'
        with self.assertRaisesRegex(ValueError,'evidence_root'):
            rate_tip_quality_card(self.card,evidence_root=self.root)

    def test_unbounded_scores_and_unregistered_materials_are_rejected(self):
        for value in (-1,4,True,1.5,'3'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                aggregate_tip_quality_scores({'signal':value},required_axes=['signal'])
        self.card['material']='AFM'
        with self.assertRaisesRegex(ValueError,'registered material'):
            rate_tip_quality_card(self.card,evidence_root=self.root)
        self.card['material']='Pb(100)'
        self.card['axes']['imaging_usability']['evidence_ids']=[]
        with self.assertRaisesRegex(ValueError,'verified evidence'):
            rate_tip_quality_card(self.card,evidence_root=self.root)

    def test_cli_marks_partial_review_and_uses_registered_offline_entry(self):
        self.card['axes']['signal_stability']['score']=None
        path=self.root/'card.json';path.write_text(json.dumps(self.card),encoding='utf-8')
        tool=Path(__file__).resolve().parents[1]/'tools/verify_tip_reference.py'
        result=subprocess.run([sys.executable,'-B',str(tool),'rate','--card',str(path),
                               '--evidence-root',str(self.root)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertIsNone(json.loads(result.stdout)['score'])


if __name__ == '__main__':
    unittest.main()
