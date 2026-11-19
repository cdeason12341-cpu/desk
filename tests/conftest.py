import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture(autouse=True)
def _fresh_gmgn_gate():
    """DECISION 88: the GMGN gate is process-wide; every test starts with it open and empty."""
    import gmgn
    gmgn.GATE.reset()
    yield
    gmgn.GATE.reset()
