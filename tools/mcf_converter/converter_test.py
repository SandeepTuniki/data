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
"""Unit tests for converter.py."""

import csv
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

import fastavro

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.dirname(os.path.dirname(_SCRIPT_DIR))
sys.path.append(_SCRIPT_DIR)
sys.path.append(os.path.join(_DATA_DIR, 'tools', 'import_differ'))

import bigquery_differ
import converter
import mcf_parser

# Modelled on real genmcf output (India_RBIStateDomesticProduct): quoted dcid
# and dates, dcid: prefixed references, keyString.
_SCHEMA_MCF = """
# Schema nodes
Node: dcid:Count_Person_Female
typeOf: dcid:StatisticalVariable
dcid: "Count_Person_Female"
populationType: dcid:Person
gender: dcid:Female
measuredProperty: dcid:count
name: "Female population"
statType: dcid:measuredValue

Node: LocalPlace
typeOf: dcs:Place
typeOf: dcs:City
name: "Some City"
"""

_OBS_MCF = """
Node: state_output/E0/a653f6e7
observationDate: "2012-03"
observationAbout: dcid:wikidataId/Q40888
unit: dcid:INR
variableMeasured: dcid:Count_Person_Female
value: 89100
measurementMethod: dcid:CurrentPrice
observationPeriod: "P1Y"
scalingFactor: 100
typeOf: dcid:StatVarObservation
keyString: "observationAbout=wikidataId/Q40888variableMeasured=Count_Person_Female"
dcid: "dc/o/7dl8b0472kl2h"

Node: dcid:obs2
typeOf: dcs:StatVarObservation
variableMeasured: dcs:Count_Person_Female
observationAbout: geoId/06
observationDate: 2020
value: "not available"
"""

_PROVENANCE = {
    'import_name': 'TestImport',
    'version': '2026_11_04',
    'input': 'input0',
}

_EXPECTED_OBS = [
    {
        'variableMeasured': 'Count_Person_Female',
        'observationAbout': 'wikidataId/Q40888',
        'observationDate': '2012-03',
        'observationPeriod': 'P1Y',
        'measurementMethod': 'CurrentPrice',
        'unit': 'INR',
        'scalingFactor': '100',
        'value': 89100.0,
        'value_raw': '89100',
        'node_id': 'state_output/E0/a653f6e7',
        'dcid': 'dc/o/7dl8b0472kl2h',
        **_PROVENANCE,
    },
    # Missing optional fields, non-numeric value, Node with a dcid: prefix
    # (kept as written) and no dcid property.
    {
        'variableMeasured': 'Count_Person_Female',
        'observationAbout': 'geoId/06',
        'observationDate': '2020',
        'observationPeriod': None,
        'measurementMethod': None,
        'unit': None,
        'scalingFactor': None,
        'value': None,
        'value_raw': '"not available"',
        'node_id': 'dcid:obs2',
        'dcid': None,
        **_PROVENANCE,
    },
]

_EXPECTED_SCHEMA = [
    {
        'dcid': 'Count_Person_Female',
        'value_combined':
            ('gender:dcid:Female;measuredProperty:dcid:count;'
             'name:"Female population";populationType:dcid:Person;'
             'statType:dcid:measuredValue;typeOf:dcid:StatisticalVariable'),
        **_PROVENANCE,
    },
    # No dcid property: the dcid comes from Node. Repeated typeOf values are
    # sorted and joined.
    {
        'dcid': 'LocalPlace',
        'value_combined': 'name:"Some City";typeOf:dcid:City, dcid:Place',
        **_PROVENANCE,
    },
]


