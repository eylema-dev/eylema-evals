"""Repository gates; implementation profiles additionally require check_rust.py."""
import argparse
import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml
from jsonschema import Draft202012Validator
from check_pr import validate_body

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
}


class CheckFailure(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate mapping key: {key}")
        result[key] = value
    return result


def load_json(path):
    return json.loads(path.read_text(), object_pairs_hook=unique_pairs)


class WorkflowLoader(yaml.SafeLoader):
    """Use true/false booleans; YAML 1.1 must not turn the Actions 'on' key into True."""
    yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)


for first, resolvers in WorkflowLoader.yaml_implicit_resolvers.items():
    WorkflowLoader.yaml_implicit_resolvers[first] = [r for r in resolvers if r[0] != "tag:yaml.org,2002:bool"]
WorkflowLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.I), list("tTfF"))


def yaml_mapping(loader, node):
    return unique_pairs((loader.construct_object(k), loader.construct_object(v)) for k, v in node.value)


WorkflowLoader.add_constructor("tag:yaml.org,2002:map", yaml_mapping)


def load_yaml(path):
    return yaml.load(path.read_text(), Loader=WorkflowLoader)


def repository_files(root):
    result = subprocess.run(["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"], cwd=root, check=True, capture_output=True)
    files = sorted(set(result.stdout.decode().split("\0")) - {""})
    for name in files:
        path = root / name
        require(not path.is_symlink(), f"Symlink is not permitted in the documentation pack: {name}")
        require(path.is_file(), f"Missing tracked file: {name}")
    return files


def local_link_errors(root, document, text):
    errors = []
    # Code examples are not navigable Markdown links.
    text = re.sub(r"^```.*?^```\s*$", "", text, flags=re.M | re.S)
    for raw in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
        target = raw.strip().strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme in {"https", "http", "mailto"} or target.startswith("#"):
            continue
        if parsed.scheme or target.startswith(("/", "\\")):
            errors.append(f"Nonportable link in {document}: {target}")
            continue
        path = (root / document).parent / unquote(parsed.path)
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            errors.append(f"Broken or escaping link in {document}: {target}")
    return errors


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def validate_schema(schema):
    for value in walk(schema):
        if isinstance(value, dict) and "$ref" in value:
            require(value["$ref"].startswith("#"), "Schemas must resolve locally; remote references are forbidden")
    Draft202012Validator.check_schema(schema)


def validate_form(form):
    require(isinstance(form, dict) and all(isinstance(form.get(k), str) and form[k].strip() for k in ["name", "description", "title"]), "Issue form needs name, description and title")
    require(isinstance(form.get("body"), list), "Issue form body must be a list")
    fields = {}
    for item in form["body"]:
        require(isinstance(item, dict), "Issue form item must be a mapping")
        kind = item.get("type")
        require(kind in {"markdown", "textarea", "input", "dropdown", "checkboxes"}, "Unsupported issue field type")
        attrs = item.get("attributes", {})
        if kind == "markdown":
            require(bool(attrs.get("value")), "Empty issue-form markdown")
            continue
        key = item.get("id", "")
        require(bool(re.fullmatch(r"[a-z][a-z0-9_-]*", key)) and key not in fields, "Invalid or duplicate issue field ID")
        require(bool(attrs.get("label")), "Issue field has no label")
        fields[key] = item
        if kind in {"dropdown", "checkboxes"}:
            require(bool(attrs.get("options")), "Choice field has no options")
    for key in ["need", "targets", "acceptance", "scope", "verification"]:
        require(fields.get(key, {}).get("validations", {}).get("required") is True, f"Issue form must require {key}")


