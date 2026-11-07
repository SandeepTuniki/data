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
"""Validates import manifest.json files.

Checks each manifest for structural problems (unknown or missing fields,
wrong value types), invalid settings (cron schedules, resource limits,
executor config overrides), missing validation config files and duplicate
import names across the repository.

The module is self-contained: the only executor code it uses is
configs.ExecutorConfig, which defines the settings that config_override may
change. A few rules that the executor also applies (allowed name characters,
simple import detection and machine types) are defined here as constants;
manifest_validator_test.py checks that they still match the executor.

The module has two main classes:
  - ManifestChecks: the core rules. Each method implements one check on data
    it is given; it does not find or read manifest files.
  - ManifestValidator: finds and reads manifests, runs the enabled checks and
    collects the errors.

The validator can be used programmatically, or from the command line (from
the repository root):

  # Validate specific manifests, e.g. while editing them:
  python import-automation/executor/app/executor/manifest_validator.py \\
      statvar_imports/who/adolescent_birth_rate/manifest.json

  # Validate all manifests under statvar_imports/ and scripts/:
  python import-automation/executor/app/executor/manifest_validator.py --all

Exit codes: 0 if all manifests are valid, 1 if errors were found, 2 for
invalid usage.
"""

import argparse
import collections
import dataclasses
import json
import os
import re
import shlex
import sys
import typing
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple

if __name__ == '__main__':
    # When run as a script, make the executor's 'app' package importable.
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from croniter import croniter

from app import configs

# The constants below mirror rules applied by other executor modules. Keep them
# in sync; manifest_validator_test.py fails if they differ.

# Characters allowed in import names and in manifest folder paths
# (import_target.py).
_IMPORT_NAME_CHARS = 'A-Za-z0-9-_'
_IMPORT_DIR_CHARS = 'A-Za-z0-9-_/'

# A .json script is run as a simple import if its path contains both of these
# strings (cloud_run_simple_import.py).
_SIMPLE_IMPORT_DIR = '/simple_imports/'
_SIMPLE_IMPORT_CONFIG_PREFIX = 'import_config'

# Machine types that Cloud Batch jobs can run on, as (name, CPUs, memory in
# GiB) tuples (cloud_batch.py).
_GCE_MACHINE_TYPES = (
    ('n2-standard-2', 2, 8),
    ('n2-standard-4', 4, 16),
    ('n2-standard-8', 8, 32),
    ('n2-standard-16', 16, 64),
    ('n2-standard-32', 32, 128),
    ('n2-standard-48', 48, 192),
    ('n2-standard-64', 64, 256),
    ('n2-highmem-2', 2, 16),
    ('n2-highmem-4', 4, 32),
    ('n2-highmem-8', 8, 64),
    ('n2-highmem-16', 16, 128),
    ('n2-highmem-32', 32, 256),
    ('n2-highmem-48', 48, 384),
    ('n2-highmem-64', 64, 512),
    ('n2-highcpu-2', 2, 2),
    ('n2-highcpu-4', 4, 4),
    ('n2-highcpu-8', 8, 8),
    ('n2-highcpu-16', 16, 16),
    ('n2-highcpu-32', 32, 32),
)


@dataclasses.dataclass(frozen=True)
class Check:
    """A validation check run by ManifestValidator.

    Attributes:
        id: Stable identifier, shown in error messages.
        method: The ManifestChecks method that implements the check.
        description: What the check verifies.
        enabled: Whether the check runs.
        reason: Why the check is disabled. Required if enabled is False.
    """
    id: str
    method: Callable[..., Any]
    description: str
    enabled: bool = True
    reason: str = ''


