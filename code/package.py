#!/usr/bin/env python3
"""Create submission artifacts only after a verified complete dataset run."""
from pathlib import Path
import csv
import hashlib
import json
import zipfile
import sys

ROOT=Path(__file__).resolve().parent.parent

def build():
    sys.path.insert(0,str(ROOT/'code'))
    from main import fingerprint
    from buy_or_wait.data import COLUMNS
    output=ROOT/'output.csv';manifest_path=ROOT/'code/evaluation/final_run.json'
    if not output.exists() or not manifest_path.exists():
        raise ValueError('Run python code/main.py successfully on the full dataset before packaging.')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    if not manifest['complete'] or manifest['error']:
        raise ValueError('Final run is not complete')
    if hashlib.sha256(output.read_bytes()).hexdigest()!=manifest['output_sha256']:
        raise ValueError('output.csv changed after the verified run. Regenerate it.')
    if fingerprint(ROOT/'dataset')!=manifest['dataset_sha256']:
        raise ValueError('Dataset changed after the verified run. Regenerate predictions.')
    with output.open(encoding='utf-8',newline='') as f:
        reader=csv.DictReader(f);rows=list(reader)
        if reader.fieldnames!=COLUMNS:raise ValueError('Incorrect output columns')
    with (ROOT/'dataset/requests.csv').open(encoding='utf-8-sig',newline='') as f:
        requests=list(csv.DictReader(f))
    if [x['request_id'] for x in rows]!=[x['request_id'] for x in requests]:
        raise ValueError('Output requests missing, duplicated, or reordered')
    if not manifest['accounting']['prices_configured']:
        raise ValueError('Set both GEMINI_*_USD_PER_MILLION prices in .env, then rerun (cache avoids new calls) to produce the required cost estimate.')
    report=ROOT/'code/evaluation/usage_report.md'
    if manifest['output_sha256'] not in report.read_text(encoding='utf-8'):
        raise ValueError('Usage report does not match final output')
    dest=ROOT/'submission';dest.mkdir(exist_ok=True)
    included=[]
    for folder in ['code','dataset','cache']:
        for p in sorted((ROOT/folder).rglob('*')):
            if not p.is_file() or '__pycache__' in p.parts or p.suffix=='.pyc':continue
            included.append((p,p.relative_to(ROOT).as_posix()))
    for name in ['README.md','QUICKSTART.md','requirements.txt','.env.example','.gitignore',
                 'AGENTS.md','CLAUDE.md','problem_statement.md']:
        if (ROOT/name).is_file():included.append((ROOT/name,name))
    # Required path at ZIP root, also kept at the original code/evaluation location.
    included.append((report,'evaluation/usage_report.md'))
    final_run=ROOT/'runs'/manifest['run_id']
    for p in sorted(final_run.rglob('*')):
        if p.is_file():included.append((p,'evaluation/final_run/'+p.relative_to(final_run).as_posix()))
    tmp=dest/'code.zip.tmp'
    with zipfile.ZipFile(tmp,'w',zipfile.ZIP_DEFLATED) as z:
        for p,name in included:z.write(p,name)
    tmp.replace(dest/'code.zip')
    (dest/'output.csv').write_bytes(output.read_bytes())
    log=ROOT/'log.txt'
    if not log.exists():raise ValueError('log.txt is missing; preserve your real development transcript.')
    (dest/'chat_transcript.txt').write_bytes(log.read_bytes())
    print('Submission files created in:',dest)
    print('code.zip | output.csv | chat_transcript.txt')
    print('Submission: https://www.hackerrank.com/contests/hackerrank-orchestrate-september26/challenges/buy-or-wait/submission')

if __name__=='__main__':
    try:build()
    except (ValueError,OSError,KeyError) as e:
        print('Cannot package: '+str(e),file=sys.stderr);raise SystemExit(1)
