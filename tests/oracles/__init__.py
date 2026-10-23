"""Load frozen projections without retaining their retired transport dependency."""
import sys
import types
from pathlib import Path


def __getattr__(name):
    if name not in ("journal_02765ac", "journal_85aa9d7"):
        raise AttributeError(name)
    path = Path(__file__).with_name(name + ".py")
    source = path.read_text(encoding="utf-8")
    # Only import routing changes in memory; the frozen algorithms/files stay intact.
    source = source.replace("from aqla_sheets_mcp.parameters import parameter_values",
                            "from aqla_agent.journal_parameters import parameter_values")
    module = types.ModuleType(__name__ + "." + name)
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    # The fixed allowlist loads only repository-owned frozen fixtures, never external input.
    exec(compile(source, str(path), "exec"), module.__dict__)  # skipcq: PYL-W0122
    globals()[name] = module
    return module
