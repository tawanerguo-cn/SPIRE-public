"""Material reference data retain empirical provenance independently of prose."""
from pathlib import Path
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class TipConditioningKnowledgeTests(unittest.TestCase):
    def test_each_registered_material_has_its_own_reference_entry(self):
        from knowledge_base import load_tip_quality_rubric
        library = load_tip_quality_rubric()
        root = Path(__file__).resolve().parents[1] / 'src/knowledge_base/tip_conditioning'
        surfaces = []
        references = [*root.glob('*_reference.toml'),
                      *root.parent.joinpath('tip_quality_references').glob('*.toml')]
        for path in references:
            reference = tomllib.loads(path.read_text(encoding='utf-8'))
            surfaces.append(reference['reference']['surface'])
            if reference['reference']['surface'] == 'Ag(100)':
                self.assertTrue(reference['reference']['empirical_local_reference_available'])
                self.assertFalse(reference['local_reference']['numeric_grade_assigned'])
        self.assertCountEqual(surfaces, library['materials'])
        self.assertEqual(len(surfaces), len(set(surfaces)))

    def test_au_reference_retains_empirical_source_and_acceptance_limits(self):
        path = Path(__file__).resolve().parents[1] / 'src/knowledge_base/tip_conditioning/au111_reference.toml'
        reference = tomllib.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(reference['reference']['surface'], 'Au(111)')
        self.assertTrue(reference['reference']['empirical_local_reference_available'])
        self.assertEqual(reference['local_reference']['verdict'], 'usable_tip_not_optimal')
        self.assertFalse(reference['local_reference']['quantitative_reproduction_available'])
        self.assertFalse(reference['local_reference']['recipe_inference_permitted'])
        self.assertFalse(reference['local_reference']['authorizes_live_action'])
        self.assertIn('herringbone', reference)
        self.assertNotIn('workflow', reference)
