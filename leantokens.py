#!/usr/bin/env python3
"""leantokens is now headforward.

This shim keeps hooks and background proxies set up under the old name
working. Run `headforward hooks install global` and `headforward desktop on`
again to move them to the new name; then this file is no longer used.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from headforward import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
