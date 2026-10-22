"""Build one row per ward-day snapshot, with 24h/48h ICU-transfer labels and features.

A snapshot is taken every day at the configured hour while the patient is on a ward,
and stops at the first ICU entry, discharge, or death. Every feature uses only data
available strictly before the snapshot time: labs by storetime (when the result was
released), prescriptions by starttime, comorbidities from earlier discharges, and
outpatient measurements from before the admission day.

Run: python -m icu_forecast.features
"""

import json

import numpy as np
import pandas as pd

from icu_forecast.config import load_config

DAY = pd.Timedelta(days=1)
KEYS = ["hadm_id", "snapshot_time"]


def next_snapshot(ts: pd.Series, hour: int) -> pd.Series:
    """First daily snapshot time strictly after each timestamp."""
    return (ts - pd.Timedelta(hours=hour)).dt.floor("D") + DAY + pd.Timedelta(hours=hour)


def build_snapshots(cohort: pd.DataFrame, transfers: pd.DataFrame, cfg: dict, report: dict) -> pd.DataFrame:
    c = cohort.copy()
    c["end"] = c[["first_icu_time", "dischtime", "deathtime"]].min(axis=1)
    c["first_snap"] = next_snapshot(c.admittime, cfg["snapshot"]["hour"])
    n = np.ceil((c.end - c.first_snap) / DAY).clip(lower=0).astype(int)
    snap = c.loc[c.index.repeat(n)].copy()
    snap["snapshot_time"] = snap.first_snap + snap.groupby(level=0).cumcount() * DAY
    report["snapshots_candidate"] = len(snap)

    snap = pd.merge_asof(
        snap.sort_values("snapshot_time"),
        transfers.sort_values("intime")[["hadm_id", "intime", "outtime", "careunit", "is_ward"]],
        left_on="snapshot_time", right_on="intime", by="hadm_id", direction="backward",
    )
    in_ward = snap.is_ward.eq(True) & (snap.outtime.isna() | (snap.outtime > snap.snapshot_time))
    report["snapshots_dropped_not_on_ward"] = int((~in_ward).sum())
    snap = snap[in_ward]
    report["snapshots_final"] = len(snap)

    for h in cfg["snapshot"]["horizons_hours"]:
        window_end = snap.snapshot_time + pd.Timedelta(hours=h)
        snap[f"y_{h}h"] = ((snap.first_icu_time > snap.snapshot_time) & (snap.first_icu_time <= window_end)).astype("int8")
        report[f"positive_rate_{h}h"] = round(float(snap[f"y_{h}h"].mean()), 5)

    snap["hours_since_admit"] = (snap.snapshot_time - snap.admittime).dt.total_seconds() / 3600
    snap["gender_male"] = (snap.gender == "M").astype("int8")
    cols = ["subject_id", *KEYS, "y_24h", "y_48h", "age", "gender_male", "admission_type",
            "admission_location", "careunit", "hours_since_admit", "days_since_prev_disch"]
    return snap[cols].reset_index(drop=True)


