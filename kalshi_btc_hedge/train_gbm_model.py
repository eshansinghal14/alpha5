from compute_ror_prob import compute_ror_with_gbm
from functions import calculate_l_v
import pandas as pd
import numpy as np
from functools import partial
import torch
import torch.optim as optim
from sklearn.metrics import roc_auc_score, roc_curve
import matplotlib.pyplot as plt
from torchmetrics import AUROC


def load_cleaned_btc_data(training: bool = True):
    btc_df = pd.read_csv("data/coinbaseUSD_1-min_data.csv")

    # Filter data from 01-01-2017 (training) or 2025-01-01 (not training) to latest data and drop NaNs
    btc_df['Date'] = pd.to_datetime(btc_df['Date'], utc=True)
    btc_df = btc_df.set_index("Date").sort_index()
    if training:
        start_date = pd.to_datetime("2017-01-01", utc=True)
    else:
        start_date = pd.to_datetime("2013-01-01", utc=True)
        end_date = pd.to_datetime("2017-01-01", utc=True)
    btc_df = btc_df[btc_df.index >= start_date]
    btc_df = btc_df[btc_df.index < end_date]
    btc_df = btc_df.dropna()

    btc_df['Price'] = (btc_df['High'] + btc_df['Low']) / 2.0

    print('Training: ', training)
    return btc_df

def define_training_constants():
    deltas = [5, 15, 30, 45, 60] # in minutes 
    ps = {
        5: [-0.02, -0.01, 0.01, 0.02],
        15: [-0.04, -0.03, 0.03, 0.04],
        30: [-0.05, -0.04, 0.04, 0.05],
        45: [-0.06, -0.05, 0.05, 0.06],
        60: [-0.07, -0.06, 0.06, 0.07]
    }
    # ror_bin_edges = [0.0, 0.005, 0.01, 0.02, 0.03, 0.04, 0.05] + list(np.arange(0.1, 1.01, 0.05))  # probability of ror
    ror_bin_edges = list(np.arange(0.0, 0.03, 0.002)) + [1.0]
    return deltas, ps, ror_bin_edges

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
    
    true_deltas = torch.tensor(true_deltas, dtype=torch.float32, requires_grad=False)
    true_price_changes = torch.tensor(true_price_changes, dtype=torch.float32, requires_grad=False)

    return true_deltas, true_price_changes

def ema_vectorized(x: torch.Tensor, alpha: torch.Tensor, block_size: int = 500):
    a   = 1.0 - alpha
    mu0 = 0.0
    out = []
    for i in range(0, x.size(0), block_size):
        block = x[i : i + block_size]
        T     = block.size(0)
        idx   = torch.arange(T, device=x.device, dtype=x.dtype)
        a_pow = a ** idx
        y     = alpha * block / a_pow
        csum  = torch.cumsum(y, dim=0)
        mu_b  = a_pow * csum + a_pow * mu0
        mu0   = mu_b[-1]
        out.append(mu_b)
        # if i % 100000 == 0:
        #     print(f"Processed {i} rows of {x.size(0)}")
    return torch.cat(out, dim=0)

def ema_mean_and_std(x: torch.Tensor, alpha: torch.Tensor):
    # 1) EMA of x
    mu = ema_vectorized(x, alpha)
    # 2) EMA of x^2
    mu2 = ema_vectorized(x*x, alpha)
    # variance = E[x^2] - (E[x])^2
    var = mu2 - mu**2
    # clamp for numeric stability
    var = torch.clamp(var, min=1e-6)
    return mu, torch.sqrt(var)

