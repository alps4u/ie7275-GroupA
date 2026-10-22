"""Discrimination and watchlist metrics shared by all models."""

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

from icu_forecast.config import load_config


def evaluate(y: np.ndarray, p: np.ndarray, top_frac: float = 0.10) -> dict:
    """ROC-AUC, PR-AUC, Brier score, and precision/recall among the top `top_frac` highest-risk snapshots."""
    y, p = np.asarray(y), np.asarray(p)
    k = int(np.ceil(top_frac * len(y)))
    flagged = y[np.argsort(-p, kind="stable")[:k]]
    return {
        "n": len(y),
        "prevalence": y.mean(),
        "roc_auc": roc_auc_score(y, p),
        "pr_auc": average_precision_score(y, p),
        "precision_top10": flagged.sum() / k,
        "recall_top10": flagged.sum() / y.sum(),
        "brier": brier_score_loss(y, p),
    }


def save_results(model: str, rows: list[dict]) -> pd.DataFrame:
    """Replace this model's rows in artifacts/results_val.csv and return the full table."""
    path = load_config()["paths"]["artifacts"] / "results_val.csv"
    new = pd.DataFrame(rows).assign(model=model)
    if path.exists():
        old = pd.read_csv(path)
        new = pd.concat([old[old.model != model], new], ignore_index=True)
    new = new[["model", "horizon", *[c for c in new.columns if c not in ("model", "horizon")]]]
    new.to_csv(path, index=False)
    return new
