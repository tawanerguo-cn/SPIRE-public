from __future__ import annotations

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from knowledge_base.tip_quality_reference import PrivateReferenceSourcesUnavailable
from tools.verify_tip_reference import main as verify_tip_reference_main
from workflow_layer.tip_reference_verification import verify_reviewed_tip_reference


class PrivateTipReferenceTests(unittest.TestCase):
    def test_verify_rejects_withheld_sources_before_reading_scan_or_spectrum(self):
        with patch(
            "workflow_layer.tip_reference_verification.analyze_tip_evidence"
        ) as analyze:
            with self.assertRaises(PrivateReferenceSourcesUnavailable):
                verify_reviewed_tip_reference()
        analyze.assert_not_called()

    def test_verify_cli_reports_withheld_sources_and_nonzero_status(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            status = verify_tip_reference_main(["verify"])
        self.assertEqual(status, 2)
        self.assertIn("not included in this release", stderr.getvalue())
        self.assertIn("Use the analyze command", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
