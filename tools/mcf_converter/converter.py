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
"""Converts MCF files into observation and schema tables (Avro and/or CSV).

Streams the MCF files once, in constant memory, and writes every node to one
of two tables:
  - observations: one record per StatVarObservation node
  - schema: one record per other node

Each table is written in every requested format, under `output_dir`:
  <output_dir>/avro/observations.avro   <output_dir>/csv/observations.csv
  <output_dir>/avro/schema.avro         <output_dir>/csv/schema.csv

If conversion fails, the partially written files are deleted and the error is
re-raised.

Usage:
  counts = convert_mcf(
      mcf_pattern='gs://bucket/import/version/input0/genmcf/*.mcf',
      output_dir='gs://bucket/import/version/input0/bq',
      import_name='MyImport',
      version='2026_11_04T00_03_46_028066_08_00',
      input_name='input0')
"""

import contextlib
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from absl import logging

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
sys.path.append(_SCRIPT_DIR)
sys.path.append(os.path.join(_DATA_DIR, 'util'))

from file_util import file_get_gcs_blob, file_is_gcs
import mcf_parser
from writers import AvroWriter, CsvWriter, Field, RecordWriter

OBSERVATIONS_TABLE = 'observations'
SCHEMA_TABLE = 'schema'

_PROVENANCE_FIELDS = [
    Field('import_name', 'string'),
    Field('version', 'string'),
    Field('input', 'string'),
]

OBSERVATION_FIELDS = [
    Field('variableMeasured', 'string'),
    Field('observationAbout', 'string'),
    Field('observationDate', 'string'),
    Field('observationPeriod', 'string', nullable=True),
    Field('measurementMethod', 'string', nullable=True),
    Field('unit', 'string', nullable=True),
    Field('scalingFactor', 'string', nullable=True),
    Field('value', 'double', nullable=True),
    Field('value_raw', 'string'),
    Field('node_id', 'string', nullable=True),
    Field('dcid', 'string', nullable=True),
] + _PROVENANCE_FIELDS

SCHEMA_FIELDS = [
    Field('dcid', 'string'),
    Field('value_combined', 'string'),
] + _PROVENANCE_FIELDS

_TABLE_FIELDS = {
    OBSERVATIONS_TABLE: OBSERVATION_FIELDS,
    SCHEMA_TABLE: SCHEMA_FIELDS,
}

# Required observation key properties; '' if missing in the MCF.
_REQUIRED_OBS_PROPS = [
    'variableMeasured', 'observationAbout', 'observationDate'
]
# Optional observation key properties; None if missing in the MCF.
_OPTIONAL_OBS_PROPS = [
    'observationPeriod', 'measurementMethod', 'unit', 'scalingFactor'
]

# Output format name -> function(path, table_name, fields) -> RecordWriter.
_WRITER_FACTORIES: Dict[str, Callable[
    [str, str, List[Field]], RecordWriter]] = {
        'avro': AvroWriter,
        'csv': lambda path, table_name, fields: CsvWriter(path, fields),
    }

DEFAULT_FORMATS = ('avro', 'csv')


def parse_number(value_raw: str) -> Optional[float]:
    """Returns value_raw as a float, or None if it isn't a plain number."""
    try:
        return float(value_raw)
    except (TypeError, ValueError):
        return None


def to_observation_record(node: Dict[str, Any],
                          provenance: Dict[str, str]) -> Dict[str, Any]:
    """Returns the observation record for a StatVarObservation node."""
    record: Dict[str, Any] = {}
    for prop in _REQUIRED_OBS_PROPS:
        record[prop] = mcf_parser.clean_value(node.get(prop))
    for prop in _OPTIONAL_OBS_PROPS:
        record[prop] = mcf_parser.clean_value(node.get(prop)) or None
    record['value_raw'] = mcf_parser.format_raw_prop(node.get('value'))
    record['value'] = parse_number(record['value_raw'])
    # Node ids are kept as written: a 'dcid:' prefix is meaningful there.
    record['node_id'] = mcf_parser.format_raw_prop(node.get('Node')) or None
    record['dcid'] = mcf_parser.clean_value(node.get('dcid')) or None
    record.update(provenance)
    return record


