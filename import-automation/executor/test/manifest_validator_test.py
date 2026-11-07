# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for manifest_validator.py."""

import contextlib
import dataclasses
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from app.executor import cloud_batch
from app.executor import cloud_run_simple_import
from app.executor import import_target
from app.executor import manifest_validator
from app.executor.manifest_validator import ImportSpec
from app.executor.manifest_validator import ManifestChecks
from app.executor.manifest_validator import ManifestError
from app.executor.manifest_validator import ManifestValidator

_IMPORT_DIR = 'statvar_imports/who/tb'
_MANIFEST = f'{_IMPORT_DIR}/manifest.json'
# Folder used as the import folder in tests that don't touch the disk.
_ABS_IMPORT_DIR = f'/repo/{_IMPORT_DIR}'


def _valid_spec(**overrides):
    """Returns a valid import specification with the given fields replaced.

    A field set to None is removed.
    """
    spec = {
        'import_name': 'WHO_TB',
        'curator_emails': ['support@datacommons.org'],
        'provenance_url': 'https://www.who.int/',
        'provenance_description': 'WHO tuberculosis data',
        'scripts': ['download.py', 'process.sh --mode=full'],
        'import_inputs': [{
            'template_mcf': 'output/tb.tmcf',
            'cleaned_csv': 'output/tb.csv',
        }],
        'cron_schedule': '30 10 * * 0',
    }
    spec.update(overrides)
    return {k: v for k, v in spec.items() if v is not None}


def _spec(abs_import_dir=_ABS_IMPORT_DIR, **overrides):
    """Returns an ImportSpec for a valid specification with overrides."""
    return ImportSpec(_MANIFEST, 'WHO_TB', abs_import_dir,
                      _valid_spec(**overrides))


# Tests for ManifestChecks. These call the checks directly, without files.


class ManifestLevelChecksTest(unittest.TestCase):

    def setUp(self):
        self.checks = ManifestChecks()

    def test_valid_folder_path_is_accepted(self):
        self.assertEqual(self.checks.folder_path_chars(_MANIFEST, {}), [])

    def test_folder_path_with_invalid_characters_is_reported(self):
        self.assertEqual(
            self.checks.folder_path_chars(
                'statvar_imports/who data/manifest.json', {}), [
                    'folder path "statvar_imports/who data" may only contain '
                    'letters, digits, "_", "-" and "/"'
                ])

    def test_only_import_specifications_at_top_level_is_accepted(self):
        manifest = {'import_specifications': [_valid_spec()]}
        self.assertEqual(self.checks.top_level_fields(_MANIFEST, manifest), [])

    def test_unknown_top_level_field_is_reported(self):
        manifest = {
            'import_specifications': [_valid_spec()],
            'config_override': {
                'invoke_differ_tool': False
            },
        }
        self.assertEqual(self.checks.top_level_fields(_MANIFEST, manifest), [
            'unknown top-level field "config_override"; only '
            '"import_specifications" is allowed at the top level'
        ])


