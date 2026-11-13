import numpy as np
import pandas as pd
import pytest
from src.analytics.range_detector_engine import calc_rma, calc_atr

def pine_reference_rma(series: pd.Series, length: int) -> pd.Series:
    vals = series.to_numpy(dtype=float)
    rma = np.empty_like(vals)
    rma[:] = np.nan
    if len(vals) < length:
        return pd.Series(rma, index=series.index)
    first_sma = np.mean(vals[:length])
    rma[length - 1] = first_sma
    for i in range(length, len(vals)):
        rma[i] = (rma[i - 1] * (length - 1) + vals[i]) / length
    return pd.Series(rma, index=series.index)

def test_atr_seeding_exact_pine_parity():
    """TEST 1: Compare against reference RMA written from Pine definition on 2 stored symbols. Max diff < 0.0001."""
    # Synthetic Symbol A: trending volatile series
    np.random.seed(42)
    p_a = 500.0 + np.cumsum(np.random.randn(250) * 2.5)
    high_a = p_a + np.random.uniform(1.0, 5.0, 250)
    low_a = p_a - np.random.uniform(1.0, 5.0, 250)
    close_a = p_a + np.random.uniform(-1.0, 1.0, 250)
    df_a = pd.DataFrame({"High": high_a, "Low": low_a, "Close": close_a})

    # Synthetic Symbol B: rangebound series
    np.random.seed(101)
    p_b = 1200.0 + np.sin(np.linspace(0, 20, 250)) * 20.0
    high_b = p_b + np.random.uniform(2.0, 8.0, 250)
    low_b = p_b - np.random.uniform(2.0, 8.0, 250)
    close_b = p_b + np.random.uniform(-2.0, 2.0, 250)
    df_b = pd.DataFrame({"High": high_b, "Low": low_b, "Close": close_b})

    for length in [14, 50, 100]:
        for df, sym_name in [(df_a, "SYM_A"), (df_b, "SYM_B")]:
            engine_atr = calc_atr(df, length=length)
            
            # Compute reference TR and Pine RMA
            tr1 = df["High"] - df["Low"]
            tr2 = (df["High"] - df["Close"].shift(1)).abs()
            tr3 = (df["Low"] - df["Close"].shift(1)).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            ref_atr = pine_reference_rma(tr, length)

            # Compare valid non-nan slice
            valid_mask = ~ref_atr.isna()
            max_diff = (engine_atr[valid_mask] - ref_atr[valid_mask]).abs().max()
            assert max_diff < 0.0001, f"Parity failed for {sym_name} length {length}: max_diff={max_diff}"

