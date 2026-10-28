"""Zero-install entry point: python3 run.py --help."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from kb_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