class FieldChecksTest(unittest.TestCase):

    def setUp(self):
        self.checks = ManifestChecks()

    def test_valid_spec_has_no_unknown_fields(self):
        self.assertEqual(self.checks.unknown_fields(_spec()), [])

    def test_unknown_field_is_reported(self):
        self.assertEqual(
            self.checks.unknown_fields(_spec(source_file=['input/*.csv'])),
            ['unknown field "source_file"'])

    def test_comment_field_is_allowed(self):
        self.assertEqual(
            self.checks.unknown_fields(_spec(_comment='Weekly on Sunday')), [])

    def test_valid_spec_has_all_required_fields(self):
        self.assertEqual(self.checks.required_fields(_spec()), [])

    def test_missing_required_field_is_reported(self):
        self.assertEqual(
            self.checks.required_fields(_spec(provenance_description=None)),
            ['required field "provenance_description" is missing or empty'])

    def test_empty_required_list_is_reported(self):
        self.assertEqual(self.checks.required_fields(_spec(scripts=[])),
                         ['required field "scripts" is missing or empty'])

    def test_curator_emails_as_string_is_reported(self):
        self.assertEqual(
            self.checks.curator_emails_type(
                _spec(curator_emails='support@datacommons.org')),
            ['"curator_emails" must be a list of strings'])

    def test_curator_email_format_is_not_checked(self):
        self.assertEqual(
            self.checks.curator_emails_type(
                _spec(curator_emails=['Data Commons team'])), [])

    def test_wrong_basic_types_are_reported(self):
        spec = _spec(provenance_url=42,
                     source_files='input.csv',
                     user_script_timeout=0)
        self.assertEqual(self.checks.field_types(spec), [
            '"provenance_url" must be a string',
            '"source_files" must be a list of strings',
            '"user_script_timeout" must be a positive number',
        ])

    def test_optional_fields_with_correct_types_are_accepted(self):
        spec = _spec(image='gcr.io/datcom-ci/image:stable',
                     data_download_url=['https://example.com/data.csv'],
                     source_files=['input/*.csv'],
                     user_script_timeout=3600)
        self.assertEqual(self.checks.field_types(spec), [])

    def test_import_name_with_invalid_characters_is_reported(self):
        self.assertEqual(
            self.checks.import_name_chars(_spec(import_name='WHO:TB')), [
                '"import_name" "WHO:TB" may only contain letters, digits, "_" '
                'and "-"'
            ])

    def test_import_name_with_dash_and_underscore_is_accepted(self):
        self.assertEqual(
            self.checks.import_name_chars(_spec(import_name='WHO_TB-2024')), [])


class ScriptChecksTest(unittest.TestCase):

    def setUp(self):
        self.checks = ManifestChecks()

    def test_unclosed_quote_is_reported(self):
        messages = self.checks.script_syntax(
            _spec(scripts=["process.py --pv_map='pvmap.csv"]))

        self.assertEqual(len(messages), 1)
        self.assertIn('cannot parse script entry', messages[0])

    def test_quoted_arguments_are_accepted(self):
        spec = _spec(scripts=[
            "../../tools/statvar_importer/stat_var_processor.py "
            "--input_data='input/*.csv' --pv_map=pvmap.csv"
        ])
        self.assertEqual(self.checks.script_syntax(spec), [])

    def test_empty_script_entry_is_reported(self):
        self.assertEqual(
            self.checks.script_syntax(_spec(scripts=['download.py', ' '])),
            ['script entry is empty'])

    def test_python_and_shell_scripts_are_accepted(self):
        self.assertEqual(self.checks.script_type(_spec()), [])

    def test_unsupported_script_extension_is_reported(self):
        self.assertEqual(
            self.checks.script_type(
                _spec(scripts=['download.pyy --year=2024'])),
            [
                'script "download.pyy" must end in .py or .sh (or be a simple '
                'import config)'
            ])

    def test_json_script_outside_simple_imports_is_reported(self):
        self.assertEqual(
            self.checks.script_type(_spec(scripts=['import_config.json'])), [
                'script "import_config.json" must end in .py or .sh (or be a '
                'simple import config)'
            ])

    def test_simple_import_config_is_accepted(self):
        spec = _spec(abs_import_dir='/repo/simple_imports/sample',
                     scripts=['import_config.json'])
        self.assertEqual(self.checks.script_type(spec), [])


