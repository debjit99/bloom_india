from bloom_india.mf.analytics.pnl import lumpsum_pnl, sip_pnl

print("Testing Lump Sum P&L...")
r = lumpsum_pnl(100033, "2021-01-04", "2024-01-04", 100000)
print("LUMP SUM:")
print(f"  Invested:  Rs.{r['invested']:,.2f}")
print(f"  Value:     Rs.{r['current_value']:,.2f}")
print(f"  P&L:       Rs.{r['pnl']:+,.2f} ({r['pnl_pct']:+.2f}%)")
print(f"  CAGR:      {r['cagr_pct']:+.2f}%")
if r['warnings']:
    for w in r['warnings']:
        print(f"  WARN: {w}")

print("\nTesting SIP P&L...")
s = sip_pnl(100033, "2020-01-06", "2024-12-31", 5000, 30)
print("SIP:")
print(f"  Instalments:  {s['instalment_count']}")
print(f"  Invested:     Rs.{s['total_invested']:,.2f}")
print(f"  Value:        Rs.{s['current_value']:,.2f}")
print(f"  XIRR:         {s['xirr_pct']:+.2f}%")
if s['warnings']:
    for w in s['warnings']:
        print(f"  WARN: {w}")

print("\nAll P&L tests done.")