"""Test configuration shared across all cardinal_cfn tests."""

import os
import sys

# Add src/ to import path so `import cardinal_cfn` works.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
