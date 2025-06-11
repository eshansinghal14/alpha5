#!/usr/bin/env python
# coding: utf-8

# In[ ]:


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


warnings.filterwarnings(
    "ignore",
    category=pd.errors.PerformanceWarning
)


# In[104]:


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


# In[105]:


def define_training_constants():
    # Constants for time ranges, percentage price changes, and risk of ruin bin edges
    # deltas = [10, 30, 60, 120, 300] # in minutes
    # ps = [-0.005, -0.01, -0.02, -0.05] # percentage changes (-0.05, -0.1 seem rly rly small)
    deltas = [5, 25, 50, 100, 120] # in minutes
    ps = [-0.005, -0.01, -0.015, -0.02, -0.025] # percentage changes (-0.05, -0.1 seem rly rly small)
    ror_bin_edges = [0.0, 0.005, 0.01, 0.02, 0.03, 0.04, 0.05] + list(np.arange(0.1, 1.01, 0.05))  # probability of ror
    return deltas, ps, ror_bin_edges

deltas, ps, ror_bin_edges = define_training_constants()


# In[ ]:


calculate_l_v(np.array([[1 for _ in range(2)] for _ in range(6)]), 2, 1, 2)


# In[ ]:


def determine_gbm_params(btc_df, deltas, c_l_v=None):
    # Calculate rolling mean and std (GBM)
    btc_df['log_returns'] = np.log(btc_df['Price'] / btc_df['Price'].shift(1))
    # new_cols = {}
    for d in deltas:
        for p in ps:
            # l_eq takes in d, p and outputs rolling window length, v_eq takes in and outputs df
            if c_l_v is not None:
                l, v = c_l_v(d, p)
            else:
                l = d
                v = 1

            rolling_mean = btc_df['log_returns'].rolling(f"{l}min").mean()
            rolling_std = btc_df['log_returns'].rolling(f"{l}min").std()
            btc_df[f"mu_{d}min,{p}price"] = rolling_mean + 0.5 * rolling_std ** 2
            btc_df[f"sigma_{d}min,{p}price"]  = rolling_std
            btc_df[f"kurt_{d}min,{p}price"] = v

    btc_df = btc_df.dropna()
    return btc_df

W_star = np.array([[0.9335578548910446, 0.22748139942318604], [0.9108372996057686, 0.4630051795860236], [0.6785285464371982, 0.8211117174744395], [0.5351057488216742, 0.2597588578134788], [0.649160619231901, 0.16839060975157696], [0.13189063397155243, 0.17128778544168569], [0.27383109598201233, 0.6423312331104211], [0.6598464623059107, 0.9609032021234301], [0.10142513169730116, 0.35585813341501726], [0.8649687047058844, 0.35351778591859834]])
btc_df = determine_gbm_params(btc_df, deltas, partial(calculate_l_v, W_star, 3))


# In[141]:


def compute_and_bucket_ror_probs(btc_df, deltas, ps, ror_bin_edges, sampling_prop=1):
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

    empirical_data_buckets = np.zeros((len(deltas), len(ps), len(ror_bin_edges) - 1, 2), dtype=np.int64)

    batch_indices = np.random.choice(len(btc_df), size=int(sampling_prop * len(btc_df)), replace=False)
    batch_mask = np.zeros(len(btc_df), dtype=bool)
    batch_mask[batch_indices] = True

    for d in range(len(deltas)):
        delta = true_deltas[d, :][batch_mask].ravel()
        true_price_change = true_price_changes[d, :][batch_mask].ravel()
        p_0 = btc_df['Price'].to_numpy()[batch_mask].ravel()

        for p in range(len(ps)):
            mu = btc_df[f"mu_{deltas[d]}min,{ps[p]}price"].to_numpy()[batch_mask].ravel()
            sigma = btc_df[f"sigma_{deltas[d]}min,{ps[p]}price"].to_numpy()[batch_mask].ravel()
            nu_arr = btc_df[f"kurt_{deltas[d]}min,{ps[p]}price"].to_numpy()[batch_mask].ravel()

            ror_arr = compute_ror_with_gbm(p_f = (1 + ps[p]) * p_0, delta = delta, mu = mu, sigma = sigma, p_0 = p_0, nu_arr= nu_arr, testing=True)

            valid_mask = ~np.isnan(ror_arr)
            valid_ror = ror_arr[valid_mask]
            valid_true_price_change = true_price_change[valid_mask]

            bin_idx_arr = np.digitize(valid_ror, ror_bin_edges) - 1
            success_mask = (valid_true_price_change < ps[p])
            total_counts = np.bincount(bin_idx_arr, minlength=len(ror_bin_edges) - 1)
            success_counts = np.bincount(bin_idx_arr, weights=success_mask.astype(int), minlength=len(ror_bin_edges) - 1)

            empirical_data_buckets[d, p, :, 0] = success_counts
            empirical_data_buckets[d, p, :, 1] = total_counts

        # print(f"Completed delta {d+1}/{len(deltas)}")

    return empirical_data_buckets

