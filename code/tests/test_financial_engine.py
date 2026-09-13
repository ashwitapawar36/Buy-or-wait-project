import unittest
import tempfile
import json
import sys
from pathlib import Path
from datetime import date,timedelta
from decimal import Decimal
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from buy_or_wait.data import Dataset,money,COLUMNS
from buy_or_wait.evidence import empty_evidence,validate_evidence,Gemini
from buy_or_wait.forecast import Forecast,Flow,month_add,occurrences
from buy_or_wait.planner import select_plan,validate_output,option_plan
from buy_or_wait.reporting import usage_numbers,write_usage

D=date(2026,1,1)

class FakeData:
    def convert(self,amount,currency,home,settlement_date):
        if currency!=home:raise ValueError('Missing exact settlement-date exchange rate')
        return money(amount)


def request(amount='500',methods='full_payment|partial_payment|installments'):
    return {'request_id':'test','user_id':'u','request_date':str(D),'request_type':'purchase',
            'requested_amount':amount,'desired_completion_date':str(D+timedelta(days=80)),
            'allows_partial_payment':'true','request_text':'Can I pay?'}

def profile(balance='1000',methods='full_payment|partial_payment|installments'):
    return {'user_id':'u','home_currency':'USD','current_available_balance':balance,
            'minimum_balance_to_keep':'200','financial_priorities':'housing',
            'expense_categories_to_protect':'rent|groceries',
            'expense_categories_user_is_willing_to_stop':'streaming|cloud_storage',
            'expense_categories_user_is_willing_to_reduce':'dining',
            'payment_methods_user_will_consider':methods,'max_installment_months':'3'}

def event(eid,dt,amount='100',category='rent',status='settled',direction='debit',description=None):
    return {'event_id':eid,'user_id':'u','event_type':'expense' if direction=='debit' else 'income',
            'description':description or category,'category':category,'direction':direction,'amount':amount,
            'currency':'USD','event_date':str(dt),'settlement_date':str(dt),'status':status,
            'linked_event_id':'','flexibility':'fixed','minimum_allowed_amount':''}

def context(events=(),balance='1000',amount='500',methods='full_payment|partial_payment|installments'):
    return {'request':request(amount),'profile':profile(balance,methods),'events':list(events),
            'messages':[],'images':[]}

def forecast(ctx=None,ev=None):return Forecast(FakeData(),ctx or context(),ev or empty_evidence())

def offer(amount='200',n=3,fee='0',first=D,interval=30,ident='payment_option_2'):
    return {'payment_option_id':ident,'request_id':'test','payment_method':'installments',
            'payment_amount':amount,'number_of_payments':str(n),'first_payment_date':str(first),
            'payment_frequency_days':str(interval),'financing_fee':fee,
            'total_payable_amount':str(money(amount)*n)}

