"""Black-Scholes pricing, Greeks and implied volatility (European, no dividends).

Units: sigma is annualised (0.15 = 15 vol points); vega is per 1 vol point;
theta is per calendar day.
"""
import math


def _cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_price(S, K, T, r, sigma, option_type):
    typ = option_type.upper()
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if typ == 'CE' else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if typ == 'CE':
        return S * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
    return K * math.exp(-r * T) * _cdf(-d2) - S * _cdf(-d1)


def black_scholes(S, K, T, r, sigma, option_type):
    if min(S, K, T, sigma) <= 0:
        return {'delta': None, 'gamma': None, 'theta': None, 'vega': None, 'rho': None}
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if option_type.upper() == 'CE':
        delta = _cdf(d1)
        theta = (-S * _pdf(d1) * sigma / (2 * math.sqrt(T)) - r * K * math.exp(-r * T) * _cdf(d2)) / 365
        rho = K * T * math.exp(-r * T) * _cdf(d2) / 100
    else:
        delta = _cdf(d1) - 1
        theta = (-S * _pdf(d1) * sigma / (2 * math.sqrt(T)) + r * K * math.exp(-r * T) * _cdf(-d2)) / 365
        rho = -K * T * math.exp(-r * T) * _cdf(-d2) / 100
    gamma = _pdf(d1) / (S * sigma * math.sqrt(T))
    vega = S * _pdf(d1) * math.sqrt(T) / 100
    return {'delta': delta, 'gamma': gamma, 'theta': theta, 'vega': vega, 'rho': rho}


def implied_vol(price, S, K, T, r, option_type, lo=0.005, hi=5.0, tol=1e-6):
    """Bisection implied volatility, or None when the price is outside no-arbitrage bounds."""
    if price is None or price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(S - K * math.exp(-r * T), 0.0) if option_type.upper() == 'CE' else max(K * math.exp(-r * T) - S, 0.0)
    if price < intrinsic - 1e-9 or price > (S if option_type.upper() == 'CE' else K):
        return None
    f_lo = bs_price(S, K, T, r, lo, option_type) - price
    f_hi = bs_price(S, K, T, r, hi, option_type) - price
    if f_lo * f_hi > 0:
        return None  # not bracketed: no volatility explains this price, so none is reported (never a floor value)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(S, K, T, r, mid, option_type) - price
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)
