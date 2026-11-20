"""What the standalone app starts with (see deephack.spec): the same as `python -m deephack`."""
import sys

from deephack.app import main

sys.exit(main())
