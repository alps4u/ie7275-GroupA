"""Patient-level train / validation / test split.

All admissions and snapshots of a patient land in the same split, so no patient's
history leaks across splits. The split is stratified by whether the patient ever had
a ward-to-ICU transfer, to keep the rare positive class balanced across splits.

Run: python -m icu_forecast.split
"""

import pandas as pd
from sklearn.model_selection import train_test_split

from icu_forecast.config import load_config


def make_split(cohort: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    seed, sizes = cfg["seed"], cfg["split"]
    patients = cohort.groupby("subject_id").has_icu.any().rename("ever_icu").reset_index()

    rest, test = train_test_split(
        patients, test_size=sizes["test"], stratify=patients.ever_icu, random_state=seed
    )
    train, val = train_test_split(
        rest, test_size=sizes["val"] / (sizes["train"] + sizes["val"]),
        stratify=rest.ever_icu, random_state=seed,
    )
    return pd.concat([
        train.assign(split="train"), val.assign(split="val"), test.assign(split="test")
    ])[["subject_id", "split"]].sort_values("subject_id").reset_index(drop=True)


def main() -> None:
    cfg = load_config()
    out = cfg["paths"]["processed"]
    cohort = pd.read_parquet(out / "cohort.parquet")
    split = make_split(cohort, cfg)

    assert split.subject_id.is_unique, "a patient appears in more than one split"
    split.to_csv(out / "split.csv", index=False)

    summary = cohort.merge(split, on="subject_id").groupby("split").agg(
        patients=("subject_id", "nunique"), admissions=("hadm_id", "size"), icu_rate=("has_icu", "mean")
    ).loc[["train", "val", "test"]]
    print(summary.to_string(float_format="{:.4f}".format))
    print(f"wrote {out / 'split.csv'}")


if __name__ == "__main__":
    main()
