# Bad Fixture Catalog

This directory documents historical failure shapes from live Atticus runs.

The executable catalog lives in `atticus.testing.bad_fixtures`. Each fixture has:

- `fixture_id`
- `category`
- `expected_outcome`: `reject`, `repair`, or `operator_attention`
- `reason`
- `payload`

These fixtures are intentionally small and synthetic. They are regression tripwires for control-plane behavior, not legal evidence.
