Test outputs for `teammap failures`.

- `pytest*.log`, `unittest.log`, `tap.log`, `junit.xml`, `pass.log`: real output of pytest 8.4, Python 3.11 unittest and
  Node 20's test runner, from a small project (`app/billing.py`, `app/tests/test_billing.py`, `js/calc.test.js`) the
  tests rebuild. Machine paths are replaced with `{ROOT}` (`{ROOT_ESCAPED}` where TAP escapes backslashes).
- `jest.log`, `vitest.log`: written by hand in the format of Jest 29 and Vitest 1 (neither was installed); they test the
  parsers, not those tools' exact output.
- `jest_old_node.log`: a hand-written Jest traceback with Node <=14 `internal/` runtime paths and an unindexed
  project test file; runtime frames must not replace that project frame in the failure group.
- `api_route.py`, `app_internal.log`: synthetic parser fixtures for an `api_route` endpoint and an application
  traceback through `app/internal/boom.py`; the Python source is parsed, not imported or run.
