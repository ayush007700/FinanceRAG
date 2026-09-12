"""Session-wide defaults for the test run.

Only environment that makes the suite behave the same on a laptop as in CI.
Anything a specific test needs, it sets itself.
"""

from __future__ import annotations

import os

# CI has a Postgres service container; a laptop often does not. Without a
# short connect timeout every TestClient lifespan and every /health probe
# waits out the OS TCP timeout against nothing -- minutes per test. Two
# seconds reports "absent" quickly and the DB-gated tests skip as designed.
os.environ.setdefault("DB_CONNECT_TIMEOUT", "2")
