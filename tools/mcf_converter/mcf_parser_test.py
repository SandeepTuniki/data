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
"""Unit tests for mcf_parser.py."""

import os
import sys
import tempfile
import unittest

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_SCRIPT_DIR)

import mcf_parser


def _write(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


class IterMcfNodesTest(unittest.TestCase):

    def test_splits_nodes_on_node_lines_blank_lines_and_comments(self):
        mcf = '\n'.join([
            '# A comment before the first node',
            'Node: dcid:A',
            'typeOf: dcs:Thing',
            '',
            'Node: B',
            'name: "Bee"',
            # A Node line ends the previous node even without a blank line.
            'Node: C',
            'name: "Cee"',
            '# A comment also ends the current node',
            'name: "orphan"',
        ])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'test.mcf')
            _write(path, mcf)
            nodes = list(mcf_parser.iter_mcf_nodes(path))

        self.assertEqual(nodes, [
            {
                'Node': 'dcid:A',
                'typeOf': 'dcs:Thing'
            },
            {
                'Node': 'B',
                'name': '"Bee"'
            },
            {
                'Node': 'C',
                'name': '"Cee"'
            },
            {
                'name': '"orphan"'
            },
        ])

    def test_repeated_properties_become_lists(self):
        mcf = '\n'.join([
            'Node: LocalPlace',
            'typeOf: dcs:Place',
            'typeOf: dcs:City',
            'typeOf: dcs:AdministrativeArea',
            'name: "Some City"',
        ])
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'test.mcf')
            _write(path, mcf)
            nodes = list(mcf_parser.iter_mcf_nodes(path))

        self.assertEqual(nodes, [{
            'Node': 'LocalPlace',
            'typeOf': ['dcs:Place', 'dcs:City', 'dcs:AdministrativeArea'],
            'name': '"Some City"',
        }])

    def test_values_keep_colons_after_the_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'test.mcf')
            _write(path, 'Node: X\nurl: "https://example.com/a"\n')
            nodes = list(mcf_parser.iter_mcf_nodes(path))

        self.assertEqual(nodes, [{
            'Node': 'X',
            'url': '"https://example.com/a"'
        }])

    def test_wildcard_reads_all_matching_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _write(os.path.join(tmpdir, 'a.mcf'), 'Node: A\nname: "a"\n')
            _write(os.path.join(tmpdir, 'b.mcf'), 'Node: B\nname: "b"\n')
            _write(os.path.join(tmpdir, 'ignored.txt'), 'Node: C\n')
            nodes = list(
                mcf_parser.iter_mcf_nodes(os.path.join(tmpdir, '*.mcf')))

        self.assertEqual(sorted(n['Node'] for n in nodes), ['A', 'B'])


class CleanValueTest(unittest.TestCase):

    def test_strips_namespace_prefixes(self):
        self.assertEqual(mcf_parser.clean_value('dcid:Count_Person'),
                         'Count_Person')
        self.assertEqual(mcf_parser.clean_value('dcs:Count_Person'),
                         'Count_Person')
        self.assertEqual(mcf_parser.clean_value('dcid:wikidataId/Q40888'),
                         'wikidataId/Q40888')

    def test_strips_enclosing_quotes(self):
        self.assertEqual(mcf_parser.clean_value('"2020-05"'), '2020-05')
        self.assertEqual(mcf_parser.clean_value('"dc/o/7dl8b0472kl2h"'),
                         'dc/o/7dl8b0472kl2h')
        # Text inside quotes is kept as is, including colons.
        self.assertEqual(mcf_parser.clean_value('"a: b"'), 'a: b')

    def test_keeps_values_without_prefix_or_quotes(self):
        self.assertEqual(mcf_parser.clean_value('geoId/06'), 'geoId/06')
        self.assertEqual(mcf_parser.clean_value('2020'), '2020')
        self.assertEqual(mcf_parser.clean_value('  P1Y  '), 'P1Y')

    def test_lists_are_cleaned_sorted_and_joined(self):
        self.assertEqual(
            mcf_parser.clean_value(['dcs:Place', 'dcid:City', '"Area"']),
            'Area, City, Place')

    def test_empty_values(self):
        self.assertEqual(mcf_parser.clean_value(None), '')
        self.assertEqual(mcf_parser.clean_value(''), '')


if __name__ == '__main__':
    unittest.main()
