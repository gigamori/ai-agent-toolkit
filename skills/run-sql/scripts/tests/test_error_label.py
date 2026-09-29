import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_sql  # noqa: E402

_SECRET = "PWMARK7391"
_USER = "probeuser"
_SCHEME_LESS_DSNS = (_SECRET, f"{_USER}:{_SECRET}@127.0.0.1/probedb")
_VALID_SCHEMES = (
    "postgresql+psycopg2",
    "mysql+pymysql",
    "redshift+redshift_connector",
    "snowflake",
    "bigquery",
    "duckdb",
    "databricks",
)


def _failing_sqlalchemy() -> types.ModuleType:
    def create_engine(*args, **kwargs):
        raise RuntimeError("stub connect failure")

    module = types.ModuleType("sqlalchemy")
    module.create_engine = create_engine
    module.text = str
    return module


class ErrorLabelTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "connections.toml"

    def _stderr_of_query(self, dsn: str, *extra_args: str) -> str:
        self.config.write_text(f'[connections.probe]\ndsn = "{dsn}"\n', encoding="utf-8")
        argv = ["run_sql.py", "query", "--connection", "probe", "--sql", "select 1", *extra_args]
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"RUN_SQL_CONFIG": str(self.config)}),
            mock.patch.dict(sys.modules, {"sqlalchemy": _failing_sqlalchemy()}),
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(sys, "stdin", io.StringIO()),
            redirect_stdout(io.StringIO()),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            run_sql.main()
        return stderr.getvalue()

    def _assert_no_dsn_fragment(self, stderr: str) -> None:
        self.assertNotIn(_SECRET.lower(), stderr.lower())
        self.assertNotIn(_USER, stderr)

    def test_connect_error_label_hides_scheme_less_dsn(self):
        for extra_args in ((), ("--control-stdin",)):
            for dsn in _SCHEME_LESS_DSNS:
                with self.subTest(dsn=dsn, extra_args=extra_args):
                    self._assert_no_dsn_fragment(self._stderr_of_query(dsn, *extra_args))

    def test_dry_run_error_label_hides_scheme_less_dsn(self):
        for dsn in _SCHEME_LESS_DSNS:
            with self.subTest(dsn=dsn):
                self._assert_no_dsn_fragment(self._stderr_of_query(dsn, "--dry-run"))

    def test_connect_error_label_keeps_a_valid_scheme(self):
        for scheme in _VALID_SCHEMES:
            with self.subTest(scheme=scheme):
                stderr = self._stderr_of_query(f"{scheme}://{_USER}:{_SECRET}@127.0.0.1:1/probedb")
                self.assertIn(f"[{scheme}]", stderr)


if __name__ == "__main__":
    unittest.main()
