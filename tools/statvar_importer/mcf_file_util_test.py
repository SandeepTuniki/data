# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for stat_var_processor.py."""

from collections import OrderedDict
import os
import sys
import tempfile
import unittest

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_SCRIPT_DIR)
sys.path.append(os.path.dirname(_SCRIPT_DIR))
sys.path.append(
    os.path.join(os.path.dirname(os.path.dirname(_SCRIPT_DIR)), 'util'))

from counters import Counters
from mcf_diff import diff_mcf_nodes
import mcf_file_util

# module_dir_ is the path to where this test is running from.
_module_dir_ = os.path.dirname(__file__)


class TestMCFFileUtil(unittest.TestCase):

    def setUp(self):
        # logging.set_verbosity(2)
        self.maxDiff = None

    def test_strip_namespace(self):
        self.assertEqual(mcf_file_util.strip_namespace('Count_Person'),
                         'Count_Person')
        self.assertEqual(mcf_file_util.strip_namespace('dcs:Count_Person'),
                         'Count_Person')
        self.assertEqual(mcf_file_util.strip_namespace('dcid:Count_Person'),
                         'Count_Person')
        self.assertEqual(mcf_file_util.strip_namespace('"abc:123"'),
                         '"abc:123"')
        self.assertEqual(mcf_file_util.strip_namespace(10), 10)

    def test_add_namespace(self):
        self.assertEqual(mcf_file_util.add_namespace('Count_Person'),
                         'dcid:Count_Person')
        self.assertEqual(mcf_file_util.add_namespace('dcs:Count_Person'),
                         'dcs:Count_Person')
        self.assertEqual(mcf_file_util.add_namespace('"abc 123"'), '"abc 123"')
        self.assertEqual(mcf_file_util.add_namespace(10), 10)

    def test_normalize_mcf_node(self):
        mcf_dict = {
            '#': 'Comment',
            'Node': 'dcid:Count_Person_10Years',
            'typeOf': 'dcs:StatisticalVariable',
            'populationType': 'schema:Person',
            'measuredProperty': 'count',
            'age': '[10 Years]',
            # List of values are sorted
            'name': ['"Count of children"', '"Child Population"'],
            # String value with , is not sorted.
            'description': '"people of 10 years,number of children"',
        }
        normalized_node = mcf_file_util.normalize_mcf_node(mcf_dict)
        # Properties are ordered aphabetically.
        expected_node = OrderedDict([
            ('Node', 'dcid:Count_Person_10Years'),
            ('age', '[10 Years]'),
            ('description', '"people of 10 years,number of children"'),
            ('measuredProperty', 'dcid:count'),
            ('name', '"Child Population","Count of children"'),
            ('populationType', 'dcid:Person'),
            ('typeOf', 'dcid:StatisticalVariable'),
        ])
        self.assertEqual(expected_node, normalized_node)
        # Normalize quantity range to dcid
        normalized_node = mcf_file_util.normalize_mcf_node(
            mcf_dict, quantity_range_to_dcid=True)
        expected_node['age'] = 'dcid:Years10'
        self.assertEqual(expected_node, normalized_node)

    def test_normalize_range(self):
        self.assertEqual('[10 20 Years]',
                         mcf_file_util.normalize_value('[ 10 20 Years ]'))
        self.assertEqual(
            'dcid:Years10To20',
            mcf_file_util.normalize_value('[ 10 20 Years ]', True),
        )
        self.assertEqual('[- 20 Years]',
                         mcf_file_util.normalize_value('[ - 20 Years ]'))
        self.assertEqual(
            'dcid:YearsUpto20',
            mcf_file_util.normalize_value('[ - 20 Years ]', True),
        )
        self.assertEqual('dcid:Years20',
                         mcf_file_util.normalize_value('[ 20  Years ]', True))
        self.assertEqual(
            'dcid:Years20Onwards',
            mcf_file_util.normalize_value('[ 20 - Years ]', True),
        )
        self.assertEqual('dcid:Upto20',
                         mcf_file_util.normalize_value('[ - 20 ]', True))

    def test_normalize_value_list_with_brackets(self):
        # Test bracketed lists of values with commas.
        self.assertEqual(
            '[dcid:A,dcid:B]',
            mcf_file_util.normalize_value('[dcid:B,dcid:A]'),
        )
        self.assertEqual(
            '[dcid:A,dcid:B]',
            mcf_file_util.normalize_value('[ dcid:B, dcid:A ]'),
        )
        self.assertEqual(
            '["ValueA","ValueB"]',
            mcf_file_util.normalize_value('[ "ValueB", "ValueA" ]'),
        )

    def test_load_mcf_file(self):
        mcf_nodes = mcf_file_util.load_mcf_nodes(
            os.path.join(_module_dir_, 'test_data',
                         'sample_output_stat_vars.mcf'))
        self.assertTrue(len(mcf_nodes) > 1)

        # Verify all nodes are keyed by dcid and have expected properties.
        for dcid, node in mcf_nodes.items():
            for expected_prop in ['Node', 'typeOf']:
                self.assertTrue(expected_prop in node)
            self.assertEqual(mcf_file_util.strip_namespace(dcid),
                             mcf_file_util.strip_namespace(node['Node']))

        # Verify loading node with additional property added to existing node.
        dcid = list(mcf_nodes.keys())[0]
        old_node = mcf_nodes[dcid]
        # Change node type to non-StatVar to allow addition of property
        old_node['typeOf'] = 'dcid:TestNode'
        new_node = {'Node': old_node['Node']}
        # Copy some properties
        for prop in list(old_node.keys())[len(old_node) - 2:]:
            new_node[prop] = old_node[prop]
        # Add a new property
        new_node['newProperty'] = 'dcid:NewValue'
        new_node['typeOf'] = 'dcid:TestNode'
        expected_node = dict(old_node)
        expected_node.update(new_node)
        with tempfile.TemporaryDirectory() as tmp_dir:
            new_node_file = os.path.join(tmp_dir, 'new_node.mcf')
            mcf_file_util.write_mcf_nodes(
                node_dicts=[{
                    dcid: new_node
                }],
                filename=new_node_file,
                default_pvs={},
                header='# Node with new property',
            )

            # Load the new node into the dict with old nodes.
            mcf_file_util.load_mcf_nodes(new_node_file, mcf_nodes)

            # Verify the updated node is a union of all PVs.
            self.assertTrue(old_node != new_node)
            self.assertTrue(expected_node != old_node)
            self.assertTrue(expected_node != new_node)
            diff_str = diff_mcf_nodes({dcid: expected_node},
                                      {dcid: mcf_nodes[dcid]})
            self.assertEqual(diff_str, '')

    def test_get_numeric_value(self):
        self.assertEqual(2010, mcf_file_util.get_numeric_value('2010'))
        self.assertEqual(2020, mcf_file_util.get_numeric_value('2020.0'))
        self.assertEqual(5.6, mcf_file_util.get_numeric_value('05.6%'))
        self.assertEqual(123456, mcf_file_util.get_numeric_value('1,23,456.0'))
        self.assertEqual(10.123, mcf_file_util.get_numeric_value('10.123'))
        self.assertEqual(-1.23, mcf_file_util.get_numeric_value('-1.23'))
        self.assertEqual(
            1234567.8,
            mcf_file_util.get_numeric_value(
                '1 234 567,8',
                decimal_char=',',
                separator_chars=' ',
            ))
        self.assertEqual(None, mcf_file_util.get_numeric_value('not number'))
        self.assertEqual(None, mcf_file_util.get_numeric_value('10e5'))

    def test_check_nodes_can_merge(self):
        node1 = {
            'Node': 'dcs:TestNode1',
            'typeOf': 'dcs:TestNode',
            'prop1': 'dcs:Value1',
            'prop2': 'dcid:Value2',
        }
        node2 = {
            'Node': 'dcs:TestNode1',
            'typeOf': 'dcs:TestNode',
            'prop1': 'dcs:Value1',
            'prop3': 'dcid:Value3',
        }

        nodes = {}
        mcf_file_util.add_mcf_node(node1, nodes)
        # Nodes with additional property can be merged.
        self.assertTrue(mcf_file_util.check_nodes_can_merge(node1, node2))
        self.assertTrue(mcf_file_util.add_mcf_node(node2, nodes))
        self.assertEqual(
            nodes['dcid:TestNode1'], {
                'Node': 'dcs:TestNode1',
                'prop1': 'dcs:Value1',
                'prop2': 'dcid:Value2',
                'prop3': 'dcid:Value3',
                'typeOf': 'dcs:TestNode'
            })

        # Statvar nodes with new properties cannot be merged.
        node1['typeOf'] = 'dcs:StatisticalVariable'
        node2['typeOf'] = 'dcid:StatisticalVariable'
        self.assertFalse(mcf_file_util.check_nodes_can_merge(node1, node2))

        # Statvar nodes with new values for existing properties cannot be merged.
        node1['typeOf'] = 'StatisticalVariable'
        node3 = dict(node1)
        node3['prop1'] = 'dcid:NewValue'
        self.assertFalse(mcf_file_util.check_nodes_can_merge(node1, node2))

        # Statvar nodes can't be merged with non-Statvar node
        node1['typeOf'] = 'StatisticalVariable'
        node3 = dict(node1)
        node3['typeOf'] = 'dcid:TestNode'
        self.assertFalse(mcf_file_util.check_nodes_can_merge(node1, node2))

        # Statvar nodes with names can be merged
        node3 = {
            'Node': 'dcid:TestNode1',
            'typeOf': 'dcid:StatisticalVariable',
            'prop1': 'dcs:Value1',
            'prop2': 'dcid:Value2',
            'name': 'test node with name',
            'description': 'TestNode with addiotnal name can be merged.'
        }
        self.assertTrue(mcf_file_util.check_nodes_can_merge(node1, node3))

    def test_is_valid_property(self):
        self.assertTrue(mcf_file_util.is_valid_property('prop1'))
        self.assertFalse(mcf_file_util.is_valid_property('#prop1'))
        self.assertFalse(mcf_file_util.is_valid_property('Prop1'))
        self.assertFalse(mcf_file_util.is_valid_property(1))

    def test_is_leaf_object(self):
        self.assertFalse(mcf_file_util.is_leaf_object('prop1'))
        self.assertFalse(mcf_file_util.is_leaf_object('#prop1'))
        self.assertFalse(mcf_file_util.is_leaf_object('Value1'))
        self.assertFalse(mcf_file_util.is_leaf_object('dcid:Value1'))
        self.assertTrue(mcf_file_util.is_leaf_object(1))
        self.assertTrue(mcf_file_util.is_leaf_object(1.1))
        self.assertTrue(mcf_file_util.is_leaf_object('1.1'))
        self.assertTrue(mcf_file_util.is_leaf_object('"abc"'))
        self.assertTrue(mcf_file_util.is_leaf_object('"abc",1.2'))
        self.assertTrue(mcf_file_util.is_leaf_object('"abc",1.2,"def"'))

    def test_is_number(self):
        self.assertTrue(mcf_file_util.is_number('1'))
        self.assertTrue(mcf_file_util.is_number('1.1'))
        self.assertFalse(mcf_file_util.is_number('1.1.1'))
        self.assertTrue(mcf_file_util.is_number(1.1))
        self.assertTrue(mcf_file_util.is_number(1))


