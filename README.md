# eylema-evals

Behavioral, performance and hardware evaluation. [Türkçe](README.tr.md).

**Status: evaluation tooling and versioned observations.** G18 supplies a fixed native task measurement harness and a three-trial M4 result. G22 adds a bounded combined Metal/WebGPU measurement harness and an exact source/artifact profile; its real Chrome combined-load acceptance is pending. Production execution remains in core; no model or training run is provided here.

## Scope

Versioned synthetic fixtures, workload definitions, harnesses and public result summaries. Report hardware, code/model/data revisions, limits, failures and resource use; compare quality before speed.

Production Rust code, shared types, runtime/model execution and the CLI stay in [eylema](https://github.com/eylema-dev/eylema); product contracts live in [English docs](https://github.com/eylema-dev/eylema-docs-en) and [Turkish docs](https://github.com/eylema-dev/eylema-docs-tr). [The repository map](https://github.com/eylema-dev/devguide/blob/main/docs/en/repository-map.md) defines the boundaries.

## Evaluation gates

[G18 native profile](profiles/m4-native-task-v1.json), [workload](workloads/native-task-demo-v1.json) and [raw result](results/m4-native-task-v1.json) bind the accepted CPU task observation to its exact core build. That profile leaves Metal, WebGPU, models and energy unmeasured.

[G22 combined profile](profiles/m4-combined-gpu-v1.json), [workload](workloads/combined-gpu-v1.json) and [measurement tool](tools/run_combined_measurement.py) describe three 120-frame Chrome trials while one bounded native Metal process checks exact integer readbacks. The profile pins core commit/tree, native/CLI/browser artifacts and workload bytes. Build the clean pinned core with its recorded command into a fresh isolated Cargo target directory. Start the resulting CLI service with a private `EYLEMA_DATA_DIR`; use only its observed loopback `/console` URL.

Run the tool's `collect` command with `--core-root`, `--worker`, `--cli`, `--profile`, `--console-url` and `--private-dir`. With authorized real Chrome control, open each `/console#g22=120&trial=1` through trial 3, save its visible JSON as `browser-1.json` through `browser-3.json`, and actually close each tab. `mark --trial <1..3> --phase <opened|closed> --tab-id <observed-id>` records those observations; it does not open or close a tab itself. After native work continues, `stop` requests cooperative termination. `finalize --private-dir ... --profile ... --output ...` preserves raw samples and failures, and accepts only three trials with overlapping checked Metal work, actual process RSS and further work by the same PID after each close.

Animation callback intervals and queue completion wall times are host observations, not hardware GPU or presentation timestamps. Browser JS heap is an optional estimate; unavailable values remain null. Native RSS excludes a separate browser process. Physical VRAM/driver allocations, energy and models remain explicitly unmeasured. Distributions use nearest-rank p50/p95/p99 and retain raw samples; there is no approved performance SLA. Synthetic regression fixtures never establish hardware acceptance. Review public records for private paths, credentials and unrelated data before committing them.

Python is development/evaluation tooling, not an application runtime. The repository retains its documentation source profile and shared gates; the collector/validator regressions run in ordinary Python CI. Rust/GPU execution belongs to the pinned first-party core and requires separate hardware evidence.

Use [CONTRIBUTING](CONTRIBUTING.md) and [AGENTS](AGENTS.md). Keep private data, credentials, operational records and model weights outside Git. Public artifacts need their own approved storage, provenance and license decision; no store or service is provisioned here. First-party source and documentation are licensed under [Apache-2.0](LICENSE). Separately identified third-party material, model weights and datasets retain their own terms.