def validate_workflow(workflow):
    require(set(workflow) == {'name', 'on', 'permissions', 'concurrency', 'jobs'}, 'Unexpected workflow-level trust boundary')
    require(workflow.get("permissions") == {"contents": "read"}, "Workflow token must only have contents: read")
    events = workflow.get("on", {})
    require(set(events) == {"push", "pull_request", "workflow_dispatch"}, "Unexpected or privileged workflow trigger")
    require(events["push"].get("branches") == ["main"], "Push CI must cover main")
    required_events = {"opened", "synchronize", "reopened", "edited", "ready_for_review"}
    require(set(events["pull_request"].get("types", [])) == required_events, "PR body and head changes must retrigger CI")
    require(set(workflow.get("jobs", {})) == {"repository-checks", "rust-checks"}, "Unexpected CI job boundary")
    rust = workflow['jobs']['rust-checks']
    require(set(rust) == {'name', 'if', 'runs-on', 'timeout-minutes', 'steps'}, 'Unexpected Rust job configuration')
    require(rust['name'] == 'rust-checks' and rust['runs-on'] == 'macos-26' and rust['timeout-minutes'] == 20,
            'Rust CI requires bounded native ARM64 macOS runner')
    require(rust['if'] == "github.repository == 'eyleme-dev/eyleme' || github.repository == 'eyleme-dev/eyleme-examples' || github.repository == 'eyleme-dev/eyleme-evals'", 'Rust job role mismatch')
    require(rust['steps'] == [
        {'name': 'Checkout reviewed revision', 'uses': 'actions/checkout@' + ACTIONS['actions/checkout'],
         'with': {'fetch-depth': 0, 'persist-credentials': False}},
        {'name': 'Set up CI Python', 'uses': 'actions/setup-python@' + ACTIONS['actions/setup-python'],
         'with': {'python-version': '3.14'}},
        {'name': 'Run stage-aware native and Wasm gates', 'run': 'python tools/check_rust.py --provision'},
    ], 'Rust gate cannot be skipped, replaced or supplied privileged inputs')
    job = workflow["jobs"]["repository-checks"]
    require(set(job) == {'name', 'runs-on', 'timeout-minutes', 'steps'}, 'Unexpected repository job configuration')
    require(job.get("runs-on") == "ubuntu-24.04" and job.get("timeout-minutes") == 10, "CI needs bounded hosted runner")
    require(not any(k in job for k in ["permissions", "environment", "container", "services", "uses"]), "Unexpected CI trust boundary")
    used = set()
    commands = []
    for step in job.get("steps", []):
        if "uses" in step:
            name, sep, sha = step["uses"].partition("@")
            require(sep and ACTIONS.get(name) == sha, "Action must use a reviewed full commit SHA")
            used.add(name)
            if name == "actions/checkout":
                require(step.get("with", {}).get("persist-credentials") is False, "Checkout credentials must not persist")
        if "run" in step:
            require("${{" not in step["run"], "Pass expressions via environment, never interpolate into shell code")
            commands.append(step["run"])
        require("secrets." not in json.dumps(step), "Public PR CI must not consume secrets")
        require(not any(k in step for k in ['continue-on-error', 'working-directory', 'shell']), 'Unexpected gate override')
        if 'env' in step:
            require(step.get('name') == 'Record environment and revisions'
                    and step['env'] == {'PR_HEAD_SHA': '${{ github.event.pull_request.head.sha }}'}, 'Unexpected gate environment override')
    require(used == set(ACTIONS), "Missing pinned provisioning actions")
    joined = "\n".join(commands)
    for command in ["--require-hashes", "unittest discover", "tools/check_repository.py", "tools/check_pr.py"]:
        require(command in joined, f"CI missing required command: {command}")
    pr_steps = [s for s in job["steps"] if "tools/check_pr.py" in s.get("run", "")]
    require(len(pr_steps) == 1 and pr_steps[0].get("if") == "github.event_name == 'pull_request'", "Real PR gate must run on PR events")
    require("--allow-template" not in joined, "CI must not treat actual PR bodies as blank templates")
    integration = [s for s in job['steps'] if 'tools/check_workspace.py' in s.get('run', '')]
    require(len(integration) == 1 and integration[0].get('if') == "github.repository == 'eyleme-dev/devguide'"
            and integration[0]['run'] == '.venv/bin/python tools/check_workspace.py --fetch',
            'Devguide must run the pinned repository-set gate')


