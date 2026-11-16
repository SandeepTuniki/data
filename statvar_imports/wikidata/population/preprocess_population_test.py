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
"""Tests for preprocess_population.py."""

import os
import tempfile
import unittest

from .preprocess_population import clean_date
from .preprocess_population import clean_value
from .preprocess_population import preprocess
from .preprocess_population import preprocess_rows

_TEST_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'test_data')


class CleanDateTest(unittest.TestCase):

    def test_year_precision(self):
        self.assertEqual(clean_date('2015-01-01T00:00:00Z', '9'), '2015')

    def test_month_precision(self):
        self.assertEqual(clean_date('1995-06-01T00:00:00Z', '10'), '1995-06')

    def test_day_precision_keeps_full_date(self):
        self.assertEqual(clean_date('2020-04-01T00:00:00Z', '11'), '2020-04-01')

    def test_first_of_january_becomes_year(self):
        self.assertEqual(clean_date('2010-01-01T00:00:00Z', '11'), '2010')

    def test_end_of_december_becomes_next_year(self):
        self.assertEqual(clean_date('1999-12-31T00:00:00Z', '11'), '2000')

    def test_coarse_precision_is_dropped(self):
        self.assertIsNone(clean_date('1960-01-01T00:00:00Z', '8'))
        self.assertIsNone(clean_date('1900-01-01T00:00:00Z', '7'))

    def test_negative_year_is_dropped(self):
        self.assertIsNone(clean_date('-0500-01-01T00:00:00Z', '9'))


class CleanValueTest(unittest.TestCase):

    def test_whole_numbers(self):
        self.assertEqual(clean_value('5244'), '5244')
        self.assertEqual(clean_value('+67460'), '67460')
        self.assertEqual(clean_value('007'), '7')

    def test_non_whole_numbers_are_dropped(self):
        self.assertIsNone(clean_value('958.2'))
        self.assertIsNone(clean_value('-5'))
        self.assertIsNone(clean_value(''))
        self.assertIsNone(clean_value('_:genid2-16-1638'))


class PreprocessRowsTest(unittest.TestCase):

    def _row(self, statement_id, rank, value, date='2015-01-01T00:00:00Z'):
        return {
            'dcid': 'geoId/01',
            'qid': 'Q1',
            'statement_id': statement_id,
            'rank': rank,
            'value': value,
            'date': date,
            'precision': '9',
        }

    def test_preferred_rank_wins(self):
        rows, _ = preprocess_rows([
            self._row('Q1-A', 'NormalRank', '100'),
            self._row('Q1-B', 'PreferredRank', '200'),
        ])
        self.assertEqual([r['population'] for r in rows], ['200'])

    def test_tie_is_broken_by_lowest_statement_id(self):
        first = [
            self._row('Q1-B', 'NormalRank', '200'),
            self._row('Q1-A', 'NormalRank', '100'),
        ]
        rows, counters = preprocess_rows(first)
        self.assertEqual([r['population'] for r in rows], ['100'])
        self.assertEqual(counters['dates_with_conflicting_values'], 1)
        # Same answer regardless of input order.
        rows_reversed, _ = preprocess_rows(list(reversed(first)))
        self.assertEqual(rows, rows_reversed)

    def test_deprecated_is_ignored_even_if_only_value(self):
        rows, counters = preprocess_rows(
            [self._row('Q1-A', 'DeprecatedRank', '100')])
        self.assertEqual(rows, [])
        self.assertEqual(counters['dropped_deprecated_rank'], 1)

    def test_same_value_twice_is_not_a_conflict(self):
        rows, counters = preprocess_rows([
            self._row('Q1-A', 'NormalRank', '100'),
            self._row('Q1-B', 'NormalRank', '100'),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(counters['dates_with_conflicting_values'], 0)


class PreprocessEndToEndTest(unittest.TestCase):

    def test_sample_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_csv = os.path.join(tmp_dir, 'out', 'output.csv')
            counters = preprocess(
                os.path.join(_TEST_DATA_DIR, 'preprocess_population_input.csv'),
                output_csv)
            with open(output_csv) as got, open(
                    os.path.join(_TEST_DATA_DIR,
                                 'preprocess_population_expected.csv')) as want:
                self.assertEqual(got.read(), want.read())
        self.assertEqual(counters['rows_read'], 14)
        self.assertEqual(counters['rows_written'], 6)
        self.assertEqual(counters['places_written'], 2)
        self.assertEqual(counters['dropped_missing_dcid'], 1)
        self.assertEqual(counters['dropped_deprecated_rank'], 1)
        self.assertEqual(counters['dropped_unusable_date'], 2)
        self.assertEqual(counters['dropped_non_integer_value'], 2)
        self.assertEqual(counters['dates_with_conflicting_values'], 2)


if __name__ == '__main__':
    unittest.main()
