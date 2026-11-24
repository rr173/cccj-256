"""Allow `python3 -m eqcheck ...` to reach the same CLI as run.py."""
import sys

from .run import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
