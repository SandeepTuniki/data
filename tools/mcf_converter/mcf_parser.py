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
"""Shared, streaming MCF parsing helpers.

Reads MCF files (local or `gs://` wildcard paths) one node at a time in
constant memory, and normalizes property values. Shared by the BigQuery differ
(`tools/import_differ/bigquery_differ.py`) and the MCF converter.
"""

import contextlib
import io
import os
import sys
from typing import Any, Dict, Iterator

from absl import logging
from google.cloud import storage

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
sys.path.append(os.path.join(_DATA_DIR, 'util'))
sys.path.append(os.path.join(_DATA_DIR, 'tools', 'statvar_importer'))

from file_util import file_get_gcs_blob, file_get_matching, file_is_gcs
from mcf_file_util import normalize_value, strip_namespace

# Properties that together identify a StatVarObservation.
OBSERVATION_KEY_PROPERTIES = [
    'variableMeasured',
    'observationAbout',
    'observationDate',
    'observationPeriod',
    'measurementMethod',
    'unit',
    'scalingFactor',
]


def normalize_prop_val(value) -> str:
    """Normalizes a property value string identically to ImportDiffer."""
    if isinstance(value, list):
        return ', '.join(sorted([normalize_prop_val(v) for v in value]))
    if isinstance(value, str):
        return str(normalize_value(value))
    return str(value) if value is not None else ''


def format_raw_prop(value) -> str:
    """Formats a property value without numeric/namespace coercion."""
    if isinstance(value, list):
        return ', '.join(str(v).strip() for v in value)
    return str(value).strip() if value is not None else ''


def clean_value(value) -> str:
    """Returns a property value without namespace prefix or enclosing quotes.

    Examples: 'dcid:Count_Person' -> 'Count_Person', 'dcs:geoId/06' ->
    'geoId/06', '"2020-05"' -> '2020-05'. Text inside quotes is otherwise
    kept as is. Repeated values are cleaned, sorted and joined with ', '.

    Args:
      value: a raw property value string, a list of them, or None.

    Returns:
      The cleaned string; '' for None or empty values.
    """
    if isinstance(value, list):
        return ', '.join(sorted(clean_value(v) for v in value))
    if value is None:
        return ''
    value = str(value).strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return strip_namespace(value)


def is_observation(node: Dict[str, Any]) -> bool:
    """Returns True if the node's typeOf includes StatVarObservation."""
    type_of = node.get('typeOf', '')
    type_of_list = type_of if isinstance(type_of, list) else [type_of]
    return any('StatVarObservation' in str(t) for t in type_of_list)


def schema_value_combined(node: Dict[str, Any]) -> str:
    """Returns all properties except Node and dcid as sorted 'prop:value' pairs.

    Values are normalized with normalize_prop_val and pairs are joined with ';',
    identically to ImportDiffer.
    """
    props = []
    for k in sorted(node.keys()):
        if k not in ('Node', 'dcid'):
            props.append(f'{k}:{normalize_prop_val(node[k])}')
    return ';'.join(props)


@contextlib.contextmanager
def open_text_stream(path: str, mode: str = 'r'):
    """Opens a local file or gs:// URI as a streaming UTF-8 text file without /tmp buffering."""
    newline = '' if mode.startswith('w') else None
    if file_is_gcs(path):
        if mode.startswith('r'):
            blob = file_get_gcs_blob(path, exists=True)
            if not blob:
                raise FileNotFoundError(f'GCS blob not found: {path}')
            raw_stream = storage.fileio.BlobReader(blob)
            with io.TextIOWrapper(raw_stream,
                                  encoding='utf-8',
                                  errors='replace') as stream:
                yield stream
        elif mode.startswith('w'):
            blob = file_get_gcs_blob(path, exists=False)
            if not blob:
                raise RuntimeError(
                    f'Failed to create GCS blob for write: {path}')
            raw_stream = storage.fileio.BlobWriter(blob, ignore_flush=True)
            with io.TextIOWrapper(raw_stream, encoding='utf-8',
                                  newline=newline) as stream:
                yield stream
        else:
            raise ValueError(f'Unsupported mode for GCS stream: {mode}')
    else:
        if mode.startswith('w'):
            parent_dir = os.path.dirname(path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
        with open(path,
                  mode=mode,
                  encoding='utf-8',
                  newline=newline,
                  errors='replace' if mode.startswith('r') else None) as stream:
            yield stream


def iter_mcf_nodes(mcf_pattern: str) -> Iterator[Dict[str, Any]]:
    """Yields each node of the matching MCF files as a dict, in O(1) memory.

    Blank lines, comment lines (`#`) and a `Node:` line end the current node.
    Each `key: value` line adds a property; repeated keys become lists. Values
    are kept as raw stripped strings (no normalization).

    Args:
      mcf_pattern: local path or gs:// URI of MCF files; wildcards allowed.

    Yields:
      A dict of property name to a string value, or a list of string values
      for repeated properties.
    """
    for mcf_file in file_get_matching(mcf_pattern):
        logging.info('Reading MCF file: %s', mcf_file)
        with open_text_stream(mcf_file, mode='r') as in_f:
            current_node: Dict[str, Any] = {}
            for raw_line in in_f:
                line = raw_line.strip()
                if not line or line.startswith('#'):
                    if current_node:
                        yield current_node
                        current_node = {}
                    continue
                if ':' in line:
                    k, v = line.split(':', 1)
                    k = k.strip()
                    v = v.strip()
                    if k == 'Node' and current_node:
                        yield current_node
                        current_node = {}
                    if k in current_node:
                        if isinstance(current_node[k], list):
                            current_node[k].append(v)
                        else:
                            current_node[k] = [current_node[k], v]
                    else:
                        current_node[k] = v
            if current_node:
                yield current_node
