"""Opt-in marker for slow statistical acceptance tests (run by .github/workflows/slow-tests.yml).

The default CI job has a 3-minute budget; tests decorated with ``@slow`` run only
when ``RUN_SLOW_TESTS=1`` is set.
"""
import os
import unittest

slow = unittest.skipUnless(os.environ.get("RUN_SLOW_TESTS") == "1", "slow acceptance test; set RUN_SLOW_TESTS=1")
