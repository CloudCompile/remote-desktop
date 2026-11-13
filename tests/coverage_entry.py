"""Coverage entry point for the integration-test server subprocess (#34, #35).

Running `python3 tests/coverage_entry.py` instead of `control.py` measures
the real server in the child process: coverage is started before the
server imports, and saved after main() returns (graceful SIGTERM path).
data_suffix=True writes a parallel data file that pytest-cov combines.

Without RD_COVERAGE_RC this behaves exactly like control.py.
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

_cov = None
_rc = os.environ.get("RD_COVERAGE_RC")
if _rc:
    import coverage

    _cov = coverage.Coverage(config_file=_rc, data_suffix=True)
    _cov.start()

from remote_desktop.server import main  # noqa: E402

rc = main(sys.argv[1:])

if _cov:
    _cov.stop()
    _cov.save()
sys.exit(rc)