class TestAddPVToNode(unittest.TestCase):

    def test_add_pv_to_node_new_property(self):
        node = {}
        mcf_file_util.add_pv_to_node("prop1", "value1", node, normalize=False)
        self.assertEqual(node, {"prop1": "value1"})

    def test_add_pv_to_node_existing_property_append(self):
        node = {"prop1": "value1"}
        mcf_file_util.add_pv_to_node("prop1", "value2", node, normalize=False)
        self.assertEqual(node, {"prop1": "value1,value2"})

    def test_add_pv_to_node_existing_property_replace(self):
        node = {"prop1": "value1"}
        mcf_file_util.add_pv_to_node("prop1",
                                     "value2",
                                     node,
                                     append_value=False,
                                     normalize=False)
        self.assertEqual(node, {"prop1": "value2"})

    def test_add_pv_to_node_strip_namespaces(self):
        node = {}
        mcf_file_util.add_pv_to_node("prop1",
                                     "dcid:value1",
                                     node,
                                     strip_namespaces=True,
                                     normalize=False)
        self.assertEqual(node, {"prop1": "value1"})

    def test_add_pv_to_node_normalize_strip_namespaces(self):
        node = {}
        mcf_file_util.add_pv_to_node("prop1",
                                     "dcid:value1",
                                     node,
                                     strip_namespaces=True,
                                     normalize=True)
        self.assertEqual(node, {"prop1": "value1"})

    def test_add_pv_to_node_spaces_not_stripped(self):
        node = {}
        mcf_file_util.add_pv_to_node("prop1",
                                     "  value1  ",
                                     node,
                                     normalize=False)
        self.assertEqual(node, {"prop1": "  value1  "})

    def test_add_pv_to_node_normalize_strip_spaces(self):
        node = {}
        mcf_file_util.add_pv_to_node("prop1",
                                     "  value1  ",
                                     node,
                                     normalize=True)
        self.assertEqual(node, {"prop1": "value1"})

    def test_add_pv_to_node_comma_separated_values(self):
        node = {}
        mcf_file_util.add_pv_to_node("prop1",
                                     "value1, value2,value3",
                                     node,
                                     normalize=False)
        self.assertEqual(node, {"prop1": "value1, value2,value3"})

    def test_add_pv_to_node_normalized_comma_separated_values(self):
        node = {}
        mcf_file_util.add_pv_to_node("prop1",
                                     "value1, value2,value3",
                                     node,
                                     normalize=True)
        self.assertEqual(node, {"prop1": "dcid:value1,dcid:value2,dcid:value3"})

    def test_add_pv_to_node_list_values_no_normalize(self):
        node = {}
        mcf_file_util.add_pv_to_node("prop1", ["value1", "value2"],
                                     node,
                                     normalize=False)
        self.assertEqual(node, {"prop1": "value1,value2"})

    def test_add_pv_to_node_normalize_list_values(self):
        node = {}
        mcf_file_util.add_pv_to_node("prop1", ["value1", "value2"],
                                     node,
                                     normalize=True)
        self.assertEqual(node, {"prop1": "dcid:value1,dcid:value2"})

    def test_add_pv_to_node_existing_list_values(self):
        node = {"prop1": "value1,value2"}
        mcf_file_util.add_pv_to_node("prop1", "value3", node, normalize=False)
        self.assertEqual(node, {"prop1": "value1,value2,value3"})

    def test_add_pv_to_node_existing_list_values_with_duplicates(self):
        node = {"prop1": "value1,value2"}
        mcf_file_util.add_pv_to_node("prop1",
                                     "value2,value3",
                                     node,
                                     normalize=False)
        self.assertEqual(node, {"prop1": "value1,value2,value3"})

    def test_add_pv_to_node_existing_list_values_with_duplicates_and_quotes(
            self):
        node = {"prop1": '"value1",value2'}
        mcf_file_util.add_pv_to_node("prop1",
                                     '"value2",value3',
                                     node,
                                     normalize=False)
        self.assertEqual(node, {"prop1": '"value1","value2","value3"'})

    def test_add_pv_to_node_existing_list_values_with_quotes_and_spaces_and_duplicates(
            self):
        node = {"prop1": '"value , 1",value2'}
        mcf_file_util.add_pv_to_node("prop1",
                                     '"value , 1",value3',
                                     node,
                                     normalize=False)
        self.assertEqual(node, {"prop1": '"value , 1","value2","value3"'})

    def test_add_pv_node_mixed_types(self):
        node = {"prop1": 'value1'}
        mcf_file_util.add_pv_to_node("prop1", 10, node, normalize=False)
        self.assertEqual(node, {"prop1": 'value1,10'})


