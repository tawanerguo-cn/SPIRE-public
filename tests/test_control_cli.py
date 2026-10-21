import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from tools import nanonis_control as cli
from instrument_layer import FakeNanonisBackend, NanonisDevice, build_default_safety_gate


class ControlCliTests(unittest.TestCase):
    def test_parameters_decode_without_shell_json_and_reject_ambiguity(self):
        r=cli.build_step_request(action='set_z_controller_on', params=['enabled=true','timeout_ms=1000','completion_mode=engaged'],purpose='test')
        self.assertIs(r['arguments']['enabled'],True)
        self.assertEqual(r['arguments']['timeout_ms'],1000)
        self.assertEqual(r['arguments']['completion_mode'],'engaged')
        for params in (['x=1','x=2'],['bad-name=1'],['x='],['x={oops}'],['x=NaN'],['x=Infinity']):
            with self.subTest(params=params),self.assertRaises(ValueError):
                cli.build_step_request(action='status',params=params,purpose='test')
        with self.assertRaises(ValueError):
            cli.build_step_request(action='status',purpose=' ')
        with self.assertRaises(ValueError):
            cli.build_step_request(request_file='unused',action='status',purpose='test')

    def argv(self, result):
        return ['--active-run-db','unused.db','--authorization','unused.json','step',
                '--action','apply_bias_pulse','--param','target_bias_v=1','--param','duration_ms=10',
                '--purpose','offline CLI integration','--result-file',str(result)]

    def test_parameterized_request_uses_existing_audit_and_exactly_one_typed_pulse(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'result.json'
            events=[]
            backend=FakeNanonisBackend(scan_running=False)
            device=NanonisDevice(backend=backend,action_event_sink=events.append)
            def connect(**_kwargs):
                self.assertEqual(json.loads(output.read_text())['status'],'pending')
                return device
            with patch.object(cli,'ExistingRunActionEventSink',return_value=events.append),patch.object(cli,'load_control_authorization',return_value=build_default_safety_gate()),patch.object(cli,'NanonisDevice',side_effect=connect),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(self.argv(output)),0)
            self.assertEqual(sum(n=='Bias_Pulse' for n,_ in backend.command_log),1)
            saved=json.loads(output.read_text())
            self.assertEqual(saved['status'],'completed')
            self.assertEqual(saved['request']['arguments'],{'target_bias_v':1,'duration_ms':10})
            self.assertEqual(saved['result']['tip_quality'],'awaiting_independent_postcheck')
            self.assertTrue(events)

    def test_parameterized_drift_stop_keeps_audit_and_result_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'drift-stop.json'
            events = []
            backend = FakeNanonisBackend(scan_running=False)
            backend.drift_compensation = (1, 0., 0., 1e-12, 0, 0, 0, 3.)
            device = NanonisDevice(backend=backend, action_event_sink=events.append)
            argv = ['--active-run-db', 'unused.db', '--authorization', 'unused.json', 'step',
                    '--action', 'stop_drift_compensation', '--purpose', 'offline merge regression',
                    '--result-file', str(output)]
            with patch.object(cli, 'ExistingRunActionEventSink', return_value=events.append), \
                    patch.object(cli, 'load_control_authorization', return_value=build_default_safety_gate()), \
                    patch.object(cli, 'NanonisDevice', return_value=device), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(argv), 0)
            saved = json.loads(output.read_text())
            self.assertEqual(saved['status'], 'completed')
            self.assertFalse(saved['result']['result']['enabled'])
            self.assertEqual(saved['result']['result']['saturation_limit_percent'], 3.)
            self.assertEqual(sum(name == 'Piezo_DriftCompSet' for name, _ in backend.command_log), 1)
            self.assertTrue(events)

    def test_existing_output_refuses_before_instrument_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'result.json';output.write_text('existing evidence')
            with patch.object(cli,'ExistingRunActionEventSink'),patch.object(cli,'load_control_authorization',return_value=build_default_safety_gate()),patch.object(cli,'NanonisDevice') as connect,self.assertRaises(FileExistsError):
                cli.main(self.argv(output))
            connect.assert_not_called()
            self.assertEqual(output.read_text(),'existing evidence')

    def test_failed_pulse_is_saved_and_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'result.json'
            backend=FakeNanonisBackend(scan_running=False)
            device=NanonisDevice(backend=backend)
            error=OSError('ack lost')
            error.acquired_evidence={'response_received':True,'raw_response_parts':[1,2,3]}
            with patch.object(backend,'Bias_Pulse',side_effect=error) as pulse,patch.object(cli,'ExistingRunActionEventSink'),patch.object(cli,'load_control_authorization',return_value=build_default_safety_gate()),patch.object(cli,'NanonisDevice',return_value=device),self.assertRaisesRegex(OSError,'ack lost'):
                cli.main(self.argv(output))
            self.assertEqual(pulse.call_count,1)
            saved=json.loads(output.read_text())
            self.assertEqual(saved['status'],'failed')
            self.assertEqual(saved['error_type'],'OSError')
            self.assertEqual(saved['acquired_evidence']['raw_response_parts'],[1,2,3])
            self.assertIn('never automatically retry',saved['retry_policy'])
            self.assertNotIn('ZCtrl_Withdraw',[n for n,_ in backend.command_log])

    def test_invalid_cli_input_is_rejected_before_connecting_or_auditing(self):
        with patch.object(cli,'NanonisDevice') as connect,patch.object(cli,'ExistingRunActionEventSink') as sink,contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            cli.main(['--active-run-db','unused','--authorization','unused','step','--action','status','--param','x=1','--param','x=2','--purpose','test'])
        connect.assert_not_called();sink.assert_not_called()

    def test_aborted_noise_has_nonzero_exit_and_retains_waveform(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'noise.json'
            device=NanonisDevice(backend=FakeNanonisBackend(scan_running=False))
            result={'result':{'status':'aborted','error':'manual state changed',
                              'rows':[[1,2,3]],'spectra':[{'data':'retained'}]}}
            argv=['--active-run-db','unused.db','--authorization','unused.json','step',
                  '--action','observe_noise','--purpose','offline regression','--result-file',str(output)]
            with patch.object(cli,'ExistingRunActionEventSink'),patch.object(cli,'load_control_authorization',return_value=build_default_safety_gate()),patch.object(cli,'NanonisDevice',return_value=device),patch.object(cli,'execute_control_step',return_value=result) as execute,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(argv),2)
            execute.assert_called_once()
            saved=json.loads(output.read_text())
            self.assertEqual(saved['status'],'needs_review')
            self.assertEqual(saved['workflow_status'],'aborted')
            self.assertEqual(saved['result'],result)

    def test_composite_quality_and_state_stops_are_not_shell_success(self):
        for action,value in [
            ('pulse_burst',{'status':'state_fault'}),
            ('pulse_burst',{'status':'burst_time_budget_reached'}),
            ('pulse_burst',{'status':'no_signal_candidate_within_budget'}),
            ('pulse_burst',{'status':'unresolved_quality_at_burst_end'}),
            ('pulse_and_assess',{'observation':{'status':'completed'},'decision':'reassess_within_explicit_attempt_budget'}),
            ('pulse_and_assess',{'observation':{'status':'aborted'},'decision':'stop_and_review'}),
        ]:
            with self.subTest(action=action,value=value):
                self.assertEqual(cli.step_outcome(action,{'result':value})[1],2)

    def test_pulse_stdout_is_compact_but_result_keeps_the_full_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'pulse.json'
            device=NanonisDevice(backend=FakeNanonisBackend(scan_running=False))
            value={'observation':{'status':'completed','rows':[[1,2,3]],'row_phases':['during']},
                   'decision':'insufficient_acceptance_evidence','response':{'peak_abs_current_a':1e-8}}
            argv=['--active-run-db','unused.db','--authorization','unused.json','step',
                  '--action','pulse_and_assess','--observer-port','6504',
                  '--purpose','offline regression','--result-file',str(output)]
            stdout=io.StringIO()
            with patch.object(cli,'ExistingRunActionEventSink'), \
                    patch.object(cli,'load_control_authorization',return_value=build_default_safety_gate()), \
                    patch.object(cli,'NanonisDevice',return_value=device), \
                    patch.object(cli,'execute_control_step',return_value={'result':value}) as execute, \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(cli.main(argv),2)
            execute.assert_called_once()
            shown=json.loads(stdout.getvalue())
            self.assertEqual(shown['response'],value['response'])
            self.assertEqual(shown['result_file'],str(output))
            self.assertNotIn('observation',shown)
            self.assertEqual(json.loads(output.read_text())['result']['result'],value)

    def test_noise_final_read_failure_is_not_retried_by_outer_step(self):
        device=NanonisDevice(backend=FakeNanonisBackend(scan_running=False))
        observation={'status':'aborted','error':'current ceiling','rows':[[1,2,3]],
                     'final_state_error':'final readback unavailable'}
        original=device.get_tip_state_snapshot
        with patch.object(device,'get_tip_state_snapshot',side_effect=[original(),OSError('read failed')]) as readback,patch('workflow_layer.control_step.observe_noise',return_value=observation):
            result=cli.execute_control_step(device,{'action':'observe_noise','arguments':{},'purpose':'offline regression'})
        self.assertEqual(readback.call_count,1)
        self.assertEqual(result['result'],observation)
        self.assertEqual(result['after'],{'unavailable':'final readback unavailable'})

    def test_outer_postcheck_failure_preserves_the_completed_step(self):
        device = NanonisDevice(backend=FakeNanonisBackend(scan_running=False))
        before = device.get_tip_state_snapshot()
        with patch.object(device, 'get_tip_state_snapshot', side_effect=[before, OSError('final read failed')]), \
                patch.object(device, 'set_bias_v', return_value=.1) as action:
            with self.assertRaisesRegex(OSError, 'final read failed') as caught:
                cli.execute_control_step(device, {'action': 'set_bias_v', 'arguments': {'value': .1}, 'purpose': 'regression'})
        action.assert_called_once()
        self.assertEqual(caught.exception.acquired_evidence['control_step']['result'], .1)
        self.assertNotIn('ZCtrl_Withdraw', [n for n, _ in device.backend.command_log])

    def test_completed_workflows_keep_success_without_claiming_tip_acceptance(self):
        for action,value in [
            ('observe_noise',{'status':'completed','authorizes_next_action':False}),
            ('pulse_burst',{'status':'burst_completed'}),
            ('pulse_burst',{'status':'signal_candidate_for_review'}),
            ('pulse_and_assess',{'observation':{'status':'completed'},'decision':'candidate_for_next_stage'}),
        ]:
            with self.subTest(action=action,value=value):
                self.assertEqual(cli.step_outcome(action,{'result':value})[1],0)
