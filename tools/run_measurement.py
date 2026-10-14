"""Run the fixed, versioned native task workload and preserve every sample."""
import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def git_value(root, *args):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                          text=True, timeout=10).stdout.strip()


def verify_core_checkout(root, profile):
    root = root.resolve()
    require(git_value(root, 'rev-parse', '--show-toplevel') == str(root),
            'Core checkout root differs from the supplied path')
    require(git_value(root, 'rev-parse', 'HEAD') == profile['core']['revision'],
            'Core checkout revision differs from the profile')
    require(git_value(root, 'rev-parse', 'HEAD^{tree}') == profile['core']['tree'],
            'Core checkout tree differs from the profile')
    require(not git_value(root, 'status', '--porcelain'),
            'Core checkout must be clean before measurement')
    return root


def validate_profile(profile, workload):
    require(profile.get('format_version') == 1, 'Unsupported measurement profile version')
    require(profile.get('workload') == workload.get('workload_id'), 'Profile/workload identity mismatch')
    require(workload.get('format_version') == 1 and workload.get('revision') == 1,
            'Unsupported workload version')
    core = profile.get('core', {})
    require(core.get('repository') in {'https://github.com/eyleme-dev/eyleme',
                                         'https://github.com/eyleme-dev/eylema',
                                         'https://github.com/eylema-dev/eylema'},
            'Only the canonical first-party core is accepted')
    require(len(core.get('revision', '')) == 40 and all(c in '0123456789abcdef' for c in core['revision']),
            'Core revision must be a full immutable commit')
    require(len(core.get('tree', '')) == 40 and all(c in '0123456789abcdef' for c in core['tree']),
            'Core tree must be a full immutable tree')
    require(core.get('toolchain') == '1.99.0' and core.get('package_version') == '0.1.0',
            'Profile must bind the reviewed core toolchain and package version')
    require(len(core.get('artifact_sha256', '')) == 64
            and all(c in '0123456789abcdef' for c in core['artifact_sha256']),
            'Profile must bind the expected CLI artifact SHA-256')
    require(profile.get('target') == {'execution_context': 'NativeMacos', 'backend': 'NativeCpu'},
            'This profile measures only the native macOS CPU target')
    require(type(profile.get('repetitions')) is int and 1 <= profile['repetitions'] <= 10,
            'Repetition count must be between 1 and 10')
    require(type(profile.get('warm_repetitions_per_trial')) is int
            and 1 <= profile['warm_repetitions_per_trial'] <= 10,
            'Warm repetition count must be between 1 and 10')
    require(1 <= profile.get('startup_timeout_seconds', 0) <= 60
            and 1 <= profile.get('command_timeout_seconds', 0) <= 60,
            'Measurement timeouts must be between 1 and 60 seconds')
    require(profile.get('commands') == {
        'version': ['--version'], 'start': ['start'], 'health': ['doctor'],
        'measure': workload.get('input', {}).get('command'), 'stop': ['stop', 'service']},
        'Workload commands must match the reviewed fixed CLI argument vectors')
    require(workload.get('expected_result') == {'exit_code': 0, 'json_status': 'ok'},
            'Workload must declare the expected CLI result')
    unmeasured = profile.get('unmeasured_capabilities')
    require(isinstance(unmeasured, list) and unmeasured
            and all(item.get('status') == 'unmeasured' and item.get('value') is None
                    and item.get('name') and item.get('reason') for item in unmeasured),
            'Unsupported capabilities must remain explicit unmeasured nulls')
    require({item['name'] for item in unmeasured} ==
            {'Metal', 'browser WebGPU', 'model inference', 'energy'},
            'Metal, WebGPU, model and energy status must remain visible')


def parse_json_line(output):
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def sample(sample_id, trial, phase, status, duration_ms=None, exit_code=None,
           error_code=None, reason=None):
    require(status in {'passed', 'failed', 'unmeasured'}, 'Unknown sample status')
    require(duration_ms is None or (isinstance(duration_ms, (int, float)) and duration_ms >= 0),
            'Duration must be null or non-negative')
    return {'id': sample_id, 'trial': trial, 'phase': phase, 'status': status,
            'duration_ms': duration_ms, 'exit_code': exit_code,
            'error_code': error_code, 'reason': reason}


def command_sample(sample_id, trial, phase, result, duration_ms, expected_status='ok'):
    payload = parse_json_line(result.stdout or '')
    if result.returncode == 0 and payload and payload.get('status') == expected_status:
        return sample(sample_id, trial, phase, 'passed', duration_ms, 0)
    error_code = payload.get('code') if payload else None
    if not error_code:
        error_code = 'UNEXPECTED_RESULT' if result.returncode == 0 else 'PROCESS_FAILED'
    return sample(sample_id, trial, phase, 'failed', duration_ms, result.returncode, error_code)


