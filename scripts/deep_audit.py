"""Static + dynamic self-check: compile, lint, secret scan, test suite."""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
py = sorted((ROOT / 'backend/app').glob('*.py'))
for p in py:
    ast.parse(p.read_text())
danger = [(p.name, pat) for p in py for pat in (r'\beval\s*\(', r'\bexec\s*\(', r'os\.system\(', r'subprocess\.', r'pickle\.loads\(')
          if re.search(pat, p.read_text())]
secrets = [p.name for p in py + sorted((ROOT / 'scripts').glob('*.py'))
           if re.search(r'(api[_-]?key|access[_-]?token|password|secret|totp|mpin)\s*[:=]\s*["\'][^"\']{8,}["\']', p.read_text(), re.I)]
routes = int(subprocess.run([sys.executable, '-c', 'import app.main as m; print(len(m.app.routes))'], cwd=ROOT / 'backend',
                            env={'DATABASE_URL': 'sqlite:///:memory:', 'DATA_DIR': '/tmp/iort-audit', 'PATH': '/usr/bin:/bin',
                                 'REQUIRE_DURABLE_EVENT_BUS': 'false'}, text=True, capture_output=True).stdout.strip() or 0)
lint = subprocess.run([sys.executable, '-m', 'pyflakes', str(ROOT / 'backend/app')], text=True, capture_output=True)
tests = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider'], cwd=ROOT, text=True, capture_output=True)
report = {'version': (ROOT / 'backend/app/version.py').read_text().split("'")[1], 'python_files': len(py),
          'python_lines': sum(len(p.read_text().splitlines()) for p in py),
          'http_routes': routes,
          'dangerous_dynamic_execution': danger, 'hardcoded_secret_candidates': secrets,
          'pyflakes': 'PASS' if lint.returncode == 0 else lint.stdout.strip()[-1500:],
          'pytest_returncode': tests.returncode, 'pytest_summary': tests.stdout.strip().splitlines()[-1] if tests.stdout.strip() else ''}
print(json.dumps(report, indent=2))
sys.exit(1 if (danger or secrets or lint.returncode or tests.returncode) else 0)
