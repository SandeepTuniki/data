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
"""Unit tests for writers.py."""

import csv
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

import fastavro

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_SCRIPT_DIR)

import writers

_FIELDS = [
    writers.Field('name', 'string'),
    writers.Field('score', 'double', nullable=True),
    writers.Field('note', 'string', nullable=True),
]

_RECORDS = [
    {
        'name': 'a',
        'score': 1.5,
        'note': 'first'
    },
    {
        'name': 'b',
        'score': None,
        'note': None
    },
    # Missing optional fields are written as null/empty.
    {
        'name': 'c'
    },
]


def _read_avro(path):
    with open(path, 'rb') as f:
        reader = fastavro.reader(f)
        return reader.metadata, reader.writer_schema, list(reader)


class AvroSchemaTest(unittest.TestCase):

    def test_nullable_fields_are_unions_with_null(self):
        schema = writers.avro_schema('t', _FIELDS)
        self.assertEqual(schema['name'], 't')
        self.assertEqual(schema['fields'], [
            {
                'name': 'name',
                'type': 'string'
            },
            {
                'name': 'score',
                'type': ['null', 'double'],
                'default': None
            },
            {
                'name': 'note',
                'type': ['null', 'string'],
                'default': None
            },
        ])


class CsvWriterTest(unittest.TestCase):

    def test_writes_header_and_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'sub', 'table.csv')
            with writers.CsvWriter(path, _FIELDS) as w:
                for record in _RECORDS:
                    w.write(record)

            with open(path, encoding='utf-8', newline='') as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows, [
                ['name', 'score', 'note'],
                ['a', '1.5', 'first'],
                ['b', '', ''],
                ['c', '', ''],
            ])

    def test_quotes_values_with_commas_and_newlines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'table.csv')
            with writers.CsvWriter(path, _FIELDS) as w:
                w.write({'name': 'x,y', 'note': 'line1\nline2'})

            with open(path, encoding='utf-8', newline='') as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[1], ['x,y', '', 'line1\nline2'])


class AvroWriterTest(unittest.TestCase):

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'sub', 'table.avro')
            with writers.AvroWriter(path, 'table', _FIELDS) as w:
                for record in _RECORDS:
                    w.write(record)

            metadata, schema, rows = _read_avro(path)
            self.assertEqual(metadata['avro.codec'], 'deflate')
            self.assertEqual(schema['name'], 'table')
            self.assertEqual(rows, [
                {
                    'name': 'a',
                    'score': 1.5,
                    'note': 'first'
                },
                {
                    'name': 'b',
                    'score': None,
                    'note': None
                },
                {
                    'name': 'c',
                    'score': None,
                    'note': None
                },
            ])

    def test_empty_table_has_schema_and_no_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'table.avro')
            writers.AvroWriter(path, 'table', _FIELDS).close()

            _, schema, rows = _read_avro(path)
            self.assertEqual([f['name'] for f in schema['fields']],
                             ['name', 'score', 'note'])
            self.assertEqual(rows, [])

    def test_ignores_extra_record_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'table.avro')
            with writers.AvroWriter(path, 'table', _FIELDS) as w:
                w.write({'name': 'a', 'unexpected': 'x'})

            _, _, rows = _read_avro(path)
            self.assertEqual(rows, [{'name': 'a', 'score': None, 'note': None}])

    def test_wrong_type_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'table.avro')
            with self.assertRaises(Exception):
                with writers.AvroWriter(path, 'table', _FIELDS) as w:
                    w.write({'name': 'a', 'score': 'not a number'})

    @mock.patch('writers.storage.fileio.BlobWriter')
    @mock.patch('writers.file_get_gcs_blob')
    def test_gcs_path_streams_to_blob_writer(self, mock_get_blob,
                                             mock_blob_writer_cls):
        mock_blob = mock.MagicMock()
        mock_get_blob.return_value = mock_blob
        buf = io.BytesIO()
        buf.close = mock.MagicMock()
        mock_blob_writer_cls.return_value = buf

        with writers.AvroWriter('gs://bucket/out/table.avro', 'table',
                                _FIELDS) as w:
            w.write(_RECORDS[0])

        mock_get_blob.assert_called_once_with('gs://bucket/out/table.avro',
                                              exists=False)
        mock_blob_writer_cls.assert_called_once_with(mock_blob,
                                                     ignore_flush=True)
        buf.close.assert_called_once()
        rows = list(fastavro.reader(io.BytesIO(buf.getvalue())))
        self.assertEqual(rows, [_RECORDS[0]])


if __name__ == '__main__':
    unittest.main()
