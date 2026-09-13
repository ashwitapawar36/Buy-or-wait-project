"""Gemini REST extraction with schema validation, atomic cache and usage provenance."""
from __future__ import annotations
import base64
import hashlib
import json
import os
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone
from .data import atomic_json, money, day

PROMPT = Path(__file__).with_name('evidence_prompt.txt').read_text(encoding='utf-8')
S = {'type':'string'}
B = {'type':'boolean'}
I = {'type':'integer'}
SA = {'type':'array', 'items':S}

def obj(props):
    return {'type':'object','properties':props,'required':list(props)}
def arr(props):
    return {'type':'array','items':obj(props)}
FLOW = {'source_ids':SA,'amount':S,'currency':S,'first_date':S,
        'frequency':{'type':'string','enum':['monthly','days','once']},
        'interval_days':I,'end_date':S,'reason':S}
SCHEMA = obj({
    'event_overrides':arr({'event_id':S,'amount':S,'currency':S,'settlement_date':S,
        'status':{'type':'string','enum':['unchanged','settled','pending','scheduled','failed','cancelled','unrealized']},
        'evidence_ids':SA,'reason':S}),
    'excluded_events':arr({'event_id':S,'evidence_ids':SA,'reason':S}),
    'income_streams':arr(FLOW),
    'expense_adjustments':arr({'event_id':S,'amount':S,'multiplier':S,'next_date':S,
        'stop':B,'evidence_ids':SA,'reason':S}),
    'extra_debits':arr(FLOW),
    'nonrecurring_event_ids':SA,'reviewed_evidence_ids':SA,'unresolved':SA,'notes':SA})


def empty_evidence():
    return {k:[] for k in SCHEMA['properties']}


def validate_schema(value, schema, path='evidence'):
    kind = schema['type']
    if kind == 'object':
        if not isinstance(value,dict) or set(value) != set(schema['properties']):
            raise ValueError(f'{path}: unexpected or missing fields')
        for k,s in schema['properties'].items(): validate_schema(value[k],s,path+'.'+k)
    elif kind == 'array':
        if not isinstance(value,list): raise ValueError(f'{path}: expected list')
        for i,v in enumerate(value): validate_schema(v,schema['items'],f'{path}[{i}]')
    elif kind == 'string':
        if not isinstance(value,str): raise ValueError(f'{path}: expected string')
    elif kind == 'integer':
        if type(value) is not int: raise ValueError(f'{path}: expected integer')
    elif kind == 'boolean':
        if type(value) is not bool: raise ValueError(f'{path}: expected boolean')
    if 'enum' in schema and value not in schema['enum']: raise ValueError(f'{path}: invalid enum')


