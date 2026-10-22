"""LightGBM for 24h and 48h unplanned ICU transfer, evaluated on the validation split.

Early stopping uses 10% of the training patients, so the validation split is used only
for evaluation and for fitting the isotonic calibrator. The test split is not touched.

Run: python -m icu_forecast.models.lgbm
"""

import json

import joblib
import lightgbm as lgb
import matplotlib.pyplot as plt
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import GroupShuffleSplit

from icu_forecast.config import ROOT, load_config
from icu_forecast.models.evaluate import evaluate, save_results
from icu_forecast.prepare import load_features, to_tree_frame

PARAMS = dict(
    objective="binary", metric="average_precision", n_estimators=3000, learning_rate=0.03,
    num_leaves=31, min_child_samples=200, subsample=0.8, subsample_freq=1,
    colsample_bytree=0.7, reg_lambda=1.0, n_jobs=-1, verbose=-1,
)


def fit_lgbm(X: pd.DataFrame, y: pd.Series, groups: pd.Series, seed: int) -> lgb.LGBMClassifier:
    fit_idx, es_idx = next(GroupShuffleSplit(n_splits=1, test_size=0.1, random_state=seed).split(X, y, groups))
    model = lgb.LGBMClassifier(**PARAMS, random_state=seed)
    model.fit(
        X.iloc[fit_idx], y.iloc[fit_idx],
        eval_set=[(X.iloc[es_idx], y.iloc[es_idx])],
        callbacks=[lgb.early_stopping(150, verbose=False), lgb.log_evaluation(250)],
    )
    return model


def plot_importance(model: lgb.LGBMClassifier, path, top: int = 20) -> None:
    gain = pd.Series(model.booster_.feature_importance("gain"), index=model.booster_.feature_name())
    gain = (gain / gain.sum()).sort_values().tail(top)
    fig, ax = plt.subplots(figsize=(7, 6))
    ax.barh(gain.index, gain.values * 100, color="#27ae60")
    ax.set_xlabel("Share of total split gain (%)")
    ax.set_title("LightGBM, 24 h model: top features")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main() -> None:
    cfg = load_config()
    artifacts = cfg["paths"]["artifacts"]
    with open(artifacts / "feature_spec.json", encoding="utf-8") as f:
        spec = json.load(f)

    df = load_features(cfg)
    train, val = df[df.split == "train"], df[df.split == "val"]
    X_train = to_tree_frame(train, spec["numeric"], spec["categories"])
    X_val = to_tree_frame(val, spec["numeric"], spec["categories"])

    rows = []
    for label in spec["labels"]:
        print(f"--- {label}")
        model = fit_lgbm(X_train, train[label], train.subject_id, cfg["seed"])
        p_val = model.predict_proba(X_val)[:, 1]
        calibrator = IsotonicRegression(out_of_bounds="clip").fit(p_val, val[label])

        metrics = evaluate(val[label].to_numpy(), p_val)
        rows.append({"horizon": label, "best_iteration": model.best_iteration_, **metrics})
        print({k: round(v, 4) if isinstance(v, float) else v for k, v in rows[-1].items()})

        joblib.dump({"model": model, "calibrator": calibrator, "spec": spec}, artifacts / f"lgbm_{label}.joblib")
        if label == "y_24h":
            plot_importance(model, ROOT / "reports" / "figures" / "fig8_lgbm_importance.png")

    print(save_results("lightgbm", rows).to_string(index=False, float_format="{:.4f}".format))


if __name__ == "__main__":
    main()
