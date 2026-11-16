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
"""Fetches population values from Wikidata for Data Commons places.

Reads a CSV of Data Commons places that have a Wikidata ID (columns `dcid` and
`qid`), asks the Wikidata Query Service for every population (P1082)
statement with a point-in-time (P585) qualifier, and writes the raw answers to
a CSV. Preprocessing happens separately in `preprocess_population.py`.

Requests are sent one at a time in batches of QIDs over a single HTTP session.
The session retries throttling (HTTP 429), server errors (5xx) and connection
errors with exponential backoff and jitter, honoring the `Retry-After` header.
If a batch still fails, the script exits with an error and does not leave a
partial output.

Usage:
  python3 fetch_population.py \
      --places_csv=input_files/places.csv \
      --output_csv=input_files/wikidata_population_raw.csv
"""

import csv
import os
import re
import time
from typing import Callable, Dict, Iterable, List, Sequence

from absl import app
from absl import flags
from absl import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_FLAGS = flags.FLAGS
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

ENDPOINT = 'https://query.wikidata.org/sparql'
USER_AGENT = ('DataCommonsWikidataPopulationImport/1.0 '
              '(https://datacommons.org; support@datacommons.org)')

RAW_COLUMNS = [
    'dcid', 'qid', 'statement_id', 'rank', 'value', 'date', 'precision'
]

# Wikidata item IDs, for example "Q486860". Anything else is rejected so that
# input values can never change the meaning of the SPARQL query.
_QID_PATTERN = re.compile(r'^Q[1-9][0-9]*$')

# A population statement with a point-in-time qualifier whose date uses the
# proleptic Gregorian calendar (Q1985727). Rank is fetched so the preprocessing
# step can apply its rules.
_QUERY_TEMPLATE = """
SELECT ?place ?statement ?rank ?population ?date ?precision WHERE {{
  VALUES ?place {{ {values} }}
  ?place p:P1082 ?statement .
  ?statement ps:P1082 ?population ;
             wikibase:rank ?rank ;
             pqv:P585 ?dateNode .
  ?dateNode wikibase:timeValue ?date ;
            wikibase:timePrecision ?precision ;
            wikibase:timeCalendarModel wd:Q1985727 .
}}
"""


def _define_flags():
    flags.DEFINE_string(
        'places_csv', os.path.join(_SCRIPT_DIR, 'input_files', 'places.csv'),
        'CSV with columns "dcid" and "qid" listing the places to fetch.')
    flags.DEFINE_string(
        'output_csv',
        os.path.join(_SCRIPT_DIR, 'input_files', 'wikidata_population_raw.csv'),
        'Where to write the raw population values.')
    flags.DEFINE_integer('batch_size', 1000,
                         'Number of places to ask about per request.')
    flags.DEFINE_float('pause_sec', 1.0,
                       'Pause between requests, to stay polite.')
    flags.DEFINE_integer('max_retries', 5,
                         'Retries per request before giving up.')
    flags.DEFINE_float('timeout_sec', 120.0, 'Timeout for one request.')


def read_places(path: str) -> Dict[str, List[str]]:
    """Reads the places CSV and returns a map from QID to DCIDs.

    Rows with a missing DCID or an invalid QID are skipped and logged.
    """
    qid_to_dcids: Dict[str, List[str]] = {}
    skipped = 0
    with open(path, newline='') as f:
        reader = csv.DictReader(f)
        missing = {'dcid', 'qid'} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f'{path} is missing columns: {sorted(missing)}')
        for row in reader:
            dcid = (row.get('dcid') or '').strip()
            qid = (row.get('qid') or '').strip()
            if not dcid or not _QID_PATTERN.match(qid):
                skipped += 1
                continue
            dcids = qid_to_dcids.setdefault(qid, [])
            if dcid not in dcids:
                dcids.append(dcid)
    if skipped:
        logging.warning('Skipped %d rows with a missing DCID or invalid QID.',
                        skipped)
    logging.info('Read %d distinct QIDs from %s.', len(qid_to_dcids), path)
    return qid_to_dcids


def build_query(qids: Sequence[str]) -> str:
    """Builds the SPARQL query for a batch of QIDs."""
    for qid in qids:
        if not _QID_PATTERN.match(qid):
            raise ValueError(f'Invalid Wikidata QID: {qid!r}')
    values = ' '.join(f'wd:{qid}' for qid in qids)
    return _QUERY_TEMPLATE.format(values=values)


