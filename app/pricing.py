FEE_RATE_BPS=1000
FEE_MIN_CENTS=500

def platform_fee(goods_cents,rate_bps=FEE_RATE_BPS):
    if not 800<=rate_bps<=1200: raise ValueError('fee rate must be 8–12%')
    return max(FEE_MIN_CENTS,(goods_cents*rate_bps+5000)//10000)
