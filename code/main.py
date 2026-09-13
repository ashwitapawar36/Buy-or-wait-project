#!/usr/bin/env python3
"""Run from any working directory: python code/main.py --check, --samples, or no flags."""
from __future__ import annotations
import argparse
import hashlib
from html import parser
import json
import sys
from pathlib import Path
from datetime import datetime,timezone
from buy_or_wait.data import Dataset,load_env,atomic_json,write_csv
from buy_or_wait.evidence import Gemini
from buy_or_wait.forecast import Forecast
from buy_or_wait.planner import select_plan
from buy_or_wait.reporting import write_usage,compare_samples

ROOT=Path(__file__).resolve().parent.parent

def fingerprint(folder):
    h=hashlib.sha256()
    for p in sorted(Path(folder).rglob('*')):
        if p.is_file():
            h.update(p.relative_to(folder).as_posix().encode());h.update(p.read_bytes())
    return h.hexdigest()

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',type=Path,default=ROOT/'dataset')
    parser.add_argument('--check',action='store_true',help='Validate provided files without any API calls')
    parser.add_argument('--samples',action='store_true',help='Run 25 public examples and compare the answers')
    parser.add_argument('--limit',type=int,help='Run only first N rows; never overwrite final output.csv')
    parser.add_argument('--request-id',help='Run one request; never overwrite final output.csv')
    parser.add_argument('--skip-request', action='append', default=[])
    parser.add_argument('--offline',action='store_true',help='Require existing Gemini evidence cache; no network')
    parser.add_argument('--refresh',action='store_true',help='Ignore old cache and call Gemini again')
    parser.add_argument('--expense-estimator',choices=['conservative','recent-median','recent-six-median']  ,default='conservative')
    args=parser.parse_args(argv)
    if args.limit is not None and args.limit<1:parser.error('--limit must be positive')
    if args.offline and args.refresh:parser.error('--offline and --refresh conflict')
    load_env(ROOT/'.env')
    dataset=Dataset(args.dataset)
    if args.check:
        for k,v in dataset.tables.items():print(f'{k}: {len(v)} rows')
        print('Dataset structure and required image links OK. No API calls made.')
        return 0
    source='sample_requests' if args.samples else 'requests'
    selected=list(dataset.tables[source])
    if args.request_id:selected=[r for r in selected if r['request_id']==args.request_id]
    if args.skip_request:
        known = {r['request_id'] for r in dataset.tables[source]}
        if set(args.skip_request) - known:
            parser.error('Unknown --skip-request ID')
        selected = [
            r for r in selected
            if r['request_id'] not in args.skip_request
        ]
    if args.limit:selected=selected[:args.limit]
    if not selected:raise ValueError('No matching requests')
    is_full = (
        not args.samples
        and not args.limit
        and not args.request_id
        and not args.skip_request
    )
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    run_dir=ROOT/'runs'/run_id; run_dir.mkdir(parents=True)
    client=Gemini(dataset,ROOT/'cache',run_dir,args.offline,args.refresh)
    results=[]; error=None
    needs_review=[]
    try:
        for i,request in enumerate(selected,1):
            print(f'[{i}/{len(selected)}] {request["request_id"]}: interpreting evidence...',flush=True)
            context=dataset.context(request)
            try:
                evidence,cache_key=client.extract(context)
            except ValueError as exc:
                detail = str(exc)
                unresolved_prefixes = (
                    'Material unresolved evidence:',
                    'Gemini evidence did not pass validation after repair: Material unresolved evidence:',
                )
                if not detail.startswith(unresolved_prefixes):
                    raise

                needs_review.append({
                    'request_id': request['request_id'],
                    'reason': detail,
                    'payment_recommended': False,
                    'amount_safe_to_pay': None,
                })
                atomic_json(run_dir/'needs_review.json', needs_review)
                print('  NEEDS REVIEW: ' + detail, flush=True)
                continue
            forecast=Forecast(dataset,context,evidence,args.expense_estimator)
            row,plan=select_plan(forecast,request,dataset.options[request['request_id']])
            results.append(row)
            _,trace=forecast.trace(plan.payments if plan else (),plan.changes if plan else ())
            atomic_json(run_dir/'audit'/(request['request_id']+'.json'),{
                'request_id':request['request_id'],'cache_key':cache_key,'evidence':evidence,
                'forecast_assumptions':{'days':90,'estimator':args.expense_estimator,
                    'same_day_order':'existing debits, confirmed credits, recommended payment'},
                'baseline_flows':[f.serial() for f in forecast.flows],
                'daily_balance_after_plan':trace,'warnings':forecast.warnings,'prediction':row})
            write_csv(run_dir/'predictions.csv',results)
            print('  '+row['affordability_status']+' / '+row['recommended_payment_method'],flush=True)
    except (ValueError,OSError,KeyError,TypeError) as exc:
        error=str(exc)
    complete = (
        not error
        and not needs_review
        and is_full
        and len(results) == len(dataset.tables['requests'])
    )   
    pred=run_dir/'predictions.csv'
    digest=hashlib.sha256(pred.read_bytes()).hexdigest() if pred.exists() else ''
    accounting=write_usage(run_dir/'usage_report.md',client,len(results),complete,run_id,digest)
    manifest={'run_id':run_id,'complete':complete,'sample_run':args.samples,'requested_rows':len(selected),
              'produced_rows':len(results),'dataset_sha256':fingerprint(dataset.folder),
              'output_sha256':digest,'model':client.model,'expense_estimator':args.expense_estimator,
              'error':error,'needs_review':needs_review,'accounting':accounting,'cache_keys':[p.stem for p in (ROOT/'cache').glob('*.json')]}
    atomic_json(run_dir/'manifest.json',manifest)
    if args.samples and results:
        scores=compare_samples(dataset.tables['sample_requests'],results)
        atomic_json(run_dir/'sample_scores.json',scores)
        print('Public sample matches:',json.dumps(scores['correct_counts']))
    if complete:
        write_csv(ROOT/'output.csv',results)
        (ROOT/'code'/'evaluation'/'usage_report.md').write_text((run_dir/'usage_report.md').read_text(encoding='utf-8'),encoding='utf-8')
        atomic_json(ROOT/'code'/'evaluation'/'final_run.json',manifest)
        print('Full run complete: output.csv and code/evaluation/usage_report.md generated.')
    print('Run details:',run_dir)
    if error:
        print('Stopped safely: '+error,file=sys.stderr)
        print('No incomplete file was published as final output.csv. Fix the issue and rerun; completed evidence is cached.',file=sys.stderr)
        return 1

    if needs_review:
        print(
            f'{len(needs_review)} request(s) need review; '
            'see needs_review.json. No final output was published.'
        )
        return 2
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except (ValueError,OSError) as exc:
        print('Error: '+str(exc),file=sys.stderr);sys.exit(1)