def _last_part(uri: str, separator: str = '/') -> str:
    return uri.rsplit(separator, 1)[-1]


def parse_bindings(bindings: Iterable[dict]) -> List[Dict[str, str]]:
    """Converts SPARQL JSON result bindings into raw rows (without dcid)."""
    rows = []
    for b in bindings:
        rows.append({
            'qid': _last_part(b['place']['value']),
            'statement_id': _last_part(b['statement']['value']),
            'rank': _last_part(b['rank']['value'], '#'),
            'value': b['population']['value'],
            'date': b['date']['value'],
            'precision': b['precision']['value'],
        })
    return rows


def make_session(max_retries: int = 5) -> requests.Session:
    """Returns an HTTP session for the Wikidata Query Service.

    The session sends the headers Wikidata asks clients to send, keeps the
    connection open between requests, and retries throttling and server errors
    with exponential backoff and jitter. POST is retried too: the query only
    reads data, so repeating it is safe.
    """
    retry = Retry(
        total=max_retries,
        backoff_factor=2,
        backoff_jitter=1,
        backoff_max=120,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=['POST'],
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount('https://', HTTPAdapter(max_retries=retry))
    session.headers.update({
        'User-Agent': USER_AGENT,
        'Accept': 'application/sparql-results+json',
    })
    return session


def fetch_batch(qids: Sequence[str],
                session: requests.Session,
                timeout_sec: float = 120.0) -> List[dict]:
    """Returns the SPARQL result bindings for one batch of QIDs.

    Retries are handled by the session. Raises a `requests` exception if the
    request still fails.
    """
    response = session.post(ENDPOINT,
                            data={'query': build_query(qids)},
                            timeout=timeout_sec)
    response.raise_for_status()
    return response.json()['results']['bindings']


def fetch_all(qid_to_dcids: Dict[str, List[str]],
              output_csv: str,
              fetch: Callable[[Sequence[str]], List[dict]],
              batch_size: int = 1000,
              pause_sec: float = 1.0,
              sleep: Callable[[float], None] = time.sleep) -> int:
    """Fetches all places in batches and writes the raw CSV.

    Output is written to a temporary file and renamed at the end, so a failed
    run never leaves a partial output behind.

    Args:
      qid_to_dcids: Map from QID to the Data Commons places that use it.
      output_csv: Where to write the raw CSV.
      fetch: Function that returns SPARQL bindings for a batch of QIDs.
      batch_size: Number of QIDs per call to fetch.
      pause_sec: Pause between batches.
      sleep: Injected for tests.

    Returns:
      The number of rows written.
    """
    qids = sorted(qid_to_dcids)
    os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)
    tmp_path = output_csv + '.tmp'
    rows_written = 0
    num_batches = (len(qids) + batch_size - 1) // batch_size
    try:
        with open(tmp_path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=RAW_COLUMNS)
            writer.writeheader()
            for i in range(0, len(qids), batch_size):
                batch = qids[i:i + batch_size]
                start = time.monotonic()
                rows = parse_bindings(fetch(batch))
                for row in rows:
                    for dcid in qid_to_dcids.get(row['qid'], []):
                        writer.writerow({'dcid': dcid, **row})
                        rows_written += 1
                logging.info('Batch %d/%d: %d values in %.1fs.',
                             i // batch_size + 1, num_batches, len(rows),
                             time.monotonic() - start)
                if i + batch_size < len(qids):
                    sleep(pause_sec)
        os.replace(tmp_path, output_csv)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    logging.info('Wrote %d rows to %s.', rows_written, output_csv)
    return rows_written


def main(_):
    qid_to_dcids = read_places(_FLAGS.places_csv)
    with make_session(max_retries=_FLAGS.max_retries) as session:
        fetch_all(qid_to_dcids,
                  _FLAGS.output_csv,
                  lambda qids: fetch_batch(qids, session, _FLAGS.timeout_sec),
                  batch_size=_FLAGS.batch_size,
                  pause_sec=_FLAGS.pause_sec)


if __name__ == '__main__':
    _define_flags()
    app.run(main)