def verify_translations(root, manifest, write=False):
    pairs = manifest.get("pairs", [])
    documents = manifest.get("documents", [])
    require(bool(pairs or documents), "Missing translation records")
    seen = set()
    for pair in pairs:
        for language in ["en", "tr"]:
            name = pair[language]
            path = root / name
            require(name not in seen and path.resolve().is_relative_to(root.resolve()) and path.is_file(), f"Invalid translation path: {name}")
            seen.add(name)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            key = language + "_sha256"
            if write:
                pair[key] = digest
            else:
                require(pair.get(key) == digest, f"Translation review fingerprint is stale: {name}")
    for document in documents:
        name = document['path']
        path = root / name
        require(name not in seen and path.resolve().is_relative_to(root.resolve()) and path.is_file(), f"Invalid translation path: {name}")
        seen.add(name)
        require(document.get('language') in {'en', 'tr'}, f"Missing document language: {name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if write:
            document['sha256'] = digest
        else:
            require(document.get('sha256') == digest, f"Translation review fingerprint is stale: {name}")
    return len(pairs) + len(documents)


ROLES = {
    'eyleme': 'core', 'eyleme-docs-en': 'docs-en', 'eyleme-docs-tr': 'docs-tr',
    'devguide': 'guide', '.github': 'community', 'eyleme-examples': 'examples',
    'eyleme-evals': 'evals', 'eyleme-models': 'models', 'eyleme-training': 'training',
}
PRODUCT_DOCS = ('architecture', 'api-reference', 'development-and-operations',
                'learning-and-training', 'project-structure', 'roadmap')
SHARED_FILES = {
    'tools/check_repository.py', 'tools/check_pr.py', 'tools/requirements-ci.txt',
    'tools/tests/test_gates.py', '.github/PULL_REQUEST_TEMPLATE.md',
    'tools/check_rust.py', 'tools/tests/test_rust_gates.py',
    '.github/CODEOWNERS', '.github/copilot-instructions.md', '.github/workflows/ci.yml',
    '.github/ISSUE_TEMPLATE/config.yml', '.github/ISSUE_TEMPLATE/feature.yml',
    '.github/ISSUE_TEMPLATE/bug.yml', '.github/ISSUE_TEMPLATE/architecture.yml',
}


def validate_repository_config(config):
    name = config.get('repository', '').removeprefix('eyleme-dev/')
    require(config.get('repository') == 'eyleme-dev/' + name and name in ROLES, 'Unexpected repository identity')
    require(config.get('format_version') == 1 and config.get('role') == ROLES[name], 'Invalid repository role/version')
    stage = config.get('stage')
    require(stage in {'documentation', 'implementation'}, 'Unknown source stage')
    if stage == 'documentation':
        require(config.get('runtime_implemented') is False and config.get('gate_profile') == 'documentation-v1',
                'Documentation profile must not claim runtime implementation')
    else:
        require(name in {'eyleme', 'eyleme-examples', 'eyleme-evals'}
                and config.get('runtime_implemented') is True and config.get('gate_profile') == 'rust-v1',
                'Implementation requires an approved Rust-capable role/profile')
    require(config.get('tooling_version') == '0.3.0', 'Unexpected tooling version')
    return name


def verify_document_coverage(names, manifest):
    expected = {n for n in names if n in {'README.md', 'README.tr.md', 'CONTRIBUTING.md', 'CONTRIBUTING.tr.md'}
                or (n.endswith('.md') and (n.startswith(('docs/en/', 'docs/tr/', 'profile/'))
                    or n in {f'docs/{d}.md' for d in PRODUCT_DOCS}
                    or n.startswith('assets/eylema-logo/README')))}
    covered = {p[lang] for p in manifest.get('pairs', []) for lang in ('en', 'tr')}
    covered.update(d['path'] for d in manifest.get('documents', []))
    require(covered == expected, f'Translation coverage mismatch: missing={sorted(expected-covered)}, unexpected={sorted(covered-expected)}')


def verify_vendor(root, manifest):
    require(manifest.get('source_repository') == 'eyleme-dev/devguide'
            and manifest.get('tooling_version') == '0.3.0', 'Unknown shared tooling source')
    files = manifest.get('files', {})
    require(set(files) == SHARED_FILES, 'Incomplete or unexpected tooling inventory')
    for name, digest in files.items():
        path = root / name
        require(path.resolve().is_relative_to(root.resolve()) and path.is_file(), f'Invalid vendored path: {name}')
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest, f'Shared tooling copy changed: {name}')
    return len(files)


def validate_api(en, tr):
    pattern = r"^\| `([^`]+)` \| `(GET|POST) ([^`]+)` \|"
    operations = re.findall(pattern, en, re.M)
    require(bool(operations) and operations == re.findall(pattern, tr, re.M), "EN/TR API operation mismatch")
    require(set(re.findall(r"`([^`\n]+)`", en)) == set(re.findall(r"`([^`\n]+)`", tr)), "EN/TR API inline identifier mismatch")
    require(re.findall(r"^source_revision: (.+)$", en, re.M) == re.findall(r"^source_revision: (.+)$", tr, re.M), "API source revision mismatch")
    return len(operations)


