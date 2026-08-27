"""Allow running cronpypeline as a module: python -m cronpypeline."""

import sys

from cronpypeline.cli import main

sys.exit(main())
