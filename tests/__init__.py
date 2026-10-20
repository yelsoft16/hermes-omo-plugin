import importlib.util
import sys
from pathlib import Path

# Tests import the plugin as `omo`. Under Hermes the plugin directory is
# always named `omo`, but a plain git clone is usually not -- alias the
# package under that name so the suite runs from any directory name.
root = Path(__file__).resolve().parents[1]
if root.name == "omo":
    sys.path.insert(0, str(root.parent))
else:
    spec = importlib.util.spec_from_file_location(
        "omo",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the omo plugin package from {root}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["omo"] = module
    spec.loader.exec_module(module)
