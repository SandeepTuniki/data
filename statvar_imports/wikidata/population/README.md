# Wikidata Population

This import loads population counts for places from
[Wikidata](https://www.wikidata.org/) into Data Commons. Data Commons uses it as
a low-priority fallback source for `Count_Person`, mainly for small places such
as villages and neighborhoods that have no official statistics in Data Commons.

It asks the Wikidata Query Service directly, and only about places Data
Commons already has.

## How it works

1. **Place list (`input_files/places.csv`).** Every Data Commons place that has
   a `wikidataId`, with columns `dcid` and `qid`. Created with
   [`export_places.sql`](export_places.sql).
2. **Fetch ([`fetch_population.py`](fetch_population.py)).** Asks the
   [Wikidata Query Service](https://query.wikidata.org/) for every population
   (P1082) statement with a point-in-time (P585) qualifier, 1,000 places per
   request, one request at a time. Throttling and server errors are retried with
   exponential backoff and jitter. Writes
   `input_files/wikidata_population_raw.csv`.
3. **Preprocess ([`preprocess_population.py`](preprocess_population.py)).** Applies the rules
   below and writes `input_files/wikidata_population_preprocessed.csv`, a plain CSV
   with columns `dcid`, `date` and `population`.
4. **Convert (StatVar Processor).**
   [`stat_var_processor.py`](../../../tools/statvar_importer/stat_var_processor.py)
   turns the preprocessed CSV into Data Commons format using
   [`wikidata_population_pvmap.csv`](wikidata_population_pvmap.csv) and
   [`wikidata_population_statvar_processor_config.csv`](wikidata_population_statvar_processor_config.csv).
   Each row becomes a `Count_Person` observation with measurement method
   `WikidataPopulation`. Writes `output/wikidata_population.csv` and
   `output/wikidata_population.tmcf`.

Because the place list already pairs each Wikidata ID with its Data Commons ID,
no place resolution is needed.

### Preprocessing rules

- **Values:** only whole, non-negative numbers are kept.
- **Dates:** only day, month or year precision is kept. Coarser dates, such as
  decades, are dropped. A full date on 1 January becomes just the year, and a
  full date on 31 December becomes the following year.
- **Several values for the same place and date:** deprecated statements are
  ignored, a preferred-rank statement wins, and any remaining tie goes to the
  lowest statement ID. The choice is the same on every run. This has to happen
  before the StatVar Processor, which drops conflicting values instead of
  choosing one.

## Running it

Set up the repository environment as described in the
[top-level README](../../../README.md), then from this directory:

```shell
# 1. Export the place list (needs read access to the BigQuery mirror).
mkdir -p input_files
bq query --project_id=datcom-store --use_legacy_sql=false \
  --format=csv --max_rows=10000000 < export_places.sql \
  > input_files/places.csv

# 2. Fetch from Wikidata (about 3,000 requests; a few hours).
python3 fetch_population.py \
  --places_csv=input_files/places.csv \
  --output_csv=input_files/wikidata_population_raw.csv

# 3. Preprocess.
python3 preprocess_population.py \
  --input_csv=input_files/wikidata_population_raw.csv \
  --output_csv=input_files/wikidata_population_preprocessed.csv

# 4. Convert to Data Commons format.
python3 ../../../tools/statvar_importer/stat_var_processor.py \
  --input_data=input_files/wikidata_population_preprocessed.csv \
  --pv_map=wikidata_population_pvmap.csv \
  --config_file=wikidata_population_statvar_processor_config.csv \
  --output_path=output/wikidata_population \
  --output_counters=counters/wikidata_population_counters.csv
```

## Status: phase 1

The place list is exported by hand for now. Because of that, the manifest has
no `cron_schedule`, pauses the import workflow (`disable_import_workflow`), and
skips production ingestion (`skip_prod_ingestion`) until the first full output
has been reviewed against the data currently in Data Commons.

Phase 2 will automate the place list, add a monthly `cron_schedule`, and remove
both overrides. The paused workflow then has to be unpaused once in Airflow.

## Test data

`test_data/` holds a small StatVar Processor input
(`wikidata_population_input.csv`) and its expected output
(`wikidata_population_output.csv` and `.tmcf`). Regenerate them whenever the
PV map or config changes:

```shell
python3 ../../../tools/statvar_importer/stat_var_processor.py \
  --input_data=test_data/wikidata_population_input.csv \
  --pv_map=wikidata_population_pvmap.csv \
  --config_file=wikidata_population_statvar_processor_config.csv \
  --output_path=test_data/wikidata_population_output \
  --output_counters=/tmp/wikidata_population_test_counters.csv
```
