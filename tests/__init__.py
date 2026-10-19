import sys
from pathlib import Path

# ~/.hermes/plugins must be on sys.path so `import omo` resolves the plugin package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