class CronScheduleCheckTest(unittest.TestCase):

    def setUp(self):
        self.checks = ManifestChecks()

    def test_missing_cron_schedule_is_accepted(self):
        self.assertEqual(self.checks.cron_schedule(_spec(cron_schedule=None)),
                         [])

    def test_cron_values_accepted_by_croniter_are_accepted(self):
        # croniter also accepts presets and an optional 6th (seconds) field.
        for schedule in ('0 5 * * 1', '@daily', '30 10 * * 0 15'):
            with self.subTest(schedule=schedule):
                self.assertEqual(
                    self.checks.cron_schedule(_spec(cron_schedule=schedule)),
                    [])

    def test_invalid_cron_schedule_is_reported(self):
        self.assertEqual(
            self.checks.cron_schedule(_spec(cron_schedule='60 5 * * *')),
            ['"cron_schedule" "60 5 * * *" is not a valid cron expression'])

    def test_non_string_cron_schedule_is_reported(self):
        self.assertEqual(self.checks.cron_schedule(_spec(cron_schedule=5)),
                         ['"cron_schedule" 5 is not a valid cron expression'])


class ResourceLimitsChecksTest(unittest.TestCase):

    def setUp(self):
        self.checks = ManifestChecks()

    def test_valid_resource_limits_are_accepted(self):
        spec = _spec(resource_limits={
            'cpu': 16,
            'memory': 128,
            'disk': 300,
            'machine': 'n2-highmem-16',
        })
        self.assertEqual(self.checks.resource_limits_types(spec), [])
        self.assertEqual(self.checks.resource_limits_size(spec), [])

    def test_partial_resource_limits_are_accepted(self):
        spec = _spec(resource_limits={'memory': 64})
        self.assertEqual(self.checks.resource_limits_types(spec), [])
        self.assertEqual(self.checks.resource_limits_size(spec), [])

    def test_unknown_resource_field_is_reported(self):
        spec = _spec(resource_limits={'cpu': 4, 'memroy': 16})
        self.assertEqual(self.checks.resource_limits_types(spec),
                         ['unknown field "resource_limits.memroy"'])

    def test_non_numeric_resource_values_are_reported(self):
        spec = _spec(resource_limits={'cpu': '4', 'disk': True, 'machine': 8})
        self.assertEqual(self.checks.resource_limits_types(spec), [
            '"resource_limits.cpu" must be a positive number',
            '"resource_limits.disk" must be a positive number',
            '"resource_limits.machine" must be a string',
        ])

    def test_resource_limits_must_be_object(self):
        spec = _spec(resource_limits=[8, 32])
        self.assertEqual(self.checks.resource_limits_types(spec),
                         ['"resource_limits" must be an object'])

    def test_largest_machine_size_is_accepted(self):
        # n2-highmem-64 (64 CPUs, 512 GB) is the largest machine type.
        spec = _spec(resource_limits={'cpu': 64, 'memory': 512})
        self.assertEqual(self.checks.resource_limits_size(spec), [])

    def test_request_larger_than_any_machine_is_reported(self):
        spec = _spec(resource_limits={'cpu': 96, 'memory': 1000})
        self.assertEqual(self.checks.resource_limits_size(spec), [
            '"resource_limits" (cpu=96 and memory=1000) is larger than the '
            'largest available machine type'
        ])

    def test_memory_alone_larger_than_any_machine_is_reported(self):
        spec = _spec(resource_limits={'memory': 1024})
        self.assertEqual(self.checks.resource_limits_size(spec), [
            '"resource_limits" (memory=1024) is larger than the largest '
            'available machine type'
        ])

    def test_size_is_not_checked_for_invalid_values(self):
        # Invalid values are reported by resource-limits-types instead.
        spec = _spec(resource_limits={'cpu': '1000'})
        self.assertEqual(self.checks.resource_limits_size(spec), [])


