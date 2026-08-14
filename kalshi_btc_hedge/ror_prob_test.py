#!/usr/bin/env python
# coding: utf-8

# In[1]:


get_ipython().system('jupyter nbconvert --to script ror_prob_test.ipynb')

get_ipython().run_line_magic('load_ext', 'autoreload')
get_ipython().run_line_magic('autoreload', '2')

from compute_ror_prob import compute_ror_with_gbm
from functions import calculate_l_v
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import t
from sklearn.preprocessing import PolynomialFeatures
from functools import partial
import warnings
from scipy.optimize import minimize
import torch
import torch.nn as nn
import torch.optim as optim


warnings.filterwarnings(
    "ignore",
    category=pd.errors.PerformanceWarning
)


# In[2]:


def load_cleaned_btc_data():
    btc_df = pd.read_csv("data/coinbaseUSD_1-min_data.csv")

    # Filter data from 01-01-2017 to latest data and drop NaNs
    btc_df['Date'] = pd.to_datetime(btc_df['Date'], utc=True)
    btc_df = btc_df.set_index("Date").sort_index()
    start_date = pd.to_datetime("2017-01-01", utc=True)
    btc_df = btc_df[btc_df.index >= start_date]
    btc_df = btc_df.dropna()

    btc_df['Price'] = (btc_df['High'] + btc_df['Low']) / 2.0
    return btc_df

btc_df = load_cleaned_btc_data()


# In[3]:


def define_training_constants():
    deltas = [5, 15, 30, 45, 60] # in minutes
    ps = [0.05, 0.04, 0.03, 0.02, 0.01, 0.005, -0.005, -0.01, -0.02, -0.03, -0.04, -0.05] # percentage changes (-0.05, -0.1 seem rly rly small)
    # ror_bin_edges = [0.0, 0.005, 0.01, 0.02, 0.03, 0.04, 0.05] + list(np.arange(0.1, 1.01, 0.05))  # probability of ror
    ror_bin_edges = list(np.arange(0.0, 0.03, 0.002)) + [1.0]
    return deltas, ps, ror_bin_edges

deltas, ps, ror_bin_edges = define_training_constants()


# In[4]:


def compute_empirical_delta_price_change(btc_df, deltas):
    # Determine true delta indices
    td_deltas = np.array([np.timedelta64(int(d), 'm') for d in deltas])
    timestamps_broadcasted = np.broadcast_to(btc_df.index.values, (len(deltas), len(btc_df)))
    deltas_broadcasted = td_deltas[:, np.newaxis]
    target_final_times = timestamps_broadcasted + deltas_broadcasted
    flat_search = np.searchsorted(btc_df.index.values, target_final_times.ravel(), side='left')
    all_target_idx = flat_search.reshape((len(deltas), len(btc_df)))
    all_target_idx = np.minimum(all_target_idx, len(btc_df) - 1)

    # Compute true deltas and true price changes
    true_final_times = btc_df.index.values.astype('int64')[all_target_idx]
    true_deltas = (true_final_times - btc_df.index.values.astype('int64')[np.newaxis, :]) / (1e9 * 60.0)
    true_price_changes = (btc_df['Price'].values[all_target_idx] - btc_df['Price'].values[np.newaxis, :]) / btc_df['Price'].values[np.newaxis, :]
    return true_deltas, true_price_changes

true_deltas, true_price_changes = compute_empirical_delta_price_change(btc_df, deltas)


# In[5]:


def ema_vectorized(x: torch.Tensor, alpha: torch.Tensor):
    """
    Compute the EMA of x[0..T-1] with decay α in O(T) time, no Python loop.
    x:    shape [T]
    alpha: scalar in (0,1)
    returns: mu of shape [T]
    """
    T = x.shape[0]
    a = 1 - alpha
    # a^t for t = 0..T-1
    idx   = torch.arange(T, dtype=x.dtype, device=x.device)
    a_pow = a ** idx                 # shape [T]
    # build y_i = α * x_i / a^i
    y     = alpha * x / a_pow        # shape [T]
    # prefix sums of y
    csum  = torch.cumsum(y, dim=0)   # shape [T]
    # μ_t = a^t * sum_{i=0..t} y_i
    mu    = a_pow * csum             # shape [T]
    return mu

