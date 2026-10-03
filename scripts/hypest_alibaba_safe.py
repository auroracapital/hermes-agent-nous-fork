#!/usr/bin/env python3
"""Fail-closed Alibaba list refresher. Existing Hermes browser only; candidate output only."""
import argparse
import copy
import datetime as dt
import json
import re
import time
from pathlib import Path

ACCOUNT_URL = 'https://i.alibaba.com/buyer/accounts/account_settings'
LIST_URL = 'https://biz.alibaba.com/order/list.htm'
import os
MEMBER_ID = os.environ.get('HYPEST_ALIBABA_MEMBER_ID', '')
NAME = os.environ.get('HYPEST_ALIBABA_ACCOUNT_NAME', '')
LIVE = Path('/home/ubuntu/hypest-deliverables/logistiek/alibaba_orders.json')
JS = r'''JSON.stringify((()=>{
const text=(e,s)=>e.querySelector(s)?.innerText?.trim()||'';
const p=document.querySelector('.next-pagination');
const label=p?.querySelector('.next-current')?.getAttribute('aria-label')||'';
const m=label.match(/Page (\d+), (\d+) pages/);
const total=[...document.querySelectorAll('*')].filter(e=>e.children.length===0).map(e=>e.textContent.trim()).find(t=>/^\d+ items$/.test(t));
return {url:location.href,page:m?Number(m[1]):null,pages:m?Number(m[2]):null,total:total?Number(total.split(' ')[0]):null,all:document.querySelector('[role=tab][aria-selected=true]')?.innerText==='All',rows:[...document.querySelectorAll('.order-list-item')].map(e=>({order_id:text(e,'.order-id').replace('Order #',''),order_date_raw:text(e,'.order-date').replace('Order date: ',''),supplier:text(e,'.supplier-name'),status:text(e,'.order-status'),amount_raw:text(e,'.order-amount'),list_raw_text:e.innerText,list_items:[...e.querySelectorAll('.product-desc')].map(p=>p.innerText.trim()),list_items_complete:!(/View all \d+ products|and \d+ more variations/.test(e.innerText))}))};
})())'''


def require(condition, message):
    if not condition:
        raise ValueError(message)


def merge(existing, capture):
    require(bool(MEMBER_ID) and bool(NAME), 'Expected account identity not configured')
    require(capture['account']['member_id'] == MEMBER_ID and capture['account']['name'] == NAME, 'Wrong HYPEST account')
    pages = capture['pages']
    require(bool(pages), 'No pages')
    total, count = pages[0]['total'], pages[0]['pages']
    require(isinstance(total, int) and total > 0 and isinstance(count, int) and count > 0, 'Missing counts')
    require(len(pages) == count and [p['page'] for p in pages] == list(range(1, count + 1)), 'Incomplete pagination')
    rows = [r for p in pages for r in p['rows']]
    require(all(p['total'] == total and p['pages'] == count and p['all'] and p['url'] == LIST_URL for p in pages), 'Changed totals, filter or URL')
    ids = [r['order_id'] for r in rows]
    require(len(ids) == total and len(set(ids)) == total, 'Duplicate/missing orders')
    require(all(re.fullmatch(r'\d{16,18}', i) for i in ids), 'Invalid order id')
    old = {r['order_id']: r for r in existing['orders']}
    require(len(old) == len(existing['orders']), 'Duplicate old orders')
    require(set(old).issubset(ids), 'Existing order disappeared: refusing replacement')
    result = copy.deepcopy(existing)
    result['orders'] = []
    for row in rows:
        require(row['supplier'] and row['status'] and row['list_items'], 'Incomplete row')
        amount = re.fullmatch(r'Total:\s*([A-Z]{3})\s+([\d,]+\.\d{2})', row['amount_raw'])
        if amount is None:
            raise ValueError('Invalid amount')
        date_text = row['order_date_raw'].split(',')[0]
        day = (dt.date.fromisoformat(date_text) if re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_text) else dt.datetime.strptime(date_text, '%d %b %Y').date()).isoformat()
        out = copy.deepcopy(old.get(row['order_id'], {'order_id': row['order_id'], 'items': [], 'delivery_note': None, 'tracking': None, 'carrier': None, 'paid_amount': None, 'paid_currency': None}))
        # Never overwrite detail fields, even with a seemingly complete list summary.
        for key in ('supplier', 'status', 'order_date_raw', 'list_raw_text', 'list_items', 'list_items_complete'):
            out[key] = copy.deepcopy(row[key])
        out.update(order_date=day, amount=amount[2].replace(',', ''), currency=amount[1], list_checked_at=capture['captured_at'])
        result['orders'].append(out)
    result.update(captured_at=capture['captured_at'], total_order_count=total)
    result['list_capture'] = {'page_count': count, 'account': capture['account'], 'detail_fields_preserved': True}
    return result


