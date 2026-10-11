"""Validator/runner regression fixtures. No Rust compiler or GPU is executed here."""
import copy
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_rust as rust
from check_repository import (ROOT, CheckFailure, load_yaml, validate_repository_config,
                              validate_source_stage, validate_workflow, validate_schema_ownership)


def config(stage='implementation', repository='eylema'):
    roles = {'eylema': 'core', 'eylema-examples': 'examples', 'eylema-evals': 'evals', 'eylema-models': 'models'}
    return {'format_version': 1, 'repository': 'eylema-dev/' + repository, 'role': roles[repository],
            'stage': stage, 'runtime_implemented': stage == 'implementation', 'tooling_version': '0.3.0',
            'gate_profile': 'rust-v1' if stage == 'implementation' else 'documentation-v1',
            **({'wasm_packages': ['owned']} if stage == 'implementation' else {})}


def initialize_git(root):
    subprocess.run(['git', 'init', '--quiet', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), 'remote', 'add', 'origin', 'https://github.com/eylema-dev/eylema.git'], check=True)


def fixture(root):
    if not (root / '.git').exists(): initialize_git(root)
    (root / 'repository.json').write_text(json.dumps(config()))
    (root / 'rust-toolchain.toml').write_text('[toolchain]\nchannel = "1.99.0"\nprofile = "minimal"\ncomponents = ["rustfmt", "clippy"]\ntargets = ["wasm32-unknown-unknown"]\n')
    (root / 'Cargo.toml').write_text('[workspace]\n')
    (root / 'Cargo.lock').write_text('version = 4\n')
    (root / 'src').mkdir(); (root / 'src/lib.rs').write_text('')
    (root / 'tools').mkdir(); (root / 'tools/check_rust.py').write_text('fixture only')
    return {'workspace_root': str(root), 'workspace_members': ['owned'],
            'packages': [{'id': 'owned', 'name': 'owned', 'source': None, 'manifest_path': str(root / 'Cargo.toml'),
                          'dependencies': [], 'targets': [{'kind': ['lib']}]}]}


