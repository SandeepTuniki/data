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
"""Pluggable, streaming record writers (CSV and Avro).

Each writer writes one table (a sequence of flat dict records with a fixed set
of fields) to a single local or `gs://` file, in constant memory. Writers know
nothing about MCF; the caller defines the fields.

Usage:
  fields = [Field('name', 'string'), Field('score', 'double', nullable=True)]
  with AvroWriter('gs://bucket/out/table.avro', 'table', fields) as w:
      w.write({'name': 'a', 'score': 1.5})
"""

import abc
import contextlib
import csv
import dataclasses
import os
import sys
from typing import Any, Dict, List

from fastavro import parse_schema
from fastavro.write import Writer as _FastavroWriter
from google.cloud import storage

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
sys.path.append(_SCRIPT_DIR)
sys.path.append(os.path.join(_DATA_DIR, 'util'))

from file_util import file_get_gcs_blob, file_is_gcs
from mcf_parser import open_text_stream

# Avro compression codec. BigQuery can load deflate-compressed Avro directly.
AVRO_CODEC = 'deflate'


@dataclasses.dataclass(frozen=True)
class Field:
    """A column in a table.

    Attributes:
      name: column name.
      type: Avro primitive type name, e.g. 'string' or 'double'.
      nullable: whether the value may be None.
    """
    name: str
    type: str
    nullable: bool = False


def avro_schema(table_name: str, fields: List[Field]) -> Dict[str, Any]:
    """Returns the Avro record schema for a table."""
    avro_fields = []
    for f in fields:
        if f.nullable:
            avro_fields.append({
                'name': f.name,
                'type': ['null', f.type],
                'default': None
            })
        else:
            avro_fields.append({'name': f.name, 'type': f.type})
    return {'type': 'record', 'name': table_name, 'fields': avro_fields}


@contextlib.contextmanager
def open_binary_write_stream(path: str):
    """Opens a local file or gs:// URI for streaming binary writes."""
    if file_is_gcs(path):
        blob = file_get_gcs_blob(path, exists=False)
        if not blob:
            raise RuntimeError(f'Failed to create GCS blob for write: {path}')
        with storage.fileio.BlobWriter(blob, ignore_flush=True) as stream:
            yield stream
    else:
        parent_dir = os.path.dirname(path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(path, mode='wb') as stream:
            yield stream


class RecordWriter(abc.ABC):
    """Writes flat dict records to one output file.

    Can be used as a context manager; close() is called on exit.
    """

    def __init__(self) -> None:
        self._stack = contextlib.ExitStack()

    @abc.abstractmethod
    def write(self, record: Dict[str, Any]) -> None:
        """Writes one record. Missing fields are written as null/empty."""

    def close(self) -> None:
        """Flushes and closes the output file."""
        self._stack.close()

    def __enter__(self) -> 'RecordWriter':
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class CsvWriter(RecordWriter):
    """Writes records as CSV: a header row, then one row per record.

    None values are written as empty cells.
    """

    def __init__(self, path: str, fields: List[Field]) -> None:
        super().__init__()
        self._field_names = [f.name for f in fields]
        stream = self._stack.enter_context(open_text_stream(path, mode='w'))
        self._writer = csv.writer(stream)
        self._writer.writerow(self._field_names)

    def write(self, record: Dict[str, Any]) -> None:
        self._writer.writerow([
            '' if record.get(name) is None else record.get(name)
            for name in self._field_names
        ])


class AvroWriter(RecordWriter):
    """Writes records to a deflate-compressed Avro container file."""

    def __init__(self, path: str, table_name: str, fields: List[Field]) -> None:
        super().__init__()
        self._field_names = [f.name for f in fields]
        stream = self._stack.enter_context(open_binary_write_stream(path))
        self._writer = _FastavroWriter(stream,
                                       parse_schema(
                                           avro_schema(table_name, fields)),
                                       codec=AVRO_CODEC)
        # Flush buffered records before the stream is closed.
        self._stack.callback(self._writer.flush)

    def write(self, record: Dict[str, Any]) -> None:
        self._writer.write(
            {name: record.get(name) for name in self._field_names})