@dataclasses.dataclass(frozen=True, order=True)
class ManifestError:
    """A validation error found in a manifest.

    Attributes:
        manifest_path: Path to the manifest, relative to the repository root.
        import_name: Import the error belongs to, or None for errors that
            apply to the whole manifest. Imports without a valid name are
            identified by their position, e.g. 'import_specifications[1]'.
        message: Description of the problem.
        check: Id of the check that found the error, or None for problems
            with the requested path itself (e.g. file not found).
    """
    manifest_path: str
    import_name: Optional[str]
    message: str
    check: Optional[str] = None

    def __str__(self) -> str:
        location = self.manifest_path
        if self.import_name is not None:
            location += f' [{self.import_name}]'
        suffix = f' ({self.check})' if self.check else ''
        return f'{location}: {self.message}{suffix}'


@dataclasses.dataclass(frozen=True)
class ImportSpec:
    """One import specification inside a manifest.

    Attributes:
        manifest_path: Path to the manifest, relative to the repository root.
        label: The import_name, or the position of the specification (e.g.
            'import_specifications[1]') if it has no valid name.
        abs_import_dir: Absolute path to the folder containing the manifest.
            Relative paths in the specification are resolved against it.
        fields: The specification's JSON object.
    """
    manifest_path: str
    label: str
    abs_import_dir: str
    fields: Dict[str, Any]