class ConfigOverrideCheckTest(unittest.TestCase):

    def setUp(self):
        self.checks = ManifestChecks()

    def test_valid_executor_config_overrides_are_accepted(self):
        spec = _spec(
            config_override={
                'invoke_differ_tool': False,
                'user_script_timeout': 3600,
                'import_tool_num_threads': 8,
                'cron_schedule_override': '0 5 * * 1',
                'user_script_env': {
                    'KEY': 'value'
                },
                'user_script_args': ['--year=2024'],
            })
        self.assertEqual(self.checks.config_override(spec), [])

    def test_unknown_setting_is_reported(self):
        spec = _spec(config_override={'invoke_difer_tool': False})
        self.assertEqual(
            self.checks.config_override(spec),
            ['"config_override.invoke_difer_tool" is not an executor setting'])

    def test_string_for_bool_setting_is_reported(self):
        spec = _spec(config_override={'invoke_differ_tool': 'false'})
        self.assertEqual(
            self.checks.config_override(spec),
            ['"config_override.invoke_differ_tool" must be of type bool'])

    def test_bool_for_int_setting_is_reported(self):
        spec = _spec(config_override={'import_tool_num_threads': True})
        self.assertEqual(
            self.checks.config_override(spec),
            ['"config_override.import_tool_num_threads" must be of type int'])

    def test_non_string_list_item_is_reported(self):
        spec = _spec(config_override={'user_script_args': ['--year', 2024]})

        messages = self.checks.config_override(spec)

        self.assertEqual(len(messages), 1)
        self.assertIn('"config_override.user_script_args" must be of type',
                      messages[0])

    def test_config_override_must_be_object(self):
        spec = _spec(config_override=['invoke_differ_tool'])
        self.assertEqual(self.checks.config_override(spec),
                         ['"config_override" must be an object'])

    def test_uses_injected_executor_settings(self):
        checks = ManifestChecks(executor_setting_types={'dry_run': bool})

        self.assertEqual(
            checks.config_override(_spec(config_override={'dry_run': True})),
            [])
        self.assertEqual(
            checks.config_override(
                _spec(config_override={'invoke_differ_tool': False})),
            ['"config_override.invoke_differ_tool" is not an executor setting'])


class ValidationConfigFileCheckTest(unittest.TestCase):

    def test_existing_file_is_accepted_and_resolved_against_import_dir(self):
        checked_paths = []

        def file_exists(path):
            checked_paths.append(path)
            return True

        checks = ManifestChecks(file_exists=file_exists)

        messages = checks.validation_config_file(
            _spec(validation_config_file='../validation_config.json'))

        self.assertEqual(messages, [])
        self.assertEqual(checked_paths,
                         [f'{_ABS_IMPORT_DIR}/../validation_config.json'])

    def test_missing_file_is_reported(self):
        checks = ManifestChecks(file_exists=lambda path: False)
        self.assertEqual(
            checks.validation_config_file(
                _spec(validation_config_file='validation_config.json')),
            [
                '"validation_config_file" "validation_config.json" does not '
                'exist'
            ])

    def test_non_string_value_is_reported(self):
        checks = ManifestChecks(file_exists=lambda path: True)
        self.assertEqual(
            checks.validation_config_file(_spec(validation_config_file=5)),
            ['"validation_config_file" must be a string'])

    def test_default_rejects_a_folder(self):
        # By default, the check uses os.path.isfile, so folders don't count.
        with tempfile.TemporaryDirectory() as import_dir:
            os.makedirs(os.path.join(import_dir, 'configs'))
            spec = _spec(abs_import_dir=import_dir,
                         validation_config_file='configs')

            messages = ManifestChecks().validation_config_file(spec)

        self.assertEqual(messages,
                         ['"validation_config_file" "configs" does not exist'])


class ImportInputsCheckTest(unittest.TestCase):

    def setUp(self):
        self.checks = ManifestChecks()

    def test_any_key_name_and_list_values_are_accepted(self):
        spec = _spec(import_inputs=[{
            'template_mcf': 'output/tb.tmcf',
            'cleaned_csv': ['output/tb_1.csv', 'output/tb_2.csv'],
            'stat_var_mcf': 'output/tb_stat_vars.mcf',
        }])
        self.assertEqual(self.checks.import_inputs(spec), [])

    def test_import_inputs_must_be_list(self):
        spec = _spec(import_inputs={'cleaned_csv': 'output/tb.csv'})
        self.assertEqual(self.checks.import_inputs(spec),
                         ['"import_inputs" must be a list'])

    def test_non_object_item_is_reported(self):
        spec = _spec(import_inputs=['output/tb.csv'])
        self.assertEqual(self.checks.import_inputs(spec),
                         ['"import_inputs[0]" must be an object'])

    def test_non_string_value_is_reported(self):
        spec = _spec(import_inputs=[{'cleaned_csv': 5}])
        self.assertEqual(self.checks.import_inputs(spec), [
            '"import_inputs[0].cleaned_csv" must be a string or a list of '
            'strings'
        ])


