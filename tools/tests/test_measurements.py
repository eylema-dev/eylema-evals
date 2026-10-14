"""Failure-preserving measurement profile and result-format tests."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_measurement as measurement

ROOT = Path(__file__).resolve().parents[2]


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((ROOT / 'profiles/m4-native-task-v1.json').read_text())
        self.workload = json.loads((ROOT / 'workloads/native-task-demo-v1.json').read_text())

    def test_org_rename_preserves_profile_identity_and_rejects_foreign_owner(self):
        for repository in ('eylema-dev/eylema', 'eyleme-dev/eylema', 'eyleme-dev/eyleme'):
            changed = copy.deepcopy(self.profile)
            changed['core']['repository'] = f'https://github.com/{repository}'
            measurement.validate_profile(changed, self.workload)
        for url in ('https://github.com/other/eylema', 'https://github.com/eyleme-dev-evil/eylema',
                    'https://github.com/eyleme-dev/other'):
            changed = copy.deepcopy(self.profile)
            changed['core']['repository'] = url
            with self.assertRaises(ValueError):
                measurement.validate_profile(changed, self.workload)

    def test_reviewed_profile_binds_exact_core_and_fixed_commands(self):
        measurement.validate_profile(self.profile, self.workload)
        changed = copy.deepcopy(self.profile)
        changed['core']['revision'] = 'main'
        with self.assertRaisesRegex(ValueError, 'full immutable commit'):
            measurement.validate_profile(changed, self.workload)
        changed = copy.deepcopy(self.profile)
        changed['commands']['measure'] = ['sh', '-c', 'echo unsafe']
        with self.assertRaisesRegex(ValueError, 'fixed CLI argument vectors'):
            measurement.validate_profile(changed, self.workload)
        changed = copy.deepcopy(self.profile)
        changed['core']['artifact_sha256'] = 'not-a-sha256'
        with self.assertRaisesRegex(ValueError, 'expected CLI artifact SHA-256'):
            measurement.validate_profile(changed, self.workload)

    def test_command_failure_retains_error_and_measured_duration(self):
        completed = Mock(returncode=1, stdout='{"status":"error","code":"OUTCOME_UNKNOWN"}\n')
        row = measurement.command_sample('cold-1', 1, 'cold_task', completed, 12.5)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['error_code'], 'OUTCOME_UNKNOWN')
        self.assertEqual(row['duration_ms'], 12.5)
        self.assertEqual(row['exit_code'], 1)

    def test_unmeasured_null_values_survive_json_round_trip(self):
        row = measurement.unmeasured_sample('metal-1', 1, 'metal', 'No Metal workload in this profile')
        self.assertIsNone(row['duration_ms'])
        capabilities = self.profile['unmeasured_capabilities']
        result = {'format_version': 1, 'status': 'unmeasured', 'samples': [row],
                  'unmeasured_capabilities': capabilities}
        restored = json.loads(json.dumps(result))
        measurement.validate_result(restored)
        self.assertIsNone(restored['samples'][0]['duration_ms'])
        self.assertIsNone(restored['unmeasured_capabilities'][0]['value'])

    def test_result_validator_rejects_hidden_failures_and_zero_substitution(self):
        failed = measurement.sample('bad', 1, 'cold_task', 'failed', 11, 1, 'PROCESS_FAILED')
        result = {'format_version': 1, 'status': 'passed', 'samples': [failed]}
        with self.assertRaisesRegex(ValueError, 'cannot hide failures'):
            measurement.validate_result(result)
        invalid = measurement.unmeasured_sample('unknown', 1, 'gpu', 'Not measured')
        invalid['duration_ms'] = 0
        result = {'format_version': 1, 'status': 'passed', 'samples': [invalid]}
        with self.assertRaisesRegex(ValueError, 'Unmeasured duration must remain null'):
            measurement.validate_result(result)

    def test_result_rejects_missing_failure_code_and_unmeasured_reason(self):
        failed = measurement.sample('bad', 1, 'warm_task', 'failed', 3, 1)
        result = {'format_version': 1, 'status': 'failed', 'samples': [failed]}
        with self.assertRaisesRegex(ValueError, 'stable error code'):
            measurement.validate_result(result)
        unknown = measurement.unmeasured_sample('unknown', 1, 'webgpu', 'Not available')
        unknown['reason'] = None
        result = {'format_version': 1, 'status': 'passed', 'samples': [unknown]}
        with self.assertRaisesRegex(ValueError, 'retain a reason'):
            measurement.validate_result(result)

    def test_build_ignores_stale_same_version_artifact_in_core_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core = root / 'core'
            core.mkdir()
            stale = core / 'target/release/eylema'
            stale.parent.mkdir(parents=True)
            stale.write_text('stale same-version artifact')
            target = root / 'fresh-target'
            target.mkdir()

            def fake_build(command, **kwargs):
                self.assertEqual(command, ['cargo', '+1.99.0', 'build', '--workspace',
                                           '--locked', '--release', '--offline'])
                self.assertEqual(kwargs['cwd'], core)
                self.assertEqual(kwargs['env']['CARGO_TARGET_DIR'], str(target.resolve()))
                self.assertEqual(set(kwargs['env']), {
                    'PATH', 'HOME', 'TMPDIR', 'CARGO_HOME', 'RUSTUP_HOME', 'CARGO_TARGET_DIR'})
                artifact = target / 'release/eylema'
                artifact.parent.mkdir(parents=True)
                artifact.write_text('built from pinned source')
                artifact.chmod(0o755)
                return Mock(returncode=0, stderr='')

            with patch.object(measurement.shutil, 'which', return_value='/test/cargo'), \
                    patch.object(measurement.subprocess, 'run', side_effect=fake_build), \
                    patch.object(measurement, 'sha256_file', return_value=self.profile['core']['artifact_sha256']):
                binary, command, _ = measurement.build_cli(core, self.profile, target)

            self.assertNotEqual(binary, stale)
            self.assertEqual(binary.read_text(), 'built from pinned source')
            self.assertEqual(command[-1], '--offline')

    def test_build_rejects_artifact_hash_different_from_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            core = root / 'core'
            core.mkdir()
            target = root / 'fresh-target'
            target.mkdir()

            def fake_build(command, **kwargs):
                artifact = target / 'release/eylema'
                artifact.parent.mkdir(parents=True)
                artifact.write_text('different artifact')
                artifact.chmod(0o755)
                return Mock(returncode=0, stderr='')

            with patch.object(measurement.shutil, 'which', return_value='/test/cargo'), \
                    patch.object(measurement.subprocess, 'run', side_effect=fake_build), \
                    patch.object(measurement, 'sha256_file', return_value='0' * 64):
                with self.assertRaisesRegex(ValueError, 'differs from the profile'):
                    measurement.build_cli(core, self.profile, target)


if __name__ == '__main__':
    unittest.main()