def timed_run(binary, arguments, env, timeout, sample_id, trial, phase,
              expected_status='ok'):
    started = time.monotonic_ns()
    try:
        result = subprocess.run([str(binary), *arguments], env=env, capture_output=True,
                                text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        elapsed = (time.monotonic_ns() - started) / 1_000_000
        return sample(sample_id, trial, phase, 'failed', elapsed, None, 'TIMEOUT')
    except OSError:
        elapsed = (time.monotonic_ns() - started) / 1_000_000
        return sample(sample_id, trial, phase, 'failed', elapsed, None, 'EXECUTION_ERROR')
    elapsed = (time.monotonic_ns() - started) / 1_000_000
    return command_sample(sample_id, trial, phase, result, elapsed, expected_status)


def unmeasured_sample(sample_id, trial, phase, reason):
    return sample(sample_id, trial, phase, 'unmeasured', reason=reason)


def safe_environment(data_dir):
    home = data_dir / 'home'
    home.mkdir(mode=0o700)
    return {'EYLEMA_DATA_DIR': str(data_dir / 'data'), 'HOME': str(home),
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'TMPDIR': tempfile.gettempdir(), 'LANG': os.environ.get('LANG', 'C')}


def build_cli(core_root, profile, target_dir):
    """Build the pinned source into a fresh target directory and return its CLI."""
    cargo = shutil.which('cargo')
    require(cargo is not None, 'Cargo must be available to build the pinned core source')
    target_dir = target_dir.resolve()
    require(not any(target_dir.iterdir()), 'Cargo target directory must be fresh and empty')
    environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                   'HOME': os.environ.get('HOME', str(Path.home())),
                   'TMPDIR': tempfile.gettempdir(),
                   'CARGO_HOME': os.environ.get('CARGO_HOME', str(Path.home() / '.cargo')),
                   'RUSTUP_HOME': os.environ.get('RUSTUP_HOME', str(Path.home() / '.rustup')),
                   'CARGO_TARGET_DIR': str(target_dir)}
    command = ['cargo', '+' + profile['core']['toolchain'], 'build', '--workspace',
               '--locked', '--release', '--offline']
    built = subprocess.run(command, cwd=core_root, env=environment, capture_output=True,
                           text=True, timeout=900, check=False)
    require(built.returncode == 0,
            'Pinned core build failed: ' + (built.stderr[-2000:] if built.stderr else 'no diagnostics'))
    binary = target_dir / 'release' / 'eylema'
    require(binary.is_file() and os.access(binary, os.X_OK),
            'Pinned core build did not produce the expected CLI artifact')
    require(sha256_file(binary) == profile['core']['artifact_sha256'],
            'Built CLI artifact SHA-256 differs from the profile')
    return binary, command, environment


def start_service(binary, commands, env, timeout, trial):
    started = time.monotonic_ns()
    try:
        process = subprocess.Popen([str(binary), *commands['start']], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
    except OSError:
        elapsed = (time.monotonic_ns() - started) / 1_000_000
        return None, sample(f'trial-{trial}-service-cold-start', trial, 'service_cold_start',
                            'failed', elapsed, None, 'EXECUTION_ERROR')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            elapsed = (time.monotonic_ns() - started) / 1_000_000
            return process, sample(f'trial-{trial}-service-cold-start', trial,
                                   'service_cold_start', 'failed', elapsed, process.returncode,
                                   'SERVICE_EXITED')
        try:
            health = subprocess.run([str(binary), *commands['health']], env=env,
                                    capture_output=True, text=True, timeout=1, check=False)
        except (OSError, subprocess.TimeoutExpired):
            health = None
        if health and command_sample('health', trial, 'readiness_probe', health, 0).get('status') == 'passed':
            elapsed = (time.monotonic_ns() - started) / 1_000_000
            return process, sample(f'trial-{trial}-service-cold-start', trial,
                                   'service_cold_start', 'passed', elapsed, 0)
        time.sleep(0.025)
    elapsed = (time.monotonic_ns() - started) / 1_000_000
    return process, sample(f'trial-{trial}-service-cold-start', trial,
                           'service_cold_start', 'failed', elapsed, None, 'STARTUP_TIMEOUT')


def stop_service(binary, commands, env, process, timeout, trial):
    if process is None or process.poll() is not None:
        return None
    stopped = timed_run(binary, commands['stop'], env, min(timeout, 5),
                        f'trial-{trial}-service-stop-request', trial, 'service_stop',
                        expected_status='stop_requested')
    try:
        process.wait(timeout=min(timeout, 5))
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        return sample(f'trial-{trial}-service-shutdown', trial, 'service_shutdown',
                      'failed', None, None, 'SHUTDOWN_TIMEOUT')
    if stopped['status'] != 'passed' or process.returncode != 0:
        return sample(f'trial-{trial}-service-shutdown', trial, 'service_shutdown',
                      'failed', None, process.returncode, stopped.get('error_code') or 'SERVICE_EXITED')
    return sample(f'trial-{trial}-service-shutdown', trial, 'service_shutdown', 'passed', None, 0)


def host_environment():
    chip = None
    memory_bytes = None
    if platform.system() == 'Darwin' and shutil.which('sysctl'):
        result = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'],
                                capture_output=True, text=True, timeout=3, check=False)
        if result.returncode == 0:
            chip = result.stdout.strip()
        memory = subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True,
                                 text=True, timeout=3, check=False)
        if memory.returncode == 0 and memory.stdout.strip().isdigit():
            memory_bytes = int(memory.stdout.strip())
    return {'os': platform.system(), 'os_version': platform.release(),
            'architecture': platform.machine(), 'chip_model': chip,
            'memory_bytes': memory_bytes, 'logical_cpu_count': os.cpu_count()}