def validate_evidence(ev, context):
    validate_schema(ev,SCHEMA)
    events = {e['event_id']:e for e in context['events']}
    source_evidence = {x['message_id'] for x in context['messages']} | {x['image_id'] for x in context['images']}
    sources = set(events) | source_evidence
    def refs(ids, allowed):
        if not ids or not set(ids) <= allowed:
            raise ValueError('Missing or foreign source references in evidence')
    if set(ev['reviewed_evidence_ids']) != source_evidence:
        raise ValueError('Not all supplied images/messages were reviewed')
    overrides = {}
    for x in ev['event_overrides']:
        if x['event_id'] not in events or x['event_id'] in overrides:
            raise ValueError('Unknown/duplicate event override')
        refs(x['evidence_ids'],source_evidence)
        if x['amount'] and money(x['amount']) < 0: raise ValueError('Negative extracted amount')
        if x['settlement_date']: day(x['settlement_date'])
        overrides[x['event_id']] = x
    for eid,e in events.items():
        if not e['amount']:
            linked = {i['image_id'] for i in context['images'] if i['related_event_id']==eid}
            fix = overrides.get(eid,{})
            if not fix.get('amount') or not linked.intersection(fix.get('evidence_ids',[])):
                raise ValueError(f'Missing linked-image amount for {eid}; refusing zero fallback')
    for x in ev['excluded_events'] + ev['expense_adjustments']:
        if x['event_id'] not in events: raise ValueError('Unknown adjusted event')
        refs(x['evidence_ids'],source_evidence | {events[x['event_id']]['linked_event_id']} - {''})
    for x in ev['expense_adjustments']:
        if events[x['event_id']]['direction'] != 'debit': raise ValueError('Expense adjustment targets credit')
        if x['amount'] and money(x['amount']) < 0: raise ValueError('Negative expense amount')
        if x['multiplier'] and money(x['multiplier']) <= 0: raise ValueError('Invalid multiplier')
        if x['amount'] and x['multiplier']: raise ValueError('Use amount or multiplier, not both')
        if x['next_date']: day(x['next_date'])
    start = day(context['request']['request_date'])
    for x in ev['income_streams'] + ev['extra_debits']:
        refs(x['source_ids'],sources)
        if money(x['amount']) <= 0: raise ValueError('Flow amount must be positive')
        if day(x['first_date']) < start: raise ValueError('Historical income/expense replay rejected')
        if x['frequency']=='days' and not 1 <= x['interval_days'] <= 366:
            raise ValueError('Invalid recurring interval')
        if x['end_date'] and day(x['end_date']) < day(x['first_date']): raise ValueError('Invalid stream end')
        if not re.fullmatch('[A-Z]{3}',x['currency']): raise ValueError('Invalid currency')
    for flow in ev['income_streams']:
        # Pending/failed/noncash records cannot alone justify an income stream.
        if not set(flow['source_ids']) & source_evidence:
            supporting = [events[s] for s in flow['source_ids']]
            if not any(e['direction']=='credit' and e['status'] in ('settled','scheduled')
                       and e['event_type'] != 'investment_valuation' for e in supporting):
                raise ValueError('Income supported only by noncash/unconfirmed records')
    covered = {s for flow in ev['income_streams'] for s in flow['source_ids']}
    excluded = {x['event_id'] for x in ev['excluded_events']}
    for eid,original in events.items():
        row = dict(original)
        row.update({k:v for k,v in overrides.get(eid,{}).items()
                    if k in ('amount','currency','settlement_date','status') and v
and not (k == 'status' and v == 'unchanged')})
        if eid in excluded or row['direction'] != 'credit': continue
        future_settled = row['status']=='settled' and row['settlement_date'] and day(row['settlement_date'])>start
        if (row['status']=='scheduled' or future_settled) and eid not in covered:
            raise ValueError('Confirmed future credit missing from income_streams: '+eid)
    if not set(ev['nonrecurring_event_ids']) <= set(events): raise ValueError('Unknown nonrecurring event')
    if ev['unresolved']:
        raise ValueError('Material unresolved evidence: ' + '; '.join(ev['unresolved']))

