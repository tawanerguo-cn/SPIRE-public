import json
import contextlib
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_layer.recording import ExperimentMetadata, RunStore
from workflow_layer.experiment_archive import create_experiment_archive, register_experiment_artifacts


class ExperimentArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'misleading-tWSe2-name.dat'
        self.source.write_text('Experiment\tbias spectroscopy\nStart time\t22.09.2026 13:56:54\nSaved Date\t22.09.2026 14:07:25\nX (m)\t1e-9\n[DATA]\nBias calc (V)\tCurrent (A)\n-1\t-1e-10\n0\t0\n1\t1e-10\n')
        self.info = create_experiment_archive(self.root/'archive', experiment_id='experiment-1',
            sample_id='sample-au111', sample_description='Au(111); physical batch unknown',
            identity_source='operator explicitly confirmed', purpose='offline archive regression')

    def register(self, *files, **kwargs):
        return register_experiment_artifacts(self.info['database_path'], files or [('spectrum',self.source)],
            tip_state_id='tip-usable', purpose='reviewed evidence', **kwargs)

    def manifest(self):
        return json.loads(Path(self.info['manifest_path']).read_text())

    def test_explicit_identity_and_acquisition_time_survive_wrong_folder_and_late_save(self):
        r=self.register()
        m=self.manifest()
        self.assertEqual(m['experiment_metadata']['sample_id'],'sample-au111')
        content=m['artifacts'][0]['metadata']['content']
        self.assertEqual(content['header']['Start time'],'22.09.2026 13:56:54')
        self.assertEqual(content['header']['Saved Date'],'22.09.2026 14:07:25')
        self.assertEqual(content['points'],3)
        self.assertEqual(m['artifact_links'][0]['original_name'],self.source.name)
        self.assertEqual(Path(r['registered'][0]['archive_path']).read_bytes(),self.source.read_bytes())
        self.assertEqual(m['sample_count'],0)

    def test_repeated_registration_reuses_bytes_and_id_and_preserves_parent_links(self):
        first=self.register()['registered'][0]
        alias=self.root/'another-name.dat';shutil.copyfile(self.source,alias)
        second=self.register(('reference',alias),parent_artifact_ids=[first['artifact_id']])['registered'][0]
        self.assertTrue(second['reused_content'])
        self.assertEqual(first['artifact_id'],second['artifact_id'])
        m=self.manifest()
        self.assertEqual(len(m['artifacts']),1)
        self.assertEqual(len(m['artifact_links']),2)
        self.assertEqual(m['artifact_links'][1]['parent_artifact_ids'],[first['artifact_id']])
        self.assertEqual(len(list(Path(self.info['database_path']).parent.joinpath('objects').iterdir())),1)

    def test_existing_experiment_and_missing_identity_do_not_create_new_archive(self):
        with self.assertRaises(FileExistsError):
            create_experiment_archive(self.root/'archive',experiment_id='experiment-1',sample_id='other',
                sample_description='other',identity_source='test',purpose='test')
        with self.assertRaises(ValueError):
            create_experiment_archive(self.root/'archive',experiment_id='experiment-2',sample_id='',
                sample_description='Au',identity_source='test',purpose='test')

    def test_active_or_closed_recorder_cannot_be_reopened_as_archive(self):
        with RunStore.create(self.root/'recorder',signal_catalog={},requested_period_ns=0,
                experiment_metadata=ExperimentMetadata(sample_id='Au')) as recorder:
            for status in ('recording','complete'):
                if status=='complete':recorder.finalize('complete',stopped_utc_ns=1)
                with self.subTest(status=status),self.assertRaisesRegex(ValueError,'recorder'):
                    register_experiment_artifacts(recorder.database_path,[('spectrum',self.source)],
                        tip_state_id='test',purpose='test')

    def test_unknown_parent_and_missing_input_do_not_register(self):
        with self.assertRaises(ValueError):self.register(parent_artifact_ids=['invented'])
        with self.assertRaises(FileNotFoundError):self.register(('spectrum',self.root/'missing'))
        self.assertEqual(self.manifest()['artifacts'],[])

    def test_changed_source_during_copy_preserves_original_and_no_false_artifact(self):
        original=shutil.copyfile
        def changed(source,destination):
            original(source,destination)
            Path(destination).write_bytes(b'changed')
        with patch('workflow_layer.experiment_archive.shutil.copyfile',side_effect=changed):
            with self.assertRaisesRegex(RuntimeError,'source changed'):self.register()
        self.assertEqual(self.manifest()['artifacts'],[])
        self.assertEqual(self.manifest()['status'],'failed')
        self.assertIn('[DATA]',self.source.read_text())
        self.register()  # Explicit registration can recover an offline archive failure.
        self.assertEqual(self.manifest()['status'],'complete')

    def test_corrupted_registered_object_is_not_overwritten_or_silently_reused(self):
        r=self.register()['registered'][0]
        Path(r['archive_path']).write_bytes(b'corrupted')
        with self.assertRaisesRegex(RuntimeError,'corruption'):self.register()
        self.assertEqual(Path(r['archive_path']).read_bytes(),b'corrupted')

    def test_unparsed_data_is_retained_with_an_error_instead_of_invented_metadata(self):
        self.source.write_bytes(b'partial unknown DAT content')
        r=self.register()['registered'][0]
        self.assertEqual(Path(r['archive_path']).read_bytes(),self.source.read_bytes())
        m=self.manifest()['artifacts'][0]['metadata']
        self.assertIn('content_read_error',m)
        self.assertNotIn('content',m)

    def test_cli_append_uses_existing_experiment_without_loading_recorder_settings(self):
        from runtime_layer import recorder_cli as cli
        with patch.object(cli,'load_recording_settings',side_effect=AssertionError('not a recorder operation')),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['archive','--database',self.info['database_path'],
                '--tip-state-id','tip-usable','--purpose','offline CLI test',
                '--artifact','accepted_spectrum='+str(self.source)]),0)
        self.assertEqual(len(self.manifest()['artifacts']),1)


if __name__=='__main__':unittest.main()