def history_features(cohort: pd.DataFrame, adm_all: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    prior = adm_all.rename(columns={"admittime": "p_admit", "dischtime": "p_disch", "has_icu": "p_icu"})
    pairs = cohort[["hadm_id", "subject_id", "admittime"]].merge(prior.drop(columns="hadm_id"), on="subject_id")
    pairs = pairs[pairs.p_disch <= pairs.admittime].sort_values("p_disch")
    window = pd.Timedelta(days=cfg["cohort"]["readmit_window_days"])
    pairs["recent"] = pairs.p_disch >= pairs.admittime - window
    pairs["p_los_days"] = (pairs.p_disch - pairs.p_admit).dt.total_seconds() / 86400

    g = pairs.groupby("hadm_id")
    return pd.DataFrame({
        "n_prior_adm_total": g.size(),
        "n_prior_adm_365d": g.recent.sum(),
        "prior_icu_any": g.p_icu.any().astype("int8"),
        "prev_los_days": g.p_los_days.last(),
    })


def comorbidity_features(cohort: pd.DataFrame, dx: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    out = pd.DataFrame(index=cohort.hadm_id)
    for name, prefixes in cfg["comorbidities"].items():
        first_disch = dx[dx.icd_code.str.startswith(tuple(prefixes))].groupby("subject_id").dischtime.min()
        out[f"hx_{name}"] = (cohort.subject_id.map(first_disch) <= cohort.admittime).astype("int8").values
    return out


def omr_features(cohort: pd.DataFrame, omr: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    left = cohort[["hadm_id", "subject_id"]].assign(admit_day=cohort.admittime.dt.floor("D")).sort_values("admit_day")
    out = pd.DataFrame(index=cohort.hadm_id)
    for measure, g in omr.groupby("measure"):
        m = pd.merge_asof(
            left, g.sort_values("chartdate")[["subject_id", "chartdate", "value"]],
            left_on="admit_day", right_on="chartdate", by="subject_id",
            allow_exact_matches=False, tolerance=pd.Timedelta(days=cfg["omr"]["lookback_days"]),
        )
        out[f"omr_{measure}"] = m.set_index("hadm_id").value
    return out


def lab_features(keys: pd.DataFrame, labs: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Last value and change since the previous value, plus min/max/count over the last 24h."""
    if cfg["snapshot"]["lookback_hours"] != 24:
        raise ValueError("lab window bucketing assumes a 24h lookback with daily snapshots")
    keys = keys.sort_values("snapshot_time").reset_index(drop=True)
    out = keys.copy()

    labs = labs.sort_values(["hadm_id", "lab", "storetime"])
    labs["prev_value"] = labs.groupby(["hadm_id", "lab"]).valuenum.shift()
    labs = labs.sort_values("storetime")
    for name, g in labs.groupby("lab"):
        m = pd.merge_asof(
            keys, g[["hadm_id", "storetime", "valuenum", "prev_value"]],
            left_on="snapshot_time", right_on="storetime", by="hadm_id", allow_exact_matches=False,
        )
        out[f"{name}_last"] = m.valuenum
        out[f"{name}_delta"] = m.valuenum - m.prev_value

    labs["snapshot_time"] = next_snapshot(labs.storetime, cfg["snapshot"]["hour"])
    w = labs.groupby([*KEYS, "lab"]).valuenum.agg(["min", "max", "count"]).unstack("lab")
    w.columns = [f"{lab}_{stat}24h" for stat, lab in w.columns]
    out = out.merge(w, left_on=KEYS, right_index=True, how="left")
    count_cols = [c for c in out if c.endswith("_count24h")]
    out[count_cols] = out[count_cols].fillna(0)
    return out.set_index(KEYS)


def rx_features(keys: pd.DataFrame, rx: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Medication orders started in the 24h before each snapshot, overall and by drug class."""
    rx = rx.assign(
        snapshot_time=next_snapshot(rx.starttime, cfg["snapshot"]["hour"]),
        antibiotic_iv=rx.antibiotic & rx.iv,
    )
    classes = [*cfg["drug_classes"], "antibiotic_iv"]
    agg = rx.groupby(KEYS).agg(
        rx_new_24h=("iv", "size"), rx_iv_new_24h=("iv", "sum"),
        **{f"rx_{c}_24h": (c, "sum") for c in classes},
    )
    return keys.merge(agg, left_on=KEYS, right_index=True, how="left").set_index(KEYS).fillna(0)


def service_features(keys: pd.DataFrame, services: pd.DataFrame) -> pd.DataFrame:
    m = pd.merge_asof(
        keys.sort_values("snapshot_time"), services.sort_values("transfertime"),
        left_on="snapshot_time", right_on="transfertime", by="hadm_id",
    )
    return m.set_index(KEYS)[["curr_service"]]


def main() -> None:
    cfg = load_config()
    d = cfg["paths"]["processed"]
    report: dict = {}

    cohort = pd.read_parquet(d / "cohort.parquet")
    snap = build_snapshots(cohort, pd.read_parquet(d / "transfers.parquet"), cfg, report)
    print(f"snapshots: {len(snap):,}  24h positive rate: {report['positive_rate_24h']:.4%}")
    keys = snap[KEYS]

    static = pd.concat([
        history_features(cohort, pd.read_parquet(d / "admissions_all.parquet"), cfg),
        comorbidity_features(cohort, pd.read_parquet(d / "diagnoses.parquet"), cfg),
        omr_features(cohort, pd.read_parquet(d / "omr.parquet"), cfg),
    ], axis=1)
    print("static features done")
    dynamic = pd.concat([
        service_features(keys, pd.read_parquet(d / "services.parquet")),
        rx_features(keys, pd.read_parquet(d / "prescriptions.parquet"), cfg),
        lab_features(keys, pd.read_parquet(d / "labs.parquet"), cfg),
    ], axis=1)
    print("dynamic features done")

    feats = snap.merge(static, left_on="hadm_id", right_index=True, how="left") \
                .merge(dynamic, left_on=KEYS, right_index=True, how="left")
    floats = feats.select_dtypes("float64").columns
    feats[floats] = feats[floats].astype("float32")
    feats = feats.sort_values(["subject_id", *KEYS]).reset_index(drop=True)

    feats.to_parquet(d / "features.parquet", index=False)
    report["n_columns"] = feats.shape[1]
    with open(d / "features_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"wrote features.parquet: {feats.shape[0]:,} rows x {feats.shape[1]} columns")


if __name__ == "__main__":
    main()