class ManifestChecks:
    """The core manifest validation rules.

    Each public method implements one check and returns the problems it finds.
    The class does not find, read or walk manifest files; ManifestValidator
    does that and decides which checks to run. The only outside information
    the checks use is passed to the constructor, so tests can replace it.
    """

    def __init__(self,
                 executor_setting_types: Optional[Dict[str, Any]] = None,
                 file_exists: Callable[[str], bool] = os.path.isfile):
        """Initializes the checks.

        Args:
            executor_setting_types: Settings that config_override may change,
                mapped to their types. Defaults to the fields of
                configs.ExecutorConfig.
            file_exists: Returns whether a file exists at the given path. Used
                by the validation-config-file check.
        """
        if executor_setting_types is None:
            executor_setting_types = self._executor_config_setting_types()
        self._executor_setting_types = executor_setting_types
        self._file_exists = file_exists

    def _executor_config_setting_types(self) -> Dict[str, Any]:
        """Returns the fields of configs.ExecutorConfig, mapped to their types."""
        type_hints = typing.get_type_hints(configs.ExecutorConfig)
        return {
            field.name: type_hints[field.name]
            for field in dataclasses.fields(configs.ExecutorConfig)
        }

    # Manifest checks: (manifest_path, manifest) -> error messages.

    def folder_path_chars(self, manifest_path: str, manifest: Any) -> List[str]:
        del manifest  # Unused.
        import_dir = os.path.dirname(manifest_path)
        if not re.fullmatch(f'[{_IMPORT_DIR_CHARS}]+', import_dir):
            return [
                f'folder path "{import_dir}" may only contain letters, '
                'digits, "_", "-" and "/"'
            ]
        return []

    def top_level_fields(self, manifest_path: str, manifest: Any) -> List[str]:
        del manifest_path  # Unused.
        if not isinstance(manifest, dict):
            return []
        allowed_fields = {'import_specifications'}
        return [
            f'unknown top-level field "{field}"; only '
            '"import_specifications" is allowed at the top level'
            for field in sorted(set(manifest) - allowed_fields)
        ]

    # Import checks: (ImportSpec) -> error messages.

    def unknown_fields(self, spec: ImportSpec) -> List[str]:
        # Fields that the import automation code reads, plus '_comment' for
        # human-readable notes.
        allowed_fields = {
            '_comment',
            'config_override',
            'cron_schedule',
            'curator_emails',
            'data_download_url',
            'image',
            'import_inputs',
            'import_name',
            'provenance_description',
            'provenance_url',
            'resource_limits',
            'scripts',
            'source_files',
            'user_script_timeout',
            'validation_config_file',
        }
        return [
            f'unknown field "{field}"'
            for field in sorted(set(spec.fields) - allowed_fields)
        ]

    def required_fields(self, spec: ImportSpec) -> List[str]:
        required_fields = ('import_name', 'curator_emails', 'provenance_url',
                           'provenance_description', 'scripts', 'import_inputs')
        return [
            f'required field "{field}" is missing or empty'
            for field in required_fields
            if self._is_empty(spec.fields.get(field))
        ]

    def _is_empty(self, value: Any) -> bool:
        return value is None or value == '' or value == [] or value == {}

    def curator_emails_type(self, spec: ImportSpec) -> List[str]:
        if 'curator_emails' in spec.fields and not self._is_string_list(
                spec.fields['curator_emails']):
            return ['"curator_emails" must be a list of strings']
        return []

    def field_types(self, spec: ImportSpec) -> List[str]:
        string_fields = ('import_name', 'provenance_url',
                         'provenance_description', 'image')
        string_list_fields = ('scripts', 'source_files', 'data_download_url')
        fields = spec.fields
        messages = []
        for field in string_fields:
            if field in fields and not isinstance(fields[field], str):
                messages.append(f'"{field}" must be a string')
        for field in string_list_fields:
            if field in fields and not self._is_string_list(fields[field]):
                messages.append(f'"{field}" must be a list of strings')
        if 'user_script_timeout' in fields and not self._is_positive_number(
                fields['user_script_timeout']):
            messages.append('"user_script_timeout" must be a positive number')
        return messages

    def import_name_chars(self, spec: ImportSpec) -> List[str]:
        name = spec.fields.get('import_name')
        if not isinstance(name, str) or not name:
            return []
        if not re.fullmatch(f'[{_IMPORT_NAME_CHARS}]+', name):
            return [
                f'"import_name" "{name}" may only contain letters, digits, '
                '"_" and "-"'
            ]
        return []

    def script_syntax(self, spec: ImportSpec) -> List[str]:
        messages = []
        for entry in self._script_entries(spec):
            if not entry.strip():
                messages.append('script entry is empty')
                continue
            try:
                shlex.split(entry)
            except ValueError as e:
                messages.append(f'cannot parse script entry "{entry}": {e}')
        return messages

    def script_type(self, spec: ImportSpec) -> List[str]:
        script_extensions = ('.py', '.sh')
        simple_import_extension = '.json'
        messages = []
        for entry in self._script_entries(spec):
            if not entry.strip():
                continue
            # The executor picks the interpreter from the first space-separated
            # word of the entry.
            script = entry.split(' ')[0]
            if script.endswith(script_extensions):
                continue
            if script.endswith(simple_import_extension) and (
                    self._is_simple_import_config(
                        os.path.join(spec.abs_import_dir, script))):
                continue
            messages.append(
                f'script "{script}" must end in .py or .sh (or be a simple '
                'import config)')
        return messages

    def _is_simple_import_config(self, script_path: str) -> bool:
        """Returns True if the executor runs script_path as a simple import."""
        return (_SIMPLE_IMPORT_DIR in script_path and
                _SIMPLE_IMPORT_CONFIG_PREFIX in script_path)

    def cron_schedule(self, spec: ImportSpec) -> List[str]:
        if 'cron_schedule' not in spec.fields:
            return []
        schedule = spec.fields['cron_schedule']
        if not isinstance(schedule, str) or not croniter.is_valid(schedule):
            return [
                f'"cron_schedule" {json.dumps(schedule)} is not a valid cron '
                'expression'
            ]
        return []

    def resource_limits_types(self, spec: ImportSpec) -> List[str]:
        if 'resource_limits' not in spec.fields:
            return []
        limits = spec.fields['resource_limits']
        if not isinstance(limits, dict):
            return ['"resource_limits" must be an object']
        number_fields = ('cpu', 'memory', 'disk')
        allowed_fields = set(number_fields) | {'machine'}
        messages = [
            f'unknown field "resource_limits.{field}"'
            for field in sorted(set(limits) - allowed_fields)
        ]
        for field in number_fields:
            if field in limits and not self._is_positive_number(limits[field]):
                messages.append(f'"resource_limits.{field}" must be a positive '
                                'number')
        if 'machine' in limits and not isinstance(limits['machine'], str):
            messages.append('"resource_limits.machine" must be a string')
        return messages

    def resource_limits_size(self, spec: ImportSpec) -> List[str]:
        limits = spec.fields.get('resource_limits')
        if not isinstance(limits, dict):
            return []
        # Missing cpu or memory are filled in with defaults by the executor, so
        # only the values that are set need to fit on a machine type.
        requested = {
            field: limits[field]
            for field in ('cpu', 'memory')
            if field in limits
        }
        if not requested or not all(
                self._is_positive_number(v) for v in requested.values()):
            return []
        if self._fits_some_machine(requested.get('cpu', 0),
                                   requested.get('memory', 0)):
            return []
        described = ' and '.join(
            f'{field}={value}' for field, value in requested.items())
        return [
            f'"resource_limits" ({described}) is larger than the largest '
            'available machine type'
        ]

    def _fits_some_machine(self, cpu: float, memory_gib: float) -> bool:
        """Returns True if some machine type has at least this CPU and memory."""
        return any(
            machine_cpus >= cpu and machine_memory_gib >= memory_gib
            for _, machine_cpus, machine_memory_gib in _GCE_MACHINE_TYPES)

    def config_override(self, spec: ImportSpec) -> List[str]:
        if 'config_override' not in spec.fields:
            return []
        override = spec.fields['config_override']
        if not isinstance(override, dict):
            return ['"config_override" must be an object']
        messages = []
        for key in sorted(override):
            expected_type = self._executor_setting_types.get(key)
            if expected_type is None:
                messages.append(f'"config_override.{key}" is not an executor '
                                'setting')
            elif not self._matches_type(override[key], expected_type):
                messages.append(f'"config_override.{key}" must be of type '
                                f'{self._type_name(expected_type)}')
        return messages

    def _matches_type(self, value: Any, expected_type: Any) -> bool:
        """Returns True if value is a JSON value of the given Python type."""
        if expected_type is bool:
            return isinstance(value, bool)
        if expected_type is int:
            return isinstance(value, int) and not isinstance(value, bool)
        if expected_type is float:
            return isinstance(value,
                              (int, float)) and not isinstance(value, bool)
        if expected_type in (str, dict, list):
            return isinstance(value, expected_type)
        if typing.get_origin(expected_type) is list:
            (item_type,) = typing.get_args(expected_type) or (Any,)
            return isinstance(value, list) and all(
                item_type is Any or self._matches_type(item, item_type)
                for item in value)
        # Types not used by ExecutorConfig today are not checked.
        return True

    def _type_name(self, expected_type: Any) -> str:
        return getattr(expected_type, '__name__', str(expected_type))

    def validation_config_file(self, spec: ImportSpec) -> List[str]:
        if 'validation_config_file' not in spec.fields:
            return []
        path = spec.fields['validation_config_file']
        if not isinstance(path, str):
            return ['"validation_config_file" must be a string']
        if not self._file_exists(os.path.join(spec.abs_import_dir, path)):
            return [f'"validation_config_file" "{path}" does not exist']
        return []

    def import_inputs(self, spec: ImportSpec) -> List[str]:
        if 'import_inputs' not in spec.fields:
            return []
        inputs = spec.fields['import_inputs']
        if not isinstance(inputs, list):
            return ['"import_inputs" must be a list']
        messages = []
        for index, item in enumerate(inputs):
            if not isinstance(item, dict):
                messages.append(f'"import_inputs[{index}]" must be an object')
                continue
            for key in sorted(item):
                value = item[key]
                if not isinstance(value,
                                  str) and not self._is_string_list(value):
                    messages.append(f'"import_inputs[{index}].{key}" must be '
                                    'a string or a list of strings')
        return messages

    # Repository checks: (manifests by path) -> ManifestErrors.

    def unique_import_names(self, manifests: Dict[str,
                                                  Any]) -> List[ManifestError]:
        locations = collections.defaultdict(list)
        for path, manifest in manifests.items():
            if not isinstance(manifest, dict):
                continue
            specs = manifest.get('import_specifications')
            if not isinstance(specs, list):
                continue
            for fields in specs:
                if isinstance(fields, dict):
                    name = fields.get('import_name')
                    if isinstance(name, str) and name:
                        locations[name].append(path)

        errors = []
        for name, paths in locations.items():
            if len(paths) < 2:
                continue
            for path in sorted(set(paths)):
                others = sorted(set(p for p in paths if p != path))
                parts = []
                if paths.count(path) > 1:
                    parts.append(
                        f'used {paths.count(path)} times in this manifest')
                if others:
                    parts.append(f'also used in {", ".join(others)}')
                errors.append(
                    ManifestError(
                        path, name,
                        f'duplicate "import_name": {"; ".join(parts)}'))
        return errors

    # Helpers shared by several checks.

    def _script_entries(self, spec: ImportSpec) -> List[str]:
        """Returns the string entries of the spec's scripts list."""
        scripts = spec.fields.get('scripts')
        if not isinstance(scripts, list):
            return []
        return [entry for entry in scripts if isinstance(entry, str)]

    def _is_string_list(self, value: Any) -> bool:
        return isinstance(value, list) and all(
            isinstance(v, str) for v in value)

    def _is_positive_number(self, value: Any) -> bool:
        return (isinstance(value, (int, float)) and
                not isinstance(value, bool) and value > 0)


