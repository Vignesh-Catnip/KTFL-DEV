import os
import sys
from robot import run_cli

if __name__ == "__main__":
    os.makedirs("reports", exist_ok=True)
    raise SystemExit(run_cli(["--outputdir", "reports", "tests/miro_invoice.robot", *sys.argv[1:]]))
