"""Deterministic recurrence reconstruction and conservative day-by-day cash flow."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from decimal import Decimal
from calendar import monthrange
from statistics import median
from .data import money, day

ZERO=Decimal('0.00')
VARIABLE_CATEGORIES={'groceries','transport','dining'}


def month_add(d, n, anchor=None):
    total=d.year*12+d.month-1+n; y,m=divmod(total,12)
    return date(y,m+1,min(anchor or d.day,monthrange(y,m+1)[1]))


def occurrences(first,frequency,interval,end,until):
    current=first; index=0; limit=min(end or until,until)
    if frequency not in ('once','monthly','days'): raise ValueError('Invalid recurrence')
    if frequency=='days' and interval<=0: raise ValueError('Nonpositive recurrence interval')
    while current <= limit:
        yield current
        index+=1
        if frequency=='once': break
        current=month_add(first,index) if frequency=='monthly' else first+timedelta(days=interval*index)
        if index>1000: raise ValueError('Recurrence exceeds safety limit')

@dataclass
class Flow:
    date: date
    amount: Decimal       # Signed home-currency amount.
    source: str
    category: str
    adjustable_id: str=''
    native_amount: Decimal=ZERO
    native_currency: str=''
    def serial(self):
        return {k:str(v) if isinstance(v,(date,Decimal)) else v for k,v in asdict(self).items()}

@dataclass
class Expense:
    event_id: str
    category: str
    amount: Decimal
    currency: str
    first: date
    frequency: str
    interval: int
    flexibility: str
    minimum: Decimal
    members: tuple
    description: str
    stop: bool=False
    amended_first: date|None=None

class Forecast:
    def __init__(self,dataset,context,evidence,estimator='conservative'):
        self.dataset=dataset; self.context=context; self.evidence=evidence
        self.start=day(context['request']['request_date']); self.end=self.start+timedelta(days=90)
        self.profile=context['profile']; self.home=self.profile['home_currency']
        self.opening=money(self.profile['current_available_balance'])
        self.floor=money(self.profile['minimum_balance_to_keep'])
        self.estimator=estimator; self.warnings=[]
        self.events={e['event_id']:dict(e) for e in context['events']}
        for fix in evidence['event_overrides']:
            for k in ['amount','currency','settlement_date','status']:
               if fix[k] and not (k == 'status' and fix[k] == 'unchanged'):
                self.events[fix['event_id']][k] = fix[k]
        self.excluded={x['event_id'] for x in evidence['excluded_events']}
        for e in self.events.values():
            if not e['amount']: raise ValueError(f"Missing amount: {e['event_id']}")
            money(e['amount'])
        self.expenses=self.infer_expenses()
        self.flows=self.build_flows()

    def infer_expenses(self):
        groups=defaultdict(list)
        excluded=self.excluded | set(self.evidence['nonrecurring_event_ids'])
        # Linked refunds/reversals/authorizations aren't recurring purchases. Keep cash liabilities
        # in build_flows; this filter only concerns historical recurrence inference.
        for e in self.events.values():
            if e['event_id'] in excluded or e['direction']!='debit' or e['status']!='settled': continue
            if e['event_type'] in ('investment_purchase','investment_valuation','investment_sale'): continue
            if e['linked_event_id'] or not e['settlement_date']: continue
            d=day(e['settlement_date'])
            if d > self.start: continue
            key=(e['category'], '' if e['category'] in VARIABLE_CATEGORIES else e['description'],e['currency'])
            groups[key].append(e)
        result=[]
        for (category,description,currency),rows in groups.items():
            rows.sort(key=lambda e:(e['settlement_date'],e['event_id']))
            if category in VARIABLE_CATEGORIES:
                recurring_rows = []
                for item in rows:
                    prior = [
                        money(x['amount']) for x in rows
                        if x['settlement_date'] < item['settlement_date']
                    ]
                    typical = median(prior[-6:]) if len(prior) >= 6 else None

                    regular_same_day = typical is not None and any(
                        x['event_id'] != item['event_id']
                        and x['settlement_date'] == item['settlement_date']
                        and money(x['amount']) <= typical * 3
                        for x in rows
                    )

                    if (
                        typical is not None and typical > 0
                        and regular_same_day
                        and money(item['amount']) > typical * 3
                    ):
                        self.warnings.append(
                            'Isolated same-day spending spike excluded '
                            'from recurrence: ' + item['event_id']
                        )
                        continue

                    recurring_rows.append(item)

                rows = recurring_rows
            # Same-day variable expenses are one daily spending observation.
            buckets=defaultdict(list)
            for e in rows: buckets[e['settlement_date']].append(e)
            dates=sorted(day(x) for x in buckets)
            if len(dates)<3: continue
            gaps=[(b-a).days for a,b in zip(dates,dates[1:])]
            monthly=all((b.year*12+b.month)-(a.year*12+a.month)==1
                        for a,b in zip(dates[-4:],dates[-3:])) if len(dates)>=4 else False
            # Monthly recurring records retain a common day or month-end pattern.
            monthly=monthly and (max(d.day for d in dates[-4:])-min(d.day for d in dates[-4:])<=3)
            interval=int(median(gaps[-6:]))
            regular=sum(abs(g-interval)<=max(2,interval*.15) for g in gaps)/len(gaps)>=.75
            if not monthly and (not regular or interval<1): continue
            if (self.start-dates[-1]).days > max(45,interval*2):
                self.warnings.append(f'Stale expense history not extrapolated: {rows[-1]["event_id"]}')
                continue
            amounts=[sum((money(e['amount']) for e in buckets[d.isoformat()]),ZERO) for d in dates]
            amount=money(median(amounts[-3:]))
            if self.estimator=='conservative' and len(set(amounts[-3:]))>1:
                amount=max(amount,amounts[-1])
            last=rows[-1]; anchor=dates[-1]
            frequency='monthly' if monthly else 'days'
            first=month_add(anchor,1) if monthly else anchor+timedelta(days=interval)
            n=1
            while first<self.start:
                n+=1
                first=month_add(anchor,n) if monthly else anchor+timedelta(days=n*interval)
            result.append(Expense(last['event_id'],category,amount,currency,first,frequency,interval,
                last['flexibility'],money(last['minimum_allowed_amount'] or '0'),
                tuple(e['event_id'] for e in rows),last['description']))
        for fix in self.evidence['expense_adjustments']:
            matches=[x for x in result if fix['event_id'] in x.members]
            if len(matches)!=1: raise ValueError('Mandatory expense amendment has no unique recurring target: '+fix['event_id'])
            x=matches[0]
            if fix['amount']: x.amount=money(fix['amount'])
            if fix['multiplier']: x.amount=money(x.amount*Decimal(fix['multiplier']))
            if fix['next_date']:
                x.amended_first=day(fix['next_date'])
                if x.amended_first<self.start: raise ValueError('Expense amendment is before request date')
            x.stop=fix['stop']
        return result

    def build_flows(self):
        flows=[]; pending=[]
        covered_income={s for x in self.evidence['income_streams'] for s in x['source_ids']}
        for e in self.events.values():
            if e['event_id'] in self.excluded or e['status'] in ('cancelled','failed','unrealized'): continue
            if e['event_type']=='investment_valuation': continue
            if e['direction']=='credit':
                if (e['status']=='scheduled' or (e['status']=='settled' and e['settlement_date'] and day(e['settlement_date'])>self.start)) and e['event_id'] not in covered_income:
                    raise ValueError('Scheduled income not accounted for in extracted streams: '+e['event_id'])
                # ALL supported future income is expanded once from income_streams below.
                continue
            if e['status']=='settled' and e['settlement_date'] and day(e['settlement_date'])<=self.start: continue
            if e['status'] not in ('pending','scheduled','settled'): continue
            d=max(self.start,day(e['settlement_date'] or e['event_date']))
            if d>self.end: continue
            amount=self.dataset.convert(e['amount'],e['currency'],self.home,d)
            # Reserve due debits on their due/settlement date, including past-due debits today.
            flows.append(Flow(d,-amount,e['event_id'],e['category'],native_amount=money(e['amount']),native_currency=e['currency']))
            pending.append((e,d))
        for expense in self.expenses:
            if expense.stop: continue
            dates=list(occurrences(expense.first,expense.frequency,expense.interval,None,self.end))
            if expense.amended_first and dates: dates[0]=expense.amended_first
            for d in sorted(set(dates)):
                if d<self.start or d>self.end: continue
                # Explicit representation of this same bill replaces, rather than adds to, forecast.
                match=[e for e,ed in pending if ed==d and e['category']==expense.category
                       and (e['description']==expense.description or e['linked_event_id'] in expense.members)]
                if match: continue
                amount=self.dataset.convert(expense.amount,expense.currency,self.home,d)
                flows.append(Flow(d,-amount,expense.event_id,expense.category,
                                  expense.event_id,expense.amount,expense.currency))
        for field,sign in [('income_streams',1),('extra_debits',-1)]:
            seen=set()
            for i,s in enumerate(self.evidence[field]):
                fingerprint=(s['amount'],s['currency'],s['first_date'],s['frequency'],tuple(sorted(s['source_ids'])))
                if fingerprint in seen: raise ValueError('Duplicate extracted cash stream')
                seen.add(fingerprint)
                if field=='extra_debits' and any(e['event_id'] in s['source_ids'] for e,d in pending):
                    raise ValueError('Extra debit duplicates an outstanding debit row')
                for d in occurrences(day(s['first_date']),s['frequency'],s['interval_days'],
                                     day(s['end_date']) if s['end_date'] else None,self.end):
                    amount=self.dataset.convert(s['amount'],s['currency'],self.home,d)
                    flows.append(Flow(d,sign*amount,'|'.join(s['source_ids']),
                        'confirmed_income' if sign>0 else 'additional_liability'))
        return sorted(flows,key=lambda f:(f.date, f.amount>=0, f.source))

    def allowed_changes(self):
        protect=set(self.profile['expense_categories_to_protect'].split('|'))
        stop=set(self.profile['expense_categories_user_is_willing_to_stop'].split('|'))
        reduce=set(self.profile['expense_categories_user_is_willing_to_reduce'].split('|'))
        options=[]
        active={f.adjustable_id for f in self.flows if f.adjustable_id}
        for e in self.expenses:
            if e.category in protect or e.event_id not in active: continue
            if e.category in stop and e.flexibility in ('stoppable','reducible_or_stoppable'):
                options.append((e.event_id,ZERO,'stop'))
            if e.category in reduce and e.flexibility in ('reducible','reducible_or_stoppable') and e.minimum<e.amount:
                options.append((e.event_id,e.minimum,'reduce_to'))
        return options

    def adjusted_flows(self,changes=()):
        choices={eid:amount for eid,amount,kind in changes}
        for f in self.flows:
            if f.adjustable_id in choices:
                value=self.dataset.convert(choices[f.adjustable_id],f.native_currency,self.home,f.date)
                yield Flow(f.date,-value,f.source,f.category,f.adjustable_id,choices[f.adjustable_id],f.native_currency)
            else: yield f

    def trace(self,payments=(),changes=()):
        days=defaultdict(list)
        for f in self.adjusted_flows(changes): days[f.date].append(f)
        pay=defaultdict(Decimal)
        for d,a in payments:
            if d<self.start or d>self.end or a<0: raise ValueError('Invalid payment date/amount')
            pay[d]+=a
        balance=self.opening; low=balance; rows=[]
        for n in range(91):
            d=self.start+timedelta(days=n)
            daily_low=balance
            # Date-only data: conservatively debit obligations before same-day income.
            # The recommended purchase is then paid after confirmed receipts that day.
            for f in sorted(days[d],key=lambda f:(f.amount>=0,f.source)):
                balance+=f.amount; daily_low=min(daily_low,balance)
            balance-=pay[d]; daily_low=min(daily_low,balance)
            low=min(low,daily_low)
            rows.append({'date':d,'balance':balance,'minimum_during_day':daily_low,'payment':pay[d]})
        return low,rows

    def safe(self,payments=(),changes=()):
        return self.trace(payments,changes)[0]>=self.floor

    def capacities(self,requested):
        low,rows=self.trace()
        if low<self.floor:
            return ZERO,None
        # Purchase occurs at end of candidate day; future intraday troughs still count.
        suffix=None; capacities={}
        for row in reversed(rows):
            future_min=row['balance'] if suffix is None else min(row['balance'],suffix)
            capacities[row['date']]=max(ZERO,future_min-self.floor)
            suffix=row['minimum_during_day'] if suffix is None else min(suffix,row['minimum_during_day'])
        safe=min(requested,capacities[self.start])
        earliest=next((r['date'] for r in rows if capacities[r['date']]>=requested),None)
        return money(safe),earliest
