from scipy.stats import norm
import numpy as np
from scipy.stats import t

def compute_ror_with_gbm(p_f, delta, data=None, mu=None, sigma=None, p_0=None, nu_arr=None, testing=False):
    if not testing or mu is None or sigma is None or p_0 is None:
        data.loc[:, 'log_returns'] = np.log(data['Price'] / data['Price'].shift(1))
        mu = np.mean(data.loc[:, 'log_returns'])
        sigma = np.std(data.loc[:,'log_returns'])

    numerator = (np.log(p_f) - np.log(p_0) - (mu - 0.5 * sigma**2) * delta)
    denominator = sigma * np.sqrt(delta)
    
    if testing:
        safe_mask = (denominator > 0)
        ror_arr = np.full_like(denominator, np.nan, dtype=float)
        z = np.empty_like(denominator, dtype=float)
        z[safe_mask] = numerator[safe_mask] / denominator[safe_mask]
        ror_arr[safe_mask] = t.cdf(z[safe_mask], df=nu_arr[safe_mask])
    else:
        z = numerator / denominator
        ror_arr = t.cdf(z, df=nu_arr)

    return ror_arr

def compute_floor_price(delta, data, p_0, nu, risk_threshold):
    df = data.copy(deep=False)
    df.loc[:, 'log_returns'] = np.log(df.loc[:, 'Price'] / df.loc[:, 'Price'].shift(1))
    mu = np.mean(df.loc[:, 'log_returns'])
    sigma = np.std(df.loc[:, 'log_returns'])

    z_target = t.ppf(risk_threshold, df=nu)
    drift = (mu - 0.5*sigma**2) * delta
    vol_term = sigma * np.sqrt(delta) * z_target
    log1p = vol_term + drift + np.log(p_0)

    return np.exp(log1p)

# def get_data_stats(btc_df):
#     btc_df['log_returns'] = np.log(btc_df['Price'] / btc_df['Price'].shift(1))

#     mu = np.mean(btc_df['log_returns'])

#     print('mu')

#     sigma = np.std(btc_df['log_returns'])

#     print('sigma')

#     p_0 = btc_df.loc[len(btc_df) - 1, 'Price']

#     print('p_0')
    
#     return mu, sigma, p_0

