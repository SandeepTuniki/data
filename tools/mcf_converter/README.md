# MCF Converter

The MCF converter reads the MCF files of one import input and writes their
nodes as two tables, in Avro and CSV format:

-   **Observations:** one record per `StatVarObservation` node.
-   **Schema:** one record per other node, such as a `StatisticalVariable`
    definition.

The Avro files are meant to be loaded into BigQuery. The CSV files contain the
same rows, for people to read and debug.

The converter reads each MCF file once, one node at a time, so memory use stays
constant regardless of input size. Inputs and outputs can be local paths or GCS
(`gs://`) paths.

This folder is a library only. Nothing in the import pipeline calls it yet.

## Output layout

The caller passes an output directory, usually `<version>/<input>/bq/`. The
converter writes these files under it:

```
<output_dir>/
├── avro/
│   ├── observations.avro
│   └── schema.avro
└── csv/
    ├── observations.csv
    └── schema.csv
```

Avro files are compressed with `deflate`. If conversion fails, the converter
deletes the files it has written so far and re-raises the error.

## Record schemas

### Observations

The observation fields are as follows:

Field               | Type             | Notes
------------------- | ---------------- | -----------------------------------------
`variableMeasured`  | string           | Clean value; `''` if missing
`observationAbout`  | string           | Clean value; `''` if missing
`observationDate`   | string           | Clean value, kept as text; `''` if missing
`observationPeriod` | string, nullable | Clean value
`measurementMethod` | string, nullable | Clean value
`unit`              | string, nullable | Clean value
`scalingFactor`     | string, nullable | Clean value
`value`             | double, nullable | `value_raw` as a number; null if it isn't a plain number
`value_raw`         | string           | `value` as written in the MCF
`node_id`           | string, nullable | `Node:` as written in the MCF, including any `dcid:` prefix
`dcid`              | string, nullable | Clean value of the `dcid` property
`import_name`       | string           | Passed in by the caller
`version`           | string           | Passed in by the caller
`input`             | string           | Passed in by the caller, for example `input0`

A clean value has no namespace prefix (`dcid:`, `dcs:`) and no enclosing
quotes. For example, `dcid:Count_Person` becomes `Count_Person` and `"2020-05"`
becomes `2020-05`.

Other observation properties, such as `keyString`, are not written.

### Schema

The schema fields are as follows:

Field            | Type   | Notes
---------------- | ------ | -----------------------------------------------------
`dcid`           | string | Clean value of the `dcid` property, else of `Node:`
`value_combined` | string | All other properties as `prop:value` pairs, sorted by property and joined with `;`
`import_name`    | string | Passed in by the caller
`version`        | string | Passed in by the caller
`input`          | string | Passed in by the caller

`value_combined` uses the same format as the BigQuery differ: references keep
their `dcid:` prefix, text keeps its quotes, and repeated values are sorted and
joined with `, `. For example:

```
name:"Some City";typeOf:dcid:City, dcid:Place
```

## Usage

```python
import converter

counts = converter.convert_mcf(
    mcf_pattern='gs://bucket/MyImport/2026_11_04/input0/genmcf/*.mcf',
    output_dir='gs://bucket/MyImport/2026_11_04/input0/bq',
    import_name='MyImport',
    version='2026_11_04',
    input_name='input0',
    formats=['avro', 'csv'],  # Optional; default is both.
)
# counts == {'observations': 15435, 'schema_nodes': 2}
```

-   `mcf_pattern`: one local path or `gs://` URI. Wildcards are supported.
-   `formats`: the output formats to write. An empty list or an unknown format
    raises `ValueError`.

Using GCS paths requires Google Cloud Application Default Credentials.

## Files

-   `converter.py`: `convert_mcf()` and the record schemas.
-   `writers.py`: the `RecordWriter` interface with `AvroWriter` and
    `CsvWriter`. To add a format, such as Parquet, add a writer and register it
    in `converter.py`.
-   `mcf_parser.py`: streaming MCF parsing and value normalization, shared with
    `tools/import_differ/bigquery_differ.py`.

## Tests

From the repository root, run:

```bash
python3 -m unittest discover -s tools/mcf_converter -p '*_test.py'
```

`converter_test.py` includes an equivalence test against the BigQuery differ.
It checks that both produce the same observation keys and schema
`value_combined`, apart from the clean values described above.