empirical_data_buckets = compute_and_bucket_ror_probs(btc_df, deltas, ps, ror_bin_edges, sampling_prop=1)


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
            prob = np.where(total_counts > 200, success_counts / total_counts, np.nan)
            # prob = np.divide(success_counts, total_counts)

        # Plot probability vs. bin center
        ax.scatter(bin_centers, prob, marker='o')
        ax.set_title(f"Δ={deltas[i]} min, p={ps[j]:.3f}")
        ax.set_xlim(0, 1)

        # Label axes on outermost plots only
        if i == n_deltas - 1:
            ax.set_xlabel("RoR bin center")
        if j == 0:
            ax.set_ylabel("Probability")

        # Set consistent y‐limits [0,1]
        ax.set_ylim(0, 1)

plt.tight_layout()
plt.show()


# In[110]:


rows = [['Delta', 'Price Change'] + ror_bin_edges]
for d_idx in range(len(deltas)):
    for p_idx in range(len(ps)):
        rows.append([deltas[d_idx], ps[p_idx]] + empirical_data_buckets[d_idx, p_idx, :, 0].tolist())
        rows.append(['', ''] + empirical_data_buckets[d_idx, p_idx, :, 1].tolist())

df = pd.DataFrame(rows)
df.to_csv("results/compute_ror_with_gbm.csv", index=False)
print(df.head())


# In[132]:


def calculate_error(btc_df, deltas, ps, ror_bin_edges, c_l_v, test_probs, sampling_prop=1):
    btc_df = determine_gbm_params(btc_df, deltas, c_l_v)
    empirical_data_buckets = compute_and_bucket_ror_probs(btc_df, deltas, ps, ror_bin_edges, sampling_prop)

    success_counts = empirical_data_buckets[:, :, :, 0]
    total_counts = empirical_data_buckets[:, :, :, 1]
    with np.errstate(divide='ignore', invalid='ignore'):
        empirical_probs = np.where(total_counts > 200, success_counts / total_counts, np.nan)
        # empirical_probs = np.divide(success_counts, total_counts)
    empirical_probs[total_counts == 0] = np.nan
    mse = np.nanmean(abs(empirical_probs - test_probs) ** 3)
    return mse


# In[144]:


## Train for l and w
test_probs = np.array([[[(ror_bin_edges[i] + ror_bin_edges[i + 1]) / 2.0 for i in range(len(ror_bin_edges) - 1)] for _ in range(len(ps))] for _ in range(len(deltas))])

btc_df = load_cleaned_btc_data()
deltas, ps, ror_bin_edges = define_training_constants()


# In[ ]:


h = 1e-3
deg = 3
W_t = np.zeros(((deg + 2) * (deg + 1) // 2, 2), float)
W_prev = None
# W_t = np.array([[837.8272249917405, 0.001], [928.444561394567, 0.001], [78.76362407101385, 0.001], [145.02831502173672, 0.001], [0.001, 1000.0], [1000.0, 0.001]])
W_t = np.array([[0.9845246592673185, 0.33350231400253083], [0.015056183383086563, 0.4966687790283799], [0.10257691702016936, 0.8379186812244079], [0.6179161353810881, 0.257093039156744], [0.16023751939914188, 0.7260624044824605], [0.6472570987946598, 0.032059851567643904], [0.3152508628565547, 0.1550884916632449], [0.5361474849166508, 0.0319343400658717], [0.7765698010711551, 0.27009274393640015], [0.35504970517482115, 0.8353512552287403]])
curr_mse = float('inf')
iter = 0

# Alpha
a = 1e-1
A = 10.0
a_exp = 0.50
def alpha(k):
    return a / (A + k) ** a_exp

def h_func(k):
    return h / (A + k) ** a_exp

# Adam parameters
m = np.zeros_like(W_t)
v = np.zeros_like(W_t)
beta1, beta2 = 0.9, 0.999
eps = 1e-8
mse = [float('inf')]
step_sizes = []

# Sample multiple initialization W_1 to find min starting point
# for i in range(5):
#     print(f"Testing W_1 iter: {i}")
#     W_0 = np.random.rand((deg + 2) * (deg + 1) // 2, 2)
#     mse_0 = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_0, deg), test_probs)
#     print(f"W_0: {W_0}, MSE: {mse_0}")
#     if mse_0 < mse[0]:
#         mse[0] = mse_0
#         W_t = W_0

while True:
    iter += 1
    print(f"Iteration {iter}")
    print(f"W_{iter}: {W_t.tolist()}")

    # Estimate gradient with Simultaneous Perturbation Stochastic Approximation (SPSA)
    print(f"Estimating gradient")
    grad_estimates = []
    for i in range(1):
        perturbation = np.round(np.random.rand((deg + 2) * (deg + 1) // 2, 2)) * 2 - 1
        mse_pos = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_t + h_func(iter) * perturbation, deg), test_probs, sampling_prop=1)
        mse_neg = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_t - h_func(iter) * perturbation, deg), test_probs, sampling_prop=1)
        grad_estimates.append((mse_pos - mse_neg) / ((2 * h_func(iter)) * perturbation))

    grad_t = np.array(grad_estimates).mean(axis=0)

    # update biased first & second moment estimates
    m = beta1 * m + (1 - beta1) * grad_t
    v = beta2 * v + (1 - beta2) * (grad_t ** 2)

    # bias correction
    m_hat = m / (1 - beta1 ** iter)
    v_hat = v / (1 - beta2 ** iter)

    W_prev = W_t
    step = alpha(iter) * m_hat/(np.sqrt(v_hat) + eps)
    W_t -= step
    # W_t = np.clip(W_t, 1e-6, None)
    curr_mse = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_t, deg), test_probs)
    mse.append(curr_mse)

    if curr_mse > mse[-2]:
        W_t = W_prev
        step /= 2
        W_t -= step
        W_t = np.clip(W_t, 1e-6, None)
        curr_mse = calculate_error(btc_df, deltas, ps, ror_bin_edges, partial(calculate_l_v, W_t, deg), test_probs)
        if curr_mse > mse[-2]:
            print("Converged")
            break
        mse[-1] = curr_mse

    print(f"Hyperparamters: alpha- {alpha(iter)}, m_hat- {m_hat}, v_hat- {v_hat}")
    print(f"Current MSE: {curr_mse}")
    print(f"Gradient: {grad_t}")
    print(f"MSE: {mse}")
    step_sizes.append(np.linalg.norm(step))
    print(f"‖step‖ = {step_sizes[-1]:.3e}")


# In[121]:


def mse_vec(p):
    W = p.reshape(W_t.shape)
    return calculate_error(
        btc_df, deltas, ps, ror_bin_edges,
        partial(calculate_l_v, W, deg),
        test_probs,
        sampling_prop=1.0
    )

log = []
def cb(xk):
    fval = mse_vec(xk)
    log.append((xk.copy(), fval))
    print(f"Iter {len(log):3d}: MSE = {fval:.6f}")

res = minimize(
    mse_vec,
    x0=W_t.flatten(),
    method='L-BFGS-B',
    bounds=[(1e-6, None)] * W_t.size,
    options={
        "maxiter": 20,
        "ftol": 1e-4,
        "disp": True,      # turn on final convergence message
        "iprint": 1        # >0: print per major iteration; 2: per line search
    },
)
W_t = res.x.reshape(W_t.shape)
print("Final MSE:", res.fun)