class RustProfileTests(unittest.TestCase):
    def test_only_reviewed_roles_can_enter_implementation(self):
        for name in ('eylema', 'eylema-examples', 'eylema-evals'):
            self.assertEqual(validate_repository_config(config(repository=name)), name)
        for value in (config(repository='eylema-models'), dict(config(), gate_profile='trust-me'),
                      dict(config('documentation'), runtime_implemented=True)):
            with self.assertRaises(CheckFailure): validate_repository_config(value)

    def test_declared_identity_must_match_ci_and_local_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_git(root)
            with patch.dict('check_rust.os.environ', {}, clear=True):
                rust.validate_identity(root, config())
                with self.assertRaises(ValueError): rust.validate_identity(root, config(repository='eylema-examples'))
            with patch.dict('check_rust.os.environ', {'GITHUB_REPOSITORY': 'eylema-dev/eylema-training'}, clear=True):
                with self.assertRaises(ValueError): rust.validate_identity(root, config())
            with patch.dict('check_rust.os.environ', {'GITHUB_REPOSITORY': 'eylema-dev/eylema'}, clear=True):
                rust.validate_identity(root, config())

    def test_all_cargo_compiler_and_runner_overrides_fail_before_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); fixture(root)
            overrides = ('CARGO_BUILD_RUSTC', 'CARGO_BUILD_RUSTC_WRAPPER', 'CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER',
                         'CARGO_TARGET_AARCH64_APPLE_DARWIN_RUNNER', 'CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS',
                         'RUSTC', 'RUSTC_WRAPPER', 'RUSTDOC', 'RUSTFLAGS', 'RUSTC_BOOTSTRAP',
                         'CARGO_ENCODED_RUSTFLAGS', 'RUSTUP_DIST_SERVER', 'RUSTUP_TOOLCHAIN')
            with patch('check_rust.platform.system', return_value='Darwin'), patch('check_rust.platform.machine', return_value='arm64'):
                for key in overrides:
                    with self.subTest(key=key), patch.dict('check_rust.os.environ', {key: 'fixture-override'}, clear=True), patch('check_rust.run') as run:
                        with self.assertRaises(ValueError): rust.check(root, provision=True)
                        run.assert_not_called()
            with patch.dict('check_rust.os.environ', {'RUST_BACKTRACE': '1', 'CARGO_TERM_COLOR': 'always'}, clear=True):
                rust.validate_environment()

    def test_documentation_sources_and_incomplete_or_wrong_toolchain_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); fixture(root)
            names = [str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()]
            validate_source_stage(root, names, config())
            for missing in ('Cargo.lock', 'tools/check_rust.py', 'src/lib.rs'):
                with self.assertRaises(CheckFailure): validate_source_stage(root, [n for n in names if n != missing], config())
            for name in ('src/lib.rs', 'Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml'):
                with self.assertRaises(CheckFailure): validate_source_stage(root, [name], config('documentation'))
            with self.assertRaises(CheckFailure): validate_source_stage(root, names + ['.cargo/config.toml'], config())
            path = root / 'rust-toolchain.toml'; path.write_text(path.read_text().replace('1.99.0', 'stable'))
            with self.assertRaises(ValueError): validate_source_stage(root, names, config())

    def test_schemas_remain_core_owned_with_required_contracts(self):
        required = ['ai-team-protocol/request.schema.json', 'ai-team-protocol/model-set-release.schema.json']
        validate_schema_ownership('eylema', required + ['ai-team-protocol/task.schema.json'])
        for repo, schemas in [('eylema', required[:1]), ('eylema', required + ['docs/foreign.schema.json']),
                              ('eylema', required + ['ai-team-protocol/../escape.schema.json']),
                              ('eylema-models', required), ('devguide', required)]:
            with self.assertRaises(CheckFailure): validate_schema_ownership(repo, schemas)

    def test_wasm_selection_is_explicit_and_bound_to_owned_libraries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); metadata = fixture(root)
            self.assertEqual(rust.validate_wasm_packages(config()), ['owned'])
            metadata['packages'][0]['targets'].append({'kind': ['bin']})
            rust.validate_metadata(root, metadata, ['owned'])
            for selection in (None, [], ['owned', 'owned'], ['../escape'], [1]):
                with self.assertRaises(ValueError): rust.validate_wasm_packages(dict(config(), wasm_packages=selection))
            with self.assertRaises(ValueError): rust.validate_metadata(root, metadata, ['absent'])
            metadata['packages'][0]['targets'] = [{'kind': ['bin']}]
            with self.assertRaises(ValueError): rust.validate_metadata(root, metadata, ['owned'])
            self.assertEqual(rust.validate_wasm_packages(dict(config(repository='eylema-evals'), wasm_packages=[])), [])

    def test_runtime_job_cannot_be_missing_skipped_or_privileged(self):
        original = load_yaml(ROOT / '.github/workflows/ci.yml')
        variants = []
        value = copy.deepcopy(original); del value['jobs']['rust-checks']; variants.append(value)
        for field, content in [('runs-on', 'self-hosted'), ('if', 'false'), ('permissions', {'contents': 'write'})]:
            value = copy.deepcopy(original); value['jobs']['rust-checks'][field] = content; variants.append(value)
        value = copy.deepcopy(original); value['jobs']['rust-checks']['steps'][-1]['run'] = 'echo passed'; variants.append(value)
        value = copy.deepcopy(original); value['jobs']['rust-checks']['steps'][-1]['if'] = 'false'; variants.append(value)
        for value in variants:
            with self.assertRaises(CheckFailure): validate_workflow(value)

    def test_external_dependencies_build_scripts_and_foreign_roots_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); original = fixture(root); rust.validate_metadata(root, original)
            variants = []
            value = copy.deepcopy(original); value['packages'].append(dict(value['packages'][0], id='third-party')); variants.append(value)
            for key, content in [('source', 'registry+https://example.org'), ('manifest_path', str(root.parent / 'Cargo.toml')),
                                 ('dependencies', [{'source': 'registry', 'path': None}]),
                                 ('dependencies', [{'source': None, 'path': str(root.parent)}]),
                                 ('targets', [{'kind': ['custom-build']}])]:
                value = copy.deepcopy(original); value['packages'][0][key] = content; variants.append(value)
            value = copy.deepcopy(original); value['workspace_root'] = str(root.parent); variants.append(value)
            for value in variants:
                with self.assertRaises(ValueError): rust.validate_metadata(root, value)

    def test_runner_executes_required_commands_and_propagates_each_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); metadata = fixture(root)
            def response(root, args, capture=False):
                if 'rustc' in args: return 'rustc 1.99.0 (fixture)\n'
                if 'metadata' in args: return json.dumps(metadata)
            with contextlib.redirect_stdout(io.StringIO()), patch('check_rust.platform.system', return_value='Darwin'), patch('check_rust.platform.machine', return_value='arm64'), patch.dict('check_rust.os.environ', {}, clear=True), patch('check_rust.Path.home', return_value=root), patch('check_rust.run', side_effect=response) as run:
                rust.check(root)
                calls = [c.args[1] for c in run.call_args_list]
                self.assertEqual([a[4] for a in calls[1:]], ['metadata', 'fmt', 'clippy', 'test', 'build', 'build'])
                self.assertEqual(calls[-1][-2:], ['--target', rust.TARGET])
                self.assertIn('--package', calls[-1]); self.assertIn('--lib', calls[-1]); self.assertNotIn('--workspace', calls[-1])
                for index in range(len(calls)):
                    count = 0
                    def failure(root, args, capture=False):
                        nonlocal count
                        count += 1
                        if count == index + 1: raise subprocess.CalledProcessError(9, args)
                        return response(root, args, capture)
                    with patch('check_rust.run', side_effect=failure):
                        with self.assertRaises(subprocess.CalledProcessError): rust.check(root)
                    self.assertEqual(count, index + 1)
                with patch('check_rust.run', return_value='rustc 1.98.0 (fixture)'):
                    with self.assertRaises(ValueError): rust.check(root)

    def test_docs_skip_and_wrong_host_never_provision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_git(root); (root / 'repository.json').write_text(json.dumps(config('documentation')))
            with patch.dict('check_rust.os.environ', {}, clear=True), patch('check_rust.run') as run:
                with contextlib.redirect_stdout(io.StringIO()): rust.check(root, provision=True)
                run.assert_not_called()
            fixture(root)
            with patch.dict('check_rust.os.environ', {}, clear=True), patch('check_rust.platform.system', return_value='Linux'), patch('check_rust.run') as run:
                with self.assertRaises(ValueError): rust.check(root, provision=True)
                run.assert_not_called()
