"""Enumerate and rank exact payment schedules. No LLM chooses output labels."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from itertools import combinations
import re
from .data import money,fmt,day
from .forecast import month_add,ZERO


def id_key(text):
    match=re.fullmatch(r'(.*?)(\d+)',text)
    return (match[1],int(match[2])) if match else (text,0)

@dataclass
class Plan:
    method:str
    payments:list
    changes:tuple=()
    option_id:str=''
    def rank(self):
        return (bool(self.changes),sum((a for d,a in self.payments),ZERO),
                self.payments[0][0],len(self.payments),id_key(self.option_id),
                len(self.changes),tuple((x[0],x[2],str(x[1])) for x in self.changes))


def option_plan(option,request,profile,start,end):
    if option['payment_method']!='installments': return None
    n=int(option['number_of_payments']); interval=int(option['payment_frequency_days'] or '0')
    if n<2 or interval<=0 or not profile['max_installment_months']: return None
    first=day(option['first_payment_date'])
    payments=[(first+timedelta(days=i*interval),money(option['payment_amount'])) for i in range(n)]
    max_months=int(profile['max_installment_months'])
    # An N-month plan includes its first payment month. Evaluate actual duration too.
    if max_months<1 or n>max_months or payments[-1][0]>=month_add(first,max_months): return None
    if first<start or payments[-1][0]>min(end,day(request['desired_completion_date'])): return None
    total=sum((a for d,a in payments),ZERO)
    if abs(total-money(option['total_payable_amount']))>Decimal('0.01'):
        raise ValueError('Payment option total does not match its exact schedule: '+option['payment_option_id'])
    # A cent difference in n rounded installments is allowed, but never discard the fee.
    if abs(money(request['requested_amount'])+money(option['financing_fee'])-total)>Decimal('0.01'):
        raise ValueError('Payment option fee is inconsistent: '+option['payment_option_id'])
    if any(a<=0 for d,a in payments): raise ValueError('Nonpositive installment')
    return Plan('installments',payments,option_id=option['payment_option_id'])


def select_plan(forecast,request,options):
    amount=money(request['requested_amount']); safe,earliest=forecast.capacities(amount)
    methods=set(forecast.profile['payment_methods_user_will_consider'].split('|'))
    deadline=day(request['desired_completion_date'])
    seeds=[]
    if 'full_payment' in methods and forecast.start<=deadline:
        seeds.append(Plan('full_payment',[(forecast.start,amount)]))
    if 'full_payment' in methods and earliest and forecast.start<earliest<=deadline:
        seeds.append(Plan('wait',[(earliest,amount)]))
    if ('partial_payment' in methods and request['allows_partial_payment'].lower()=='true'
            and ZERO<safe<amount and earliest and forecast.start<earliest<=deadline):
        seeds.append(Plan('partial_payment',[(forecast.start,safe),(earliest,amount-safe)]))
    if 'installments' in methods:
        for option in options:
            plan=option_plan(option,request,forecast.profile,forecast.start,forecast.end)
            if plan: seeds.append(plan)
    valid=[p for p in seeds if forecast.safe(p.payments)]
    # Avoid spending changes whenever any unmodified plan works, even if it costs more.
    if not valid:
        changes=forecast.allowed_changes()
        for n in range(1,min(3,len(changes))+1):
            for combo in combinations(changes,n):
                if len({x[0] for x in combo})!=n: continue
                for p in seeds:
                    if p.method=='wait': continue
                    if forecast.safe(p.payments,combo):
                        valid.append(Plan(p.method,p.payments,combo,p.option_id))
    best=min(valid,key=lambda p:p.rank()) if valid else None
    status='not_affordable'; method='not_recommended'; plan_text='none'; changes_text='none'
    if best:
        method=best.method
        status=('affordable_with_plan' if best.changes or method in ('partial_payment','installments')
                else 'affordable_later' if method=='wait' else 'affordable_now')
        plan_text='|'.join(f'{d.isoformat()}:{fmt(a)}' for d,a in best.payments)
        changes_text='|'.join(f'stop:{eid}' if kind=='stop' else f'reduce_to:{eid}:{fmt(a)}'
                              for eid,a,kind in best.changes) or 'none'
        minimum,_=forecast.trace(best.payments,best.changes)
        total=sum((a for d,a in best.payments),ZERO)
        explanation=(f'{method.replace("_"," ").capitalize()}: {len(best.payments)} payment(s), '
            f'total {forecast.home} {fmt(total)}, completed {best.payments[-1][0]}. '
            f'Forecast minimum {fmt(minimum)} stays above the required {fmt(forecast.floor)}. '
            f'Safe today before optional changes: {fmt(safe)}.')
        if best.option_id: explanation+=f' Offer {best.option_id} includes all stated fees.'
        if best.changes: explanation+=' Requires only the listed permitted recurring spending changes.'
    else:
        explanation=(f'No eligible schedule completes {forecast.home} {fmt(amount)} by {deadline} '
            f'while preserving {fmt(forecast.floor)} throughout the 90-day forecast. '
            f'Safe today before optional changes: {fmt(safe)}.')
        if earliest: explanation+=f' Financial capacity for one full payment begins {earliest}; deadline and payment preferences still apply.'
        elif forecast.trace()[0]<forecast.floor: explanation+=' Existing commitments already breach the minimum without changes.'
    row={'request_id':request['request_id'],'amount_safe_to_pay':fmt(safe),
         'affordability_status':status,'recommended_payment_method':method,'payment_plan':plan_text,
         'earliest_date_for_full_payment':earliest.isoformat() if earliest else '',
         'spending_changes_needed':changes_text,'decision_explanation':explanation}
    validate_output(row,forecast,request,options)
    return row,best


def parse_payments(value):
    if value=='none': return []
    result=[]
    for item in value.split('|'):
        d,a=item.split(':'); result.append((day(d),money(a)))
    return result

def parse_changes(value):
    if value=='none': return ()
    result=[]
    for item in value.split('|'):
        p=item.split(':')
        if p[0]=='stop' and len(p)==2: result.append((p[1],ZERO,'stop'))
        elif p[0]=='reduce_to' and len(p)==3: result.append((p[1],money(p[2]),'reduce_to'))
        else: raise ValueError('Invalid change encoding')
    return tuple(result)


def validate_output(row,forecast,request,options):
    """Verify the emitted CSV values, rather than trusting the plan object."""
    amount=money(request['requested_amount']); safe=money(row['amount_safe_to_pay'])
    if row['request_id']!=request['request_id'] or not ZERO<=safe<=amount: raise ValueError('Output ID/amount invalid')
    expected_safe,expected_earliest=forecast.capacities(amount)
    if safe!=expected_safe or row['earliest_date_for_full_payment']!=(str(expected_earliest) if expected_earliest else ''):
        raise ValueError('Capacity fields disagree with the baseline forecast')
    method=row['recommended_payment_method']; status=row['affordability_status']
    if method not in {'full_payment','partial_payment','installments','wait','not_recommended'}: raise ValueError('Invalid method')
    if status not in {'affordable_now','affordable_with_plan','affordable_later','not_affordable'}: raise ValueError('Invalid status')
    payments=parse_payments(row['payment_plan']); changes=parse_changes(row['spending_changes_needed'])
    if len(changes)>3 or len({x[0] for x in changes})!=len(changes): raise ValueError('Conflicting spending changes')
    if not set(changes)<=set(forecast.allowed_changes()): raise ValueError('Unpermitted spending change')
    methods=set(forecast.profile['payment_methods_user_will_consider'].split('|'))
    if method=='not_recommended':
        if payments or changes or status!='not_affordable': raise ValueError('Invalid fallback')
        return
    if not payments or payments!=sorted(payments) or any(a<0 for d,a in payments): raise ValueError('Invalid payment list')
    if payments[-1][0]>day(request['desired_completion_date']): raise ValueError('Deadline missed')
    if not forecast.safe(payments,changes): raise ValueError('Unsafe emitted plan')
    if method in ('full_payment','partial_payment','installments') and method not in methods: raise ValueError('Payment preference violated')
    if method=='wait':
        if 'full_payment' not in methods or status!='affordable_later' or changes: raise ValueError('Wait preference/status violation')
        if not expected_earliest or expected_earliest<=forecast.start or payments!=[(expected_earliest,amount)]: raise ValueError('Invalid wait date')
    if method=='full_payment':
        if payments!=[(forecast.start,amount)]: raise ValueError('Invalid full payment')
        if status!=('affordable_with_plan' if changes else 'affordable_now'): raise ValueError('Invalid full payment status')
    if method=='partial_payment':
        if (status!='affordable_with_plan' or request['allows_partial_payment'].lower()!='true'
            or not ZERO<safe<amount or expected_earliest is None
            or payments!=[(forecast.start,safe),(expected_earliest,amount-safe)]): raise ValueError('Partial payment contract violated')
    if method=='installments':
        if status!='affordable_with_plan': raise ValueError('Invalid installment status')
        eligible=[option_plan(o,request,forecast.profile,forecast.start,forecast.end) for o in options]
        if not any(p and p.payments==payments for p in eligible): raise ValueError('No matching supplied installment option')
    if status=='affordable_now' and expected_earliest!=forecast.start: raise ValueError('Invalid affordable_now date')
