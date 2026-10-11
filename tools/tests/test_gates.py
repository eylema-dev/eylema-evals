"""Failure-oriented checks; all model/release data below is synthetic."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from jsonschema import Draft202012Validator, ValidationError
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_pr import SECTIONS, validate_body
from check_repository import (ROOT, CheckFailure, load_json, load_yaml, local_link_errors,
                              validate_api, validate_form, validate_schema, validate_workflow,
                              verify_translations, validate_repository_config, verify_document_coverage, verify_vendor)


def body():
    return '\n\n'.join(f'## {s}\n\nSpecific command, environment and observed result.' for s in SECTIONS)


class PRTests(unittest.TestCase):
    def test_populated_body_passes(self):
        self.assertEqual(validate_body(body()), [])

    def test_blank_template_fails_actual_gate(self):
        text = (ROOT / '.github/PULL_REQUEST_TEMPLATE.md').read_text()
        self.assertEqual(validate_body(text, allow_template=True), [])
        self.assertEqual(len(validate_body(text)), len(SECTIONS))

    def test_missing_duplicate_and_placeholder_sections_fail(self):
        for text in [body().replace('## Need', '## Other'),
                     body() + '\n## Need\nDuplicate need details.',
                     body().replace('Specific command, environment and observed result.', 'TODO: write actual evidence later')]:
            with self.subTest(body=text): self.assertTrue(validate_body(text))
        self.assertTrue(validate_body(None))

    def test_comments_and_fenced_headings_do_not_count(self):
        for text in ['<!--' + body() + '-->', '```md\n' + body() + '\n```', '~~~md\n' + body() + '\n~~~']:
            with self.subTest(body=text): self.assertEqual(len(validate_body(text)), len(SECTIONS))

    def test_shell_payload_is_only_data(self):
        self.assertEqual(validate_body(body() + '\nLiteral example: $(exit 99); `touch /tmp/do-not-run`'), [])

    def test_bilingual_heading_suffixes_do_not_replace_english_sections(self):
        old_style = '\n\n'.join(f'## {section} / Translated heading\n\nSpecific evidence for the task.' for section in SECTIONS)
        self.assertEqual(len(validate_body(old_style)), len(SECTIONS))


class RepositoryTests(unittest.TestCase):
    def test_portable_and_nonportable_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'My File.md').write_text('# Target\n')
            self.assertEqual(local_link_errors(root, 'README.md', '[ok](My%20File.md) [web](https://example.org)\n```md\n[example](absent.md)\n```\n'), [])
            for value in ['missing.md', '../outside.md', '/tmp/file.md', 'file:///tmp/file.md', '%2e%2e/outside.md']:
                with self.subTest(target=value):
                    self.assertTrue(local_link_errors(root, 'README.md', f'[bad]({value})'))

    def test_duplicate_mapping_keys_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            for name, content, loader in [('bad.json', '{"a":1,"a":2}', load_json), ('bad.yml', 'a: 1\na: 2\n', load_yaml)]:
                p = Path(temp) / name
                p.write_text(content)
                with self.assertRaises(CheckFailure): loader(p)

    def test_actions_yaml_on_key_and_boolean(self):
        workflow = load_yaml(ROOT / '.github/workflows/ci.yml')
        self.assertIn('on', workflow)
        self.assertIs(workflow['jobs']['repository-checks']['steps'][0]['with']['persist-credentials'], False)
        validate_workflow(workflow)

    def test_privileged_workflow_variants_fail(self):
        original = load_yaml(ROOT / '.github/workflows/ci.yml')
        variants = []
        x = copy.deepcopy(original); x['on']['pull_request_target'] = {}; variants.append(x)
        x = copy.deepcopy(original); x['permissions']['contents'] = 'write'; variants.append(x)
        x = copy.deepcopy(original); x['jobs']['repository-checks']['steps'][0]['uses'] = 'actions/checkout@main'; variants.append(x)
        x = copy.deepcopy(original); x['jobs']['repository-checks']['steps'][0]['with']['persist-credentials'] = True; variants.append(x)
        x = copy.deepcopy(original); x['jobs']['repository-checks']['steps'] = [step for step in x['jobs']['repository-checks']['steps'] if 'tools/check_workspace.py' not in step.get('run', '')]; variants.append(x)
        for value in variants:
            with self.subTest(value=value):
                with self.assertRaises(CheckFailure): validate_workflow(value)

    def test_issue_acceptance_and_unique_ids(self):
        original = load_yaml(ROOT / '.github/ISSUE_TEMPLATE/feature.yml')
        validate_form(original)
        missing = copy.deepcopy(original)
        missing['body'] = [x for x in missing['body'] if x.get('id') != 'acceptance']
        with self.assertRaises(CheckFailure): validate_form(missing)
        original['body'].append(original['body'][1])
        with self.assertRaises(CheckFailure): validate_form(original)

    def test_translation_edit_invalidates_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'en.md').write_text('English')
            (root / 'tr.md').write_text('Translated content')
            manifest = {'pairs': [{'en': 'en.md', 'tr': 'tr.md'}]}
            verify_translations(root, manifest, write=True)
            self.assertEqual(verify_translations(root, manifest), 1)
            (root / 'en.md').write_text('Changed')
            with self.assertRaises(CheckFailure): verify_translations(root, manifest)

    def test_api_method_drift_fails(self):
        en = 'source_revision: 0.1.0\n| `getTask` | `GET /tasks/{task_id}` | Read |\n'
        tr = en.replace('Read', 'Oku')
        self.assertEqual(validate_api(en, tr), 1)
        with self.assertRaises(CheckFailure): validate_api(en, tr.replace('`GET ', '`POST ', 1))

    def test_single_language_edit_invalidates_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'doc.md').write_text('Reviewed English')
            manifest = {'documents': [{'path': 'doc.md', 'language': 'en'}]}
            verify_translations(root, manifest, write=True)
            verify_translations(root, manifest)
            (root / 'doc.md').write_text('Unreviewed change')
            with self.assertRaises(CheckFailure): verify_translations(root, manifest)
            manifest['documents'][0]['path'] = '../outside.md'
            with self.assertRaises(CheckFailure): verify_translations(root, manifest, write=True)

    def test_missing_maintained_document_coverage_fails(self):
        manifest = {'documents': [{'path': 'README.md', 'language': 'en'}]}
        verify_document_coverage(['README.md'], manifest)
        with self.assertRaises(CheckFailure):
            verify_document_coverage(['README.md', 'docs/architecture.md'], manifest)

    def test_repository_role_and_stage_cannot_be_relaxed(self):
        config = {'format_version': 1, 'repository': 'eylema-dev/eylema', 'role': 'core',
                  'stage': 'documentation', 'runtime_implemented': False, 'tooling_version': '0.3.0', 'gate_profile': 'documentation-v1'}
        self.assertEqual(validate_repository_config(config), 'eylema')
        for key, value in [('repository', 'other/eylema'), ('role', 'reserved'), ('stage', 'runtime'), ('runtime_implemented', True)]:
            with self.subTest(key=key):
                bad = dict(config, **{key: value})
                with self.assertRaises(CheckFailure): validate_repository_config(bad)

    def test_shared_tool_copy_drift_fails(self):
        import hashlib
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            names = ['tools/check_repository.py', 'tools/check_pr.py', 'tools/requirements-ci.txt',
                     'tools/tests/test_gates.py', 'tools/check_rust.py', 'tools/tests/test_rust_gates.py', '.github/PULL_REQUEST_TEMPLATE.md',
                     '.github/CODEOWNERS', '.github/copilot-instructions.md', '.github/workflows/ci.yml',
                     '.github/ISSUE_TEMPLATE/config.yml', '.github/ISSUE_TEMPLATE/feature.yml',
                     '.github/ISSUE_TEMPLATE/bug.yml', '.github/ISSUE_TEMPLATE/architecture.yml']
            manifest = {'source_repository': 'eylema-dev/devguide', 'tooling_version': '0.3.0', 'files': {}}
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('reviewed bytes')
                manifest['files'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
            verify_vendor(root, manifest)
            incomplete = copy.deepcopy(manifest)
            del incomplete['files']['.github/workflows/ci.yml']
            with self.assertRaises(CheckFailure): verify_vendor(root, incomplete)
            (root / names[0]).write_text('changed checker')
            with self.assertRaises(CheckFailure): verify_vendor(root, manifest)