def _write(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def _read_avro(path):
    with open(path, 'rb') as f:
        return list(fastavro.reader(f))


def _read_csv(path):
    with open(path, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def _as_csv_row(record):
    """Returns the record as it reads back from CSV: all strings, None -> ''."""
    return {k: '' if v is None else str(v) for k, v in record.items()}


def _list_files(root):
    return sorted(
        os.path.relpath(os.path.join(d, f), root)
        for d, _, files in os.walk(root)
        for f in files)


class ConvertMcfTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.mcf_dir = os.path.join(self._tmp.name, 'genmcf')
        self.out_dir = os.path.join(self._tmp.name, 'bq')
        os.makedirs(self.mcf_dir)
        # Two input files, matched by one wildcard pattern.
        _write(os.path.join(self.mcf_dir, 'schema.mcf'), _SCHEMA_MCF)
        _write(os.path.join(self.mcf_dir, 'obs.mcf'), _OBS_MCF)
        self.mcf_pattern = os.path.join(self.mcf_dir, '*.mcf')

    def _convert(self, **kwargs):
        return converter.convert_mcf(mcf_pattern=self.mcf_pattern,
                                     output_dir=self.out_dir,
                                     **_PROVENANCE_ARGS,
                                     **kwargs)

    def _out(self, fmt, table):
        return os.path.join(self.out_dir, fmt, f'{table}.{fmt}')

    def test_writes_both_tables_in_both_formats(self):
        counts = self._convert()

        self.assertEqual(counts, {'observations': 2, 'schema_nodes': 2})
        self.assertEqual(_list_files(self.out_dir), [
            'avro/observations.avro',
            'avro/schema.avro',
            'csv/observations.csv',
            'csv/schema.csv',
        ])

        def by_node_id(rows):
            return sorted(rows, key=lambda r: r['node_id'], reverse=True)

        def by_dcid(rows):
            return sorted(rows, key=lambda r: r['dcid'])

        self.assertEqual(
            by_node_id(_read_avro(self._out('avro', 'observations'))),
            _EXPECTED_OBS)
        self.assertEqual(by_dcid(_read_avro(self._out('avro', 'schema'))),
                         _EXPECTED_SCHEMA)
        self.assertEqual(
            by_node_id(_read_csv(self._out('csv', 'observations'))),
            [_as_csv_row(r) for r in _EXPECTED_OBS])
        self.assertEqual(by_dcid(_read_csv(self._out('csv', 'schema'))),
                         [_as_csv_row(r) for r in _EXPECTED_SCHEMA])

    def test_csv_columns_are_in_schema_order(self):
        self._convert()

        with open(self._out('csv', 'observations'), encoding='utf-8') as f:
            header = f.readline().strip().split(',')
        self.assertEqual(header, [f.name for f in converter.OBSERVATION_FIELDS])

    def test_missing_required_field_is_written_as_empty(self):
        _write(
            os.path.join(self.mcf_dir, 'obs.mcf'), '\n'.join([
                'Node: obs3',
                'typeOf: dcs:StatVarObservation',
                'variableMeasured: dcid:Count_Person',
                'observationAbout: dcid:geoId/06',
                'value: 5',
            ]))

        self._convert(formats=['avro'])

        rows = _read_avro(self._out('avro', 'observations'))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['observationDate'], '')
        self.assertEqual(rows[0]['value'], 5.0)

    def test_formats_selects_output_files(self):
        counts = self._convert(formats=['csv'])

        self.assertEqual(counts, {'observations': 2, 'schema_nodes': 2})
        self.assertEqual(_list_files(self.out_dir),
                         ['csv/observations.csv', 'csv/schema.csv'])

    def test_invalid_formats_raise(self):
        for formats in ([], ['parquet'], ['csv', 'parquet']):
            with self.subTest(formats=formats):
                with self.assertRaises(ValueError):
                    self._convert(formats=formats)
                self.assertFalse(os.path.exists(self.out_dir))

    def test_failure_deletes_partial_outputs(self):

        def failing_nodes(_):
            yield {'Node': 'A', 'typeOf': 'dcs:Thing'}
            raise RuntimeError('simulated read failure')

        with mock.patch.object(mcf_parser, 'iter_mcf_nodes', failing_nodes):
            with self.assertRaisesRegex(RuntimeError, 'simulated'):
                self._convert()

        self.assertEqual(_list_files(self.out_dir), [])

    @mock.patch('mcf_parser.file_get_gcs_blob')
    @mock.patch('writers.file_get_gcs_blob')
    @mock.patch('writers.storage.fileio.BlobWriter')
    def test_gcs_output_dir(self, mock_blob_writer_cls, mock_writers_get_blob,
                            mock_parser_get_blob):
        buffers = {}

        def get_blob(path, exists):
            del exists
            blob = mock.MagicMock()
            blob.path = path
            return blob

        def blob_writer(blob, ignore_flush):
            del ignore_flush
            buf = io.BytesIO()
            buf.close = mock.MagicMock()
            buffers[blob.path] = buf
            return buf

        mock_writers_get_blob.side_effect = get_blob
        mock_parser_get_blob.side_effect = get_blob
        mock_blob_writer_cls.side_effect = blob_writer

        counts = converter.convert_mcf(mcf_pattern=self.mcf_pattern,
                                       output_dir='gs://bucket/v1/input0/bq/',
                                       **_PROVENANCE_ARGS)

        self.assertEqual(counts, {'observations': 2, 'schema_nodes': 2})
        self.assertEqual(sorted(buffers), [
            'gs://bucket/v1/input0/bq/avro/observations.avro',
            'gs://bucket/v1/input0/bq/avro/schema.avro',
            'gs://bucket/v1/input0/bq/csv/observations.csv',
            'gs://bucket/v1/input0/bq/csv/schema.csv',
        ])
        avro_rows = list(
            fastavro.reader(
                io.BytesIO(buffers['gs://bucket/v1/input0/bq/avro/schema.avro'].
                           getvalue())))
        self.assertEqual(len(avro_rows), 2)


class DifferEquivalenceTest(unittest.TestCase):
    """Converter output matches bigquery_differ's, apart from agreed changes.

    Agreed changes: the converter stores observation key fields and schema
    dcids without the 'dcid:' prefix and enclosing quotes.
    """

    @staticmethod
    def _undo_differ_normalization(value):
        """Removes the differ's 'dcid:' prefix and enclosing quotes."""
        if value.startswith('dcid:'):
            value = value[len('dcid:'):]
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        return value

    def test_matches_differ(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mcf_path = os.path.join(tmpdir, 'all.mcf')
            _write(mcf_path, _SCHEMA_MCF + _OBS_MCF)
            differ_obs_csv = os.path.join(tmpdir, 'differ_obs.csv')
            differ_schema_csv = os.path.join(tmpdir, 'differ_schema.csv')
            out_dir = os.path.join(tmpdir, 'bq')

            bigquery_differ.stream_mcf_to_csv(mcf_path, differ_obs_csv,
                                              differ_schema_csv)
            converter.convert_mcf(mcf_path,
                                  out_dir,
                                  **_PROVENANCE_ARGS,
                                  formats=['avro'])

            differ_obs = _read_csv(differ_obs_csv)
            differ_schema = _read_csv(differ_schema_csv)
            conv_obs = _read_avro(
                os.path.join(out_dir, 'avro', 'observations.avro'))
            conv_schema = _read_avro(
                os.path.join(out_dir, 'avro', 'schema.avro'))

        # Observations: same key fields and raw value.
        expected_obs_keys = sorted(
            tuple(
                self._undo_differ_normalization(part)
                for part in row['key_combined'].split(';')) + (row['value'],)
            for row in differ_obs)
        conv_obs_keys = sorted(
            tuple(row[prop] or ''
                  for prop in mcf_parser.OBSERVATION_KEY_PROPERTIES) +
            (row['value_raw'],)
            for row in conv_obs)
        self.assertEqual(conv_obs_keys, expected_obs_keys)

        # Schema: identical value_combined; same dcid apart from the differ's
        # legacy 'dcid:"..."' form.
        expected_schema = sorted((self._undo_differ_normalization(row['dcid']),
                                  row['value_combined'])
                                 for row in differ_schema)
        conv_schema_rows = sorted(
            (row['dcid'], row['value_combined']) for row in conv_schema)
        self.assertEqual(conv_schema_rows, expected_schema)


_PROVENANCE_ARGS = {
    'import_name': _PROVENANCE['import_name'],
    'version': _PROVENANCE['version'],
    'input_name': _PROVENANCE['input'],
}

if __name__ == '__main__':
    unittest.main()
