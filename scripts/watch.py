"""Independent Watch service. Shares the normal composition, never creates a Pi process."""
import sys

from pulse import main

if __name__ == "__main__":
    sys.argv.insert(1, "--watch-only")
    raise SystemExit(main())
