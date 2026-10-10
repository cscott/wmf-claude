#!/usr/bin/env python3
"""`wmf-sbx run --name NAME ...`: an alias of `wmf-sbx resume NAME ...`
(resume.py)."""

import sys

from .resume import run_main as main

if __name__ == "__main__":
    sys.exit(main())
