"""Validated CSV access. Input data is never modified."""
from __future__ import annotations
import csv
import json
import os
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import date
from collections import defaultdict

COLUMNS = ['request_id', 'amount_safe_to_pay', 'affordability_status',
           'recommended_payment_method', 'payment_plan', 'earliest_date_for_full_payment',
           'spending_changes_needed', 'decision_explanation']
INPUT_COLUMNS = ['request_id','user_id','request_date','request_type','requested_amount',
                 'desired_completion_date','allows_partial_payment','request_text']
CENT = Decimal('0.01')

def money(value):
    try:
        x = Decimal(str(value))
        if not x.is_finite():
            raise ValueError('Non-finite amount')
        return x.quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        raise ValueError('Invalid decimal amount') from None

def fmt(value):
    return format(money(value), '.2f')

def day(value):
    return date.fromisoformat(value)

def atomic_json(path, obj):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    temp.replace(path)

def write_csv(path, rows, columns=COLUMNS):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader(); writer.writerows(rows)
    temp.replace(path)

def load_env(path):
    """Small literal .env loader: no shell execution, interpolation, or secret logging."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key.strip().replace('_', '').isalnum():
            os.environ.setdefault(key.strip(), value.strip().strip('\"\''))

class Dataset:
    def __init__(self, folder):
        self.folder = Path(folder).resolve()
        self.tables = {}
        for name in ['requests','sample_requests','financial_profiles','financial_events',
                     'messages','images','exchange_rates','request_payment_options','output']:
            with (self.folder / (name + '.csv')).open(encoding='utf-8-sig', newline='') as f:
                self.tables[name] = list(csv.DictReader(f))
        self.profiles = self.unique('financial_profiles', 'user_id')
        self.events = self.unique('financial_events', 'event_id')
        self.unique('requests', 'request_id'); self.unique('sample_requests','request_id')
        self.unique('messages','message_id'); self.unique('images','image_id')
        self.unique('request_payment_options','payment_option_id')
        self.by_user = {}
        for name in ['financial_events', 'messages', 'images']:
            groups = defaultdict(list)
            for row in self.tables[name]:
                groups[row['user_id']].append(row)
            self.by_user[name] = groups
        self.options = defaultdict(list)
        for row in self.tables['request_payment_options']:
            self.options[row['request_id']].append(row)
        self.rates = {}
        for row in self.tables['exchange_rates']:
            key = (row['rate_date'], row['from_currency'], row['to_currency'])
            rate = Decimal(row['rate'])
            if not rate.is_finite() or rate <= 0:
                raise ValueError(f'Invalid exchange rate: {key}')
            if key in self.rates and self.rates[key] != rate:
                raise ValueError(f'Conflicting exchange rates: {key}')
            self.rates[key] = rate
        self.validate()

    def unique(self, name, key):
        rows = self.tables[name]; result = {r[key]:r for r in rows}
        if len(result) != len(rows) or '' in result:
            raise ValueError(f'Duplicate/empty {key} in {name}')
        return result

    def validate(self):
        for row in self.tables['requests'] + self.tables['sample_requests']:
            if row['user_id'] not in self.profiles:
                raise ValueError(f"Missing profile: {row['user_id']}")
            if money(row['requested_amount']) < 0:
                raise ValueError('Negative requested amount')
            day(row['request_date']); day(row['desired_completion_date'])
        linked = {i['related_event_id'] for i in self.tables['images']}
        for e in self.events.values():
            if e['user_id'] not in self.profiles:
                raise ValueError('Event user missing')
            if not e['amount'] and e['event_id'] not in linked:
                raise ValueError(f"Missing amount without image: {e['event_id']}")
            if e['amount'] and money(e['amount']) < 0:
                raise ValueError(f"Negative amount: {e['event_id']}")
            if e['linked_event_id']:
                prior = self.events.get(e['linked_event_id'])
                if not prior or prior['user_id'] != e['user_id']:
                    raise ValueError('Invalid linked_event_id')
        for name in ['images','messages']:
            for item in self.tables[name]:
                eid = item['related_event_id']
                if eid and (eid not in self.events or self.events[eid]['user_id'] != item['user_id']):
                    raise ValueError(f'Invalid evidence event link in {name}')
        for i in self.tables['images']:
            if not self.image_path(i).is_file():
                raise ValueError(f"Missing image {i['image_id']}")

    def image_path(self, image):
        ident = image['image_id']
        if not ident.replace('_', '').isalnum():
            raise ValueError('Unsafe image ID')
        return self.folder / 'media' / 'images' / (ident + '.png')

    def context(self, request):
        uid = request['user_id']; rid = request['request_id']
        def relevant(x):
            return not x['request_id'] or x['request_id'] == rid
        messages = [m for m in self.by_user['messages'][uid] if relevant(m)
                    and m['sent_at'][:10] <= request['request_date']]
        messages.sort(key=lambda x:(x['sent_at'],x['message_id']))
        images = [i for i in self.by_user['images'][uid] if relevant(i)]
        return {'request':{k:request[k] for k in INPUT_COLUMNS},
                'profile':self.profiles[uid],
                'events':self.by_user['financial_events'][uid],
                'messages':messages, 'images':images}

    def convert(self, amount, currency, home, settlement_date):
        if currency == home:
            return money(amount)
        key = (str(settlement_date), currency, home)
        if key not in self.rates:
            raise ValueError(f'Missing exact settlement-date exchange rate: {key}')
        return money(Decimal(str(amount)) * self.rates[key])
