-- Exports the Data Commons places that have a Wikidata ID.
--
-- Output columns match what fetch_population.py expects: dcid, qid.
-- Run against the BigQuery mirror of the Data Commons graph, for example:
--
--   bq query --project_id=datcom-store --use_legacy_sql=false \
--     --format=csv --max_rows=10000000 < export_places.sql \
--     > input_files/places.csv
--
-- The wikidataId value is stored with a suffix after a colon
-- (for example "Q486860:<hash>"), so only the part before it is kept.
SELECT
  subject_id AS dcid,
  SPLIT(object_id, ':')[OFFSET(0)] AS qid
FROM `datcom-store.spanner_dc_graph_prod_DEFAULT.Edge`
WHERE predicate = 'wikidataId'
ORDER BY dcid;