class ManifestValidator:
    """Validates the manifests of a data repository.

    Example:
        validator = ManifestValidator('/path/to/data')
        for error in validator.validate():
            print(error)
    """

    # The checks the validator runs, grouped by what they look at. Within a
    # group, checks run in the order listed. Two checks always run first and
    # cannot be disabled: 'valid-json' (the file is valid JSON) and
    # 'manifest-structure' (it is an object with a non-empty
    # "import_specifications" list of objects).
    #
    # To turn a check off temporarily, set enabled=False and give a reason,
    # e.g. reason='b/123456: too noisy until the cleanup lands'.

    # yapf: disable
    # Checks on a manifest file as a whole, run once per manifest.
    # Method arguments: (manifest_path, manifest). Returns error messages.
    MANIFEST_CHECKS = (
        Check('folder-path-chars', ManifestChecks.folder_path_chars,
              'Folder path only uses letters, digits, "_", "-" and "/".'),
        Check('top-level-fields', ManifestChecks.top_level_fields,
              'Only "import_specifications" is allowed at the top level.'),
    )

    # Checks on one import specification, run once per import.
    # Method arguments: (ImportSpec). Returns error messages.
    IMPORT_CHECKS = (
        Check('unknown-fields', ManifestChecks.unknown_fields,
              'Imports only have known fields (plus "_comment").'),
        Check('required-fields', ManifestChecks.required_fields,
              'Required fields are present and not empty.'),
        Check('curator-emails-type', ManifestChecks.curator_emails_type,
              'curator_emails is a list of strings.'),
        Check('field-types', ManifestChecks.field_types,
              'Other fields have the right basic types.'),
        Check('import-name-chars', ManifestChecks.import_name_chars,
              'import_name only uses letters, digits, "_" and "-".'),
        Check('script-syntax', ManifestChecks.script_syntax,
              'Script entries are non-empty and can be split into words.'),
        Check('script-type', ManifestChecks.script_type,
              'Scripts end in .py or .sh, or are simple import configs.'),
        Check('cron-schedule', ManifestChecks.cron_schedule,
              'cron_schedule, if set, is accepted by croniter.'),
        Check('resource-limits-types', ManifestChecks.resource_limits_types,
              'resource_limits has known fields with valid types.'),
        Check('resource-limits-size', ManifestChecks.resource_limits_size,
              'resource_limits cpu and memory fit on a machine type.'),
        Check('config-override', ManifestChecks.config_override,
              'config_override sets real executor settings, valid types.'),
        Check('validation-config-file', ManifestChecks.validation_config_file,
              'validation_config_file, if set, exists.'),
        Check('import-inputs', ManifestChecks.import_inputs,
              'import_inputs values are strings or lists of strings.'),
    )

    # Checks across all manifests, run once after all manifests are read.
    # Method arguments: (manifests by path). Returns ManifestErrors.
    REPO_CHECKS = (
        Check('unique-import-names', ManifestChecks.unique_import_names,
              'import_name is unique across all manifests.'),
    )
    # yapf: enable

    def __init__(self, repo_dir: str, checks: Optional[ManifestChecks] = None):
        """Initializes the validator.

        Args:
            repo_dir: Path to the root of the data repository.
            checks: The checks to run. Defaults to ManifestChecks().

        Raises:
            ValueError: The check lists are inconsistent, e.g. a check id is
                duplicated.
        """
        self._repo_dir = os.path.abspath(repo_dir)
        self._validate_check_lists()
        self._manifest_checks = self._enabled(self.MANIFEST_CHECKS)
        self._import_checks = self._enabled(self.IMPORT_CHECKS)
        self._repo_checks = self._enabled(self.REPO_CHECKS)
        self._checks = checks or ManifestChecks()

    def _validate_check_lists(self) -> None:
        """Checks that the check lists are consistent.

        Raises:
            ValueError: A check id is duplicated, or a disabled check has no
                reason.
        """
        checks = self.all_checks()
        ids = [check.id for check in checks]
        duplicates = sorted(set(i for i in ids if ids.count(i) > 1))
        if duplicates:
            raise ValueError(f'Duplicate check ids: {", ".join(duplicates)}')
        for check in checks:
            if not check.enabled and not check.reason:
                raise ValueError(f'Disabled check "{check.id}" needs a reason')

    def _enabled(self, checks: Tuple[Check, ...]) -> List[Check]:
        """Returns the checks that are turned on."""
        return [check for check in checks if check.enabled]

    # Public methods.

    @classmethod
    def all_checks(cls) -> List[Check]:
        """Returns the checks in all check lists."""
        return list(cls.MANIFEST_CHECKS + cls.IMPORT_CHECKS + cls.REPO_CHECKS)

    @classmethod
    def disabled_checks(cls) -> List[Check]:
        """Returns the checks that are turned off."""
        return [check for check in cls.all_checks() if not check.enabled]

    def find_manifests(self) -> List[str]:
        """Returns the manifests under statvar_imports/ and scripts/.

        Searches at most max_depth folders below each of these folders, skips
        hidden folders (such as .env) and does not follow symbolic links.

        Returns:
            Sorted list of manifest paths relative to the repository root,
            using '/' as the separator.
        """
        # Top-level folders (relative to the repository root) to search.
        roots = ('statvar_imports', 'scripts')
        # Maximum number of folders between a root and a manifest.
        max_depth = 5
        filename = 'manifest.json'

        manifests = []
        for root in roots:
            root_dir = os.path.join(self._repo_dir, root)
            for dir_path, dir_names, file_names in os.walk(root_dir):
                rel_dir = os.path.relpath(dir_path, root_dir)
                depth = 0 if rel_dir == os.curdir else len(rel_dir.split(
                    os.sep))
                if depth >= max_depth:
                    dir_names[:] = []
                else:
                    dir_names[:] = [
                        d for d in dir_names if not d.startswith('.')
                    ]
                if filename in file_names:
                    manifests.append(
                        self._to_repo_path(os.path.join(dir_path, filename)))
        return sorted(manifests)

    def validate(
            self,
            manifest_paths: Optional[List[str]] = None) -> List[ManifestError]:
        """Validates manifests, including repository-wide checks.

        All manifests found by find_manifests() are read so that
        repository-wide checks (such as duplicate import names) work, but
        errors are only returned for the requested manifests.

        Args:
            manifest_paths: Manifests to validate, as absolute paths or paths
                relative to the current working directory. If None, all
                manifests found by find_manifests() are validated.

        Returns:
            Errors found in the requested manifests, sorted.
        """
        all_manifests = self.find_manifests()
        errors = []
        if manifest_paths is None:
            requested = set(all_manifests)
        else:
            requested = set()
            for path in manifest_paths:
                repo_path, error = self._resolve_requested_path(path)
                if error:
                    errors.append(error)
                else:
                    requested.add(repo_path)

        parsed = {}
        for path in sorted(set(all_manifests) | requested):
            manifest, load_errors = self._load_manifest(path)
            if path in requested:
                errors.extend(load_errors)
            if manifest is not None:
                parsed[path] = manifest

        for path in sorted(requested):
            if path in parsed:
                errors.extend(self._run_manifest_checks(path, parsed[path]))

        errors.extend(error for error in self._run_repo_checks(parsed)
                      if error.manifest_path in requested)
        return sorted(errors)

    def validate_manifest(self, manifest_path: str) -> List[ManifestError]:
        """Validates a single manifest, without the repository-wide checks.

        Args:
            manifest_path: Path to the manifest, relative to the repository
                root.

        Returns:
            Errors found in the manifest, sorted.
        """
        manifest, errors = self._load_manifest(manifest_path)
        if manifest is not None:
            errors.extend(self._run_manifest_checks(manifest_path, manifest))
        return sorted(errors)

    # Loading manifests and running checks.

    def _to_repo_path(self, path: str) -> str:
        """Returns path relative to the repository root, with '/' separators."""
        return os.path.relpath(path, self._repo_dir).replace(os.sep, '/')

    def _resolve_requested_path(
            self, path: str) -> Tuple[str, Optional[ManifestError]]:
        """Converts a user-supplied manifest path to a repository path.

        Returns:
            A (repo_path, error) tuple. error is set if the path is outside
            the repository or is not a file.
        """
        abs_path = os.path.abspath(path)
        repo_path = self._to_repo_path(abs_path)
        if repo_path == '..' or repo_path.startswith('../'):
            return '', ManifestError(path, None,
                                     'manifest is outside the repository')
        if not os.path.isfile(abs_path):
            return '', ManifestError(repo_path, None, 'file not found')
        return repo_path, None

    def _load_manifest(
            self,
            manifest_path: str) -> Tuple[Optional[Any], List[ManifestError]]:
        """Reads and parses a manifest (the 'valid-json' check)."""

        def error(message: str) -> ManifestError:
            return ManifestError(manifest_path, None, message, 'valid-json')

        try:
            with open(os.path.join(self._repo_dir, manifest_path),
                      encoding='utf-8') as f:
                return json.load(f), []
        except (OSError, UnicodeDecodeError) as e:
            return None, [error(f'cannot read file: {e}')]
        except json.JSONDecodeError as e:
            return None, [error(f'invalid JSON: {e}')]

    def _run_manifest_checks(self, manifest_path: str,
                             manifest: Any) -> List[ManifestError]:
        """Runs the manifest and import checks on a parsed manifest."""
        errors = []
        for check in self._manifest_checks:
            errors.extend(
                ManifestError(manifest_path, None, message, check.id) for
                message in check.method(self._checks, manifest_path, manifest))

        def structure_error(message: str,
                            label: Optional[str] = None) -> ManifestError:
            return ManifestError(manifest_path, label, message,
                                 'manifest-structure')

        if not isinstance(manifest, dict):
            errors.append(structure_error('manifest must be a JSON object'))
            return errors
        specs = manifest.get('import_specifications')
        if not isinstance(specs, list) or not specs:
            errors.append(
                structure_error(
                    '"import_specifications" must be a non-empty list'))
            return errors

        abs_import_dir = os.path.join(self._repo_dir,
                                      os.path.dirname(manifest_path))
        for index, fields in enumerate(specs):
            label = self._spec_label(fields, index)
            if not isinstance(fields, dict):
                errors.append(
                    structure_error(
                        'import specification must be a JSON object', label))
                continue
            spec = ImportSpec(manifest_path, label, abs_import_dir, fields)
            for check in self._import_checks:
                errors.extend(
                    ManifestError(manifest_path, label, message, check.id)
                    for message in check.method(self._checks, spec))
        return errors

    def _spec_label(self, fields: Any, index: int) -> str:
        """Returns the name used to identify an import specification in errors."""
        if isinstance(fields, dict):
            name = fields.get('import_name')
            if isinstance(name, str) and name:
                return name
        return f'import_specifications[{index}]'

    def _run_repo_checks(self, manifests: Dict[str,
                                               Any]) -> List[ManifestError]:
        """Runs the repository-wide checks on all parsed manifests."""
        errors = []
        for check in self._repo_checks:
            errors.extend(
                dataclasses.replace(error, check=check.id)
                for error in check.method(self._checks, manifests))
        return errors


