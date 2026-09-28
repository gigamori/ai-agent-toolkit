import os
import sys
import tempfile

import pytest

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path_factory):
    temp = tmp_path_factory.mktemp("tmp")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tempfile, "tempdir", str(temp))
        mp.setenv("TMPDIR", str(temp))
        yield
