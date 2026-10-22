"""Logistic regression baseline on the imputed, scaled, one-hot encoded features.

Run: python -m icu_forecast.models.logreg
"""

import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from icu_forecast.config import load_config
from icu_forecast.models.evaluate import evaluate, save_results
from icu_forecast.prepare import LABELS, build_preprocessor, feature_columns, load_features


def main() -> None:
    cfg = load_config()
    df = load_features(cfg)
    numeric, categorical = feature_columns(df)
    cols = numeric + categorical
    train, val = df[df.split == "train"], df[df.split == "val"]

    rows = []
    for label in LABELS:
        print(f"--- {label}")
        pipe = Pipeline([
            ("pre", build_preprocessor(numeric, categorical)),
            ("lr", LogisticRegression(C=0.1, max_iter=2000)),
        ]).fit(train[cols], train[label])
        p_val = pipe.predict_proba(val[cols])[:, 1]
        rows.append({"horizon": label, **evaluate(val[label].to_numpy(), p_val)})
        joblib.dump(pipe, cfg["paths"]["artifacts"] / f"logreg_{label}.joblib")

    print(save_results("logistic_regression", rows).to_string(index=False, float_format="{:.4f}".format))


if __name__ == "__main__":
    main()
