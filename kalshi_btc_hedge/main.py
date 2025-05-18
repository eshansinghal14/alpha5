import numpy as np
from kalshi_btc_hedge.functions import compute_l

if __name__ == "__main__":
    # Longing BTC and shorting on bitcoin-price-abovebelow (kxbtcd) to hedge
    
    # Parameters
    p0 = 105436
    ps = np.arange(103250, 106001, 250)  # [103250, 103500, ..., 106000]
    ak = np.array([0.03, 0.16, 0.16, 0.16, 0.16, 0.18, 0.05, 0.07, 0.31, 0.56, 0.97, 0.92])
    alphas = 1.0 / ak

    # Compute l and currents
    l_value, currents = compute_l(p0, ps, alphas)
    print(f"Computed l = {l_value:.6f}")
    print("Loop currents i_0...i_n =", currents)