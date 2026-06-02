import requests, os
from dotenv import load_dotenv

load_dotenv(r'C:\Users\sagla\.tastytrade-mcp\.env')
key = os.getenv('TRADIER_API_KEY')

r = requests.get(
    'https://api.tradier.com/v1/markets/options/chains',
    params={'symbol':'SPY','expiration':'2026-06-05','greeks':'true'},
    headers={'Authorization':f'Bearer {key}','Accept':'application/json'}
)

opts = r.json()['options']['option']
puts = {o['strike']: o for o in opts if o['option_type']=='put' and o.get('greeks')}

print("EDGE SCANNER — Finding profitable spread configurations")
print("Target: breakeven WR < 75% (our historical WR is 81.5%)")
print("="*65)

edges_found = []

for short_strike, short_opt in puts.items():
    if not short_opt['greeks'].get('delta'):
        continue
    delta = abs(short_opt['greeks']['delta'])
    if delta < 0.10 or delta > 0.45:
        continue
    
    for width in [1, 2, 3, 5, 7, 10]:
        long_strike = short_strike - width
        long_opt = puts.get(long_strike)
        if not long_opt:
            continue
        
        credit = short_opt['bid'] - long_opt['ask']
        if credit <= 0.05:
            continue
        
        max_loss = (width * 100) - (credit * 100)
        if max_loss <= 0:
            continue
        
        target_profit = credit * 0.70 * 100
        breakeven_wr = max_loss / (max_loss + target_profit)
        
        if breakeven_wr < 0.75:
            edges_found.append({
                'short': short_strike,
                'long': long_strike,
                'width': width,
                'delta': delta,
                'credit': credit,
                'max_loss': max_loss,
                'target': target_profit,
                'breakeven_wr': breakeven_wr
            })

if edges_found:
    edges_found.sort(key=lambda x: x['breakeven_wr'])
    print(f"Found {len(edges_found)} configurations with edge!\n")
    for e in edges_found[:10]:
        print(f"Short: {e['short']} | Width: ${e['width']} | Delta: {e['delta']:.2f}")
        print(f"Credit: ${e['credit']:.2f} | Max loss: ${e['max_loss']:.0f}")
        print(f"Target profit: ${e['target']:.0f} | Breakeven WR: {e['breakeven_wr']*100:.1f}%")
        print()
else:
    print("NO edge found in any configuration today.")
    print("Market conditions may not support credit spreads right now.")