def tool_source(evals_root):
    return {'repository': 'eyleme-dev/eylema-evals',
            'revision': git_value(evals_root, 'rev-parse', 'HEAD'),
            'tree': git_value(evals_root, 'rev-parse', 'HEAD^{tree}')}


def summarize(samples):
    phases = {}
    for phase in sorted({row['phase'] for row in samples}):
        rows = [row for row in samples if row['phase'] == phase]
        measured = [row['duration_ms'] for row in rows
                    if row['status'] == 'passed' and row['duration_ms'] is not None]
        phases[phase] = {'attempts': len(rows),
                         'passed': sum(row['status'] == 'passed' for row in rows),
                         'failed': sum(row['status'] == 'failed' for row in rows),
                         'unmeasured': sum(row['status'] == 'unmeasured' for row in rows),
                         'median_ms': statistics.median(measured) if measured else None}
    return phases


def validate_result(result):
    require(result.get('format_version') == 1, 'Unsupported result version')
    require(result.get('status') in {'passed', 'failed', 'unmeasured'}, 'Unknown result status')
    require(isinstance(result.get('samples'), list) and result['samples'], 'Result samples are required')
    identifiers = [row.get('id') for row in result['samples']]
    require(len(identifiers) == len(set(identifiers)), 'Sample ids must be unique')
    for row in result['samples']:
        require(row.get('status') in {'passed', 'failed', 'unmeasured'}, 'Unknown sample status')
        require(row.get('duration_ms') is None or isinstance(row['duration_ms'], (int, float)),
                'Sample duration must preserve null or a numeric value')
        require(row['status'] != 'unmeasured' or row['duration_ms'] is None,
                'Unmeasured duration must remain null')
        require(row['status'] != 'failed' or bool(row.get('error_code')),
                'Failed samples must retain a stable error code')
        require(row['status'] != 'unmeasured' or bool(row.get('reason')),
                'Unmeasured samples must retain a reason')
    statuses = {row['status'] for row in result['samples']}
    expected = ('failed' if 'failed' in statuses else
                'unmeasured' if statuses == {'unmeasured'} else 'passed')
    require(result['status'] == expected, 'Aggregate result cannot hide failures or unknown samples')


def run(profile_path, workload_path, core_checkout, output_path):
    profile_bytes = profile_path.read_bytes()
    workload_bytes = workload_path.read_bytes()
    profile = json.loads(profile_bytes)
    workload = json.loads(workload_bytes)
    validate_profile(profile, workload)
    require(platform.system() == 'Darwin' and platform.machine() == 'arm64',
            'M4 native profile requires Apple Silicon macOS')
    core_root = verify_core_checkout(core_checkout, profile)
    target_directory = tempfile.TemporaryDirectory(prefix='eylema-g18-build-')
    try:
        target_dir = Path(target_directory.name)
        binary, build_command, build_environment = build_cli(core_root, profile, target_dir)
        verify_core_checkout(core_root, profile)
        return run_built(profile, workload, profile_bytes, workload_bytes, core_root,
                         binary, build_command, build_environment, output_path)
    finally:
        target_directory.cleanup()