def _write_mcf_file(dir_name: str, filename: str, lines: list) -> str:
    """Writes `lines` joined by newlines to `filename` and returns its path."""
    path = os.path.join(dir_name, filename)
    with open(path, 'w') as output_file:
        output_file.write('\n'.join(lines))
    return path


def _read_text_file(path: str) -> str:
    with open(path) as input_file:
        return input_file.read()


class TestLoadMcfNodes(unittest.TestCase):

    def setUp(self):
        self.maxDiff = None
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_dir = tmp_dir.name

    def test_loads_blank_line_separated_nodes_keyed_by_namespaced_dcid(self):
        # The last node has no trailing blank line and its Node has no prefix.
        mcf_file = _write_mcf_file(self.tmp_dir, 'nodes.mcf', [
            'Node: dcid:Count_Person',
            'typeOf: dcs:StatisticalVariable',
            'populationType: dcs:Person',
            '',
            'Node: Count_Household',
            'typeOf: dcs:StatisticalVariable',
            'populationType: dcs:Household',
        ])

        nodes = mcf_file_util.load_mcf_nodes(mcf_file)

        self.assertEqual(
            nodes, {
                'dcid:Count_Person': {
                    'Node': 'dcid:Count_Person',
                    'typeOf': 'dcs:StatisticalVariable',
                    'populationType': 'dcs:Person',
                },
                'dcid:Count_Household': {
                    'Node': 'Count_Household',
                    'typeOf': 'dcs:StatisticalVariable',
                    'populationType': 'dcs:Household',
                },
            })

    def test_keeps_property_order_from_file(self):
        mcf_file = _write_mcf_file(self.tmp_dir, 'node.mcf', [
            'Node: dcid:Count_Person',
            'populationType: dcs:Person',
            'typeOf: dcs:StatisticalVariable',
            'measuredProperty: dcs:count',
        ])

        nodes = mcf_file_util.load_mcf_nodes(mcf_file)

        self.assertEqual(
            list(nodes['dcid:Count_Person'].keys()),
            ['Node', 'populationType', 'typeOf', 'measuredProperty'])

    def test_keeps_comments_as_numbered_properties(self):
        mcf_file = _write_mcf_file(self.tmp_dir, 'node.mcf', [
            'Node: dcid:Count_Person',
            '# Source: census',
            'typeOf: dcs:StatisticalVariable',
            '# Reviewed',
        ])

        nodes = mcf_file_util.load_mcf_nodes(mcf_file)

        self.assertEqual(
            nodes['dcid:Count_Person'], {
                'Node': 'dcid:Count_Person',
                '# comment1': '# Source: census',
                'typeOf': 'dcs:StatisticalVariable',
                '# comment2': '# Reviewed',
            })

    def test_handles_quoting_from_google_sheets_export(self):
        # Sheets exports wrap each line in quotes, escape quotes as "" and
        # write empty lines as "".
        mcf_file = _write_mcf_file(self.tmp_dir, 'sheets.mcf', [
            '"Node: dcid:Count_Person"',
            '"name: ""Total Population"""',
            '""',
            '"Node: dcid:Count_Household"',
            '"name: ""Total Households"""',
        ])

        nodes = mcf_file_util.load_mcf_nodes(mcf_file)

        self.assertEqual(
            nodes, {
                'dcid:Count_Person': {
                    'Node': 'dcid:Count_Person',
                    'name': '"Total Population"',
                },
                'dcid:Count_Household': {
                    'Node': 'dcid:Count_Household',
                    'name': '"Total Households"',
                },
            })

    def test_strip_namespaces_removes_prefixes_from_keys_and_values(self):
        mcf_file = _write_mcf_file(self.tmp_dir, 'node.mcf', [
            'Node: dcid:Count_Person',
            'typeOf: dcs:StatisticalVariable',
        ])

        nodes = mcf_file_util.load_mcf_nodes(mcf_file, strip_namespaces=True)

        self.assertEqual(
            nodes, {
                'Count_Person': {
                    'Node': 'Count_Person',
                    'typeOf': 'StatisticalVariable',
                },
            })

    def test_merges_properties_of_same_node_from_comma_separated_files(self):
        file1 = _write_mcf_file(self.tmp_dir, 'file1.mcf', [
            'Node: dcid:Ohio',
            'typeOf: dcs:State',
        ])
        file2 = _write_mcf_file(self.tmp_dir, 'file2.mcf', [
            'Node: dcid:Ohio',
            'name: "Ohio"',
        ])

        nodes = mcf_file_util.load_mcf_nodes(f'{file1},{file2}')

        self.assertEqual(
            nodes, {
                'dcid:Ohio': {
                    'Node': 'dcid:Ohio',
                    'typeOf': 'dcs:State',
                    'name': '"Ohio"',
                },
            })

    def test_loads_all_files_matching_a_wildcard_pattern(self):
        _write_mcf_file(self.tmp_dir, 'ohio.mcf', ['Node: dcid:Ohio'])
        _write_mcf_file(self.tmp_dir, 'utah.mcf', ['Node: dcid:Utah'])

        nodes = mcf_file_util.load_mcf_nodes(os.path.join(
            self.tmp_dir, '*.mcf'))

        self.assertCountEqual(nodes.keys(), ['dcid:Ohio', 'dcid:Utah'])

    def test_append_values_combines_values_for_existing_property(self):
        file1 = _write_mcf_file(self.tmp_dir, 'file1.mcf', [
            'Node: dcid:Ohio',
            'name: "Old name"',
        ])
        file2 = _write_mcf_file(self.tmp_dir, 'file2.mcf', [
            'Node: dcid:Ohio',
            'name: "New name"',
        ])

        nodes = mcf_file_util.load_mcf_nodes(f'{file1},{file2}')

        # Only membership is checked: the order of appended values is not
        # consistent yet.
        self.assertCountEqual(
            mcf_file_util.get_value_list(nodes['dcid:Ohio']['name']),
            ['"Old name"', '"New name"'])

    def test_append_values_false_replaces_value_for_existing_property(self):
        file1 = _write_mcf_file(self.tmp_dir, 'file1.mcf', [
            'Node: dcid:Ohio',
            'name: "Old name"',
        ])
        file2 = _write_mcf_file(self.tmp_dir, 'file2.mcf', [
            'Node: dcid:Ohio',
            'name: "New name"',
        ])

        nodes = mcf_file_util.load_mcf_nodes(f'{file1},{file2}',
                                             append_values=False)

        self.assertEqual(nodes['dcid:Ohio']['name'], '"New name"')

    def test_keeps_first_definition_when_statvar_conflicts_and_continues(self):
        # The conflicting statvar is not the last node in the file, so the
        # nodes after it must still be loaded.
        mcf_file = _write_mcf_file(self.tmp_dir, 'nodes.mcf', [
            'Node: dcid:Count_Person',
            'typeOf: dcs:StatisticalVariable',
            'populationType: dcs:Person',
            '',
            'Node: dcid:Count_Person',
            'typeOf: dcs:StatisticalVariable',
            'populationType: dcs:Household',
            '',
            'Node: dcid:Ohio',
            'typeOf: dcs:State',
        ])
        counters = Counters()

        nodes = mcf_file_util.load_mcf_nodes(mcf_file, counters=counters)

        self.assertEqual(nodes['dcid:Count_Person']['populationType'],
                         'dcs:Person')
        self.assertIn('dcid:Ohio', nodes)
        self.assertEqual(counters.get_counter('error-mcf-node-merge'), 1)

    def test_adds_loaded_nodes_to_the_given_nodes_dict(self):
        existing_nodes = {'dcid:Utah': {'Node': 'dcid:Utah'}}
        mcf_file = _write_mcf_file(self.tmp_dir, 'ohio.mcf',
                                   ['Node: dcid:Ohio'])

        nodes = mcf_file_util.load_mcf_nodes(mcf_file, nodes=existing_nodes)

        self.assertIs(nodes, existing_nodes)
        self.assertCountEqual(nodes.keys(), ['dcid:Utah', 'dcid:Ohio'])