def plot(empirical_data_buckets, ror_bin_edges, deltas, ps):
    # Determine dimensions
    n_deltas = len(deltas)
    n_ps_per_delta = [len(ps[d]) for d in deltas]
    n_bins = empirical_data_buckets.shape[2]

    ror_bin_edges = np.asarray(ror_bin_edges, dtype=float)
    deltas = np.asarray(deltas, dtype=float)

    # Compute bin centers from edges
    bin_centers = (ror_bin_edges[:-1] + ror_bin_edges[1:]) / 2.0  # shape (n_bins,)

    # Create a grid of subplots: rows = n_deltas, columns = max n_ps for any delta
    max_n_ps = max(n_ps_per_delta)
    fig, axes = plt.subplots(
        n_deltas,
        max_n_ps,
        figsize=(4 * max_n_ps, 3 * n_deltas),
        squeeze=False  # always returns 2D axes array
    )

    bucket_idx = 0
    for i, delta in enumerate(deltas):
        ps_d = ps[delta]
        for j, p_val in enumerate(ps_d):
            ax = axes[i, j]

            # Extract counts for this (delta, p)
            success_counts = empirical_data_buckets[i, j, :, 0]  # shape (n_bins,)
            total_counts   = empirical_data_buckets[i, j, :, 1]  # shape (n_bins,)

            # Compute probability
            with np.errstate(divide='ignore', invalid='ignore'):
                prob = np.where(total_counts > 0, success_counts / total_counts, np.nan)

            # Plot probability vs. bin center
            ax.scatter(bin_centers, prob, marker='o')
            ax.set_title(f"Δ={delta} min, p={p_val:.3f}")
            ax.set_xlim(0, ror_bin_edges[-2])

            # Label axes on outermost plots only
            if i == n_deltas - 1:
                ax.set_xlabel("RoR bin center")
            if j == 0:
                ax.set_ylabel("Probability")

            # Set consistent y‐limits [0,1]
            ax.set_ylim(0, ror_bin_edges[-2])
        # Hide unused subplots in this row
        for j in range(len(ps_d), max_n_ps):
            axes[i, j].axis('off')

    plt.tight_layout()
    plt.savefig("results.png")

def compute_and_bucket_ror_probs(p_0, logret, deltas, ps, true_deltas, true_price_changes, c_l_v, sampling_prop=1.0):
    loss_sum = torch.tensor(0.0, dtype=torch.float32)
    loss_count = 0

    auroc = AUROC(task='binary')

    # empirical_data_buckets = np.zeros((len(deltas), sum(len(ps[d]) for d in deltas), len(ror_bin_edges) - 1, 2), dtype=np.int64)

    a = None
    nu = None

    start = np.random.randint(0, len(p_0) - int(sampling_prop * len(p_0)) + 1)
    end   = start + int(sampling_prop * len(p_0))
    batch_mask = np.zeros(len(p_0), dtype=bool)
    batch_mask[start:end] = True
    batch_mask = torch.from_numpy(batch_mask)

    p_0 = p_0[batch_mask]
    logret = logret[batch_mask]

    for d in range(len(deltas)):
        true_delta = true_deltas[d][batch_mask]
        true_price_change = true_price_changes[d][batch_mask]
        ps_d = ps[deltas[d]]

        b = 0
        for p in range(len(ps_d)):
            a, nu = c_l_v(d, p)
            print(f"a: {a}, nu: {nu}")
            mu, sigma = ema_mean_and_std(logret, a)
            sigma = torch.clamp(sigma, min=1e-6)

            assert torch.all(torch.isfinite(mu)), "Non-finite mu"
            assert torch.all(torch.isfinite(sigma)), "Non-finite sigma"
            assert torch.all(torch.isfinite(p_0)), "Non-finite p_0"

            ror_arr = compute_ror_with_gbm(p_f = (1 + ps_d[p]) * p_0, delta = true_delta, mu = mu, sigma = sigma, p_0 = p_0, nu = nu, testing=True)

            valid_mask = ~torch.isnan(ror_arr)
            valid_ror = ror_arr[valid_mask]
            valid_true_price_change = true_price_change[valid_mask]

            binary_outcomes = (valid_true_price_change < ps_d[p]).to(valid_ror.dtype)
            loss_sum += torch.nn.functional.binary_cross_entropy_with_logits(valid_ror, binary_outcomes, reduction='mean')
            loss_count += 1

            print("valid ror:", valid_ror)
            print("sigmoid valid ror:", torch.sigmoid(valid_ror))
            print("Positive rate:", binary_outcomes.mean().item())
            
            if ps_d[p] < 0:
                auroc.update(torch.sigmoid(valid_ror).cpu(), binary_outcomes.cpu())
            else:
                auroc.update(1 - torch.sigmoid(valid_ror).cpu(), abs(1 - binary_outcomes.cpu()))

            # num_bins = len(ror_bin_edges) - 1
            # bin_idx_arr = np.digitize(valid_ror.detach().numpy(), ror_bin_edges) - 1
            # # Clip indices to valid range
            # bin_idx_arr = np.clip(bin_idx_arr, 0, num_bins - 1)
            # success_mask = (valid_true_price_change.detach().numpy() < ps_d[p])
            # total_counts = np.bincount(bin_idx_arr, minlength=num_bins)
            # success_counts = np.bincount(bin_idx_arr, weights=success_mask.astype(int), minlength=num_bins)
            # empirical_data_buckets[d, b, :, 0] = success_counts
            # empirical_data_buckets[d, b, :, 1] = total_counts
            # print(empirical_data_buckets)
            # b += 1  

        # print(f"Completed delta {d+1}/{len(deltas)}")

    print('calculated auc roc')
    auc = auroc.compute().item()
    print(f"AUC-ROC = {auc:.4f}")

    # plot(empirical_data_buckets, ror_bin_edges, deltas, ps)

    return loss_sum / loss_count if loss_count > 0 else torch.tensor(0.0)