def run_built(profile, workload, profile_bytes, workload_bytes, core_root,
              binary, build_command, build_environment, output_path):
    version = subprocess.run([str(binary), *profile['commands']['version']], capture_output=True,
                             env=build_environment, text=True, timeout=5, check=False)
    require(version.returncode == 0 and version.stdout.strip() ==
            f"eylema {profile['core']['package_version']}", 'CLI binary version differs from profile')
    rustc = shutil.which('rustc', path=build_environment['PATH'])
    require(rustc is not None, 'Pinned rustc must be available on PATH')
    rustc_result = subprocess.run([rustc, '+' + profile['core']['toolchain'], '--version'],
                                  env=build_environment, capture_output=True, text=True,
                                  timeout=5, check=False)
    require(rustc_result.returncode == 0 and rustc_result.stdout.startswith(
            'rustc ' + profile['core']['toolchain'] + ' '), 'Rust compiler differs from profile')
    chip = host_environment()['chip_model']
    require(chip and 'M4' in chip, 'M4 native profile requires an observed M4 chip model')
    root = Path(__file__).resolve().parents[1]
    head = git_value(root, 'rev-parse', 'HEAD')
    tree = git_value(root, 'rev-parse', 'HEAD^{tree}')
    require(not git_value(root, 'diff', '--name-only'), 'Measurement tool source has uncommitted tracked changes')
    samples = []
    repetitions = profile['repetitions']
    warm_repetitions = profile['warm_repetitions_per_trial']
    commands = profile['commands']
    for trial in range(1, repetitions + 1):
        with tempfile.TemporaryDirectory(prefix='eylema-g18-') as temporary:
            data_dir = Path(temporary)
            env = safe_environment(data_dir)
            service, start_sample = start_service(binary, commands, env,
                                                  profile['startup_timeout_seconds'], trial)
            samples.append(start_sample)
            if start_sample['status'] == 'passed':
                samples.append(timed_run(binary, commands['measure'], env,
                                         profile['command_timeout_seconds'],
                                         f'trial-{trial}-cold-task', trial, 'cold_task'))
                for repeat in range(1, warm_repetitions + 1):
                    samples.append(timed_run(binary, commands['measure'], env,
                                             profile['command_timeout_seconds'],
                                             f'trial-{trial}-warm-task-{repeat}', trial,
                                             'warm_task'))
            else:
                samples.append(unmeasured_sample(f'trial-{trial}-cold-task', trial,
                                                  'cold_task', 'Service did not become ready'))
                for repeat in range(1, warm_repetitions + 1):
                    samples.append(unmeasured_sample(f'trial-{trial}-warm-task-{repeat}', trial,
                                                      'warm_task', 'Service did not become ready'))
            cleanup = stop_service(binary, commands, env, service,
                                   profile['command_timeout_seconds'], trial)
            if cleanup:
                samples.append(cleanup)
    statuses = {row['status'] for row in samples}
    overall = ('failed' if 'failed' in statuses else
               'unmeasured' if statuses == {'unmeasured'} else 'passed')
    result = {'format_version': 1,
              'status': overall,
              'profile_id': profile['profile_id'],
              'profile_sha256': sha256_bytes(profile_bytes),
              'workload_id': workload['workload_id'], 'workload_revision': workload['revision'],
              'workload_sha256': sha256_bytes(workload_bytes),
              'source': {'repository': profile['core']['repository'],
                         'revision': profile['core']['revision'], 'tree': profile['core']['tree']},
              'measurement_tool': {**tool_source(root),
                                   'script_sha256': sha256_file(Path(__file__))},
              'artifact': {'package_version': profile['core']['package_version'],
                           'sha256': sha256_file(binary),
                           'build_command': build_command},
              'toolchain': {'expected': profile['core']['toolchain'],
                            'observed': rustc_result.stdout.strip()},
              'target': profile['target'], 'environment': host_environment(),
              'samples': samples, 'summary': summarize(samples),
              'unmeasured_capabilities': profile['unmeasured_capabilities'],
              'evidence_boundary': 'Native CPU CLI only; no Metal, WebGPU, model, training, GPU, or energy execution.'}
    validate_result(result)
    output_path = output_path.resolve()
    require(output_path.is_relative_to(root / 'results'), 'Result output must stay inside results/')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=Path('profiles/m4-native-task-v1.json'))
    parser.add_argument('--workload', type=Path, default=Path('workloads/native-task-demo-v1.json'))
    parser.add_argument('--core-checkout', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=Path('results/m4-native-task-v1.json'))
    args = parser.parse_args()
    try:
        result = run(args.profile, args.workload, args.core_checkout, args.output)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError,
            json.JSONDecodeError) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        raise SystemExit(1) from error
    print(f"{result['status'].upper()}: {len(result['samples'])} preserved samples -> {args.output}")
    return 1 if result['status'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