class TestWriteMcfNodes(unittest.TestCase):

    def setUp(self):
        self.maxDiff = None
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.tmp_dir = tmp_dir.name

    def test_writes_default_properties_first_and_adds_dcid_namespace(self):
        nodes = {
            'dcid:Count_Person': {
                'customProp': 'Value',
                'populationType': 'dcs:Person',
                'name': '"Total population"',
                'typeOf': 'dcs:StatisticalVariable',
                'Node': 'dcid:Count_Person',
            }
        }
        output_file = os.path.join(self.tmp_dir, 'out.mcf')

        mcf_file_util.write_mcf_nodes(nodes, output_file)

        self.assertEqual(
            _read_text_file(output_file), 'Node: dcid:Count_Person\n'
            'typeOf: dcs:StatisticalVariable\n'
            'name: "Total population"\n'
            'populationType: dcs:Person\n'
            'customProp: dcid:Value\n'
            '\n')

    def test_writes_comments_as_is(self):
        nodes = {
            'dcid:Ohio': {
                '# comment1': '# Source: census',
                'Node': 'dcid:Ohio',
                'typeOf': 'dcs:State',
            }
        }
        output_file = os.path.join(self.tmp_dir, 'out.mcf')

        mcf_file_util.write_mcf_nodes(nodes, output_file)

        self.assertEqual(
            _read_text_file(output_file), '# Source: census\n'
            'Node: dcid:Ohio\n'
            'typeOf: dcs:State\n'
            '\n')

    def test_writes_header_before_nodes(self):
        ohio = {'dcid:Ohio': {'Node': 'dcid:Ohio'}}
        output_file = os.path.join(self.tmp_dir, 'out.mcf')

        mcf_file_util.write_mcf_nodes(ohio,
                                      output_file,
                                      header='# Generated file')

        self.assertEqual(_read_text_file(output_file),
                         '# Generated file\nNode: dcid:Ohio\n\n')

    def test_append_mode_adds_nodes_after_existing_content(self):
        ohio = {'dcid:Ohio': {'Node': 'dcid:Ohio'}}
        utah = {'dcid:Utah': {'Node': 'dcid:Utah'}}
        output_file = os.path.join(self.tmp_dir, 'out.mcf')
        mcf_file_util.write_mcf_nodes(ohio, output_file)

        mcf_file_util.write_mcf_nodes(utah, output_file, mode='a')

        self.assertEqual(_read_text_file(output_file),
                         'Node: dcid:Ohio\n\nNode: dcid:Utah\n\n')

    def test_sort_orders_nodes_by_dcid_and_normalizes_properties(self):
        nodes = {
            'dcid:Utah': {
                'Node': 'dcid:Utah',
                '# comment1': '# Dropped when sorting',
                'typeOf': 'dcs:State',
            },
            'dcid:Ohio': {
                'Node': 'dcid:Ohio',
                'zipCode': 'dcs:Zip43004',
                'typeOf': 'dcs:State',
                'areaCode': 'dcs:Code216',
            },
        }
        output_file = os.path.join(self.tmp_dir, 'out.mcf')

        mcf_file_util.write_mcf_nodes(nodes, output_file, sort=True)

        # Default properties (Node, typeOf) come first, then the remaining
        # properties in alphabetical order.
        self.assertEqual(
            _read_text_file(output_file), 'Node: dcid:Ohio\n'
            'typeOf: dcid:State\n'
            'areaCode: dcid:Code216\n'
            'zipCode: dcid:Zip43004\n'
            '\n'
            'Node: dcid:Utah\n'
            'typeOf: dcid:State\n'
            '\n')

    def test_assigns_local_dcid_to_node_without_one_without_changing_input(
            self):
        node = {'typeOf': 'dcs:State', 'name': '"Ohio"'}
        output_file = os.path.join(self.tmp_dir, 'out.mcf')

        mcf_file_util.write_mcf_nodes({'my_key': node}, output_file)

        # The local dcid is 'l:<output file name>/' + md5('my_key').
        self.assertEqual(
            _read_text_file(output_file),
            'Node: l:out.mcf/9ed6e46a3ff88783ff75296a4ba523f9\n'
            'typeOf: dcs:State\n'
            'name: "Ohio"\n'
            '\n')
        self.assertEqual(node, {'typeOf': 'dcs:State', 'name': '"Ohio"'})

    def test_csv_output_can_be_loaded_back(self):
        nodes = {
            'dcid:Count_Person': {
                'Node': 'dcid:Count_Person',
                'typeOf': 'dcs:StatisticalVariable',
                'name': '"Total population"',
            }
        }
        csv_file = os.path.join(self.tmp_dir, 'nodes.csv')

        mcf_file_util.write_mcf_nodes(nodes, csv_file)
        loaded_nodes = mcf_file_util.load_mcf_nodes(csv_file)

        self.assertEqual(loaded_nodes, nodes)

    def test_mcf_output_can_be_loaded_back_for_sample_statvars(self):
        nodes = mcf_file_util.load_mcf_nodes(
            os.path.join(_module_dir_, 'test_data',
                         'us_census_B01001_output_stat_vars.mcf'))
        output_file = os.path.join(self.tmp_dir, 'out.mcf')

        mcf_file_util.write_mcf_nodes(nodes, output_file)
        reloaded_nodes = mcf_file_util.load_mcf_nodes(output_file)

        self.assertEqual(len(nodes), 49)
        self.assertEqual(reloaded_nodes, nodes)


