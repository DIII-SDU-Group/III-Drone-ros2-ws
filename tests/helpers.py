import importlib.util
from pathlib import Path


def load_module_from_path(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem.replace(".", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module
