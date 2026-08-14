from scipy.stats import norm
import numpy as np
from scipy.stats import t
import torch
from torch.distributions import StudentT
import scipy.stats as stats
from torch.autograd import Function
import math

def compute_ror_with_gbm(p_f, delta, mu=None, sigma=None, p_0=None, nu=None, testing=False):
    numerator   = (torch.log(p_f) - torch.log(p_0) - (mu - 0.5 * sigma**2) * delta)
    denominator = sigma * torch.sqrt(delta)

    if testing:
        safe_mask = denominator > 0
        ror_arr   = torch.full_like(denominator, float('nan'))
        z = torch.zeros_like(denominator)
        z[safe_mask] = numerator[safe_mask] / denominator[safe_mask]
        cdf  = student_t_cdf(z, nu)
        ror_arr = torch.where(safe_mask, cdf, torch.full_like(z, float('nan')))
    else:
        z       = numerator / denominator
        student = StudentT(df=nu)
        ror_arr = student.cdf(z)

    return ror_arr

def compute_floor_price(delta, mu, sigma, p_0, nu, risk_threshold):
    z_target = t.ppf(risk_threshold, df=nu)
    drift = (mu - 0.5*sigma**2) * delta
    vol_term = sigma * np.sqrt(delta) * z_target
    log1p = vol_term + drift + np.log(p_0)

    return np.exp(log1p)

def student_t_cdf(z: torch.Tensor, nu: torch.Tensor) -> torch.Tensor:
    # z and nu must broadcast to the same shape
    # 1) compute x = ν / (ν + z^2)
    x = nu / (nu + z.pow(2))
    x = torch.clamp(x, min=1e-6, max=1.0 - 1e-6)

    # 2) parameters a = ν/2, b = 1/2
    a = nu * 0.5
    b = 0.5

    # 3) regularized incomplete beta I_x(a,b)
    # torch.special.betainc returns the *regularized* incomplete beta
    I = torch.special.betainc(a, b, x)

    # 4) piecewise CDF
    #  - for z < 0:  0.5 * I
    #  - for z >= 0: 1 - 0.5 * I
    return torch.where(z < 0,
                       0.5 * I,
                       1.0 - 0.5 * I)