class TestNodeHelpers(unittest.TestCase):

    def test_add_comment_to_node_numbers_comments_and_skips_duplicates(self):
        node = {}

        mcf_file_util.add_comment_to_node('# First', node)
        mcf_file_util.add_comment_to_node('# Second', node)
        mcf_file_util.add_comment_to_node('# First', node)

        self.assertEqual(node, {
            '# comment1': '# First',
            '# comment2': '# Second'
        })

    def test_get_node_dcid_prefers_dcid_and_strips_quotes_and_namespace(self):
        node = {'Node': 'dcid:OldId', 'dcid': '"dcs:NewId"'}

        self.assertEqual(mcf_file_util.get_node_dcid(node), 'NewId')

    def test_get_node_dcid_returns_empty_string_for_empty_node(self):
        self.assertEqual(mcf_file_util.get_node_dcid({}), '')

    def test_check_nodes_can_merge_rejects_different_dcids(self):
        self.assertFalse(
            mcf_file_util.check_nodes_can_merge({'Node': 'dcid:Ohio'},
                                                {'Node': 'dcid:Utah'}))

    def test_check_nodes_can_merge_rejects_statvar_with_multiple_values(self):
        node1 = {
            'Node': 'dcid:Count_Person',
            'typeOf': 'dcs:StatisticalVariable',
            'gender': 'dcs:Female,dcs:Male',
        }
        node2 = {
            'Node': 'dcid:Count_Person',
            'typeOf': 'dcs:StatisticalVariable',
            'gender': 'dcs:Female',
        }

        self.assertFalse(mcf_file_util.check_nodes_can_merge(node1, node2))

    def test_add_mcf_node_leaves_existing_node_unchanged_on_conflict(self):
        nodes = {
            'dcid:Count_Person': {
                'Node': 'dcid:Count_Person',
                'typeOf': 'dcs:StatisticalVariable',
                'populationType': 'dcs:Person',
            }
        }
        conflicting_node = {
            'Node': 'dcid:Count_Person',
            'typeOf': 'dcs:StatisticalVariable',
            'populationType': 'dcs:Household',
        }
        counters = Counters()

        added = mcf_file_util.add_mcf_node(conflicting_node,
                                           nodes,
                                           counters=counters)

        self.assertFalse(added)
        self.assertEqual(nodes['dcid:Count_Person']['populationType'],
                         'dcs:Person')
        self.assertEqual(counters.get_counter('error-mcf-node-merge'), 1)

    def test_update_mcf_nodes_merges_nodes_into_output(self):
        output_nodes = {
            'dcid:Ohio': {
                'Node': 'dcid:Ohio',
                'typeOf': 'dcs:State'
            }
        }
        new_nodes = {
            'dcid:Ohio': {
                'Node': 'dcid:Ohio',
                'name': '"Ohio"'
            },
            'dcid:Utah': {
                'Node': 'dcid:Utah'
            },
        }

        mcf_file_util.update_mcf_nodes(new_nodes, output_nodes)

        self.assertEqual(
            output_nodes, {
                'dcid:Ohio': {
                    'Node': 'dcid:Ohio',
                    'typeOf': 'dcs:State',
                    'name': '"Ohio"'
                },
                'dcid:Utah': {
                    'Node': 'dcid:Utah'
                },
            })