def validate_source_stage(root, names, config):
    validate_repository_config(config)
    sources = [n for n in names if Path(n).suffix == '.rs' or Path(n).name in {'Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'rust-toolchain'}]
    if config['stage'] == 'documentation':
        require(not sources, 'Documentation profile cannot accept Rust/Cargo sources')
        return
    require({'Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml', 'tools/check_rust.py'} <= set(names)
            and any(n.endswith('.rs') for n in sources), 'Incomplete Rust implementation gates/workspace')
    from check_rust import validate_toolchain, validate_wasm_packages
    validate_toolchain(root)
    validate_wasm_packages(config)
    require(not any(n.startswith('.cargo/') for n in names), 'Cargo overrides require a reviewed profile update')


def validate_schema_ownership(repository, schemas):
    required = {'ai-team-protocol/request.schema.json', 'ai-team-protocol/model-set-release.schema.json'}
    require((repository == 'eyleme' and required <= set(schemas)
             and all(n.startswith('ai-team-protocol/') and '..' not in Path(n).parts for n in schemas))
            or (repository != 'eyleme' and not schemas), 'Machine schemas belong only in core ai-team-protocol')


def check(root, event=None):
    names = repository_files(root)
    config = load_json(root / 'repository.json')
    repository = validate_repository_config(config)
    from check_rust import validate_identity
    validate_identity(root, config)
    required = ['README.md', 'AGENTS.md', 'CONTRIBUTING.md', 'CONTRIBUTING.tr.md']
    if repository in {'eyleme-docs-en', 'eyleme-docs-tr'}:
        required += [f'docs/{d}.md' for d in PRODUCT_DOCS]
    else:
        required += ['README.tr.md']
    if repository == 'devguide':
        required += ['tools/repository-set.json', 'tools/check_workspace.py', 'records/document-migration.json']
    if repository == '.github':
        required += ['profile/README.md', 'profile/README.tr.md']
    require(set(required) <= set(names), f'Missing required repository files: {sorted(set(required)-set(names))}')
    for name in names:
        path = root / name
        if path.suffix in {".md", ".json", ".yml", ".yaml"}:
            text = path.read_text()
            require(not re.search(r"/Users/[^/\s]+/|/home/[^/\s]+/|file://|codex://", text), f"Private/nonportable machine path: {name}")
        if path.suffix == ".json":
            load_json(path)
        if path.suffix in {".yml", ".yaml"}:
            load_yaml(path)
        if path.suffix == ".md":
            errors = local_link_errors(root, name, path.read_text())
            require(not errors, "; ".join(errors))
    validate_source_stage(root, names, config)
    schemas = [n for n in names if n.endswith(".schema.json")]
    validate_schema_ownership(repository, schemas)
    for name in schemas:
        validate_schema(load_json(root / name))
    for name in names:
        if name.endswith(".example.json"):
            schema = root / name.replace(".example.json", ".schema.json")
            require(schema.is_file(), f"Missing schema for {name}")
            Draft202012Validator(load_json(schema)).validate(load_json(root / name))
    manifest = load_json(root / "tools/translation-pairs.json")
    pair_count = verify_translations(root, manifest)
    verify_document_coverage(names, manifest)
    if repository != 'devguide':
        verify_vendor(root, load_json(root / 'tools/vendor.json'))
    forms = [n for n in names if n.startswith(".github/ISSUE_TEMPLATE/") and n.endswith(".yml") and Path(n).name != "config.yml"]
    require(len(forms) == 3, "Expected feature, bug and architecture issue forms")
    for name in forms:
        validate_form(load_yaml(root / name))
    require(not validate_body((root / ".github/PULL_REQUEST_TEMPLATE.md").read_text(), allow_template=True), "PR template section contract is incomplete")
    workflows = [n for n in names if n.startswith(".github/workflows/")]
    require(workflows == [".github/workflows/ci.yml"], "New workflows require an explicit trust-boundary review and checker update")
    validate_workflow(load_yaml(root / workflows[0]))
    base = None
    if event:
        payload = load_json(event)
        base = payload.get("pull_request", {}).get("base", {}).get("sha") or payload.get("before")
        if base:
            require(bool(re.fullmatch(r"[0-9a-f]{40}", base)), "Invalid event base SHA")
            if base == "0" * 40:
                base = None
    command = ["git", "diff", "--check", base, "HEAD"] if base else ["git", "show", "--format=", "--check", "HEAD"]
    subprocess.run(command, cwd=root, check=True)
    subprocess.run(["git", "diff", "--check"], cwd=root, check=True)
    print(f"PASS: {repository}: {len(names)} files, {len(schemas)} schemas, {pair_count} local language records, {len(forms)} issue forms")
    print('Cross-repository API/roadmap/translation/link checks run separately in devguide against pinned member commits.')
    print("Scope: repository/static validation. Rust execution requires separate check_rust.py; GPU/model NOT RUN")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path)
    parser.add_argument("--write-translation-hashes", action="store_true")
    args = parser.parse_args()
    try:
        if args.write_translation_hashes:
            path = ROOT / "tools/translation-pairs.json"
            manifest = load_json(path)
            verify_translations(ROOT, manifest, write=True)
            path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            print("Updated translation fingerprints. This records reviewed bytes, not semantic proof.")
        else:
            check(ROOT, args.event)
    except (CheckFailure, ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"FAIL: {error}") from error


if __name__ == "__main__":
    main()