def to_schema_record(node: Dict[str, Any],
                     provenance: Dict[str, str]) -> Dict[str, Any]:
    """Returns the schema record for a non-observation node."""
    record = {
        'dcid': mcf_parser.clean_value(node.get('dcid') or node.get('Node')),
        'value_combined': mcf_parser.schema_value_combined(node),
    }
    record.update(provenance)
    return record


def output_path(output_dir: str, fmt: str, table_name: str) -> str:
    """Returns the output file path for a format and table."""
    filename = f'{table_name}.{fmt}'
    if file_is_gcs(output_dir):
        return f"{output_dir.rstrip('/')}/{fmt}/{filename}"
    return os.path.join(output_dir, fmt, filename)


def _validate_formats(formats: Sequence[str]) -> None:
    """Raises ValueError if formats is empty or has an unknown format."""
    if not formats:
        raise ValueError('At least one output format is required.')
    unknown = [f for f in formats if f not in _WRITER_FACTORIES]
    if unknown:
        raise ValueError(f'Unknown output format(s): {unknown}. '
                         f'Supported: {sorted(_WRITER_FACTORIES)}')


def _delete_file(path: str) -> None:
    """Deletes a local file or gs:// blob if it exists, logging any error."""
    try:
        if file_is_gcs(path):
            blob = file_get_gcs_blob(path, exists=True)
            if blob:
                blob.delete()
        elif os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        logging.warning('Failed to delete partial output %s: %s', path, exc)


def convert_mcf(
    mcf_pattern: str,
    output_dir: str,
    import_name: str,
    version: str,
    input_name: str,
    formats: Sequence[str] = DEFAULT_FORMATS,
) -> Dict[str, int]:
    """Converts MCF files into observation and schema tables.

    Args:
      mcf_pattern: local path or gs:// URI of the MCF files; wildcards allowed.
      output_dir: local directory or gs:// prefix to write the tables under.
      import_name: import name, copied into every record.
      version: import version, copied into every record.
      input_name: import input (e.g. 'input0'), copied into every record.
      formats: output formats to write; any of 'avro', 'csv'.

    Returns:
      Counts of records written per table: 'observations' and 'schema_nodes'.

    Raises:
      ValueError: if formats is empty or has an unknown format.
    """
    _validate_formats(formats)
    provenance = {
        'import_name': import_name,
        'version': version,
        'input': input_name,
    }
    counts = {'observations': 0, 'schema_nodes': 0}
    paths: List[str] = []

    try:
        with contextlib.ExitStack() as stack:
            table_writers: Dict[str, List[RecordWriter]] = {
                table: [] for table in _TABLE_FIELDS
            }
            for fmt in formats:
                for table, fields in _TABLE_FIELDS.items():
                    path = output_path(output_dir, fmt, table)
                    paths.append(path)
                    writer = _WRITER_FACTORIES[fmt](path, table, fields)
                    stack.enter_context(writer)
                    table_writers[table].append(writer)

            for node in mcf_parser.iter_mcf_nodes(mcf_pattern):
                if mcf_parser.is_observation(node):
                    record = to_observation_record(node, provenance)
                    table = OBSERVATIONS_TABLE
                    counts['observations'] += 1
                else:
                    record = to_schema_record(node, provenance)
                    table = SCHEMA_TABLE
                    counts['schema_nodes'] += 1
                for writer in table_writers[table]:
                    writer.write(record)
    except BaseException:
        logging.error('MCF conversion failed; deleting partial outputs: %s',
                      paths)
        for path in paths:
            _delete_file(path)
        raise

    logging.info('Converted %s to %s (%s): %s', mcf_pattern, output_dir,
                 ', '.join(formats), counts)
    return counts