class TestNormalizeValue(unittest.TestCase):
    """normalize_value is relied on by import_differ to avoid reporting
    differences that are only in formatting."""

    def test_adds_dcid_namespace(self):
        self.assertEqual(mcf_file_util.normalize_value('Person'), 'dcid:Person')

    def test_replaces_other_namespace_with_dcid(self):
        self.assertEqual(mcf_file_util.normalize_value('schema:Person'),
                         'dcid:Person')

    def test_keeps_quoted_string(self):
        self.assertEqual(mcf_file_util.normalize_value('"Total population"'),
                         '"Total population"')

    def test_quotes_text_with_spaces(self):
        self.assertEqual(mcf_file_util.normalize_value('Total population'),
                         '"Total population"')

    def test_sorts_list_and_adds_dcid_namespace(self):
        self.assertEqual(mcf_file_util.normalize_value('dcs:Male, Female'),
                         'dcid:Female,dcid:Male')

    def test_sorts_concatenated_dcids(self):
        self.assertEqual(mcf_file_util.normalize_value('dcs:Male__Female'),
                         'dcid:Female__Male')

    def test_keeps_quoted_string_longer_than_100_characters_as_is(self):
        # 107 characters. Shorter strings with commas would be split into a
        # list.
        coordinates = ('"[[-82.5, 39.1], [-82.6, 39.2], [-82.7, 39.3], '
                       '[-82.8, 39.4], [-82.9, 39.5], [-83.0, 39.6], '
                       '[-83.1, 39.7]]"')

        self.assertEqual(mcf_file_util.normalize_value(coordinates),
                         coordinates)

    def test_keeps_empty_string(self):
        self.assertEqual(mcf_file_util.normalize_value(''), '')


