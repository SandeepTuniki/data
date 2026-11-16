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
"""Preprocesses raw Wikidata population values for the StatVar Processor.

Input is the raw CSV written by `fetch_population.py`. Output is a plain CSV
with one row per place and date, and columns `dcid` (bare Data Commons place
ID), `date` (`YYYY`, `YYYY-MM` or `YYYY-MM-DD`) and `population`. Converting
it into Data Commons format is left to the StatVar Processor, using
`wikidata_population_pvmap.csv`.

Preprocessing rules:
  * Values: only whole, non-negative numbers are kept.
  * Dates: only day, month or year precision is kept (Wikidata precision 11,
    10 or 9). Coarser dates such as decades are dropped. A full date on
    1 January becomes just the year, and a full date on 31 December becomes
    the following year.
  * Duplicates: when a place has more than one value for the same date,
    deprecated statements are ignored, a preferred-rank statement wins, and
    any remaining tie is broken by the lowest statement ID, so the choice is
    the same on every run. This must happen here: the StatVar Processor drops
    conflicting values instead of choosing one.

Usage:
  python3 preprocess_population.py \
      --input_csv=input_files/wikidata_population_raw.csv \
      --output_csv=input_files/wikidata_population_preprocessed.csv
"""

import collections
import csv
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

from absl import app
from absl import flags
from absl import logging

_FLAGS = flags.FLAGS
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

OUTPUT_COLUMNS = ['dcid', 'date', 'population']

_PRECISION_PATTERNS = {
    '11': re.compile(r'^(\d{4}-\d{2}-\d{2})'),  # day
    '10': re.compile(r'^(\d{4}-\d{2})'),  # month
    '9': re.compile(r'^(\d{4})'),  # year
}
_INTEGER_PATTERN = re.compile(r'^\+?(\d+)$')
_PREFERRED_RANK = 'PreferredRank'
_DEPRECATED_RANK = 'DeprecatedRank'


def _define_flags():
    flags.DEFINE_string(
        'input_csv',
        os.path.join(_SCRIPT_DIR, 'input_files', 'wikidata_population_raw.csv'),
        'Raw CSV written by fetch_population.py.')
    flags.DEFINE_string(
        'output_csv',
        os.path.join(_SCRIPT_DIR, 'input_files',
                     'wikidata_population_preprocessed.csv'),
        'Where to write the preprocessed CSV (input to the StatVar Processor).')


def clean_date(date: str, precision: str) -> Optional[str]:
    """Converts a Wikidata time value to an observation date, or None.

    Args:
      date: Wikidata time value, for example "2011-01-01T00:00:00Z".
      precision: Wikidata time precision, for example "9" for a year.

    Returns:
      "YYYY", "YYYY-MM" or "YYYY-MM-DD", or None when the date is too coarse
      or cannot be parsed.
    """
    pattern = _PRECISION_PATTERNS.get(precision.strip())
    if not pattern:
        return None
    match = pattern.match(date.strip())
    if not match:
        return None
    result = match.group(1)
    if len(result) == len('YYYY-MM-DD'):
        if result.endswith('-12-31'):
            result = str(int(result[:4]) + 1)
        elif result.endswith('-01-01'):
            result = result[:4]
    return result


def clean_value(value: str) -> Optional[str]:
    """Returns the value as a whole-number string, or None if it isn't one."""
    match = _INTEGER_PATTERN.match(value.strip())
    if not match:
        return None
    return str(int(match.group(1)))


def _preference(row: Dict[str, str]) -> Tuple[int, str]:
    """Sort key: preferred rank first, then lowest statement ID."""
    is_preferred = row.get('rank') == _PREFERRED_RANK
    return (0 if is_preferred else 1, row.get('statement_id', ''))


def preprocess_rows(
    rows: Iterable[Dict[str, str]]
) -> Tuple[List[Dict[str, str]], collections.Counter]:
    """Applies the preprocessing rules and picks one value per place and date.

    Returns:
      A tuple of (output rows sorted by place and date, counters).
    """
    counters = collections.Counter()
    candidates: Dict[Tuple[str, str],
                     List[Dict[str, str]]] = (collections.defaultdict(list))
    for row in rows:
        counters['rows_read'] += 1
        dcid = (row.get('dcid') or '').strip()
        if not dcid:
            counters['dropped_missing_dcid'] += 1
            continue
        if row.get('rank') == _DEPRECATED_RANK:
            counters['dropped_deprecated_rank'] += 1
            continue
        date = clean_date(row.get('date', ''), row.get('precision', ''))
        if date is None:
            counters['dropped_unusable_date'] += 1
            continue
        value = clean_value(row.get('value', ''))
        if value is None:
            counters['dropped_non_integer_value'] += 1
            continue
        candidates[(dcid, date)].append({**row, 'value': value})

    output = []
    for (dcid, date), options in candidates.items():
        if len({o['value'] for o in options}) > 1:
            counters['dates_with_conflicting_values'] += 1
        chosen = min(options, key=_preference)
        output.append({
            'dcid': dcid,
            'date': date,
            'population': chosen['value'],
        })
    output.sort(key=lambda r: (r['dcid'], r['date']))
    counters['rows_written'] = len(output)
    counters['places_written'] = len({r['dcid'] for r in output})
    return output, counters


def preprocess(input_csv: str, output_csv: str) -> collections.Counter:
    """Reads the raw CSV, preprocesses it, and writes the preprocessed CSV."""
    with open(input_csv, newline='') as f:
        output, counters = preprocess_rows(csv.DictReader(f))
    os.makedirs(os.path.dirname(os.path.abspath(output_csv)), exist_ok=True)
    with open(output_csv, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(output)
    for name, count in sorted(counters.items()):
        logging.info('%s: %d', name, count)
    return counters


def main(_):
    preprocess(_FLAGS.input_csv, _FLAGS.output_csv)


if __name__ == '__main__':
    _define_flags()
    app.run(main)
