"""Bounded G22 measurement tooling; never substitutes fixtures for GPU results."""
import argparse
import hashlib
import json
import math
import os
import platform
import queue
import re
import statistics
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import run_measurement as base


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_profile(profile):
    base.require(profile.get('format_version') == 1, 'PROFILE_VERSION')
    base.require(profile.get('profile_id') == 'm4-combined-gpu-v1', 'PROFILE_ID')
    base.require(profile.get('workload') == 'combined-gpu-v1'
                 and profile.get('workload_sha256') == base.sha256_file(
                     Path(__file__).resolve().parents[1] / 'workloads/combined-gpu-v1.json'),
                 'WORKLOAD_BINDING')
    core = profile.get('core', {})
    base.require(core.get('repository') == 'https://github.com/eylema-dev/eylema', 'CORE_REPOSITORY')
    for field, length in [('revision', 40), ('tree', 40), ('native_worker_sha256', 64),
                          ('cli_sha256', 64), ('browser_module_sha256', 64)]:
        base.require(bool(re.fullmatch('[0-9a-f]{' + str(length) + '}', core.get(field, ''))),
                     'CORE_' + field.upper())
    base.require(core.get('toolchain') == '1.99.0', 'CORE_TOOLCHAIN')
    base.require(profile.get('trials') == 3, 'THREE_TRIALS_REQUIRED')
    base.require(type(profile.get('frames')) is int and 2 <= profile['frames'] <= 240, 'FRAME_BOUND')
    base.require(type(profile.get('native_seconds')) is int
                 and 1 <= profile['native_seconds'] <= 120, 'DURATION_BOUND')
    base.require(profile.get('rss_interval_ms') == 200, 'RSS_INTERVAL')
    base.require(profile.get('minimum_overlap_samples') == 5, 'OVERLAP_SAMPLE_MINIMUM')


def distribution(values, unmeasured_reason=None):
    base.require(all(number(value) for value in values), 'INVALID_DISTRIBUTION_SAMPLE')
    values = sorted(values)
    if not values:
        return {'status': 'unmeasured', 'count': 0, 'min': None, 'p50': None, 'p95': None,
                'p99': None, 'max': None, 'mean': None, 'reason': unmeasured_reason}
    percentile = lambda p: values[max(0, math.ceil(len(values) * p / 100) - 1)]
    return {'status': 'measured', 'count': len(values), 'min': values[0],
            'p50': percentile(50), 'p95': percentile(95), 'p99': percentile(99),
            'max': values[-1], 'mean': statistics.mean(values)}


def validate_browser(browser, profile, trial):
    base.require(isinstance(browser, dict), 'BROWSER_INVALID_RECORD')
    base.require(browser.get('format_version') == 1 and browser.get('status') == 'passed',
                 'BROWSER_FAILED_OR_MISSING')
    base.require(browser.get('trial') == trial and browser.get('frame_count') == profile['frames'],
                 'BROWSER_TRIAL_BINDING')
    samples = browser.get('samples')
    base.require(isinstance(samples, list) and len(samples) == profile['frames'], 'BROWSER_SAMPLE_COUNT')
    for index, sample in enumerate(samples):
        base.require(isinstance(sample, dict), 'BROWSER_INVALID_SAMPLE')
        base.require(sample.get('index') == index and number(sample.get('queue_completion_ms')),
                     'BROWSER_SAMPLE_INVALID')
        interval = sample.get('raf_interval_ms')
        base.require(interval is None if index == 0 else number(interval), 'FRAME_INTERVAL_INVALID')
        heap = sample.get('js_heap_bytes')
        base.require(heap is None or number(heap), 'BROWSER_MEMORY_INVALID')
        base.require(number(sample.get('start_unix_ms')) and number(sample.get('end_unix_ms'))
                     and sample['start_unix_ms'] <= sample['end_unix_ms'], 'BROWSER_TIME_INVALID')
        if index:
            base.require(sample['start_unix_ms'] >= samples[index - 1]['end_unix_ms'],
                         'BROWSER_CLOCK_ORDER')
    base.require(browser.get('fallback_adapter') is not True, 'FALLBACK_GPU_NOT_ACCEPTED')
    info = browser.get('adapter_info', {})
    base.require(isinstance(info, dict), 'BROWSER_ADAPTER_INFO_INVALID')
    base.require('apple' in str(info.get('vendor', '')).lower()
                 or 'apple' in str(info.get('description', '')).lower(), 'M4_ADAPTER_NOT_OBSERVED')
    base.require('Chrome/' in browser.get('browser_user_agent', ''), 'CHROME_NOT_OBSERVED')
    return samples


