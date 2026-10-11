"""First-party Rust profile runner. Skips docs; never substitutes fixtures for execution."""
import argparse
import json
import os
import platform
import re
import subprocess
import tomllib
from pathlib import Path

TOOLCHAIN = '1.99.0'
TARGET = 'wasm32-unknown-unknown'
ROLES = {'eylema-dev/eylema': 'core', 'eylema-dev/eylema-examples': 'examples', 'eylema-dev/eylema-evals': 'evals'}
REPOSITORIES = {'eylema', 'devguide', '.github', 'eylema-docs-en', 'eylema-docs-tr',
                'eylema-examples', 'eylema-evals', 'eylema-models', 'eylema-training'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_identity(root, config):
    identity = config.get('repository')
    require(identity in {'eylema-dev/' + n for n in REPOSITORIES}, 'Unknown repository identity')
    actual = os.environ.get('GITHUB_REPOSITORY')
    require(actual is None or actual == identity, 'Declared repository differs from trusted CI identity')
    def remote(name):
        return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'remote', 'get-url', name],
                              cwd=root, check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    canonical = {f'https://github.com/{identity}', f'https://github.com/{identity}.git',
                 f'git@github.com:{identity}.git'}
    origin = remote('origin')
    if origin not in canonical:
        name = re.escape(identity.split('/')[1])
        fork = bool(re.fullmatch(r'https://github\.com/[A-Za-z0-9-]+/' + name + r'(?:\.git)?', origin)
                    or re.fullmatch(r'git@github\.com:[A-Za-z0-9-]+/' + name + r'\.git', origin))
        require(fork and remote('upstream') in canonical, 'Fork needs matching name and canonical upstream')
    # Local origin is consistency evidence, not proof of ownership or authorization.


def validate_environment():
    allowed = {'CARGO_HOME', 'CARGO_TERM_COLOR', 'RUSTUP_HOME', 'RUST_BACKTRACE'}
    require(not any(k.startswith(('CARGO_', 'RUST')) and k not in allowed for k in os.environ),
            'Cargo/compiler/toolchain environment overrides require a reviewed profile')


def validate_toolchain(root):
    value = tomllib.loads((root / 'rust-toolchain.toml').read_text())
    require(value == {'toolchain': {'channel': TOOLCHAIN, 'profile': 'minimal',
                                  'components': ['rustfmt', 'clippy'], 'targets': [TARGET]}},
            'Rust profile requires the exact reviewed toolchain/components/target')


def validate_wasm_packages(config):
    packages = config.get('wasm_packages')
    require(isinstance(packages, list) and all(isinstance(p, str) and re.fullmatch('[a-z][a-z0-9_-]*', p) for p in packages)
            and len(packages) == len(set(packages)), 'Declare exact unique portable Wasm packages')
    require(config.get('role') != 'core' or packages, 'Core requires portable Wasm acceptance packages')
    return packages


def validate_metadata(root, value, wasm_packages=()):
    root = root.resolve()
    require(Path(value['workspace_root']).resolve() == root, 'Foreign Cargo workspace')
    packages = value.get('packages', [])
    members = value.get('workspace_members', [])
    require(packages and len(members) == len(set(members))
            and len(packages) == len(members) and {p['id'] for p in packages} == set(members), 'External Cargo packages are forbidden in rust-v1')
    libraries = {p['name'] for p in packages if any(set(t.get('kind', [])) & {'lib', 'rlib', 'cdylib', 'staticlib'} for t in p.get('targets', []))}
    require(set(wasm_packages) <= libraries, 'Wasm packages must name actual owned workspace libraries')
    for package in packages:
        manifest = Path(package['manifest_path']).resolve()
        require(package.get('source') is None and manifest.is_relative_to(root) and manifest.is_file(),
                'Every Cargo package must be first-party workspace-local')
        require(not any(d.get('source') is not None or not d.get('path')
                        or not Path(d['path']).resolve().is_relative_to(root)
                        for d in package.get('dependencies', [])), 'External dependency declaration')
        require(not any(t.get('kind') == ['custom-build'] for t in package.get('targets', [])),
                'Build scripts require a reviewed profile update')


def run(root, args, capture=False):
    print('RUN:', ' '.join(args), flush=True)
    result = subprocess.run(args, cwd=root, check=True, text=True, capture_output=capture, timeout=600)
    return result.stdout if capture else None


def check(root, provision=False):
    config = json.loads((root / 'repository.json').read_text())
    validate_identity(root, config)
    require(config.get('tooling_version') == '0.3.0', 'Unreviewed tooling version')
    if config.get('stage') == 'documentation':
        require(config.get('gate_profile') == 'documentation-v1' and config.get('runtime_implemented') is False,
                'Invalid documentation stage')
        require(not any(root.rglob('Cargo.toml')), 'Documentation checkout contains a Cargo workspace')
        print('SKIPPED: documentation profile; Rust execution not applicable')
        return
    require(config.get('stage') == 'implementation' and config.get('gate_profile') == 'rust-v1'
            and config.get('runtime_implemented') is True
            and ROLES.get(config.get('repository')) == config.get('role'), 'Unapproved Rust implementation profile')
    validate_toolchain(root)
    wasm_packages = validate_wasm_packages(config)
    require(platform.system() == 'Darwin' and platform.machine() == 'arm64', 'Native acceptance requires ARM64 macOS')
    validate_environment()
    config_dirs = [p / '.cargo' for p in [root, *root.parents]]
    config_dirs.append(Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo')))
    require(not any((p / n).exists() for p in config_dirs for n in ('config', 'config.toml')),
            'Cargo configuration overrides require a reviewed profile')
    if provision:
        run(root, ['rustup', 'toolchain', 'install', TOOLCHAIN, '--profile', 'minimal', '--component', 'rustfmt,clippy', '--target', TARGET])
    prefix = ['rustup', 'run', TOOLCHAIN]
    version = run(root, prefix + ['rustc', '--version'], capture=True)
    require(version.startswith('rustc ' + TOOLCHAIN + ' '), 'Unexpected compiler version')
    print(version.strip())
    metadata = json.loads(run(root, prefix + ['cargo', 'metadata', '--locked', '--offline', '--all-features', '--format-version', '1'], capture=True))
    validate_metadata(root, metadata, wasm_packages)
    for command in [
        ['fmt', '--all', '--', '--check'],
        ['clippy', '--workspace', '--all-targets', '--all-features', '--locked', '--offline', '--', '-D', 'warnings'],
        ['test', '--workspace', '--all-features', '--locked', '--offline'],
        ['build', '--workspace', '--all-features', '--locked', '--offline'],
    ]:
        run(root, prefix + ['cargo', *command])
    if wasm_packages:
        selection = [arg for package in wasm_packages for arg in ('--package', package)]
        run(root, prefix + ['cargo', 'build', *selection, '--lib', '--all-features', '--locked', '--offline', '--target', TARGET])
        print('PASS: selected portable library Wasm compilation:', ', '.join(wasm_packages))
    else:
        print('NOT_APPLICABLE: reviewed native-only consumer has no Wasm library')
    print('PASS: native ARM64 fmt/clippy/tests/build and workspace dependency inventory')
    print('Wasm execution, GPU, model and training: NOT RUN. Workspace membership does not prove copied-code provenance.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provision', action='store_true')
    args = parser.parse_args()
    try:
        check(Path(__file__).resolve().parents[1], args.provision)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        raise SystemExit(f'FAIL: {error}') from error


if __name__ == '__main__':
    main()
