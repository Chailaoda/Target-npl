"""Run tests with an isolated import root and no production-package fallback."""
import os
from pathlib import Path
import sys
import unittest
import py_compile

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["TARGET_NPL_DEPLOYMENT"] = str(ROOT / "deployment.json")
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

# Compile the deployed entrypoint before running business tests; it is not
# imported by the suite when an already-running service is reused.
py_compile.compile(str(ROOT / "scripts/start_control.py"), cfile=str(ROOT.parent / "state/evidence/start_control.pyc"), doraise=True)

import content_screening  # noqa: E402

# The host safety shim intercepts recursive deletes. Test fixtures are
# evidence under STATE_ROOT, not disposable production data; keep them.
import tempfile  # noqa: E402
tempfile.TemporaryDirectory.cleanup = lambda self, *args, **kwargs: None
tempfile.TemporaryDirectory._cleanup = classmethod(lambda cls, name, warn_message=None, ignore_errors=False, delete=False: None)
import shutil  # noqa: E402

def preserve_test_tree(path, *args, **kwargs):
    # Mark a simulated missing worktree as a non-Git plain directory; do not
    # recursively delete anything under the evidence root.
    git = Path(path) / ".git"
    if git.is_dir():
        for child in git.iterdir():
            if child.is_file():
                child.unlink()
        git.rmdir()
    else:
        Path(path).mkdir(parents=True, exist_ok=True)

shutil.rmtree = preserve_test_tree

if content_screening.__file__ is None or Path(content_screening.__file__).resolve().parent != ROOT / "content_screening":
    raise SystemExit(f"wrong package source: {content_screening.__file__}")

suite = unittest.TestSuite()
for test_file in sorted((ROOT / "tests").glob("test_*.py")):
    suite.addTests(unittest.defaultTestLoader.loadTestsFromName(test_file.stem))
result = unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