def validate_native(native, profile):
    base.require(isinstance(native, dict), 'NATIVE_INVALID_RECORD')
    base.require(native.get('core') == profile['core'], 'NATIVE_CORE_BINDING')
    base.require(native.get('exit_code') == 0, 'NATIVE_PROCESS_FAILED')
    events = native.get('events', [])
    base.require(isinstance(events, list) and 3 <= len(events) <= 10002
                 and all(isinstance(event, dict) for event in events), 'NATIVE_INVALID_EVENTS')
    base.require(events[0].get('event') == 'started'
                 and events[-1].get('event') == 'finished', 'NATIVE_INCOMPLETE')
    samples = events[1:-1]
    base.require(len(samples) <= 10000 and events[-1].get('samples') == len(samples), 'NATIVE_SAMPLE_COUNT')
    base.require(events[0].get('elements') == 4096
                 and events[0].get('declared_metal_buffer_bytes') == 49152, 'NATIVE_WORKLOAD_MISMATCH')
    base.require(type(native.get('pid')) is int and native['pid'] > 0
                 and events[0].get('pid') == native['pid'], 'NATIVE_PROCESS_IDENTITY')
    base.require(number(events[0].get('unix_ms')) and number(events[-1].get('unix_ms'))
                 and events[0]['unix_ms'] <= samples[0].get('start_unix_ms', -1)
                 and events[-1]['unix_ms'] >= samples[-1].get('end_unix_ms', math.inf),
                 'NATIVE_LIFETIME_CLOCK_INVALID')
    base.require(native.get('served_browser_module_sha256') == profile['core']['browser_module_sha256'],
                 'SERVED_BROWSER_MODULE_MISMATCH')
    base.require(native.get('measurement_tool_sha256') == base.sha256_file(Path(__file__)),
                 'MEASUREMENT_TOOL_MISMATCH')
    environment = native.get('environment', {})
    base.require(environment.get('os') == 'Darwin' and environment.get('architecture') == 'arm64'
                 and 'Apple M4' in environment.get('chip', ''), 'M4_MAC_NOT_OBSERVED')
    base.require(isinstance(native.get('rss_samples'), list) and all(
        isinstance(sample, dict) and sample.get('status') in {'passed', 'unmeasured'} for sample in native['rss_samples']),
                 'NATIVE_RSS_CAPTURE_FAILED')
    for index, sample in enumerate(samples):
        base.require(sample.get('event') == 'sample' and sample.get('index') == index
                     and sample.get('status') == 'passed' and number(sample.get('duration_ns')),
                     'NATIVE_SAMPLE_FAILED')
        base.require(number(sample.get('start_unix_ms')) and number(sample.get('end_unix_ms'))
                     and sample['start_unix_ms'] <= sample['end_unix_ms'], 'NATIVE_CLOCK_INVALID')
        if index:
            base.require(sample['start_unix_ms'] >= samples[index - 1]['end_unix_ms'],
                         'NATIVE_CLOCK_ORDER')
    return samples


def analyse_trial(profile, native, browser, markers, trial):
    result = {'trial': trial, 'status': 'failed', 'browser_raw': browser,
              'tab_observations': [marker for marker in markers
                                   if isinstance(marker, dict) and marker.get('trial') == trial]}
    try:
        base.require(all(isinstance(marker, dict) for marker in markers), 'TAB_INVALID_RECORD')
        frames = validate_browser(browser, profile, trial)
        all_native = validate_native(native, profile)
        trial_markers = [marker for marker in markers if marker.get('trial') == trial]
        base.require(len(trial_markers) == 2
                     and [marker.get('phase') for marker in trial_markers] == ['opened', 'closed'],
                     'TAB_OBSERVATION_MISSING')
        opened, closed = trial_markers
        base.require(opened.get('tab_id') == closed.get('tab_id') and bool(closed.get('tab_id')),
                     'TAB_IDENTITY_MISMATCH')
        first, last = frames[0]['start_unix_ms'], frames[-1]['end_unix_ms']
        base.require(number(opened.get('unix_ms')) and number(closed.get('unix_ms'))
                     and opened['unix_ms'] <= first
                     and opened['unix_ms'] <= closed['unix_ms']
                     and closed['unix_ms'] >= last, 'TAB_CLOSE_CLOCK_INVALID')
        combined = [sample for sample in all_native
                    if sample['start_unix_ms'] <= last and sample['end_unix_ms'] >= first]
        after_close = [sample for sample in all_native if sample['start_unix_ms'] > closed['unix_ms']]
        base.require(len(combined) >= profile['minimum_overlap_samples'], 'NO_COMBINED_METAL_LOAD')
        base.require(len(after_close) >= 5, 'NATIVE_WORK_NOT_OBSERVED_AFTER_TAB_CLOSE')
        rss = [sample for sample in native['rss_samples'] if first <= sample['unix_ms'] <= last]
        base.require(rss and all(sample.get('status') == 'passed' and number(sample.get('rss_bytes'))
                                 and sample['rss_bytes'] > 0 for sample in rss), 'RSS_NOT_MEASURED')
        result.update(status='passed', tab_observations=trial_markers,
                      native_overlapping_samples=combined, native_after_close_samples=after_close[:5],
                      native_rss_raw=rss, native_pid=native['pid'],
                      distributions={
                          'native_complete_call_ms': distribution([sample['duration_ns'] / 1e6 for sample in combined]),
                          'raf_interval_ms': distribution([sample['raf_interval_ms'] for sample in frames[1:]]),
                          'queue_completion_wall_ms': distribution([sample['queue_completion_ms'] for sample in frames]),
                          'native_rss_bytes': distribution([sample['rss_bytes'] for sample in rss]),
                          'browser_js_heap_estimate_bytes': distribution(
                              [sample['js_heap_bytes'] for sample in frames if sample['js_heap_bytes'] is not None],
                              'Browser memory estimate API unavailable')})
    except (ValueError, KeyError, TypeError) as error:
        result['reason'] = str(error)
    return result


