"""Describe local capabilities without publishing data or reading credentials."""
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
result={'python':sys.version.split()[0], 'python_supported':sys.version_info>=(3,11),
        'product':'Research Alpha 2','network_services_started':False,'capabilities':{}}
for package,label in [('Pillow','image_attachments'),('cryptography','optional_signed_verification')]:
    try:result['capabilities'][label]={'available':True,'version':importlib.metadata.version(package)}
    except importlib.metadata.PackageNotFoundError:result['capabilities'][label]={'available':False}
pandoc=shutil.which('pandoc')
result['capabilities']['markdown_math_code']={'available':bool(pandoc),'fallback':'plain text'}
if pandoc:
    try:
        result['capabilities']['markdown_math_code']['version']=subprocess.run([pandoc,'--version'],capture_output=True,text=True,timeout=5,check=True).stdout.splitlines()[0]
    except (OSError,subprocess.SubprocessError):result['capabilities']['markdown_math_code']['available']=False
result['source_files_present']=all((root/p).is_file() for p in ['eksmu/server.py','eksmu/research.py','eksmu/static/research.js','eksmu/static/index.html'])
print(json.dumps(result,ensure_ascii=False,indent=2))
sys.exit(0 if result['python_supported'] and result['source_files_present'] else 1)
