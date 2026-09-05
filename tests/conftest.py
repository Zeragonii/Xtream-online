from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# The application defaults DATA_DIR to /data because that is the persistent
# volume used inside the Docker container. A normal test runner should never
# need permission to create /data, so give every pytest session isolated,
# writable temporary directories before app.config is imported.
_test_root = Path(tempfile.mkdtemp(prefix="xtream-online-tests-"))
os.environ.setdefault("DATA_DIR", str(_test_root / "data"))
os.environ.setdefault("HLS_DIR", str(_test_root / "hls"))

atexit.register(shutil.rmtree, _test_root, ignore_errors=True)
