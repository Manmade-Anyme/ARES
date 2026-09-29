"""Calibration metrics and one-sided tier significance tests."""
from dataclasses import asdict, dataclass
from typing import List

import numpy as np
import pandas as pd
from scipy.stats import fisher_exact, mannwhitneyu


@dataclass
class BrierDecomposition:
    brier_score: float
    reliability: float
    resolution: float
    uncertainty: float
    base_rate: float
    within_bin_variance: float = 0.0
    within_bin_covariance: float = 0.0


@dataclass
class ReliabilityCurve:
    bin_centers: List[float]
    bin_accuracies: List[float]
    bin_confidences: List[float]
    bin_counts: List[int]
    ece: float
    mce: float


@dataclass
class TierSignificanceResult:
    high_count: int
    medium_count: int
    high_win_rate: float
    medium_win_rate: float
    high_expectancy: float
    medium_expectancy: float
    win_rate_diff: float
    expectancy_diff: float
    fisher_p_value: float
    mann_whitney_p_value: float
    is_statistically_superior: bool
    verdict: str

    def to_dict(self):
        return asdict(self)


def _validated_arrays(y_true, y_prob):
    actual = np.asarray(y_true, dtype=float).reshape(-1)
    probability = np.asarray(y_prob, dtype=float).reshape(-1)
    if actual.size == 0 or actual.size != probability.size:
        raise ValueError("y_true and y_prob must have the same non-zero length")
    if not np.isfinite(actual).all() or not np.isfinite(probability).all():
        raise ValueError("y_true and y_prob must contain only finite values")
    if not np.isin(actual, [0.0, 1.0]).all():
        raise ValueError("y_true values must be binary (0 or 1)")
    if ((probability < 0.0) | (probability > 1.0)).any():
        raise ValueError("y_prob values must be in [0, 1]")
    return actual, probability


def _bin_indices(probability: np.ndarray, n_bins: int, strategy: str) -> np.ndarray:
    if n_bins < 1:
        raise ValueError("n_bins must be at least 1")
    if strategy == "uniform":
        edges = np.linspace(0.0, 1.0, n_bins + 1)
    elif strategy == "quantile":
        edges = np.unique(np.quantile(probability, np.linspace(0.0, 1.0, n_bins + 1)))
        if len(edges) < 2:  # pragma: no cover
            return np.zeros(len(probability), dtype=int)
        edges[0], edges[-1] = 0.0, 1.0  # pragma: no cover
    else:
        raise ValueError("strategy must be 'uniform' or 'quantile'")
    return np.clip(np.digitize(probability, edges[1:-1], right=False), 0, len(edges) - 2)


def _bin_stats(actual, probability, n_bins, strategy):
    indices = _bin_indices(probability, n_bins, strategy)
    bins = []
    for index in np.unique(indices):
        mask = indices == index
        bins.append((actual[mask], probability[mask]))
    return bins


def compute_brier_decomposition(y_true, y_prob, n_bins=10, strategy="uniform"):
    """Decompose raw Brier score with exact within-bin variance/covariance correction."""
    actual, probability = _validated_arrays(y_true, y_prob)
    base_rate = float(actual.mean())
    reliability = resolution = within_bin_variance = within_bin_covariance = 0.0
    for bin_actual, bin_probability in _bin_stats(actual, probability, n_bins, strategy):
        weight = len(bin_actual) / len(actual)
        observed = float(bin_actual.mean())
        predicted = float(bin_probability.mean())
        reliability += weight * (predicted - observed) ** 2
        resolution += weight * (observed - base_rate) ** 2
        within_bin_variance += weight * float(np.var(bin_probability))
        within_bin_covariance += weight * float(np.mean(
            (bin_probability - predicted) * (bin_actual - observed)
        ))
    uncertainty = base_rate * (1.0 - base_rate)
    brier_score = float(np.mean((probability - actual) ** 2))
    return BrierDecomposition(
        brier_score=brier_score,
        reliability=float(reliability + within_bin_variance - 2 * within_bin_covariance),
        resolution=float(resolution),
        uncertainty=float(uncertainty),
        base_rate=base_rate,
        within_bin_variance=float(within_bin_variance),
        within_bin_covariance=float(within_bin_covariance),
    )


def compute_calibration_curve(y_true, y_prob, n_bins=10, strategy="uniform"):
    """Return non-empty reliability-curve bins, ECE, and MCE."""
    actual, probability = _validated_arrays(y_true, y_prob)
    indices = _bin_indices(probability, n_bins, strategy)
    centers, accuracies, confidences, counts, gaps = [], [], [], [], []
    for index in np.unique(indices):
        mask = indices == index
        confidence = float(probability[mask].mean())
        accuracy = float(actual[mask].mean())
        count = int(mask.sum())
        centers.append(confidence)
        accuracies.append(accuracy)
        confidences.append(confidence)
        counts.append(count)
        gaps.append(abs(accuracy - confidence))
    ece = float(sum(n * gap for n, gap in zip(counts, gaps)) / len(actual))
    return ReliabilityCurve(centers, accuracies, confidences, counts, ece, float(max(gaps)))


def test_tier_significance(
    df: pd.DataFrame,
    tier_col: str = "confidence",
    outcome_col: str = "win",
    pnl_col: str = "pnl_points",
    alpha: float = 0.05,
    min_samples: int = 30,
) -> TierSignificanceResult:
    """One-sided HIGH>MEDIUM Fisher and Mann-Whitney tests, fail-closed when small."""
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    missing = {tier_col, outcome_col, pnl_col} - set(df.columns)
    if missing:
        raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")
    clean = df.loc[df[tier_col].astype(str).str.upper().isin(["HIGH", "MEDIUM"]),
                   [tier_col, outcome_col, pnl_col]].copy()
    clean[outcome_col] = pd.to_numeric(clean[outcome_col], errors="coerce")
    clean[pnl_col] = pd.to_numeric(clean[pnl_col], errors="coerce")
    clean = clean.dropna(subset=[outcome_col, pnl_col])
    clean = clean[clean[outcome_col].isin([0, 1])]
    high = clean[clean[tier_col].astype(str).str.upper() == "HIGH"]
    medium = clean[clean[tier_col].astype(str).str.upper() == "MEDIUM"]
    high_n, med_n = len(high), len(medium)
    high_win = float(high[outcome_col].mean()) if high_n else 0.0
    med_win = float(medium[outcome_col].mean()) if med_n else 0.0
    high_pnl = float(high[pnl_col].mean()) if high_n else 0.0
    med_pnl = float(medium[pnl_col].mean()) if med_n else 0.0
    enough = high_n >= min_samples and med_n >= min_samples
    if enough:
        table = [[int(high[outcome_col].sum()), high_n - int(high[outcome_col].sum())],
                 [int(medium[outcome_col].sum()), med_n - int(medium[outcome_col].sum())]]
        fisher_p = float(fisher_exact(table, alternative="greater").pvalue)
        mw_p = float(mannwhitneyu(high[pnl_col], medium[pnl_col], alternative="greater").pvalue)
    else:
        fisher_p = mw_p = 1.0
    win_diff, pnl_diff = high_win - med_win, high_pnl - med_pnl
    superior = enough and win_diff > 0 and pnl_diff > 0 and fisher_p < alpha and mw_p < alpha
    verdict = "SEPARATED" if superior else (
        "INVERTED" if win_diff < 0 or pnl_diff < 0 else "INSIGNIFICANT"
    )
    return TierSignificanceResult(
        high_n, med_n, high_win, med_win, high_pnl, med_pnl,
        win_diff, pnl_diff, fisher_p, mw_p, superior, verdict,
    )
