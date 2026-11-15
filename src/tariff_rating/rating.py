def rate(accounts,tiers,fixed):
    bills=[]
    for a in sorted(accounts,key=lambda a:a['id']):
        lower=0;lines=[]
        for t in tiers:
            upper=t['up_to'];quantity=max(0,(a['quantity'] if upper is None else min(a['quantity'],upper))-lower)
            if quantity:lines.append({'from':lower,'up_to':upper,'quantity':quantity,'rate':t['rate'],'charge':quantity*t['rate']})
            if upper is None:break
            lower=upper
        variable=sum(l['charge'] for l in lines);bills.append({'id':a['id'],'quantity':a['quantity'],'lines':lines,'variable_charge':variable,'fixed_charge':fixed,'total':variable+fixed})
    return {'bills':bills,'total_quantity':sum(b['quantity'] for b in bills),'total_variable_charge':sum(b['variable_charge'] for b in bills),'total_fixed_charge':fixed*len(bills),'grand_total':sum(b['total'] for b in bills)}
