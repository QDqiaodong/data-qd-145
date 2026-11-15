from .common import require,ident,number,rows
from .rating import rate

def analyze(data):
    require(data,'currency fixed_charge tiers accounts');currency=ident(data['currency']);fixed=number(data['fixed_charge'],0,10**9);tiers=[];previous=0
    for t in rows(data['tiers'],100):
        require(t,'up_to rate');upper=t['up_to'];price=number(t['rate'],0,10**9)
        if previous is None:raise ValueError('unbounded tier must be last')
        if upper is not None:
            number(upper,1,10**12)
            if upper<=previous:raise ValueError('tier boundaries must increase')
        tiers.append({'up_to':upper,'rate':price});previous=upper
    if not tiers or tiers[-1]['up_to'] is not None:raise ValueError('last tier must be unbounded')
    accounts=[];seen=set()
    for a in rows(data['accounts'],20000):
        require(a,'id quantity');aid=ident(a['id']);quantity=number(a['quantity'],0,10**12)
        if aid in seen:raise ValueError('duplicate account snapshot')
        seen.add(aid);accounts.append({'id':aid,'quantity':quantity})
    return {'currency':currency,**rate(accounts,tiers,fixed)}