def final_result(profile, native, browsers, markers):
    validate_profile(profile)
    trials = [analyse_trial(profile, native, browsers.get(trial, {}), markers, trial)
              for trial in range(1, 4)]
    return {'format_version': 1, 'goal': 'G22', 'profile_id': profile['profile_id'],
            'status': 'passed' if all(trial['status'] == 'passed' for trial in trials) else 'failed',
            'core': profile['core'], 'environment': native.get('environment', {}),
            'measurement_tool_sha256': native.get('measurement_tool_sha256'),
            'served_browser_module_sha256': native.get('served_browser_module_sha256'),
            'native_exit_code': native.get('exit_code'), 'native_events_raw': native.get('events', []),
            'native_rss_raw': native.get('rss_samples', []), 'tab_observations_raw': markers,
            'trials': trials,
            'unmeasured': {name: {'status': 'unmeasured', 'value': None, 'reason': reason}
                           for name, reason in {
                               'physical_vram_and_driver_bytes': 'No physical GPU allocation instrument configured',
                               'gpu_hardware_and_presented_timestamps': 'Host clocks and queue completion only',
                               'energy': 'No energy instrument configured',
                               'model_inference': 'Integer addition and three-object scene only'}.items()}}


def console_asset(url):
    parsed = urllib.parse.urlsplit(url)
    base.require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                 and parsed.username is None and parsed.password is None
                 and parsed.port is not None and 1024 <= parsed.port <= 65535
                 and parsed.path == '/console' and not parsed.query and not parsed.fragment,
                 'LOCAL_CONSOLE_URL_REQUIRED')
    asset = urllib.parse.urlunsplit(('http', parsed.netloc, '/webgpu-view.mjs', '', ''))
    with urllib.request.urlopen(asset, timeout=5) as response:
        data = response.read(65537)
        base.require(len(data) <= 65536, 'BROWSER_ASSET_BOUND')
        return hashlib.sha256(data).hexdigest()


