# Ensures the project root is importable so `import app` works regardless of
# the pytest invocation directory.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
