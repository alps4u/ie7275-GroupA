"""Train-only preprocessing for the snapshot features.

Two views of the same features, both fitted on the training split only:
- tree models (LightGBM, XGBoost) take the raw features, with categoricals as pandas
  categories whose levels come from the training split; missing values stay missing,
  because a lab that was not ordered is itself informative.
- linear models take the output of a ColumnTransformer: median imputation with
  missing-value indicators, standard scaling, and one-hot encoding.

Run: python -m icu_forecast.prepare
"""

import json

import joblib
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from icu_forecast.config import load_config

ID_COLS = ["subject_id", "hadm_id", "snapshot_time", "split"]
LABELS = ["y_24h", "y_48h"]
CATEGORICAL = ["admission_type", "admission_location", "careunit", "curr_service"]


def load_features(cfg: dict) -> pd.DataFrame:
    d = cfg["paths"]["processed"]
    feats = pd.read_parquet(d / "features.parquet")
    split = pd.read_csv(d / "split.csv")
    return feats.merge(split, on="subject_id", how="inner", validate="many_to_one")


def feature_columns(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    numeric = [c for c in df.columns if c not in ID_COLS + LABELS + CATEGORICAL]
    return numeric, CATEGORICAL


def build_preprocessor(numeric: list[str], categorical: list[str]) -> ColumnTransformer:
    num = Pipeline([
        ("impute", SimpleImputer(strategy="median", add_indicator=True)),
        ("scale", StandardScaler()),
    ])
    cat = Pipeline([
        ("impute", SimpleImputer(strategy="constant", fill_value="missing")),
        ("onehot", OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=0.001, sparse_output=False)),
    ])
    return ColumnTransformer([("num", num, numeric), ("cat", cat, categorical)])


def to_tree_frame(df: pd.DataFrame, numeric: list[str], categories: dict[str, list[str]]) -> pd.DataFrame:
    """Raw features for tree models; categories unseen in training become missing."""
    X = df[numeric + list(categories)].copy()
    for col, levels in categories.items():
        X[col] = pd.Categorical(X[col], categories=levels)
    return X


def main() -> None:
    cfg = load_config()
    out = cfg["paths"]["processed"]
    artifacts = cfg["paths"]["artifacts"]
    artifacts.mkdir(parents=True, exist_ok=True)

    df = load_features(cfg)
    numeric, categorical = feature_columns(df)
    train = df[df.split == "train"]

    summary = df.groupby("split").agg(snapshots=("y_24h", "size"), y_24h=("y_24h", "mean"), y_48h=("y_48h", "mean"))
    print(summary.loc[["train", "val", "test"]].to_string(float_format="{:.4f}".format))

    missing = train[numeric + categorical].isna().mean().sort_values(ascending=False)
    missing.rename("missing_share_train").to_csv(out / "missingness_train.csv")
    print(f"\nfeatures: {len(numeric)} numeric, {len(categorical)} categorical")
    print("most-missing features in train:\n" + missing.head(10).to_string(float_format="{:.3f}".format))

    pre = build_preprocessor(numeric, categorical).fit(train[numeric + categorical])
    n_out = pre.transform(train[numeric + categorical].head(5)).shape[1]
    joblib.dump(pre, artifacts / "preprocessor.joblib")

    categories = {c: sorted(train[c].dropna().unique().tolist()) for c in categorical}
    spec = {"numeric": numeric, "categorical": categorical, "categories": categories, "labels": LABELS}
    with open(artifacts / "feature_spec.json", "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2)
    print(f"\nlinear-model matrix width after imputation indicators and one-hot: {n_out}")
    print(f"wrote {artifacts / 'preprocessor.joblib'} and feature_spec.json")


if __name__ == "__main__":
    main()