def ema_mean_and_std(x: torch.Tensor, alpha: torch.Tensor):
    # 1) EMA of x
    mu = ema_vectorized(x, alpha)
    # 2) EMA of x^2
    mu2 = ema_vectorized(x*x, alpha)
    # variance = E[x^2] - (E[x])^2
    var = mu2 - mu**2
    # clamp for numeric stability
    var = torch.clamp(var, min=0.0)
    return mu, torch.sqrt(var)

def determine_gbm_params(btc_df, deltas, c_l_v=None):
    # Calculate rolling mean and std (GBM)
    price_series = torch.tensor(btc_df['Price'].values, dtype=torch.float32, requires_grad=True)
    logret = torch.log(price_series[1:] / price_series[:-1])
    params = {}
    for d in deltas:
        for p in ps:
            if c_l_v is not None:
                a, nu = c_l_v(d, p)
            else:
                a = 0.1
                nu = 2

            mu, sigma = ema_mean_and_std(logret, a)
            params[f"mu_{d}min,{p:.3f}price"] = torch.cat([torch.tensor([0.0], dtype=mu.dtype), mu]) 
            params[f"sigma_{d}min,{p:.3f}price"] = torch.cat([torch.tensor([0.0], dtype=sigma.dtype), sigma]) 
            params[f"nu_{d}min,{p:.3f}price"] = torch.tensor(nu, dtype=torch.float32)

    return params

W_star = np.array([[2100, 100], [0, 0], [200000, 0]])
rolling_params = determine_gbm_params(btc_df, deltas, partial(calculate_l_v, W_star, 1))
print(len(rolling_params))
print(rolling_params['mu_5min,0.050price'])


# In[ ]:


def compute_and_bucket_ror_probs(btc_df, deltas, ps, ror_bin_edges, true_deltas, true_price_changes, rolling_params, training=False):
    empirical_data_buckets = np.zeros((len(deltas), len(ps), len(ror_bin_edges) - 1, 2), dtype=np.int64)

    brier_scores = []
    p_0 = torch.tensor(btc_df['Price'].values, dtype=torch.float32, requires_grad=True)
    ps = torch.tensor(ps, dtype=torch.float32, requires_grad=True)

    for d in range(len(deltas)):
        true_delta = torch.tensor(true_deltas[d, :], dtype=torch.float32, requires_grad=True)
        true_price_change = torch.tensor(true_price_changes[d, :], dtype=torch.float32, requires_grad=True)

        for p in range(len(ps)):
            print(d, p)
            mu = rolling_params[f"mu_{deltas[d]}min,{ps[p]:.3f}price"]
            sigma = rolling_params[f"sigma_{deltas[d]}min,{ps[p]:.3f}price"]
            nu = rolling_params[f"nu_{deltas[d]}min,{ps[p]:.3f}price"]

            ror_arr = compute_ror_with_gbm(p_f = (1 + ps[p].item()) * p_0, delta = true_delta, mu = mu, sigma = sigma, p_0 = p_0, nu = nu, testing=True)
            print(f"ror_arr: {ror_arr}")

            valid_mask = ~torch.isnan(ror_arr)
            valid_ror = ror_arr[valid_mask]
            valid_true_price_change = true_price_change[valid_mask]

            if not training:
                bin_idx_arr = np.digitize(valid_ror, ror_bin_edges) - 1
                success_mask = (valid_true_price_change < ps[p])
                total_counts = np.bincount(bin_idx_arr, minlength=len(ror_bin_edges) - 1)
                success_counts = np.bincount(bin_idx_arr, weights=success_mask.astype(int), minlength=len(ror_bin_edges) - 1)

                empirical_data_buckets[d, p, :, 0] = success_counts
                empirical_data_buckets[d, p, :, 1] = total_counts

            binary_outcomes = (valid_true_price_change < ps[p]).to(valid_ror.dtype)
            brier_scores.append(torch.mean((valid_ror - binary_outcomes) ** 2).item())
            print(brier_scores)

        # print(f"Completed delta {d+1}/{len(deltas)}")

    if training:
        return sum(brier_scores) / len(brier_scores)
    else:
        return empirical_data_buckets

