"""Synthetic regressions for G22 evidence validation, never GPU acceptance."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_combined_measurement as measurement


class CombinedMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'format_version': 1, 'profile_id': 'm4-combined-gpu-v1',
                        'workload': 'combined-gpu-v1',
                        'workload_sha256': measurement.base.sha256_file(
                            Path(__file__).resolve().parents[2] / 'workloads/combined-gpu-v1.json'),
                        'core': {'repository': 'https://github.com/eyleme-dev/eyleme',
                                 'revision': 'a' * 40, 'tree': 'b' * 40,
                                 'native_worker_sha256': 'c' * 64, 'cli_sha256': 'd' * 64,
                                 'browser_module_sha256': 'e' * 64, 'toolchain': '1.99.0'},
                        'trials': 3, 'frames': 2, 'native_seconds': 120,
                        'rss_interval_ms': 200, 'minimum_overlap_samples': 5}
        self.browsers, self.markers = {}, []
        for trial, times in [(1, (1010, 1090)), (2, (1140, 1180)), (3, (1220, 1270))]:
            self.browsers[trial] = {'format_version': 1, 'status': 'passed', 'trial': trial,
                                    'frame_count': 2, 'fallback_adapter': False,
                                    'adapter_info': {'vendor': 'apple'},
                                    'browser_user_agent': 'Fixture Chrome/1',
                                    'samples': [{'index': index, 'start_unix_ms': tick,
                                                 'end_unix_ms': tick + 4,
                                                 'raf_interval_ms': None if index == 0 else times[1] - times[0],
                                                 'queue_completion_ms': 1.5, 'js_heap_bytes': None}
                                                for index, tick in enumerate(times)]}
            self.markers.extend([{'trial': trial, 'phase': 'opened', 'unix_ms': times[0] - 5,
                                  'tab_id': str(trial)},
                                 {'trial': trial, 'phase': 'closed', 'unix_ms': times[1] + 5,
                                  'tab_id': str(trial)}])
        self.native = {'core': copy.deepcopy(self.profile['core']), 'pid': 42, 'exit_code': 0,
                       'served_browser_module_sha256': self.profile['core']['browser_module_sha256'],
                       'measurement_tool_sha256': measurement.base.sha256_file(Path(measurement.__file__)),
                       'environment': {'os': 'Darwin', 'architecture': 'arm64', 'chip': 'Apple M4 Pro'},
                       'rss_samples': [{'unix_ms': t, 'rss_bytes': 10000, 'status': 'passed'}
                                       for t in [1012, 1142, 1222]],
                       'events': [{'event': 'started', 'unix_ms': 990, 'pid': 42, 'elements': 4096,
                                   'declared_metal_buffer_bytes': 49152}]
                       + [{'event': 'sample', 'index': i, 'start_unix_ms': 1000 + i * 10,
                           'end_unix_ms': 1004 + i * 10, 'duration_ns': 4000000, 'status': 'passed'}
                          for i in range(35)]
                       + [{'event': 'finished', 'unix_ms': 1350, 'samples': 35}]}

    def result(self):
        return measurement.final_result(self.profile, self.native, self.browsers, self.markers)

    def test_org_rename_preserves_profile_identity_and_rejects_foreign_owner(self):
        for repository in ('eylema-dev/eylema', 'eyleme-dev/eylema', 'eyleme-dev/eyleme'):
            changed = copy.deepcopy(self.profile)
            changed['core']['repository'] = f'https://github.com/{repository}'
            measurement.validate_profile(changed)
        for url in ('https://github.com/other/eylema', 'https://github.com/eyleme-dev-evil/eylema',
                    'https://github.com/eyleme-dev/other'):
            changed = copy.deepcopy(self.profile)
            changed['core']['repository'] = url
            with self.assertRaises(ValueError):
                measurement.validate_profile(changed)

    def test_three_trials_require_overlap_and_same_process_after_each_close(self):
        result = self.result()
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(all(len(t['native_overlapping_samples']) >= 5 for t in result['trials']))
        self.assertTrue(all(len(t['native_after_close_samples']) == 5 for t in result['trials']))
        self.assertTrue(all(t['native_pid'] == 42 for t in result['trials']))

    def test_profile_rejects_mutable_revision_unbounded_inputs_and_bool(self):
        for key, value in [('frames', 241), ('frames', True), ('trials', 2),
                           ('native_seconds', 121), ('rss_interval_ms', 0)]:
            changed = copy.deepcopy(self.profile)
            changed[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                measurement.validate_profile(changed)
        changed = copy.deepcopy(self.profile)
        changed['workload_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'WORKLOAD_BINDING'):
            measurement.validate_profile(changed)
        self.profile['core']['revision'] = 'main'
        with self.assertRaisesRegex(ValueError, 'CORE_REVISION'):
            measurement.validate_profile(self.profile)

    def test_failed_browser_keeps_partial_samples_and_failed_overall_status(self):
        self.browsers[2]['status'] = 'failed'
        self.browsers[2]['samples'] = self.browsers[2]['samples'][:1]
        result = self.result()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['trials'][1]['reason'], 'BROWSER_FAILED_OR_MISSING')
        self.assertEqual(len(result['trials'][1]['browser_raw']['samples']), 1)
        self.assertEqual(result['native_events_raw'], self.native['events'])

    def test_missing_trial_is_failed_and_unmeasured_values_remain_null(self):
        del self.browsers[3]
        result = self.result()
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(all(v['value'] is None for v in result['unmeasured'].values()))
        heap = result['trials'][0]['distributions']['browser_js_heap_estimate_bytes']
        self.assertEqual(heap['status'], 'unmeasured')
        self.assertIsNone(heap['p50'])
        self.assertEqual(heap['count'], 0)

    def test_failure_or_clock_reversal_in_native_is_preserved(self):
        self.native['events'][2]['status'] = 'failed'
        result = self.result()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['native_events_raw'][2]['status'], 'failed')
        self.native['events'][2]['status'] = 'passed'
        self.native['events'][2]['end_unix_ms'] = 900
        self.assertEqual(self.result()['trials'][0]['reason'], 'NATIVE_CLOCK_INVALID')

    def test_no_overlap_and_no_work_after_close_cannot_pass(self):
        for sample in self.browsers[1]['samples']:
            sample['start_unix_ms'] += 10000
            sample['end_unix_ms'] += 10000
        self.markers[1]['unix_ms'] += 10000
        self.assertEqual(self.result()['trials'][0]['reason'], 'NO_COMBINED_METAL_LOAD')
        self.setUp()
        self.markers[1]['unix_ms'] = 1400
        self.assertEqual(self.result()['trials'][0]['reason'], 'NATIVE_WORK_NOT_OBSERVED_AFTER_TAB_CLOSE')

    def test_tab_identity_clock_and_missing_marker_cannot_pass(self):
        self.markers[1]['tab_id'] = 'different'
        self.assertEqual(self.result()['trials'][0]['reason'], 'TAB_IDENTITY_MISMATCH')
        self.setUp()
        self.markers[1]['unix_ms'] = 1090
        self.assertEqual(self.result()['trials'][0]['reason'], 'TAB_CLOSE_CLOCK_INVALID')
        self.setUp()
        del self.markers[1]
        result = self.result()
        self.assertEqual(result['trials'][0]['reason'], 'TAB_OBSERVATION_MISSING')
        self.assertEqual(len(result['trials'][0]['tab_observations']), 1)

    def test_tab_open_after_first_or_final_frame_cannot_pass(self):
        for timestamp in [1011, 1095]:
            self.markers[0]['unix_ms'] = timestamp
            with self.subTest(timestamp=timestamp):
                self.assertEqual(self.result()['trials'][0]['reason'], 'TAB_CLOSE_CLOCK_INVALID')
        self.assertEqual(self.result()['trials'][0]['tab_observations'][0]['unix_ms'], 1095)

    def test_missing_or_failed_rss_cannot_be_hidden(self):
        self.native['rss_samples'] = []
        self.assertEqual(self.result()['trials'][0]['reason'], 'RSS_NOT_MEASURED')
        self.setUp()
        self.native['rss_samples'][0]['status'] = 'failed'
        result = self.result()
        self.assertEqual(result['trials'][0]['reason'], 'NATIVE_RSS_CAPTURE_FAILED')
        self.assertEqual(result['native_rss_raw'][0]['status'], 'failed')

    def test_source_artifact_and_tool_binding_mismatch_cannot_pass(self):
        for key, reason in [('core', 'NATIVE_CORE_BINDING'),
                            ('served_browser_module_sha256', 'SERVED_BROWSER_MODULE_MISMATCH'),
                            ('measurement_tool_sha256', 'MEASUREMENT_TOOL_MISMATCH')]:
            self.setUp()
            self.native[key] = {} if key == 'core' else '0' * 64
            with self.subTest(key=key):
                self.assertEqual(self.result()['trials'][0]['reason'], reason)

    def test_fallback_unobserved_adapter_and_non_m4_host_are_failed(self):
        self.browsers[1]['fallback_adapter'] = True
        self.assertEqual(self.result()['trials'][0]['reason'], 'FALLBACK_GPU_NOT_ACCEPTED')
        self.setUp()
        self.browsers[1]['adapter_info'] = {}
        self.assertEqual(self.result()['trials'][0]['reason'], 'M4_ADAPTER_NOT_OBSERVED')
        self.setUp()
        self.native['environment']['chip'] = 'Unknown'
        self.assertEqual(self.result()['trials'][0]['reason'], 'M4_MAC_NOT_OBSERVED')

    def test_remote_urls_and_queries_are_rejected_before_network(self):
        with patch.object(measurement.urllib.request, 'urlopen') as network:
            for url in ['https://example.com/console', 'http://localhost:8000/console',
                        'http://127.0.0.1:8000/console?x=1', 'http://user@127.0.0.1:8000/console']:
                with self.subTest(url=url), self.assertRaisesRegex(ValueError, 'LOCAL_CONSOLE_URL_REQUIRED'):
                    measurement.console_asset(url)
            network.assert_not_called()

    def test_malformed_browser_and_event_rows_fail_without_losing_raw_input(self):
        self.browsers[1] = ['malformed']
        result = self.result()
        self.assertEqual(result['trials'][0]['reason'], 'BROWSER_INVALID_RECORD')
        self.assertEqual(result['trials'][0]['browser_raw'], ['malformed'])
        self.setUp()
        self.native['events'][2] = 'invalid row'
        result = self.result()
        self.assertEqual(result['trials'][0]['reason'], 'NATIVE_INVALID_EVENTS')
        self.assertEqual(result['native_events_raw'][2], 'invalid row')

    def test_negative_and_nonfinite_distributions_are_rejected(self):
        for value in [-1, float('nan'), float('inf'), True]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'INVALID_DISTRIBUTION_SAMPLE'):
                measurement.distribution([value])
        self.assertEqual(measurement.distribution([1, 2, 3])['p95'], 3)


if __name__ == '__main__':
    unittest.main()
