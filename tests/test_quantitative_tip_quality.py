from pathlib import Path
from tempfile import TemporaryDirectory
import json
import sys
import unittest

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis_layer.tip_quality import score_quantitative_tip_metrics
from analysis_layer.tip_reference_verification import measure_retrace_pair, measure_poke_shape
from runtime_layer.spm_tools import SPMTools
from data_layer import read_nanonis_sxm
from test_tip_reference_verification import _write_sxm


class QuantitativeTipQualityTests(unittest.TestCase):
    def test_synthetic_partial_flat_background_needs_no_poke_or_signal_and_is_not_failed_for_low_correlation(self):
        rows, columns, acquired = 48, 64, 42
        y, x = np.indices((rows, columns))
        plane = x * 0.02e-9 + y * 0.01e-9
        forward = plane + np.where(x % 2, 4e-12, -4e-12)
        backward = plane + np.where(y % 2, 3e-12, -3e-12)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan = root / "synthetic-partial-flat.sxm"
            _write_sxm(scan, forward=forward, backward=np.fliplr(backward), scan_direction="down",
                       range_x_m=columns * 1e-9, range_y_m=rows * 1e-9,
                       acquired_rows=acquired)
            tools = SPMTools(read_roots=[root], output_root=root / "out")
            result = tools.quantify_tip_quality(material='Ag(100)',
                scan_path=str(scan), background_roi=[0,acquired,0,columns]).data
            brief = result['brief']
            self.assertAlmostEqual(brief['background']['retrace_rms_pm'], 5.0, delta=0.02)
            self.assertLess(brief['background']['retrace_correlation'], .1)
            self.assertEqual(brief['background']['paired_pixels'], acquired * columns)
            self.assertEqual(brief['background']['paired_fraction'], 1.0)
            self.assertIsNone(brief['current']); self.assertIsNone(brief['poke'])
            self.assertEqual(result['issues'], [])
            self.assertIsNone(result['quantitative']['score'])
            self.assertFalse(brief['automatic_acceptance'])
            whole = tools.quantify_tip_quality(material='Ag(100)',
                scan_path=str(scan)).data
            self.assertAlmostEqual(whole['brief']['background']['paired_fraction'], acquired / rows)
            self.assertIn('all acquired', whole['brief']['background']['selection'])

    def test_negative_poke_measurement_distinguishes_noise_scale_from_residual_depression(self):
        y, x = np.indices((81,81))
        peak = np.exp(-((x-40)**2+(y-40)**2)/50)*1e-9
        rng = np.random.default_rng(7)
        noisy = peak + rng.normal(0, 3e-12, peak.shape)
        depressed = noisy - np.exp(-((x-55)**2+(y-40)**2)/18)*.2e-9
        good = measure_poke_shape(noisy, noisy, pixel_x_m=1e-10, pixel_y_m=1e-10)
        bad = measure_poke_shape(depressed, depressed, pixel_x_m=1e-10, pixel_y_m=1e-10)
        self.assertLess(good['minimum_relative_height_m'], 0)
        self.assertGreater(good['minimum_relative_height_m'], -15e-12)
        self.assertLess(bad['minimum_relative_height_m'], -150e-12)
        self.assertAlmostEqual(good['border_residual_rms_m'], bad['border_residual_rms_m'], delta=1e-13)
        self.assertEqual(good['half_height_components'], bad['half_height_components'])
        self.assertAlmostEqual(good['equivalent_fwhm_radius_m'],
                               np.sqrt(good['fwhm_x_m']*good['fwhm_y_m'])/2)

    def test_synthetic_poke_fwhm_matches_anisotropic_gaussian_model(self):
        rows = columns = 81
        sigma_x_px, sigma_y_px = 2.2, 1.7
        amplitude_m = 0.8e-9
        y, x = np.indices((rows, columns))
        height = amplitude_m * np.exp(-0.5 * (((x - 40) / sigma_x_px) ** 2
                                               + ((y - 40) / sigma_y_px) ** 2))
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic-gaussian-poke.sxm"
            _write_sxm(path, forward=height, backward=height, scan_direction="down",
                       range_x_m=columns * 0.8e-9, range_y_m=rows * 0.8e-9)
            scan = read_nanonis_sxm(path)
            shape = measure_poke_shape(scan.oriented_channel('Z', 'forward'),
                scan.oriented_channel('Z', 'backward'),
                pixel_x_m=scan.range_x_m/scan.columns, pixel_y_m=scan.range_y_m/scan.rows)
        pixel_m = 0.8e-9
        fwhm_factor = 2 * np.sqrt(2 * np.log(2))
        self.assertEqual(shape['half_height_components'], 1)
        self.assertAlmostEqual(shape['height_m'], amplitude_m, delta=1e-14)
        self.assertAlmostEqual(shape['fwhm_x_m'], fwhm_factor * sigma_x_px * pixel_m,
                               delta=0.05 * pixel_m)
        self.assertAlmostEqual(shape['fwhm_y_m'], fwhm_factor * sigma_y_px * pixel_m,
                               delta=0.05 * pixel_m)

    def test_synthetic_current_receipt_rms_matches_sinusoidal_noise(self):
        sample_count, interval_s = 100, 0.02
        setpoint_a, amplitude_a, frequency_hz = 100e-12, 10e-12, 5.0
        times = np.arange(sample_count) * interval_s
        current = setpoint_a + amplitude_a * np.sin(2 * np.pi * frequency_hz * times)
        z = 1e-9 + 2e-12 * np.cos(2 * np.pi * frequency_hz * times)
        rows = [[i, 0, int(t * 1e9), 1_700_000_000_000_000_000 + int(t * 1e9),
                 float(current[i]), 0.0, 0.0, float(z[i])] for i, t in enumerate(times)]
        context = {"minimum_duration_s": 1.9, "maximum_sample_gap_s": 0.03,
                   "expected_current_a": setpoint_a}
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan = root / "synthetic-scan.sxm"
            _write_sxm(scan)
            signal = root / "synthetic-signal.json"
            signal.write_text(json.dumps({"rows": rows, "context": context}), encoding="utf-8")
            result = SPMTools(read_roots=[root], output_root=root / "out").quantify_tip_quality(
                material="Ag(111)", scan_path=str(scan), signal_path=str(signal),
                conditions="Deterministic sinusoidal current receipt").data
            report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
        expected_rms_a = amplitude_a / np.sqrt(2)
        self.assertFalse(report["current"]["quality_flags"])
        self.assertAlmostEqual(report["current"]["metrics"]["current_rms_a"], expected_rms_a, delta=1e-15)
        self.assertAlmostEqual(report["brief"]["current"]["rms_pa"], expected_rms_a * 1e12, delta=1e-3)
        self.assertAlmostEqual(report["brief"]["current"]["rms_fraction"], expected_rms_a / setpoint_a, places=10)
        self.assertIsNone(report["quantitative"]["score"])
        self.assertFalse(report["authorizes_live_action"])

    def test_retrace_noise_and_gain_errors_are_not_hidden_by_correlation(self):
        y,x = np.indices((30,40))
        image = np.sin(x/3)*1e-9 + np.cos(y/4)*1e-9
        good = measure_retrace_pair(image, image)
        bad = measure_retrace_pair(image, 2*image)
        self.assertEqual(good['retrace_rms_m'],0)
        self.assertGreater(bad['retrace_rms_m'],1e-10)
        flat = measure_retrace_pair(np.zeros((10,10)),np.zeros((10,10)))
        self.assertIsNone(flat['retrace_correlation'])
        partial = image.copy(); partial[:5] = np.nan
        self.assertAlmostEqual(measure_retrace_pair(image,partial)['paired_fraction'],25/30)

    def test_poke_roundness_and_split_lobes_use_physical_pixels(self):
        y,x=np.indices((61,61))
        round_feature=np.exp(-((x-30)**2+(y-30)**2)/32)*1e-9
        round_result=measure_poke_shape(round_feature,round_feature,pixel_x_m=1e-10,pixel_y_m=1e-10)
        self.assertAlmostEqual(round_result['principal_aspect_ratio'],1)
        self.assertEqual(round_result['secondary_lobe_fraction'],0)
        stretched=measure_poke_shape(round_feature,round_feature,pixel_x_m=2e-10,pixel_y_m=1e-10)
        self.assertAlmostEqual(stretched['principal_aspect_ratio'],2)
        split=(np.exp(-((x-20)**2+(y-30)**2)/18)+np.exp(-((x-40)**2+(y-30)**2)/18))*1e-9
        split_result=measure_poke_shape(split,split,pixel_x_m=1e-10,pixel_y_m=1e-10)
        self.assertEqual(split_result['half_height_components'],2)
        self.assertGreater(split_result['secondary_lobe_fraction'],0.45)
        broken=round_feature.copy();broken[0]=np.nan
        with self.assertRaisesRegex(ValueError,'fully acquired'):
            measure_poke_shape(broken,broken,pixel_x_m=1e-10,pixel_y_m=1e-10)

    def test_anchors_are_directional_and_missing_values_stay_unknown(self):
        groups={'noise':{'rms':0.1},'shape':{'correlation':0.9}}
        anchors={'noise':{'rms':{'good':0,'bad':0.2}},'shape':{'correlation':{'good':1,'bad':0}}}
        scored=score_quantitative_tip_metrics(groups,anchors)
        self.assertAlmostEqual(scored['score'],50)
        self.assertFalse(scored['automatic_acceptance'])
        self.assertIsNone(score_quantitative_tip_metrics(groups,{})['score'])
        groups['shape']['correlation']=None
        self.assertIsNone(score_quantitative_tip_metrics(groups,anchors)['score'])
        anchors['noise']['rms']['good']=0.2
        with self.assertRaises(ValueError):
            score_quantitative_tip_metrics(groups,anchors)

    def test_single_agent_call_persists_report_without_hardware(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); scan=root/'scan.sxm';_write_sxm(scan)
            rows=[]
            for i in range(101):
                rows.append([i,0,i*20000000,i*20000000,1e-10+1e-12*np.sin(i),0,0,1e-9+1e-12*np.cos(i)])
            signal=root/'signal.json'
            signal.write_text(json.dumps({'rows':rows,'context':{'minimum_duration_s':1.9,
                'maximum_sample_gap_s':0.03,'expected_current_a':1e-10}}))
            tools=SPMTools(read_roots=[root],output_root=root/'out')
            args=dict(material='Ag(100)',scan_path=str(scan),signal_path=str(signal),
                background_roi=[0,8,0,10],poke_roi=[7,24,11,30],conditions='synthetic fixture',poke_depth_m=1e-9)
            result=tools.quantify_tip_quality(**args).data
            self.assertEqual(result['status'],'metrics_only')
            self.assertIsNone(result['quantitative']['score'])
            report=json.loads(Path(result['report_path']).read_text())
            self.assertLess(report['poke']['principal_aspect_ratio'],1.3)
            self.assertEqual(report['poke_depth_m_supplied'],1e-9)
            self.assertEqual(report['material'],'Ag(100)')
            self.assertFalse(report['authorizes_live_action'])
            receipt=root/'receipt.json'
            receipt.write_text(json.dumps({'result':{'result':{'rows':rows,
                'analysis':{'supplied_context':{'minimum_duration_s':1.9,
                'maximum_sample_gap_s':0.03,'expected_current_a':1e-10}}}}}))
            again=tools.quantify_tip_quality(**(args|{'signal_path':str(receipt)})).data
            self.assertEqual(result['quantitative'],again['quantitative'])
            calibration={'id':'fixture','source':'synthetic fixture only','material':'Ag(111)',
                'purpose':'basic_conditioning','anchors':{}}
            anchor_path=root/'anchors.json';anchor_path.write_text(json.dumps(calibration))
            with self.assertRaisesRegex(ValueError,'match material'):
                tools.quantify_tip_quality(**args,calibration_path=str(anchor_path))
            with self.assertRaisesRegex(ValueError,'outside'):
                tools.quantify_tip_quality(**(args|{'signal_path':str(root.parent/'outside.json')}))

if __name__ == '__main__':
    unittest.main()
