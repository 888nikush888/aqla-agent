import subprocess
import sys
from pathlib import Path


def test_native_pipeline_bootstraps_without_the_application_environment():
    target = Path(__file__).with_name("native_pipeline_smoke.py")
    source = f"""import importlib.abc, runpy, sys
class ApplicationUnavailable(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('aqla_agent', 'aqla_hyperliquid_mcp'):
            raise ModuleNotFoundError('Application environment is deliberately unavailable')
sys.meta_path.insert(0, ApplicationUnavailable())
sys.path.insert(0, {str(target.parent)!r})
runpy.run_path({str(target)!r}, run_name='native_import_only')
"""
    result = subprocess.run([sys.executable, "-I", "-c", source], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