class UniqueImportNamesCheckTest(unittest.TestCase):

    def setUp(self):
        self.checks = ManifestChecks()

    def test_unique_names_are_accepted(self):
        manifests = {
            'scripts/a/manifest.json': {
                'import_specifications': [_valid_spec(import_name='A')]
            },
            'scripts/b/manifest.json': {
                'import_specifications': [_valid_spec(import_name='B')]
            },
        }
        self.assertEqual(self.checks.unique_import_names(manifests), [])

    def test_duplicate_across_manifests_is_reported_on_each(self):
        manifests = {
            'scripts/a/manifest.json': {
                'import_specifications': [_valid_spec()]
            },
            'scripts/b/manifest.json': {
                'import_specifications': [_valid_spec()]
            },
        }
        self.assertEqual(self.checks.unique_import_names(manifests), [
            ManifestError(
                'scripts/a/manifest.json', 'WHO_TB',
                'duplicate "import_name": also used in '
                'scripts/b/manifest.json'),
            ManifestError(
                'scripts/b/manifest.json', 'WHO_TB',
                'duplicate "import_name": also used in '
                'scripts/a/manifest.json'),
        ])

    def test_duplicate_within_one_manifest_is_reported(self):
        manifests = {
            _MANIFEST: {
                'import_specifications': [_valid_spec(),
                                          _valid_spec()]
            }
        }
        self.assertEqual(self.checks.unique_import_names(manifests), [
            ManifestError(
                _MANIFEST, 'WHO_TB',
                'duplicate "import_name": used 2 times in this manifest')
        ])


# Tests for ManifestValidator. These use manifest files in a temporary repo.


