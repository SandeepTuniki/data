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
"""Tests for fetch_population.py."""

import csv
import os
import tempfile
import unittest
import urllib.parse

import requests
import requests_mock

from .fetch_population import ENDPOINT
from .fetch_population import USER_AGENT
from .fetch_population import build_query
from .fetch_population import fetch_all
from .fetch_population import fetch_batch
from .fetch_population import make_session
from .fetch_population import parse_bindings
from .fetch_population import read_places


def _binding(qid, statement, value, date, precision='9', rank='NormalRank'):
    entity = 'http://www.wikidata.org/entity/'
    return {
        'place': {
            'value': entity + qid
        },
        'statement': {
            'value': entity + 'statement/' + statement
        },
        'rank': {
            'value': 'http://wikiba.se/ontology#' + rank
        },
        'population': {
            'value': value
        },
        'date': {
            'value': date
        },
        'precision': {
            'value': precision
        },
    }


def _ok(bindings):
    return {'status_code': 200, 'json': {'results': {'bindings': bindings}}}


def _mocked_session(responses):
    """Returns a session whose requests to ENDPOINT get the given responses."""
    session = make_session()
    adapter = requests_mock.Adapter()
    adapter.register_uri('POST', ENDPOINT, responses)
    session.mount('https://', adapter)
    return session, adapter


class ReadPlacesTest(unittest.TestCase):

    def test_valid_invalid_and_shared_qids(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, 'places.csv')
            with open(path, 'w') as f:
                f.write('dcid,qid\n'
                        'geoId/06,Q99\n'
                        'wikidataId/Q99,Q99\n'
                        'geoId/06,Q99\n'
                        'geoId/36,Q1384\n'
                        'bad/1,Q99 } ?x ?y ?z {\n'
                        'bad/2,P31\n'
                        ',Q5\n')
            self.assertEqual(read_places(path), {
                'Q99': ['geoId/06', 'wikidataId/Q99'],
                'Q1384': ['geoId/36'],
            })

    def test_missing_columns(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, 'places.csv')
            with open(path, 'w') as f:
                f.write('place,wikidata\ngeoId/06,Q99\n')
            with self.assertRaises(ValueError):
                read_places(path)


class BuildQueryTest(unittest.TestCase):

    def test_values_clause(self):
        query = build_query(['Q1', 'Q42'])
        self.assertIn('VALUES ?place { wd:Q1 wd:Q42 }', query)
        self.assertIn('p:P1082', query)
        self.assertIn('pqv:P585', query)

    def test_rejects_anything_but_qids(self):
        for bad in ['Q1 } DELETE {', 'P31', 'Q0', 'q5', '']:
            with self.assertRaises(ValueError):
                build_query(['Q1', bad])


class ParseBindingsTest(unittest.TestCase):

    def test_parse(self):
        rows = parse_bindings([
            _binding('Q99',
                     'Q99-ABC',
                     '5244',
                     '2015-01-01T00:00:00Z',
                     rank='PreferredRank')
        ])
        self.assertEqual(rows, [{
            'qid': 'Q99',
            'statement_id': 'Q99-ABC',
            'rank': 'PreferredRank',
            'value': '5244',
            'date': '2015-01-01T00:00:00Z',
            'precision': '9',
        }])


class FetchBatchTest(unittest.TestCase):

    def test_sends_query_and_headers_and_returns_bindings(self):
        bindings = [_binding('Q1', 'Q1-A', '10', '2020-01-01T00:00:00Z')]
        session, adapter = _mocked_session([_ok(bindings)])
        self.assertEqual(fetch_batch(['Q1', 'Q2'], session), bindings)
        request = adapter.request_history[0]
        self.assertEqual(request.headers['User-Agent'], USER_AGENT)
        self.assertEqual(request.headers['Accept'],
                         'application/sparql-results+json')
        query = urllib.parse.parse_qs(request.text)['query'][0]
        self.assertIn('VALUES ?place { wd:Q1 wd:Q2 }', query)


class FetchAllTest(unittest.TestCase):

    def test_writes_rows_for_every_dcid_in_batches(self):
        qid_to_dcids = {
            'Q1': ['geoId/01'],
            'Q2': ['geoId/02', 'wikidataId/Q2'],
            'Q3': ['geoId/03'],
        }
        batches = []

        def fake_fetch(qids):
            batches.append(list(qids))
            return [
                _binding(q, f'{q}-A', '100', '2020-01-01T00:00:00Z')
                for q in qids
                if q != 'Q3'  # Q3 has no population in Wikidata
            ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            out = os.path.join(tmp_dir, 'raw', 'raw.csv')
            written = fetch_all(qid_to_dcids,
                                out,
                                batch_size=2,
                                fetch=fake_fetch,
                                sleep=lambda _: None)
            with open(out) as f:
                rows = list(csv.DictReader(f))
        self.assertEqual(batches, [['Q1', 'Q2'], ['Q3']])
        self.assertEqual(written, 3)
        self.assertEqual(sorted(r['dcid'] for r in rows),
                         ['geoId/01', 'geoId/02', 'wikidataId/Q2'])

    def test_no_partial_output_on_failure(self):

        def failing_fetch(qids):
            raise requests.HTTPError('503 Server Error')

        with tempfile.TemporaryDirectory() as tmp_dir:
            out = os.path.join(tmp_dir, 'raw.csv')
            with self.assertRaises(requests.HTTPError):
                fetch_all({'Q1': ['geoId/01']},
                          out,
                          fetch=failing_fetch,
                          sleep=lambda _: None)
            self.assertEqual(os.listdir(tmp_dir), [])


if __name__ == '__main__':
    unittest.main()