class Gemini:
    def __init__(self, dataset, cache_dir, run_dir, offline=False, refresh=False):
        self.dataset = dataset
        self.cache_dir=Path(cache_dir); self.cache_dir.mkdir(parents=True,exist_ok=True)
        self.run_dir=Path(run_dir); self.run_dir.mkdir(parents=True,exist_ok=True)
        self.model = os.getenv('GEMINI_MODEL','gemini-2.5-flash')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', self.model): raise ValueError('Invalid GEMINI_MODEL')
        self.key=os.getenv('GEMINI_API_KEY','')
        self.offline=offline; self.refresh=refresh
        self.calls=[]; self.reused=[]; self.last_call=0

    def extract(self, context):
        parts=[{'text':json.dumps(context,ensure_ascii=False,separators=(',',':'))}]
        for i in context['images']:
            parts.append({'text':f"Evidence image {i['image_id']}; linked event {i['related_event_id']}"})
            parts.append({'inlineData':{'mimeType':'image/png','data':base64.b64encode(
                self.dataset.image_path(i).read_bytes()).decode('ascii')}})
        config={'temperature':0,'responseMimeType':'application/json','responseSchema':SCHEMA,
                'maxOutputTokens':12000}
        if self.model.startswith('gemini-2.5-flash'):
            config['thinkingConfig']={'thinkingBudget':1024}
        body={'systemInstruction':{'parts':[{'text':PROMPT}]},
              'contents':[{'role':'user','parts':parts}], 'generationConfig':config}
        digest=hashlib.sha256(json.dumps({'model':self.model,'body':body},sort_keys=True).encode()).hexdigest()
        path=self.cache_dir/(digest+'.json')
        if path.exists() and not self.refresh:
            saved=json.loads(path.read_text(encoding='utf-8'))
            validate_evidence(saved['evidence'],context)
            self.reused.append({'cache_key':digest,'request_id':context['request']['request_id'],
                                'usage':saved['usage'],'model':saved['model']})
            return saved['evidence'],digest
        if self.offline:
            raise ValueError('No valid cached evidence. Run with your Gemini key first, or run --check for an offline dataset check.')
        if not self.key or self.key in ('your_actual_key_here','paste_your_key_here'):
            raise ValueError('Set GEMINI_API_KEY in the root .env file. Never paste it into chat.')
        result=None; usages=[]
        for repair in range(2):
            result,usage=self.request(body,context['request']['request_id']); usages.append(usage)
            try:
                candidates=result.get('candidates',[])
                if not candidates or candidates[0].get('finishReason') not in ('STOP',None):
                    raise ValueError('Gemini response was blocked or truncated')
                chunks=candidates[0]['content']['parts']
                raw=''.join(p.get('text','') for p in chunks if not p.get('thought'))
                ev=json.loads(raw); validate_evidence(ev,context)
            except (ValueError,KeyError,IndexError,TypeError) as exc:
                if repair: raise ValueError('Gemini evidence did not pass validation after repair: '+str(exc)) from None
                # Regenerate against original source context; don't echo untrusted model instructions.
                body['contents'][0]['parts'].append({'text':'VALIDATION ERROR: '+str(exc)+'. Return corrected complete JSON; original rules still apply.'})
                continue
            saved={'model':self.model,'created_at':datetime.now(timezone.utc).isoformat(),
                   'usage':usages,'evidence':ev}
            atomic_json(path,saved)
            return ev,digest
        raise ValueError('Evidence extraction failed')

    def request(self,body,request_id):
        payload=json.dumps(body).encode()
        for attempt in range(5):
            pause=float(os.getenv('GEMINI_MIN_INTERVAL_SECONDS','4'))-(time.monotonic()-self.last_call)
            if pause>0: time.sleep(min(pause,60))
            self.last_call=time.monotonic()
            req=urllib.request.Request(
                'https://generativelanguage.googleapis.com/v1beta/models/'+self.model+':generateContent',
                data=payload,headers={'Content-Type':'application/json','x-goog-api-key':self.key},method='POST')
            try:
                with urllib.request.urlopen(req,timeout=180) as response:
                    result=json.load(response)
                usage=result.get('usageMetadata',{})
                if 'promptTokenCount' not in usage:
                    raise ValueError('Gemini response omitted token usage; cannot report usage accurately')
                entry={'request_id':request_id,'model':self.model,'status':'response','usage':usage,
                       'timestamp':datetime.now(timezone.utc).isoformat()}
                self.calls.append(entry)
                with (self.run_dir/'api_calls.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps(entry)+'\n')
                return result,usage
            except urllib.error.HTTPError as exc:
                # Expose only the API's diagnostic message after redacting credentials.
                # Do not print response metadata, headers, or request payloads.
                try:
                    error_payload=json.loads(exc.read(65536).decode('utf-8',errors='replace'))
                    detail=str(error_payload.get('error',{}).get('message',''))
                except (ValueError,AttributeError,TypeError):
                    detail='Google returned no readable diagnostic message.'
                if self.key:
                    detail=detail.replace(self.key,'[REDACTED]')
                detail=re.sub(r'AIza[A-Za-z0-9_-]+','[REDACTED]',detail)
                detail=re.sub(r'(?i)([?&]key=|x-goog-api-key[\"\s:=]+)[^\s\"&]+',r'\1[REDACTED]',detail)
                detail=' '.join(detail.split())[:2000]
                self.calls.append({'request_id':request_id,'model':self.model,'status':f'HTTP {exc.code}', 'usage':{}})
                if exc.code in (429,500,502,503,504) and attempt<4:
                    time.sleep(min(2**attempt*3,45)); continue
                hints={400:'Invalid API request/model configuration.',401:'API key authentication failed.',
                       403:'API key permissions or API access rejected.',404:'Model unavailable: set GEMINI_MODEL to an available model.',
                       429:'Quota/rate limit reached. Cached successful requests are preserved; retry when quota is available.'}
                raise ValueError(f'Gemini HTTP {exc.code}: '+hints.get(exc.code,'Service request failed.')+' Google diagnostic: '+(detail or 'not provided')) from None
            except (urllib.error.URLError,TimeoutError):
                self.calls.append({'request_id':request_id,'model':self.model,'status':'network error; billing unknown','usage':{}})
                if attempt<4: time.sleep(min(2**attempt*3,45)); continue
                raise ValueError('Could not reach Gemini. Check your connection; successful evidence is cached.') from None