class ManifestValidatorTestBase(unittest.TestCase):

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = self._tmp_dir.name

    def tearDown(self):
        self._tmp_dir.cleanup()

    def write_file(self, path, content=''):
        abs_path = os.path.join(self.repo_dir, path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(content)

    def write_manifest(self, manifest, path=_MANIFEST):
        self.write_file(path, json.dumps(manifest))
        return path

    def write_spec(self, path=_MANIFEST, **overrides):
        return self.write_manifest(
            {'import_specifications': [_valid_spec(**overrides)]}, path)

    def validate_manifest(self, path=_MANIFEST):
        return ManifestValidator(self.repo_dir).validate_manifest(path)

    def messages(self, path=_MANIFEST):
        return [error.message for error in self.validate_manifest(path)]


class ValidateManifestTest(ManifestValidatorTestBase):

    def test_valid_manifest_has_no_errors(self):
        self.write_spec()
        self.assertEqual(self.messages(), [])

    def test_invalid_json_is_reported(self):
        self.write_file(_MANIFEST, '{"import_specifications": [}')

        errors = self.validate_manifest()

        self.assertEqual(len(errors), 1)
        self.assertIsNone(errors[0].import_name)
        self.assertEqual(errors[0].check, 'valid-json')
        self.assertIn('invalid JSON', errors[0].message)

    def test_manifest_must_be_object(self):
        self.write_manifest([_valid_spec()])
        self.assertEqual(self.validate_manifest(), [
            ManifestError(_MANIFEST, None, 'manifest must be a JSON object',
                          'manifest-structure')
        ])

    def test_missing_import_specifications_is_reported(self):
        self.write_manifest({})
        self.assertEqual(self.messages(),
                         ['"import_specifications" must be a non-empty list'])

    def test_empty_import_specifications_is_reported(self):
        self.write_manifest({'import_specifications': []})
        self.assertEqual(self.messages(),
                         ['"import_specifications" must be a non-empty list'])

    def test_spec_must_be_object(self):
        self.write_manifest({'import_specifications': ['WHO_TB']})
        self.assertEqual(self.validate_manifest(), [
            ManifestError(_MANIFEST, 'import_specifications[0]',
                          'import specification must be a JSON object',
                          'manifest-structure')
        ])

    def test_manifest_check_error_has_check_id(self):
        self.write_manifest({
            'import_specifications': [_valid_spec()],
            'import_name': 'WHO_TB',
        })
        self.assertEqual(self.validate_manifest(), [
            ManifestError(
                _MANIFEST, None, 'unknown top-level field "import_name"; only '
                '"import_specifications" is allowed at the top level',
                'top-level-fields')
        ])

    def test_import_check_error_has_import_name_and_check_id(self):
        self.write_spec(source_file=['input/*.csv'])
        self.assertEqual(self.validate_manifest(), [
            ManifestError(_MANIFEST, 'WHO_TB', 'unknown field "source_file"',
                          'unknown-fields')
        ])

    def test_spec_without_name_is_labelled_by_position(self):
        self.write_spec(import_name=None)
        self.assertEqual(self.validate_manifest(), [
            ManifestError(_MANIFEST, 'import_specifications[0]',
                          'required field "import_name" is missing or empty',
                          'required-fields')
        ])

    def test_uses_injected_checks(self):
        self.write_spec(validation_config_file='validation_config.json')
        checks = ManifestChecks(file_exists=lambda path: True)

        errors = ManifestValidator(self.repo_dir,
                                   checks).validate_manifest(_MANIFEST)

        self.assertEqual(errors, [])


class FindManifestsTest(ManifestValidatorTestBase):

    def test_finds_manifests_only_under_manifest_roots(self):
        self.write_spec(path='statvar_imports/a/manifest.json')
        self.write_spec(path='scripts/b/c/manifest.json')
        self.write_spec(path='simple_imports/d/manifest.json')
        self.write_spec(path='import-automation/executor/manifest.json')

        self.assertEqual(
            ManifestValidator(self.repo_dir).find_manifests(), [
                'scripts/b/c/manifest.json',
                'statvar_imports/a/manifest.json',
            ])

    def test_respects_maximum_depth(self):
        # find_manifests() searches at most 5 folders below the root folder.
        at_limit = 'scripts/1/2/3/4/5/manifest.json'
        too_deep = 'scripts/1/2/3/4/5/6/manifest.json'
        self.write_spec(path=at_limit)
        self.write_spec(path=too_deep)

        self.assertEqual(
            ManifestValidator(self.repo_dir).find_manifests(), [at_limit])

    def test_skips_hidden_folders(self):
        self.write_spec(path='scripts/.env/manifest.json')
        self.assertEqual(ManifestValidator(self.repo_dir).find_manifests(), [])


class ValidateTest(ManifestValidatorTestBase):

    def test_duplicate_import_name_is_reported_with_check_id(self):
        self.write_spec(path='scripts/a/manifest.json')
        self.write_spec(path='statvar_imports/b/manifest.json')

        errors = ManifestValidator(self.repo_dir).validate()

        self.assertEqual(errors, [
            ManifestError(
                'scripts/a/manifest.json', 'WHO_TB',
                'duplicate "import_name": also used in '
                'statvar_imports/b/manifest.json', 'unique-import-names'),
            ManifestError(
                'statvar_imports/b/manifest.json', 'WHO_TB',
                'duplicate "import_name": also used in '
                'scripts/a/manifest.json', 'unique-import-names'),
        ])

    def test_requested_manifest_reports_clash_with_unrequested_one(self):
        self.write_spec(path='scripts/a/manifest.json')
        self.write_spec(path='statvar_imports/b/manifest.json')

        errors = ManifestValidator(self.repo_dir).validate(
            [os.path.join(self.repo_dir, 'scripts/a/manifest.json')])

        self.assertEqual(errors, [
            ManifestError(
                'scripts/a/manifest.json', 'WHO_TB',
                'duplicate "import_name": also used in '
                'statvar_imports/b/manifest.json', 'unique-import-names')
        ])

    def test_errors_in_unrequested_manifests_are_not_reported(self):
        self.write_spec(path='scripts/a/manifest.json', import_name='A')
        self.write_spec(path='scripts/b/manifest.json',
                        import_name='B',
                        provenance_url=None)

        errors = ManifestValidator(self.repo_dir).validate(
            [os.path.join(self.repo_dir, 'scripts/a/manifest.json')])

        self.assertEqual(errors, [])

    def test_missing_requested_file_is_reported(self):
        errors = ManifestValidator(self.repo_dir).validate(
            [os.path.join(self.repo_dir, 'scripts/a/manifest.json')])

        self.assertEqual(
            errors,
            [ManifestError('scripts/a/manifest.json', None, 'file not found')])

    def test_requested_file_outside_repo_is_reported(self):
        with tempfile.TemporaryDirectory() as other_dir:
            outside = os.path.join(other_dir, 'manifest.json')

            errors = ManifestValidator(self.repo_dir).validate([outside])

        self.assertEqual(errors, [
            ManifestError(outside, None, 'manifest is outside the repository')
        ])


class CheckListsTest(ManifestValidatorTestBase):

    def test_every_check_method_is_listed(self):
        # A ManifestChecks method missing from the check lists would silently
        # never run. To turn a check off, set enabled=False instead of
        # removing it.
        methods = {
            name for name, value in vars(ManifestChecks).items()
            if callable(value) and not name.startswith('_')
        }
        listed = {
            check.method.__name__ for check in ManifestValidator.all_checks()
        }
        self.assertEqual(methods, listed)

    def test_disabled_check_does_not_run(self):
        self.write_spec(cron_schedule='60 5 * * *', source_file=['x'])

        with _import_check_disabled('cron-schedule'):
            messages = self.messages()

        self.assertEqual(messages, ['unknown field "source_file"'])

    def test_summary_lists_disabled_checks(self):
        self.write_spec()

        with _import_check_disabled('cron-schedule'):
            out = io.StringIO()
            manifest_validator.run(self.repo_dir,
                                   [os.path.join(self.repo_dir, _MANIFEST)],
                                   False, out, io.StringIO())

        self.assertEqual(
            out.getvalue(),
            'The manifest is valid. Disabled checks: cron-schedule.\n')

    def test_disabled_check_without_reason_is_rejected(self):
        with _import_check_disabled('cron-schedule', reason=''):
            with self.assertRaisesRegex(ValueError, 'needs a reason'):
                ManifestValidator(self.repo_dir)

    def test_duplicate_check_id_across_lists_is_rejected(self):
        checks = ManifestValidator.REPO_CHECKS + (manifest_validator.Check(
            'cron-schedule', ManifestChecks.cron_schedule, 'Duplicate.'),)

        with mock.patch.object(ManifestValidator, 'REPO_CHECKS', checks):
            with self.assertRaisesRegex(ValueError, 'Duplicate check ids'):
                ManifestValidator(self.repo_dir)


def _import_check_disabled(check_id, reason='test'):
    """Returns a context manager that disables one of the IMPORT_CHECKS."""
    checks = tuple(
        dataclasses.replace(check, enabled=False, reason=reason) if check.id ==
        check_id else check for check in ManifestValidator.IMPORT_CHECKS)
    return mock.patch.object(ManifestValidator, 'IMPORT_CHECKS', checks)


class ExecutorRulesInSyncTest(unittest.TestCase):
    """Checks that rules copied from other executor modules still match.

    manifest_validator.py keeps its own copies of these rules so that it does
    not depend on those modules. If one of these tests fails, update the
    constant in manifest_validator.py to match the executor.
    """

    def test_import_name_chars_match_import_target(self):
        self.assertEqual(manifest_validator._IMPORT_NAME_CHARS,
                         import_target._ALLOWED_RELATIVE_IMPORT_NAME_CHARS)

    def test_import_dir_chars_match_import_target(self):
        self.assertEqual(manifest_validator._IMPORT_DIR_CHARS,
                         import_target._ALLOWED_PATH_CHARS)

    def test_simple_import_rule_matches_cloud_run_simple_import(self):
        self.assertEqual(manifest_validator._SIMPLE_IMPORT_DIR,
                         cloud_run_simple_import._IMPORT_CONFIG_DIR)
        self.assertEqual(manifest_validator._SIMPLE_IMPORT_CONFIG_PREFIX,
                         cloud_run_simple_import._DEFAULT_CONFIG_PREFIX)

    def test_machine_types_match_cloud_batch(self):
        executor_machine_types = [(m['name'], m['cpus'], m['memory_gib'])
                                  for m in cloud_batch._GCE_MACHINE_TYPES]
        self.assertEqual(list(manifest_validator._GCE_MACHINE_TYPES),
                         executor_machine_types)


class CommandLineTest(ManifestValidatorTestBase):

    def run_cli(self, paths, validate_all=False):
        out, err = io.StringIO(), io.StringIO()
        exit_code = manifest_validator.run(self.repo_dir, paths, validate_all,
                                           out, err)
        return exit_code, out.getvalue(), err.getvalue()

    def test_all_valid_returns_zero(self):
        self.write_spec(path='scripts/a/manifest.json', import_name='A')
        self.write_spec(path='scripts/b/manifest.json', import_name='B')

        exit_code, out, _ = self.run_cli([], validate_all=True)

        self.assertEqual(exit_code, manifest_validator.EXIT_OK)
        self.assertEqual(out, 'All 2 manifests are valid.\n')

    def test_errors_return_one_with_summary(self):
        self.write_spec(path='scripts/a/manifest.json',
                        import_name='A',
                        source_file=['x'])
        self.write_spec(path='scripts/b/manifest.json', import_name='B')

        exit_code, out, _ = self.run_cli([], validate_all=True)

        self.assertEqual(exit_code, manifest_validator.EXIT_ERRORS)
        self.assertEqual(
            out, 'scripts/a/manifest.json [A]: unknown field "source_file" '
            '(unknown-fields)\n'
            '\n1 error in 1 of 2 manifests.\n')

    def test_single_valid_manifest(self):
        path = self.write_spec()

        exit_code, out, _ = self.run_cli([os.path.join(self.repo_dir, path)])

        self.assertEqual(exit_code, manifest_validator.EXIT_OK)
        self.assertEqual(out, 'The manifest is valid.\n')

    def test_paths_and_all_together_is_usage_error(self):
        path = self.write_spec()

        exit_code, _, err = self.run_cli([os.path.join(self.repo_dir, path)],
                                         validate_all=True)

        self.assertEqual(exit_code, manifest_validator.EXIT_USAGE)
        self.assertIn('--all', err)

    def test_no_paths_and_no_all_is_usage_error(self):
        exit_code, _, _ = self.run_cli([])
        self.assertEqual(exit_code, manifest_validator.EXIT_USAGE)

    def test_main_parses_paths_and_repo_dir(self):
        path = self.write_spec()

        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = manifest_validator.main([
                '--repo_dir', self.repo_dir,
                os.path.join(self.repo_dir, path)
            ])

        self.assertEqual(exit_code, manifest_validator.EXIT_OK)

    def test_main_parses_all_flag(self):
        self.write_spec(path='scripts/a/manifest.json', source_file=['x'])

        with contextlib.redirect_stdout(io.StringIO()):
            exit_code = manifest_validator.main(
                ['--repo_dir', self.repo_dir, '--all'])

        self.assertEqual(exit_code, manifest_validator.EXIT_ERRORS)


if __name__ == '__main__':
    unittest.main()
