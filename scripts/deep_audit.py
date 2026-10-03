import ast,re,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
py=list((ROOT/'backend/app').glob('*.py'))
lines=sum(len(p.read_text(errors='ignore').splitlines()) for p in py)
syntax=[]
for p in py:
    ast.parse(p.read_text(errors='ignore')); syntax.append(p.name)
main=(ROOT/'backend/app/main.py').read_text()
routes=len(re.findall(r'@app\.(?:get|post|put|delete|websocket)\(',main))
danger=[]
for p in py:
    s=p.read_text(errors='ignore')
    for pat in [r'\beval\s*\(',r'\bexec\s*\(',r'os\.system\(',r'subprocess\.',r'pickle\.loads\(']:
        if re.search(pat,s): danger.append((p.name,pat))
secrets=[]
for p in list((ROOT/'backend/app').rglob('*.py'))+list((ROOT/'scripts').rglob('*.py')):
    s=p.read_text(errors='ignore')
    if re.search(r'(api[_-]?key|access[_-]?token|password|secret|totp|mpin)\s*[:=]\s*["\'][^"\']{8,}["\']',s,re.I): secrets.append(p.name)
res=subprocess.run([sys.executable,'-m','pytest','-q'],cwd=ROOT,text=True,capture_output=True)
report={'version':(ROOT/'backend/app/version.py').read_text().strip(), 'python_files':len(py),'python_lines':lines,'routes':routes,'ast_parse':'PASS','dangerous_dynamic_execution':danger,'hardcoded_secret_candidates':sorted(set(secrets)),'pytest_returncode':res.returncode,'pytest_output':res.stdout.strip()[-2000:]}
print(json.dumps(report,indent=2))
if res.returncode: sys.exit(res.returncode)
