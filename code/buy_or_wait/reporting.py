"""Run accounting; no fabricated API tokens or sample scores."""
from collections import defaultdict
from decimal import Decimal
import os
from .data import atomic_json


def usage_numbers(u):
    prompt=int(u.get('promptTokenCount',0))
    output=int(u.get('candidatesTokenCount',0))+int(u.get('thoughtsTokenCount',0))
    return prompt,output,int(u.get('totalTokenCount',prompt+output))


def write_usage(path,client,count,complete,run_id,output_hash=''):
    groups=defaultdict(lambda:[0,0,0,0]); unknown=0
    for call in client.calls:
        if 'promptTokenCount' not in call['usage']:
            unknown+=1; continue
        p,o,t=usage_numbers(call['usage'])
        g=groups[call['model']]; g[0]+=1; g[1]+=p; g[2]+=o; g[3]+=t
    reused=defaultdict(lambda:[0,0,0,0])
    for item in client.reused:
        for usage in item['usage']:
            p,o,t=usage_numbers(usage); g=reused[item['model']]
            g[0]+=1; g[1]+=p; g[2]+=o; g[3]+=t
    rate_in=os.getenv('GEMINI_INPUT_USD_PER_MILLION','')
    rate_out=os.getenv('GEMINI_OUTPUT_USD_PER_MILLION','')
    configured=bool(rate_in and rate_out)
    if configured:
        a,b=Decimal(rate_in),Decimal(rate_out)
        if not a.is_finite() or not b.is_finite() or a<0 or b<0: raise ValueError('Invalid configured token price')
    def cost(g):
        if not configured:return 'not configured'
        return f'${(Decimal(g[1])*a+Decimal(g[2])*b)/1000000:.6f}'
    lines=['# Token usage and cost analysis','',
        f'Run: `{run_id}`',f'Status: {"completed full evaluation dataset" if complete else "not a completed full evaluation dataset"}',
        f'Predictions produced: {count}',f'Output SHA-256: `{output_hash or "not available"}`','',
        'Provider: Google Gemini Developer API. Output tokens below include thinking tokens.',
        'Local disk cache hits cause no API call in this run. Their original usage is listed separately.',
        'Only actual returned usageMetadata is counted; failed calls with unknown usage are not represented as free.', '',
        '## API usage in this run','',
        '| Model | Responses | Input tokens | Output tokens | Total tokens | Estimated cost (USD) |',
        '|---|---:|---:|---:|---:|---:|']
    for model,g in sorted(groups.items()):lines.append(f'| {model} | {g[0]} | {g[1]} | {g[2]} | {g[3]} | {cost(g)} |')
    total=[sum(g[i] for g in groups.values()) for i in range(4)]
    lines.append(f'| Overall | {total[0]} | {total[1]} | {total[2]} | {total[3]} | {cost(total)} |')
    avg=total[3]/count if count else 0
    avg_cost=cost([0,total[1]/count,total[2]/count,0]) if count else 'not applicable'
    lines+=['',f'Average tokens per produced request: {avg:.2f}',
            f'Estimated average cost per produced request: {avg_cost}',
            f'Failed attempts with unknown usage/billing: {unknown}',
            f'Locally cached request results reused: {len(client.reused)}','',
            '## Original extraction usage attributable to reused evidence','',
            '| Model | Original responses | Input tokens | Output tokens | Total tokens | Estimated original cost |',
            '|---|---:|---:|---:|---:|---:|']
    for model,g in sorted(reused.items()):lines.append(f'| {model} | {g[0]} | {g[1]} | {g[2]} | {g[3]} | {cost(g)} |')
    orig=[sum(g[i] for g in reused.values()) for i in range(4)]
    combined=[total[i]+orig[i] for i in range(4)]
    lines+=['',f'Combined current-run + reused-origin tokens: {combined[3]}',
            f'Combined attributable estimated cost: {cost(combined)}',
            f'Combined average tokens per request: {combined[3]/count if count else 0:.2f}',
            f'Combined estimated cost per request: {cost([0,combined[1]/count,combined[2]/count,0]) if count else "not applicable"}',
            '', '## Pricing assumptions','',
            f'Input USD per million: {rate_in or "not configured"}; output USD per million: {rate_out or "not configured"}.',
            'Set both prices in .env to match your model/tier; set both to 0 only for an applicable free tier.',
            'These are token-based estimates, not a billing statement. Unknown attempts may incur additional cost.',
            'Original cached usage is provenance, not a second charge in this run.',
            'Development-assistant tokens are not available to this program and are not fabricated.',
            '[Official pricing](https://ai.google.dev/gemini-api/docs/pricing).','']
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('\n'.join(lines),encoding='utf-8')
    return {'api_totals':total,'cache_origin_totals':orig,'unknown_attempts':unknown,'prices_configured':configured}


def compare_samples(expected,actual):
    from .data import money
    exact=['affordability_status','recommended_payment_method','payment_plan',
           'earliest_date_for_full_payment','spending_changes_needed']
    wanted={r['request_id']:r for r in expected}; counts={k:0 for k in ['amount_safe_to_pay']+exact}
    details=[]
    for row in actual:
        target=wanted[row['request_id']]; match={}
        match['amount_safe_to_pay']=abs(money(row['amount_safe_to_pay'])-money(target['amount_safe_to_pay']))<=Decimal('.01')
        from .planner import parse_payments,parse_changes
        for field in exact:
            if field=='payment_plan':match[field]=parse_payments(row[field])==parse_payments(target[field])
            elif field=='spending_changes_needed':match[field]=set(parse_changes(row[field]))==set(parse_changes(target[field]))
            else:match[field]=row[field]==target[field]
        for k,v in match.items():counts[k]+=int(v)
        details.append({'request_id':row['request_id'],'matches':match,
                        'expected':{k:target[k] for k in counts},'actual':{k:row[k] for k in counts}})
    return {'evaluated':len(actual),'correct_counts':counts,'all_fields_correct':sum(all(x['matches'].values()) for x in details),
            'note':'Public sample agreement only; not the hidden evaluation score. No sample answers enter model prompts.',
            'details':details}