def collect(core_root, worker, cli, profile, console_url, directory):
    validate_profile(profile)
    base.require(platform.system() == 'Darwin' and platform.machine() == 'arm64', 'M4_MAC_REQUIRED')
    base.verify_core_checkout(core_root, profile)
    base.require(base.sha256_file(worker) == profile['core']['native_worker_sha256'], 'WORKER_ARTIFACT_MISMATCH')
    base.require(base.sha256_file(cli) == profile['core']['cli_sha256'], 'CLI_ARTIFACT_MISMATCH')
    asset_digest = console_asset(console_url)
    base.require(asset_digest == profile['core']['browser_module_sha256'], 'SERVED_BROWSER_MODULE_MISMATCH')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = directory / 'native.json'
    base.require(not output.exists(), 'COLLECTION_ALREADY_EXISTS')
    messages = queue.Queue()
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'LANG') if key in os.environ}
    stderr_path = directory / 'native-stderr.private.txt'
    with stderr_path.open('w') as stderr:
        process = subprocess.Popen([str(worker), str(profile['native_seconds'])], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=stderr, text=True, env=environment)
        def read_lines():
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except json.JSONDecodeError:
                    messages.put({'event': 'failed', 'reason': 'INVALID_NATIVE_JSON',
                                  'line_sha256': hashlib.sha256(line.encode()).hexdigest()})
        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        events, rss = [], []
        started = time.monotonic()
        stop_sent = False
        print(json.dumps({'status': 'collecting', 'pid': process.pid}), flush=True)
        while process.poll() is None:
            while not messages.empty():
                events.append(messages.get_nowait())
            now = time.time_ns() // 1000000
            try:
                observed = subprocess.run(['ps', '-o', 'rss=', '-p', str(process.pid)],
                                          capture_output=True, text=True, timeout=3, check=False)
                value = int(observed.stdout.strip()) * 1024
                base.require(observed.returncode == 0 and value > 0, 'RSS_READ_FAILED')
                rss.append({'unix_ms': now, 'rss_bytes': value, 'status': 'passed'})
            except (ValueError, OSError, subprocess.TimeoutExpired):
                rss.append({'unix_ms': now, 'rss_bytes': None,
                            'status': 'unmeasured' if process.poll() is not None else 'failed',
                            'reason': 'PROCESS_ENDED' if process.poll() is not None else 'RSS_READ_FAILED'})
            if not stop_sent and ((directory / 'stop').exists()
                                  or time.monotonic() - started > profile['native_seconds'] + 5):
                try:
                    process.stdin.write('stop\n')
                    process.stdin.flush()
                except BrokenPipeError:
                    pass
                stop_sent = True
            if time.monotonic() - started > profile['native_seconds'] + 20:
                process.kill()
                break
            time.sleep(profile['rss_interval_ms'] / 1000)
        code = process.wait(timeout=5)
        reader.join(timeout=5)
        while not messages.empty():
            events.append(messages.get_nowait())
    native = {'core': profile['core'], 'pid': process.pid, 'exit_code': code,
              'events': events, 'rss_samples': rss, 'served_browser_module_sha256': asset_digest,
              'measurement_tool_sha256': base.sha256_file(Path(__file__)),
              'environment': {'os': platform.system(), 'os_version': platform.mac_ver()[0],
                              'architecture': platform.machine(),
                              'chip': subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'],
                                                     capture_output=True, text=True, check=True, timeout=5).stdout.strip()}}
    output.write_text(json.dumps(native, indent=2) + '\n')
    print(json.dumps({'status': 'collected' if code == 0 else 'failed', 'events': len(events),
                      'rss_samples': len(rss), 'exit_code': code}), flush=True)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('collect')
    capture.add_argument('--core-root', type=Path, required=True)
    capture.add_argument('--worker', type=Path, required=True)
    capture.add_argument('--cli', type=Path, required=True)
    capture.add_argument('--profile', type=Path, required=True)
    capture.add_argument('--console-url', required=True)
    capture.add_argument('--private-dir', type=Path, required=True)
    marker = commands.add_parser('mark')
    marker.add_argument('--private-dir', type=Path, required=True)
    marker.add_argument('--trial', type=int, choices=[1, 2, 3], required=True)
    marker.add_argument('--phase', choices=['opened', 'closed'], required=True)
    marker.add_argument('--tab-id', required=True)
    stop = commands.add_parser('stop')
    stop.add_argument('--private-dir', type=Path, required=True)
    finish = commands.add_parser('finalize')
    finish.add_argument('--private-dir', type=Path, required=True)
    finish.add_argument('--profile', type=Path, required=True)
    finish.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'collect':
        return collect(args.core_root, args.worker, args.cli, json.loads(args.profile.read_text()),
                       args.console_url, args.private_dir)
    if args.command == 'mark':
        with (args.private_dir / 'markers.jsonl').open('a') as output:
            output.write(json.dumps({'trial': args.trial, 'phase': args.phase,
                                     'tab_id': args.tab_id, 'unix_ms': time.time_ns() // 1000000,
                                     'observation': 'Chrome local console tab observed via cua'}) + '\n')
    elif args.command == 'stop':
        (args.private_dir / 'stop').write_text('stop\n')
    else:
        profile = json.loads(args.profile.read_text())
        native = json.loads((args.private_dir / 'native.json').read_text())
        markers = [json.loads(line) for line in (args.private_dir / 'markers.jsonl').read_text().splitlines()]
        browsers = {trial: json.loads((args.private_dir / f'browser-{trial}.json').read_text())
                    if (args.private_dir / f'browser-{trial}.json').exists() else {}
                    for trial in range(1, 4)}
        result = final_result(profile, native, browsers, markers)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({'status': result['status'], 'trials': [trial['status'] for trial in result['trials']]}))
        return 0 if result['status'] == 'passed' else 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