brier_error = compute_and_bucket_ror_probs(btc_df, deltas, ps, ror_bin_edges, true_deltas, true_price_changes, rolling_params, training=True)
print(brier_error)


# In[ ]:


# Determine dimensions
n_deltas, n_ps, n_bins, _ = empirical_data_buckets.shape

ror_bin_edges = np.asarray(ror_bin_edges, dtype=float)
deltas = np.asarray(deltas, dtype=float)
ps = np.asarray(ps, dtype=float)    

# Compute bin centers from edges
bin_centers = (ror_bin_edges[:-1] + ror_bin_edges[1:]) / 2.0  # shape (n_bins,)

# Create a grid of subplots: rows = n_deltas, columns = n_ps
fig, axes = plt.subplots(
    n_deltas,
    n_ps,
    figsize=(4 * n_ps, 3 * n_deltas),
    squeeze=False  # always returns 2D axes array
)

# Loop over each subplot position
for i in range(n_deltas):
    for j in range(n_ps):
        ax = axes[i, j]

        # Extract counts for this (delta, p)
        success_counts = empirical_data_buckets[i, j, :, 0]  # shape (n_bins,)
        total_counts   = empirical_data_buckets[i, j, :, 1]  # shape (n_bins,)

        # Compute probability; need at least 30 samples per bin to compute probability
        with np.errstate(divide='ignore', invalid='ignore'):
            prob = np.where(total_counts > 0, success_counts / total_counts, np.nan)
            # prob = np.divide(success_counts, total_counts)

        # Plot probability vs. bin center
        ax.scatter(bin_centers, prob, marker='o')
        ax.set_title(f"Δ={deltas[i]} min, p={ps[j]:.3f}")
        ax.set_xlim(0, bin_centers[-1])

        # Label axes on outermost plots only
        if i == n_deltas - 1:
            ax.set_xlabel("RoR bin center")
        if j == 0:
            ax.set_ylabel("Probability")

        # Set consistent y‐limits [0,1]
        ax.set_ylim(0, bin_centers[-1])

plt.tight_layout()
plt.show()


# In[ ]:


def calculate_error(btc_df, deltas, ps, ror_bin_edges, c_l_v, true_deltas, true_price_changes):
    rolling_params = determine_gbm_params(btc_df, deltas, c_l_v)
    brier = compute_and_bucket_ror_probs(btc_df, deltas, ps, ror_bin_edges, true_deltas, true_price_changes, rolling_params, training=True)
    return brier

    success_counts = empirical_data_buckets[:, :, :, 0]
    total_counts = empirical_data_buckets[:, :, :, 1]
    with np.errstate(divide='ignore', invalid='ignore'):
        empirical_probs = np.where(total_counts > 0, success_counts / total_counts, np.nan)
        # empirical_probs = np.divide(success_counts, total_counts)
    empirical_probs[total_counts == 0] = np.nan
    mse = np.nanmean(abs(empirical_probs - test_probs) ** 4)
    return mse


# In[ ]:


## Train for l and w
test_probs = np.array([[[(ror_bin_edges[i] + ror_bin_edges[i + 1]) / 2.0 for i in range(len(ror_bin_edges) - 1)] for _ in range(len(ps))] for _ in range(len(deltas))])

btc_df = load_cleaned_btc_data()
deltas, ps, ror_bin_edges = define_training_constants()
true_deltas, true_price_changes = compute_empirical_delta_price_change(btc_df, deltas)


