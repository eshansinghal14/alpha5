import numpy as np
from sklearn.preprocessing import PolynomialFeatures

# def compute_l(p0, ps, alphas):
#     """
#     Computes l and loop currents i_0...i_n for the system:
#       sum_{k=0}^n i_k = 1
#       For j=1..n: (p_j - p0)/p0 * i_0 - sum_{k=1}^j i_k + sum_{k=j+1}^n alpha_k * i_k = 0
#       And: (l - p0)/p0 * i_0 + sum_{k=1}^n alpha_k * i_k = 0

#     Parameters:
#         p0 (float): Reference price p_0.
#         ps (array-like): Prices [p_1, ..., p_n].
#         alphas (array-like): Alphas [alpha_1, ..., alpha_n].

#     Returns:
#         l (float): Computed l.
#         currents (ndarray): Array of currents [i_0, i_1, ..., i_n].
#     """
#     ps = np.asarray(ps, dtype=float)
#     alphas = np.asarray(alphas, dtype=float)
#     n = len(ps)
#     size = n + 1

#     # Build full price and alpha arrays
#     p = np.concatenate(([p0], ps))
#     alpha = np.concatenate(([0.0], alphas))

#     # Set up linear system A @ i = b for i_0...i_n
#     A = np.zeros((size, size))
#     b = np.zeros(size)
#     # normalization constraint
#     A[0, :] = 1.0
#     b[0] = 1.0

#     # loop equations j = 1..n
#     for j in range(1, size):
#         A[j, 0] = (p[j] - p0) / p0
#         for k in range(1, size):
#             A[j, k] = -1.0 if k <= j else alpha[k]
#         b[j] = 0.0

#     # solve for currents
#     currents = np.linalg.solve(A, b)

#     # compute l from final equation
#     l = p0 - p0 * np.dot(alpha[1:], currents[1:]) / currents[0]
#     return l, currents

def calculate_l_v(W, deg, delta, p):
    X = [[delta / 120, abs(p)]] # Divide by 120 to norm delta because max delta is 120 mins (2 hours)
    poly = PolynomialFeatures(degree=deg, include_bias=True)
    X_kernel = poly.fit_transform(X)
    res = X_kernel @ W
    return int(res[0, 0]), res[0, 1]
