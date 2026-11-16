import sys
from pathlib import Path

# the Lambda zip has these modules at the top level, so tests import them that way too
sys.path.insert(0, str(Path(__file__).parent.parent / "audit"))