# Command line interface.

EXIT_OK = 0
EXIT_ERRORS = 1
EXIT_USAGE = 2


def _plural(count: int, noun: str) -> str:
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def run(repo_dir: str,
        manifest_paths: List[str],
        validate_all: bool,
        out: Optional[TextIO] = None,
        err: Optional[TextIO] = None) -> int:
    """Validates manifests and prints the results.

    Args:
        repo_dir: Path to the root of the data repository.
        manifest_paths: Manifests to validate. Must be empty if validate_all
            is set.
        validate_all: Whether to validate all manifests in the repository.
        out: Stream for results. Defaults to stdout.
        err: Stream for usage errors. Defaults to stderr.

    Returns:
        The process exit code.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    if validate_all == bool(manifest_paths):
        print('Pass either one or more manifest paths or --all.', file=err)
        return EXIT_USAGE

    validator = ManifestValidator(repo_dir)
    if validate_all:
        errors = validator.validate()
        num_checked = len(validator.find_manifests())
    else:
        errors = validator.validate(manifest_paths)
        num_checked = len(set(os.path.abspath(p) for p in manifest_paths))

    for error in errors:
        print(error, file=out)
    if errors:
        num_failed = len(set(error.manifest_path for error in errors))
        summary = (f'\n{_plural(len(errors), "error")} in {num_failed} of '
                   f'{_plural(num_checked, "manifest")}.')
    elif num_checked == 1:
        summary = 'The manifest is valid.'
    else:
        summary = f'All {_plural(num_checked, "manifest")} are valid.'
    disabled = ManifestValidator.disabled_checks()
    if disabled:
        summary += (' Disabled checks: ' +
                    ', '.join(check.id for check in disabled) + '.')
    print(summary, file=out)
    return EXIT_ERRORS if errors else EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    """Parses command line arguments and validates manifests.

    Args:
        argv: Command line arguments, without the program name. Defaults to
            sys.argv[1:].

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(
        description='Validates import manifest.json files.')
    parser.add_argument('manifest_paths',
                        nargs='*',
                        help='Manifests to validate.')
    parser.add_argument(
        '--all',
        action='store_true',
        help='Validate all manifests under statvar_imports/ and scripts/.')
    parser.add_argument('--repo_dir',
                        default=os.path.abspath(
                            os.path.join(os.path.dirname(__file__), '..', '..',
                                         '..', '..')),
                        help='Path to the root of the data repository.')
    args = parser.parse_args(argv)
    return run(args.repo_dir, args.manifest_paths, args.all)


if __name__ == '__main__':
    sys.exit(main())