class TestValueHelpers(unittest.TestCase):

    def test_normalize_pv_joins_property_and_normalized_value(self):
        self.assertEqual(
            mcf_file_util.normalize_pv(' typeOf ', 'dcs:StatisticalVariable'),
            'typeOf:dcid:StatisticalVariable')

    def test_get_numeric_value_returns_zero_for_zero(self):
        self.assertEqual(mcf_file_util.get_numeric_value('0'), 0)

    def test_get_numeric_value_accepts_plus_sign(self):
        self.assertEqual(mcf_file_util.get_numeric_value('+5'), 5)

    def test_get_numeric_value_accepts_periods_as_thousands_separators(self):
        self.assertEqual(mcf_file_util.get_numeric_value('1.234.567'), 1234567)


class TestFilterMcfNodes(unittest.TestCase):

    def setUp(self):
        self.nodes = {
            'dcid:Ohio': {
                'Node': 'dcid:Ohio',
                'typeOf': 'dcs:State',
                'name': '"Ohio"',
            },
            'dcid:Count_Person': {
                'Node': 'dcid:Count_Person',
                'typeOf': 'dcs:StatisticalVariable',
                'populationType': 'dcs:Person',
            },
            'dcid:GenderEnum': {
                'Node': 'dcid:GenderEnum',
                'typeOf': 'dcs:Class',
            },
        }

    def test_returns_all_nodes_without_filters(self):
        filtered = mcf_file_util.filter_mcf_nodes(self.nodes)

        self.assertEqual(filtered, self.nodes)

    def test_allow_dcids_keeps_only_listed_nodes(self):
        filtered = mcf_file_util.filter_mcf_nodes(
            self.nodes, allow_dcids=['Ohio', 'GenderEnum'])

        self.assertCountEqual(filtered.keys(), ['dcid:Ohio', 'dcid:GenderEnum'])

    def test_allow_dcids_accepts_dcids_with_namespace(self):
        filtered = mcf_file_util.filter_mcf_nodes(self.nodes,
                                                  allow_dcids=['dcid:Ohio'])

        self.assertCountEqual(filtered.keys(), ['dcid:Ohio'])

    def test_ignore_nodes_with_pv_drops_nodes_with_listed_value(self):
        filtered = mcf_file_util.filter_mcf_nodes(
            self.nodes, ignore_nodes_with_pv=['typeOf:State'])

        self.assertCountEqual(filtered.keys(),
                              ['dcid:Count_Person', 'dcid:GenderEnum'])

    def test_ignore_nodes_with_pv_without_value_drops_nodes_with_property(self):
        filtered = mcf_file_util.filter_mcf_nodes(
            self.nodes, ignore_nodes_with_pv=['populationType'])

        self.assertCountEqual(filtered.keys(), ['dcid:Ohio', 'dcid:GenderEnum'])

    def test_allow_nodes_with_pv_keeps_nodes_with_any_listed_value(self):
        # Ohio has other properties (Node, name) that are not listed, and is
        # still kept because its typeOf matches.
        filtered = mcf_file_util.filter_mcf_nodes(
            self.nodes, allow_nodes_with_pv=['typeOf:State', 'typeOf:Class'])

        self.assertCountEqual(filtered.keys(), ['dcid:Ohio', 'dcid:GenderEnum'])

    def test_allow_nodes_with_pv_without_value_keeps_nodes_with_property(self):
        filtered = mcf_file_util.filter_mcf_nodes(
            self.nodes, allow_nodes_with_pv=['populationType'])

        self.assertCountEqual(filtered.keys(), ['dcid:Count_Person'])