# In[ ]:


deg = 2
W_t = torch.randn(((deg + 2) * (deg + 1) // 2, 2), dtype=torch.float32, requires_grad=True)
optimizer = optim.Adam([W_t], lr=1e-2)
loss_arr = []

num_epochs = 500
for epoch in range(num_epochs):
    optimizer.zero_grad()

    loss = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_t, deg), true_deltas, true_price_changes)
    loss.backward()

    optimizer.step()

    loss_arr.append(loss.item())
    if epoch % 1 == 0:
        print(f"Epoch {epoch:4d}  loss = {loss.item():.6f}")
        print(f"W_t = {W_t.detach().numpy()}, loss_arr = {loss_arr}")


# In[ ]:


h = 1e-2
deg = 2
W_t = np.zeros(((deg + 2) * (deg + 1) // 2, 2), float)
W_prev = None
curr_mse = float('inf')
iter = 0

# Alpha
a = 0.01
A = 100.0
a_exp = 0.50
def alpha(k):
    return a

def h_func(k):
    return h

# Adam parameters
m = np.zeros_like(W_t)
v = np.zeros_like(W_t)
beta1, beta2 = 0.9, 0.999
eps = 1e-8
mse = [float('inf')]
step_sizes = []

# Sample multiple initialization W_1 to find min starting point
for i in range(5):
    print(f"Testing W_1 iter: {i}")
    W_0 = np.random.rand((deg + 2) * (deg + 1) // 2, 2) * 10
    mse_0 = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_0, deg), test_probs)
    print(f"W_0: {W_0}, MSE: {mse_0}")
    if mse_0 < mse[0]:
        mse[0] = mse_0
        W_t = W_0

while True:
    iter += 1
    print(f"Iteration {iter}")
    print(f"W_{iter}: {W_t.tolist()}")

    # Estimate gradient with Simultaneous Perturbation Stochastic Approximation (SPSA)
    print(f"Estimating gradient")
    grad_estimates = []
    for i in range(5):
        print(f"iter: {i}")
        perturbation = np.round(np.random.rand((deg + 2) * (deg + 1) // 2, 2)) * 2 - 1
        mse_pos = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_t + h_func(iter) * perturbation, deg), test_probs, sampling_prop=0.2)
        mse_neg = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_t - h_func(iter) * perturbation, deg), test_probs, sampling_prop=0.2)
        grad_estimates.append((mse_pos - mse_neg) / ((2 * h_func(iter)) * perturbation))

    grad_t = np.array(grad_estimates).mean(axis=0)

    # update biased first & second moment estimates
    m = beta1 * m + (1 - beta1) * grad_t
    v = beta2 * v + (1 - beta2) * (grad_t ** 2)

    # bias correction
    m_hat = m / (1 - beta1 ** iter)
    v_hat = v / (1 - beta2 ** iter)

    W_prev = W_t
    # step = alpha(iter) * m_hat/(np.sqrt(v_hat) + eps)
    step = alpha(iter) * grad_t
    W_t -= step
    # W_t = np.clip(W_t, 1e-6, None)
    curr_mse = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_t, deg), test_probs)
    mse.append(curr_mse)

    # if curr_mse > mse[-2]:
    #     W_t = W_prev
    #     step /= 2
    #     W_t -= step
    #     W_t = np.clip(W_t, 1e-6, None)
    #     curr_mse = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_t, deg), test_probs)
    #     if curr_mse > mse[-2]:
    #         print("Converged")
    #         break
    #     mse[-1] = curr_mse

    print(f"alpha- {alpha(iter)}")
    print(f"Current MSE: {curr_mse}")
    print(f"Gradient: {grad_t}")
    print(f"MSE: {mse}")
    step_sizes.append(np.linalg.norm(step))
    print(f"‖step‖ = {step_sizes[-1]:.3e}")

