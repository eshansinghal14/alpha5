import numpy as np
from sklearn.preprocessing import PolynomialFeatures
import torch
import matplotlib.pyplot as plt
from compute_ror_prob import compute_floor_price, compute_ror_with_gbm

def calculate_l_v(W, deg, delta, p):
    # Build your raw inputs as a 1-d tensor x0 of length 2
    x0 = torch.tensor([float(delta) / 60.0, float(abs(p))], dtype=torch.float32)

    # Manually compute polynomial features up to degree deg:
    feats = [torch.tensor(1.0)]  # bias term, a 0-d tensor
    for d in range(1, deg + 1):
        for i in range(d + 1):
            feats.append(x0[0]**(d - i) * x0[1]**i)  # also 0-d tensors

    # Now stacking a list of 0-d tensors gives a 1-d feature vector
    X_torch = torch.stack(feats)  # shape [num_features]

    # Linear map: features @ weights → 2 outputs (raw α, raw ν)
    out = X_torch @ W             # shape [2], requires_grad=True

    # Squash into (0, α_max) and (0, ∞)
    a_min = 1.0 / 3000
    a_max = 0.1
    alpha = a_min + (a_max - a_min) * torch.sigmoid(out[0])
    nu    = torch.nn.functional.softplus(out[1])

    return alpha, nu

def plot_loss(loss):
    plt.figure(figsize=(10, 5))
    plt.plot(loss, label='Loss')
    plt.xlabel('Epochs')
    plt.ylabel('Loss Value')
    plt.title('Training Loss Over Epochs')
    plt.legend()
    plt.grid()
    plt.show()

def ema_mu_sigma(prices: np.ndarray, a: float, eps: float = 1e-6):
    one_m_a = 1.0 - a

    r = np.empty_like(prices, dtype=float)
    r[0] = 0.0
    r[1:] = np.log(prices[1:] / prices[:-1])

    mu = 0.0
    nu = 0.0
    for rt in r:
        mu = a * rt      + one_m_a * mu
        nu = a * rt*rt   + one_m_a * nu

    var   = nu - mu*mu
    var   = max(var, eps)
    sigma = np.sqrt(var)

    return mu, sigma

def p_f_prob_em(btc_df, W_star, deg, delta_min, p_0, prob):
    # Simple loop to determine gbm params and ror_price
    i = 0
    p_f = p_0
    a, nu = None, None
    while True:
        new_a, new_nu = calculate_l_v(W_star, deg, delta_min, (p_f - p_0) / p_0)
        btc_win = btc_df.tail(int(2 / new_a - 1))
        mu, sigma = ema_mu_sigma(btc_win['Price'].values, new_a)
        new_p_f = compute_floor_price(delta_min, mu, sigma, p_0, new_nu, prob)
        if i < 5:
            if a is not None and abs(a - new_a) < 1e-3 and abs(nu - new_nu) < 1e-2 and abs(p_f - new_p_f) < 2:
                return p_f
            a = new_a
            nu = new_nu
            p_f = new_p_f
        else:
            a = (new_a + a) // 2
            nu = (new_nu + nu) // 2
            p_f = (new_p_f + p_f) // 2
        i += 1