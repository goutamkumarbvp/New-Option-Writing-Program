import math

def _norm_cdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def _norm_pdf(x): return math.exp(-0.5*x*x)/math.sqrt(2*math.pi)
def black_scholes(S,K,T,r,sigma,option_type):
    if min(S,K,T,sigma)<=0:return {'delta':None,'gamma':None,'theta':None,'vega':None,'rho':None}
    d1=(math.log(S/K)+(r+0.5*sigma*sigma)*T)/(sigma*math.sqrt(T)); d2=d1-sigma*math.sqrt(T); typ=option_type.upper()
    if typ=='CE':
        delta=_norm_cdf(d1); theta=(-S*_norm_pdf(d1)*sigma/(2*math.sqrt(T))-r*K*math.exp(-r*T)*_norm_cdf(d2))/365; rho=K*T*math.exp(-r*T)*_norm_cdf(d2)
    else:
        delta=_norm_cdf(d1)-1; theta=(-S*_norm_pdf(d1)*sigma/(2*math.sqrt(T))+r*K*math.exp(-r*T)*_norm_cdf(-d2))/365; rho=-K*T*math.exp(-r*T)*_norm_cdf(-d2)
    gamma=_norm_pdf(d1)/(S*sigma*math.sqrt(T)); vega=S*_norm_pdf(d1)*math.sqrt(T)/100
    return {'delta':delta,'gamma':gamma,'theta':theta,'vega':vega,'rho':rho}