class ExistingHermesBrowser:
    def __init__(self):
        # Measured existing local endpoint and identity, no credential-file reads.
        import os
        os.environ.setdefault('CAMOFOX_URL', 'http://127.0.0.1:9377')
        os.environ.setdefault('CAMOFOX_USER_ID', 'agent-default')
        from tools import browser_camofox as camo, browser_tool as bt
        require(camo.is_camofox_mode(), 'Not the proven Camofox backend')
        session = camo._get_session(None)
        require(session.get('tab_id'), 'No existing tab; do not create/login')
        require(session['user_id'] == 'agent-default', 'Unexpected browser identity')
        self.bt = bt
        self.identity = {'backend': 'camofox', 'url': camo.get_camofox_url(), 'tab_id': session['tab_id'], 'user_id': session['user_id']}

    def call(self, func, **kwargs):
        result = json.loads(getattr(self.bt, func)(**kwargs))
        require(result.get('success'), result.get('error', 'Browser operation failed'))
        return result

    def navigate(self, url):
        return self.call('browser_navigate', url=url)

    def evaluate(self, expression):
        return self.call('browser_console', expression=expression)['result']

    def wait(self, expression, predicate):
        for _ in range(30):
            value = self.evaluate(expression)
            if predicate(value):
                return value
            time.sleep(1)
        raise ValueError('Browser content did not become ready')

    def account(self):
        require(bool(NAME) and bool(MEMBER_ID), 'Expected account identity not configured')
        self.navigate(ACCOUNT_URL)
        text = self.wait('document.body.innerText', lambda s: NAME in s and MEMBER_ID in s)
        return {'member_id': MEMBER_ID, 'name': NAME, 'url': ACCOUNT_URL, 'browser': self.identity}

    def capture(self):
        account = self.account()
        self.navigate(LIST_URL)
        first = self.wait(JS, lambda p: p.get('page') == 1 and bool(p.get('rows')))
        self.evaluate("document.querySelector('.notice-dialog-close')?.click(); 'notice dismissed'")
        pages = [first]
        for number in range(2, first['pages'] + 1):
            # Fixed pagination selector only: no payment/order action or arbitrary page JS.
            self.evaluate("document.querySelector('.next-pagination button[aria-label=\"Page " + str(number) + ", " + str(first['pages']) + " pages\"]').click(); 'page requested'")
            pages.append(self.wait(JS, lambda p: p.get('page') == number and p.get('rows') and p['rows'][0]['order_id'] != pages[-1]['rows'][0]['order_id']))
        # Reprove same account after collecting every page.
        require(self.account()['member_id'] == account['member_id'], 'Account changed')
        return {'account': account, 'pages': pages, 'captured_at': dt.datetime.now(dt.timezone.utc).isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--existing', type=Path, default=LIVE)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    require(args.candidate.resolve() != LIVE.resolve() and args.candidate.resolve() != args.existing.resolve(), 'Never write live/source orders')
    require(args.evidence.resolve() not in (LIVE.resolve(), args.existing.resolve(), args.candidate.resolve()), 'Evidence target collision')
    require(not args.candidate.exists() and not args.evidence.exists(), 'Output must be new')
    existing = json.loads(args.existing.read_text())
    capture = ExistingHermesBrowser().capture()
    candidate = merge(existing, capture)
    args.evidence.write_text(json.dumps(capture, ensure_ascii=False, indent=2) + '\n')
    args.candidate.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'verified_orders': len(candidate['orders']), 'pages': len(capture['pages']), 'candidate': str(args.candidate), 'evidence': str(args.evidence), 'live_written': False}))


if __name__ == '__main__':
    main()