def calculate_error(p_0, logret_norm, deltas, ps, c_l_v, true_deltas, true_price_changes):
    brier = compute_and_bucket_ror_probs(p_0, logret_norm, deltas, ps, true_deltas, true_price_changes, c_l_v, sampling_prop=1.0)
    return brier

if __name__ == "__main__":
    print('starting')
    btc_df = load_cleaned_btc_data(False)
    deltas, ps, ror_bin_edges = define_training_constants()
    true_deltas, true_price_changes = compute_empirical_delta_price_change(btc_df, deltas)

    # thresholds = list(np.arange(-0.07, 0.08, 0.01))
    # for thr in thresholds:
    #     print(f"Calculating for threshold: {thr:.2f}")
    #     for i, delta in enumerate(deltas):
    #         changes = true_price_changes[i]            # shape [N]
    #         if thr < 0:
    #             mask = changes <= thr                     # dtype bool
    #         else:
    #             mask = changes >= thr                     # dtype bool
    #         freq = float(mask.float().mean().item() * 100.0)
    #         print(f"{delta:>3d}-min horizon: {freq:5.2f}% of times ΔPrice ≥ {thr*100:.1f}%")

    print("calculated constants")
    
    deg = 3
    W_t = torch.randn(((deg + 2) * (deg + 1) // 2, 2), dtype=torch.float32, requires_grad=True)
    W_t = torch.tensor([[-1.2204472, 1.9125451],
 [-1.7227014, 0.92008674],
 [-0.95544356, 0.16302508],
 [-1.4011312, 0.18511115],
 [-0.67936695, 1.2698789],
 [-0.35657087, -0.7216567],
 [-0.36971992, 2.1376672],
 [-0.9033503, 0.48063892],
 [0.64475167, 0.23095784],
 [0.06801502, 1.5565889]], dtype=torch.float32, requires_grad=False)
    # init_data = torch.tensor([[-2.549068, 1.8759766 ],
    # [-1.4085447, 3.4859176 ],
    # [-1.8611243, 0.8071274 ],
    # [-0.6276222, 5.7739005 ],
    # [ 0.09339391, 3.3934176 ],
    # [-2.07149, 3.3466191 ]], dtype=torch.float32)
    # with torch.no_grad():
    #     W_t[:init_data.shape[0]] = init_data
    # W_t.requires_grad_()
    print(W_t)
    optimizer = optim.Adam([W_t], lr=1e-2)
    loss_arr = []

    p_0 = torch.tensor(btc_df['Price'].values, dtype=torch.float32, requires_grad=False)
    times = btc_df['Unix Timestamp'].values
    logret = torch.log(p_0[1:] / p_0[:-1])
    times_diff = torch.tensor((times[1:] - times[:-1]) / 60.0, dtype=torch.float32, requires_grad=False)  # convert to minutes
    logret_norm = torch.cat([torch.tensor([0.0]), logret / times_diff])

    num_epochs = 5000
    accum_steps = 4

    # torch.autograd.set_detect_anomaly(True)
    for epoch in range(num_epochs):
        optimizer.zero_grad()

        loss = calculate_error(p_0, logret_norm, deltas, ps, partial(calculate_l_v, W_t, deg), true_deltas, true_price_changes)
        (loss / accum_steps).backward()

        if torch.isnan(W_t.grad).any():
            print("NaN detected in W_t.grad at epoch", epoch)
            print("W_t:", W_t)
            break

        if (epoch + 1) % accum_steps == 0:
            print(f"After {epoch+1} micro-batches:")
            loss_arr.append(loss.item())
            # print("W_t.grad norm:", torch.norm(W_t.grad).item())

            # perform the optimizer step
            optimizer.step()
            # zero out for the next accumulation cycle
            optimizer.zero_grad()

            print(f"W_t = {W_t.detach().numpy()}, loss_arr = {loss_arr}")

        if epoch % 1 == 0:
            print(f"Epoch {epoch:4d}  loss = {loss.item():.6f}")

