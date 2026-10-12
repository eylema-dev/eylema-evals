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

    def test_local_forks_require_matching_canonical_upstream(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); initialize_git(root)
            subprocess.run(['git', '-C', str(root), 'remote', 'set-url', 'origin', 'https://github.com/contributor/eylema.git'], check=True)
            with patch.dict('check_rust.os.environ', {}, clear=True):
                with self.assertRaises(subprocess.CalledProcessError): rust.validate_identity(root, config())
                subprocess.run(['git', '-C', str(root), 'remote', 'add', 'upstream', 'https://github.com/eylema-dev/eylema.git'], check=True)
                rust.validate_identity(root, config())
                with self.assertRaises(ValueError): rust.validate_identity(root, config(repository='eylema-examples'))
                subprocess.run(['git', '-C', str(root), 'remote', 'set-url', 'upstream', 'https://github.com/other/eylema.git'], check=True)
                with self.assertRaises(ValueError): rust.validate_identity(root, config())

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

    def test_examples_may_use_only_the_exact_first_party_core_contract(self):
        revision = 'b06b7bac1aa121a4fee81e9fd0f79e4cf2eeeb3f'
        config_value = config(repository='eylema-examples')
        config_value['wasm_packages'] = []
        config_value['first_party_git_dependencies'] = {
            'eylema-contracts': {'repository': 'https://github.com/eylema-dev/eylema', 'revision': revision}}
        pins = rust.core_dependency_pins(config_value)
        self.assertEqual(set(pins), {'eylema-contracts'})
        expected_source = f'git+https://github.com/eylema-dev/eylema?rev={revision}#{revision}'
        with tempfile.TemporaryDirectory() as temp:
            metadata = fixture(Path(temp))
            metadata['packages'][0]['dependencies'] = [{'name': 'eylema-contracts', 'source': expected_source.rsplit('#', 1)[0], 'path': None}]
            metadata['packages'].append({'id': 'git+eylema-contracts', 'name': 'eylema-contracts',
                                         'source': expected_source, 'manifest_path': '/tmp/cargo/eylema-contracts/Cargo.toml',
                                         'dependencies': [], 'targets': [{'kind': ['lib']}]})
            source_map = {name: expected_source for name in pins}
            self.assertIsNone(rust.validate_metadata(Path(metadata['workspace_root']), metadata, [], source_map))
            for bad_source in (expected_source.replace(revision, '0' * 40), 'registry+https://example.invalid'):
                changed = copy.deepcopy(metadata)
                changed['packages'][1]['source'] = bad_source
                changed['packages'][0]['dependencies'][0]['source'] = bad_source
                with self.assertRaises(ValueError): rust.validate_metadata(Path(metadata['workspace_root']), changed, [], pins)
        for bad_config in (
            dict(config_value, first_party_git_dependencies={'eylema-contracts': {'repository': 'https://example.invalid/core', 'revision': revision}}),
            dict(config_value, first_party_git_dependencies={'other-crate': {'repository': 'https://github.com/eylema-dev/eylema', 'revision': revision}}),
            dict(config_value, first_party_git_dependencies={'eylema-contracts': {'repository': 'https://github.com/eylema-dev/eylema', 'revision': 'main'}}),
            dict(config_value, repository='eylema-dev/eylema'),
        ):
            with self.assertRaises(ValueError): rust.core_dependency_pins(bad_config)

    def test_examples_dependency_manifest_requires_immutable_core_git_revision(self):
        revision = 'b06b7bac1aa121a4fee81e9fd0f79e4cf2eeeb3f'
        repo_config = config(repository='eylema-examples')
        repo_config['first_party_git_dependencies'] = {
            'eylema-contracts': {'repository': 'https://github.com/eylema-dev/eylema', 'revision': revision}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'task-console').mkdir()
            (root / 'Cargo.toml').write_text(
                '[workspace]\nmembers = ["task-console"]\nresolver = "3"\n\n'
                '[workspace.dependencies]\n'
                f'eylema-contracts = {{ git = "https://github.com/eylema-dev/eylema", rev = "{revision}", package = "eylema-contracts" }}\n')
            package = root / 'task-console/Cargo.toml'
            package.write_text('[package]\nname = "task-console"\nversion = "0.1.0"\n'
                               'edition = "2024"\n\n[dependencies]\neylema-contracts.workspace = true\n')
            expected = {'eylema-contracts': f'git+https://github.com/eylema-dev/eylema?rev={revision}#{revision}'}
            self.assertEqual(rust.validate_dependency_manifests(root, repo_config, rust.core_dependency_pins(repo_config)), expected)
            package.write_text(package.read_text() + '\n[dev-dependencies]\nother = "1"\n')
            with self.assertRaises(ValueError): rust.validate_dependency_manifests(root, repo_config, rust.core_dependency_pins(repo_config))
            package.write_text(package.read_text().replace('other = "1"', f'eylema-contracts = {{ git = "https://github.com/eylema-dev/eylema", branch = "main" }}'))
            with self.assertRaises(ValueError): rust.validate_dependency_manifests(root, repo_config, rust.core_dependency_pins(repo_config))
            package.write_text('[package]\nname = "task-console"\nversion = "0.1.0"\nedition = "2024"\n\n'
                               '[dependencies]\neylema-contracts = { workspace = true, git = "https://example.invalid/core" }\n')
            with self.assertRaises(ValueError): rust.validate_dependency_manifests(root, repo_config, rust.core_dependency_pins(repo_config))

    def test_runner_executes_required_commands_and_propagates_each_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); metadata = fixture(root)
            def response(root, args, capture=False):
                if 'rustc' in args: return 'rustc 1.99.0 (fixture)\n'
                if 'metadata' in args: return json.dumps(metadata)
            with contextlib.redirect_stdout(io.StringIO()), patch('check_rust.platform.system', return_value='Darwin'), patch('check_rust.platform.machine', return_value='arm64'), patch.dict('check_rust.os.environ', {}, clear=True), patch('check_rust.Path.home', return_value=root), patch('check_rust.validate_dependency_manifests', return_value={}), patch('check_rust.run', side_effect=response) as run:
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

    def test_runner_fetches_only_after_validating_pins_and_before_offline_metadata(self):
        revision = 'b06b7bac1aa121a4fee81e9fd0f79e4cf2eeeb3f'
        expected_source = f'git+https://github.com/eylema-dev/eylema?rev={revision}#{revision}'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            initialize_git(root)
            config_value = config(repository='eylema-examples')
            config_value['wasm_packages'] = []
            config_value['first_party_git_dependencies'] = {
                'eylema-contracts': {'repository': 'https://github.com/eylema-dev/eylema', 'revision': revision}}
            (root / 'repository.json').write_text(json.dumps(config_value))
            (root / 'Cargo.toml').write_text(
                '[workspace]\nmembers = ["task-console"]\nresolver = "3"\n\n[workspace.dependencies]\n'
                f'eylema-contracts = {{ git = "https://github.com/eylema-dev/eylema", rev = "{revision}" }}\n')
            (root / 'task-console').mkdir()
            (root / 'task-console/Cargo.toml').write_text(
                '[package]\nname = "task-console"\nversion = "0.1.0"\nedition = "2024"\n\n'
                '[dependencies]\neylema-contracts.workspace = true\n')
            (root / 'rust-toolchain.toml').write_text(
                '[toolchain]\nchannel = "1.99.0"\nprofile = "minimal"\n'
                'components = ["rustfmt", "clippy"]\ntargets = ["wasm32-unknown-unknown"]\n')
            metadata = {'workspace_root': str(root), 'workspace_members': ['task-console'], 'packages': [
                {'id': 'task-console', 'name': 'task-console', 'source': None,
                 'manifest_path': str(root / 'task-console/Cargo.toml'),
                 'dependencies': [{'name': 'eylema-contracts', 'source': expected_source.rsplit('#', 1)[0], 'path': None}],
                 'targets': [{'kind': ['bin']}]},
                {'id': 'eylema-contracts', 'name': 'eylema-contracts', 'source': expected_source,
                 'manifest_path': '/tmp/cargo/eylema-contracts/Cargo.toml', 'dependencies': [],
                 'targets': [{'kind': ['lib']}]},
            ]}
            calls = []

            def response(root, args, capture=False):
                calls.append(args)
                if 'rustc' in args:
                    return 'rustc 1.99.0 (fixture)\n'
                if 'metadata' in args:
                    return json.dumps(metadata)
                return None

            allowed = {'eylema-contracts': expected_source}
            with contextlib.redirect_stdout(io.StringIO()), \
                    patch('check_rust.validate_identity'), \
                    patch('check_rust.validate_environment'), \
                    patch('check_rust.validate_metadata'), \
                    patch('check_rust.platform.system', return_value='Darwin'), \
                    patch('check_rust.platform.machine', return_value='arm64'), \
                    patch.dict('check_rust.os.environ', {}, clear=True), \
                    patch('check_rust.Path.home', return_value=root), \
                    patch('check_rust.run', side_effect=response):
                rust.check(root)
            fetch = next(index for index, args in enumerate(calls) if 'fetch' in args)
            metadata_call = next(index for index, args in enumerate(calls) if 'metadata' in args)
            self.assertLess(fetch, metadata_call)

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