class FinancialTests(unittest.TestCase):
    def test_money_rounding(self):self.assertEqual(money('1.005'),Decimal('1.01'))
    def test_nonfinite_money_rejected(self):
        for x in ['NaN','Infinity','-Infinity']:
            with self.assertRaises(ValueError):money(x)
    def test_month_end_anchor(self):
        self.assertEqual(list(occurrences(date(2024,1,31),'monthly',0,None,date(2024,3,31))),
                         [date(2024,1,31),date(2024,2,29),date(2024,3,31)])
    def test_historical_settled_not_replayed(self):
        f=forecast(context([event('old',D-timedelta(days=2),'900')]))
        self.assertEqual(f.trace()[0],money('1000'))
    def test_pending_debit_reserved_credit_ignored(self):
        f=forecast(context([event('debit',D+timedelta(days=2),'400',status='pending'),
                            event('credit',D+timedelta(days=2),'9999',status='pending',direction='credit')]))
        self.assertEqual(f.capacities(money('1000'))[0],money('400'))
    def test_failed_cancelled_unrealized_ignored(self):
        f=forecast(context([event(str(i),D+timedelta(days=1),'9999',status=s)
                            for i,s in enumerate(['failed','cancelled','unrealized'])]))
        self.assertEqual(f.trace()[0],money('1000'))
    def test_temporary_intraday_breach_rejected(self):
        f=forecast(context(balance='300'))
        f.flows=[Flow(D+timedelta(days=2),money('-200'),'a','rent'),
                 Flow(D+timedelta(days=2),money('1000'),'b','salary')]
        self.assertFalse(f.safe())
    def test_safe_today_uses_entire_horizon(self):
        f=forecast();f.flows=[Flow(D+timedelta(days=85),money('-700'),'bill','rent')]
        self.assertEqual(f.capacities(money('500')),(money('100'),None))
    def test_earliest_independent_of_preferences(self):
        ctx=context(methods='installments',amount='600');f=forecast(ctx)
        row,p=select_plan(f,ctx['request'],[offer()])
        self.assertEqual(row['earliest_date_for_full_payment'],str(D))
        self.assertEqual(row['recommended_payment_method'],'installments')
    def test_partial_exact_two_payments(self):
        ctx=context(balance='500',amount='600');f=forecast(ctx)
        payday=D+timedelta(days=10)
        f.flows=[Flow(payday,money('800'),'salary','salary')]
        row,p=select_plan(f,ctx['request'],[])
        self.assertEqual(row['payment_plan'],f'{D}:300.00|{payday}:300.00')
        self.assertEqual(row['affordability_status'],'affordable_with_plan')
    def test_partial_disallowed_by_request(self):
        ctx=context(balance='500',amount='600');ctx['request']['allows_partial_payment']='false';f=forecast(ctx)
        f.flows=[Flow(D+timedelta(days=10),money('800'),'salary','salary')]
        row,p=select_plan(f,ctx['request'],[])
        self.assertEqual(row['recommended_payment_method'],'wait')
    def test_deadline_blocks_future_payment(self):
        ctx=context(balance='500',amount='600');ctx['request']['desired_completion_date']=str(D+timedelta(days=2));f=forecast(ctx)
        f.flows=[Flow(D+timedelta(days=10),money('800'),'salary','salary')]
        row,p=select_plan(f,ctx['request'],[])
        self.assertEqual(row['recommended_payment_method'],'not_recommended')
        self.assertEqual(row['earliest_date_for_full_payment'],str(D+timedelta(days=10)))
    def test_no_spending_cuts_to_protected_category(self):
        events=[event(f'e{i}',date(2025,m,5),'100','rent') for i,m in enumerate([10,11,12])]
        for e in events:e['flexibility']='stoppable'
        ctx=context(events);ctx['profile']['expense_categories_user_is_willing_to_stop']='rent'
        self.assertEqual(forecast(ctx).allowed_changes(),[])
    def test_stop_changes_do_not_inflate_safe_today(self):
        events=[event(f'e{i}',date(2025,m,5),'100','streaming') for i,m in enumerate([10,11,12])]
        for e in events:e['flexibility']='stoppable'
        ctx=context(events,balance='900',amount='600',methods='full_payment');f=forecast(ctx)
        row,p=select_plan(f,ctx['request'],[])
        self.assertEqual(row['amount_safe_to_pay'],'400.00')
        self.assertEqual(row['affordability_status'],'affordable_with_plan')
        self.assertEqual(row['spending_changes_needed'],'stop:e2')
    def test_fee_included(self):
        ctx=context(amount='600',methods='installments');f=forecast(ctx)
        row,p=select_plan(f,ctx['request'],[offer('210',fee='30')])
        self.assertEqual(sum(a for d,a in p.payments),money('630'))
    def test_cheaper_offer_wins(self):
        ctx=context(amount='600',methods='installments');f=forecast(ctx)
        row,p=select_plan(f,ctx['request'],[offer('210',fee='30'),offer(ident='payment_option_3')])
        self.assertEqual(p.option_id,'payment_option_3')
    def test_numeric_option_tie_break(self):
        ctx=context(amount='600',methods='installments');f=forecast(ctx)
        row,p=select_plan(f,ctx['request'],[offer(ident='payment_option_10'),offer(ident='payment_option_2')])
        self.assertEqual(p.option_id,'payment_option_2')
    def test_preferences_reject_full_even_when_affordable(self):
        ctx=context(methods='installments');row,p=select_plan(forecast(ctx),ctx['request'],[])
        self.assertEqual(row['recommended_payment_method'],'not_recommended')
        self.assertEqual(row['earliest_date_for_full_payment'],str(D))
    def test_missing_image_amount_rejected(self):
        ctx=context([event('missing',D,amount='')]);ctx['images']=[{'image_id':'img','related_event_id':'missing'}]
        with self.assertRaises(ValueError):validate_evidence(empty_evidence(),ctx)
    def test_cross_user_hallucinated_source_rejected(self):
        ev=empty_evidence();ev['income_streams']=[{'source_ids':['foreign'],'amount':'500','currency':'USD',
            'first_date':str(D),'frequency':'once','interval_days':0,'end_date':'','reason':'fake'}]
        with self.assertRaises(ValueError):validate_evidence(ev,context())
    def test_pending_credit_cannot_justify_income(self):
        ctx=context([event('p',D,'500',status='pending',direction='credit')])
        ev=empty_evidence();ev['income_streams']=[{'source_ids':['p'],'amount':'500','currency':'USD',
            'first_date':str(D),'frequency':'once','interval_days':0,'end_date':'','reason':'pending'}]
        with self.assertRaises(ValueError):validate_evidence(ev,ctx)
    def test_independent_validator_rejects_unsafe_csv(self):
        ctx=context();f=forecast(ctx);row,p=select_plan(f,ctx['request'],[])
        row['payment_plan']=f'{D}:9999.00'
        with self.assertRaises(ValueError):validate_output(row,f,ctx['request'],[])
    def test_missing_fx_rate_never_guessed(self):
        ctx=context([event('p',D+timedelta(days=1),'50',status='pending')]);ctx['events'][0]['currency']='EUR'
        with self.assertRaises(ValueError):forecast(ctx)
    def test_usage_includes_thinking(self):
        self.assertEqual(usage_numbers({'promptTokenCount':100,'candidatesTokenCount':20,'thoughtsTokenCount':30,'totalTokenCount':150}),(100,50,150))
    def test_cache_replay_does_not_call_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=Gemini(FakeData(),Path(tmp)/'cache',Path(tmp)/'run');client.key='test-placeholder'
            response={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(empty_evidence())}]}}]}
            with patch.object(client,'request',return_value=(response,{'promptTokenCount':100,'totalTokenCount':120,'candidatesTokenCount':20})) as call:
                first,key=client.extract(context());second,key2=client.extract(context())
                self.assertEqual(call.call_count,1);self.assertEqual(key,key2);self.assertEqual(first,second)
                self.assertEqual(len(client.reused),1)
    def test_full_dataset_integrity(self):
        root=Path(__file__).resolve().parents[2]
        if not (root/'dataset').exists():self.skipTest('Dataset not present')
        ds=Dataset(root/'dataset')
        self.assertEqual(len(ds.tables['requests']),250)
        for req in ds.tables['requests']:
            ctx=ds.context(req)
            self.assertNotIn('amount_safe_to_pay',ctx['request'])
            self.assertTrue(ds.options[req['request_id']])

if __name__=='__main__':unittest.main()
