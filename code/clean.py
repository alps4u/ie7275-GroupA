"""Clean the raw MIMIC-IV tables and build the study cohort.

Reads data/raw (never modified) and writes parquet files plus cleaning_report.json
to data/processed. Every filter records how many rows it removed.

Run: python -m icu_forecast.clean
"""

import json
from numbers import Integral
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv

from icu_forecast.config import load_config


def note(report: dict, key: str, value) -> None:
    if isinstance(value, Integral):
        value = int(value)
    report[key] = value
    print(f"  {key}: {value:,}" if isinstance(value, int) else f"  {key}: {value}")


def to_ns(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        df[c] = pd.to_datetime(df[c]).astype("datetime64[ns]")
    return df


def stream_filter(path: Path, column_types: dict, keep) -> pd.DataFrame:
    """Read a large gzipped CSV in blocks, keeping only rows where keep(batch) is true."""
    reader = pacsv.open_csv(
        path,
        read_options=pacsv.ReadOptions(block_size=1 << 26),
        parse_options=pacsv.ParseOptions(newlines_in_values=True),
        convert_options=pacsv.ConvertOptions(
            include_columns=list(column_types), column_types=column_types
        ),
    )
    n_read, parts = 0, []
    for batch in reader:
        n_read += batch.num_rows
        parts.append(pa.Table.from_batches([batch]).filter(keep(batch)))
    print(f"  streamed {n_read:,} rows from {path.name}")
    return pa.concat_tables(parts).to_pandas(), n_read


def clean_admissions(raw: Path, cfg: dict, report: dict) -> pd.DataFrame:
    print("admissions")
    adm = pd.read_csv(raw / "hosp/admissions.csv.gz")
    adm = to_ns(adm, ["admittime", "dischtime", "deathtime"])
    note(report, "admissions_raw", len(adm))

    dup = adm.duplicated("hadm_id")
    note(report, "admissions_duplicate_hadm_id", dup.sum())
    adm = adm[~dup]

    bad_time = adm.dischtime <= adm.admittime
    note(report, "admissions_discharge_not_after_admit", bad_time.sum())
    adm = adm[~bad_time]

    bad_death = adm.deathtime.notna() & (adm.deathtime < adm.admittime)
    note(report, "admissions_death_before_admit_set_missing", bad_death.sum())
    adm.loc[bad_death, "deathtime"] = pd.NaT

    pat = pd.read_csv(raw / "hosp/patients.csv.gz", usecols=["subject_id", "gender", "anchor_age", "anchor_year"])
    adm = adm.merge(pat, on="subject_id", how="inner", validate="many_to_one")
    adm["age"] = adm.anchor_age + adm.admittime.dt.year - adm.anchor_year
    adm["planned"] = adm.admission_type.isin(cfg["cohort"]["planned_admission_types"])

    adm = adm.sort_values(["subject_id", "admittime"]).reset_index(drop=True)
    prev_disch = adm.groupby("subject_id").dischtime.shift()
    gap_days = (adm.admittime - prev_disch).dt.total_seconds() / 86400
    note(report, "admissions_overlapping_previous_stay", (gap_days < 0).sum())
    adm["days_since_prev_disch"] = gap_days
    # Changed from 0 to 1/24 to exclude readmissions within 1 hour of the previous discharge for administsrative purpose
    adm["readmit"] = gap_days.between(1/24, cfg["cohort"]["readmit_window_days"])
    return adm


def clean_transfers(raw: Path, cfg: dict, report: dict) -> pd.DataFrame:
    print("transfers")
    tr = pd.read_csv(raw / "hosp/transfers.csv.gz")
    tr = to_ns(tr, ["intime", "outtime"])
    note(report, "transfers_raw", len(tr))

    no_hadm = tr.hadm_id.isna()
    note(report, "transfers_without_hadm_id", no_hadm.sum())
    tr = tr[~no_hadm].astype({"hadm_id": "int64"})

    discharge = tr.eventtype == "discharge"
    note(report, "transfers_discharge_events_dropped", discharge.sum())
    tr = tr[~discharge]

    bad = tr.outtime.notna() & (tr.outtime < tr.intime)
    note(report, "transfers_outtime_before_intime", bad.sum())
    tr = tr[~bad]

    dup = tr.duplicated(["hadm_id", "careunit", "intime"])
    note(report, "transfers_duplicates", dup.sum())
    tr = tr[~dup]

    tr["careunit"] = tr.careunit.fillna("UNKNOWN")
    tr["is_icu"] = tr.careunit.isin(cfg["cohort"]["icu_units"])
    tr["is_ward"] = ~tr.is_icu & ~tr.careunit.isin(cfg["cohort"]["non_ward_units"])
    return tr


def build_cohort(adm: pd.DataFrame, tr: pd.DataFrame, raw: Path, cfg: dict, report: dict):
    print("cohort")
    first_icu = tr[tr.is_icu].groupby("hadm_id").intime.min().rename("first_icu_time")
    first_ward = tr[tr.is_ward].groupby("hadm_id").intime.min().rename("first_ward_time")
    adm = adm.join(first_icu, on="hadm_id").join(first_ward, on="hadm_id")
    adm["has_icu"] = adm.first_icu_time.notna()

    icu = pd.read_csv(raw / "icu/icustays.csv.gz", usecols=["hadm_id", "first_careunit", "intime"])
    icu = to_ns(icu, ["intime"])
    icu = icu[icu.first_careunit.isin(cfg["cohort"]["icu_units"])].groupby("hadm_id").intime.min()
    both = adm.set_index("hadm_id").first_icu_time.dropna().to_frame().join(icu, how="inner")
    agree = ((both.first_icu_time - both.intime).abs() <= pd.Timedelta(minutes=1)).mean()
    note(report, "icu_entry_agreement_transfers_vs_icustays", round(float(agree), 4))

    c = adm
    note(report, "cohort_step0_all_admissions", len(c))
    c = c[c.readmit]
    note(report, "cohort_step1_readmitted_within_window", len(c))
    c = c[~c.planned]
    note(report, "cohort_step2_unplanned", len(c))
    c = c[c.age >= cfg["cohort"]["min_age"]]
    note(report, "cohort_step3_adult", len(c))
    c = c[c.first_ward_time.notna()]
    note(report, "cohort_step4_has_ward_stay", len(c))
    direct = c.has_icu & ~(c.first_ward_time < c.first_icu_time)
    note(report, "cohort_excluded_direct_to_icu", direct.sum())
    c = c[~direct]
    note(report, "cohort_final_admissions", len(c))
    note(report, "cohort_final_patients", c.subject_id.nunique())
    note(report, "cohort_ward_to_icu_admissions", c.has_icu.sum())

    cols = ["subject_id", "hadm_id", "admittime", "dischtime", "deathtime", "admission_type",
            "admission_location", "gender", "age", "days_since_prev_disch", "first_icu_time", "has_icu"]
    return adm, c[cols].reset_index(drop=True)


def clean_labs(raw: Path, cohort: pd.DataFrame, cfg: dict, report: dict) -> pd.DataFrame:
    print("labevents")
    labs_cfg = cfg["labs"]
    itemids = pa.array([v[0] for v in labs_cfg.values()], type=pa.int64())
    hadm_ids = pa.array(cohort.hadm_id.unique(), type=pa.int64())
    types = {"hadm_id": pa.int64(), "itemid": pa.int64(), "charttime": pa.timestamp("s"),
             "storetime": pa.timestamp("s"), "valuenum": pa.float64()}

    def keep(b):
        return pc.and_(pc.is_in(b["itemid"], value_set=itemids), pc.is_in(b["hadm_id"], value_set=hadm_ids))

    lab, n_read = stream_filter(raw / "hosp/labevents.csv.gz", types, keep)
    lab = to_ns(lab, ["charttime", "storetime"])
    note(report, "labs_rows_read", n_read)
    note(report, "labs_rows_cohort_selected_items", len(lab))

    no_value = lab.valuenum.isna()
    note(report, "labs_missing_numeric_value", no_value.sum())
    lab = lab[~no_value]

    no_store = lab.storetime.isna()
    note(report, "labs_missing_storetime_filled_with_charttime", no_store.sum())
    lab.loc[no_store, "storetime"] = lab.loc[no_store, "charttime"]
    early = lab.storetime < lab.charttime
    note(report, "labs_storetime_before_charttime_fixed", early.sum())
    lab.loc[early, "storetime"] = lab.loc[early, "charttime"]

    id_to_name = {v[0]: k for k, v in labs_cfg.items()}
    lab["lab"] = lab.itemid.map(id_to_name)
    lo = lab.lab.map({k: v[1] for k, v in labs_cfg.items()})
    hi = lab.lab.map({k: v[2] for k, v in labs_cfg.items()})
    implausible = (lab.valuenum < lo) | (lab.valuenum > hi)
    report["labs_implausible_by_lab"] = lab[implausible].lab.value_counts().to_dict()
    note(report, "labs_implausible_removed", implausible.sum())
    lab = lab[~implausible]

    dup = lab.duplicated(["hadm_id", "itemid", "charttime", "valuenum"])
    note(report, "labs_duplicates_removed", dup.sum())
    lab = lab[~dup]
    note(report, "labs_final_rows", len(lab))
    return lab[["hadm_id", "lab", "charttime", "storetime", "valuenum"]].reset_index(drop=True)


def clean_prescriptions(raw: Path, cohort: pd.DataFrame, cfg: dict, report: dict) -> pd.DataFrame:
    print("prescriptions")
    hadm_ids = pa.array(cohort.hadm_id.unique(), type=pa.int64())
    types = {"hadm_id": pa.int64(), "starttime": pa.timestamp("s"), "drug": pa.string(), "route": pa.string()}
    rx, n_read = stream_filter(raw / "hosp/prescriptions.csv.gz", types,
                               lambda b: pc.is_in(b["hadm_id"], value_set=hadm_ids))
    rx = to_ns(rx, ["starttime"])
    note(report, "rx_rows_read", n_read)
    note(report, "rx_rows_cohort", len(rx))

    bad = rx.starttime.isna() | rx.drug.isna()
    note(report, "rx_missing_starttime_or_drug", bad.sum())
    rx = rx[~bad]
    rx["drug"] = rx.drug.str.lower().str.strip()
    dup = rx.duplicated(["hadm_id", "starttime", "drug", "route"])
    note(report, "rx_duplicates_removed", dup.sum())
    rx = rx[~dup]

    rx["iv"] = rx.route.isin(cfg["iv_routes"])
    for name, pattern in cfg["drug_classes"].items():
        rx[name] = rx.drug.str.contains(pattern, regex=True)
        note(report, f"rx_class_{name}", rx[name].sum())
    keep = ["hadm_id", "starttime", "iv", *cfg["drug_classes"]]
    return rx[keep].reset_index(drop=True)


def clean_omr(raw: Path, cohort: pd.DataFrame, cfg: dict, report: dict) -> pd.DataFrame:
    print("omr")
    omr = pd.read_csv(raw / "hosp/omr.csv.gz")
    omr = to_ns(omr[omr.subject_id.isin(cohort.subject_id)].copy(), ["chartdate"])
    note(report, "omr_rows_cohort_patients", len(omr))

    bp = omr[omr.result_name == "Blood Pressure"].result_value.str.split("/", expand=True)
    parts = [
        omr.loc[bp.index, ["subject_id", "chartdate"]].assign(measure="sbp", value=pd.to_numeric(bp[0], errors="coerce")),
        omr.loc[bp.index, ["subject_id", "chartdate"]].assign(measure="dbp", value=pd.to_numeric(bp[1], errors="coerce")),
    ]
    for name, measure in [("Weight (Lbs)", "weight_lbs"), ("BMI (kg/m2)", "bmi")]:
        m = omr[omr.result_name == name]
        parts.append(m[["subject_id", "chartdate"]].assign(measure=measure, value=pd.to_numeric(m.result_value, errors="coerce")))
    long = pd.concat(parts, ignore_index=True)

    unparsable = long.value.isna()
    note(report, "omr_unparsable_values", unparsable.sum())
    long = long[~unparsable]
    ranges = cfg["omr"]["ranges"]
    lo = long.measure.map({k: v[0] for k, v in ranges.items()})
    hi = long.measure.map({k: v[1] for k, v in ranges.items()})
    implausible = (long.value < lo) | (long.value > hi)
    report["omr_implausible_by_measure"] = long[implausible].measure.value_counts().to_dict()
    note(report, "omr_implausible_removed", implausible.sum())
    long = long[~implausible]
    long = long.groupby(["subject_id", "chartdate", "measure"], as_index=False).value.mean()
    note(report, "omr_final_rows", len(long))
    return long


def clean_services(raw: Path, cohort: pd.DataFrame, report: dict) -> pd.DataFrame:
    print("services")
    sv = pd.read_csv(raw / "hosp/services.csv.gz", usecols=["hadm_id", "transfertime", "curr_service"])
    sv = to_ns(sv[sv.hadm_id.isin(cohort.hadm_id)].copy(), ["transfertime"])
    dup = sv.duplicated()
    note(report, "services_duplicates_removed", dup.sum())
    return sv[~dup].reset_index(drop=True)


def clean_diagnoses(raw: Path, adm: pd.DataFrame, cohort: pd.DataFrame, report: dict) -> pd.DataFrame:
    print("diagnoses")
    dx = pd.read_csv(raw / "hosp/diagnoses_icd.csv.gz", usecols=["subject_id", "hadm_id", "icd_code"], dtype={"icd_code": str})
    dx = dx[dx.subject_id.isin(cohort.subject_id)]
    dx["icd_code"] = dx.icd_code.str.strip().str.upper()
    dup = dx.duplicated(["hadm_id", "icd_code"])
    note(report, "diagnoses_duplicates_removed", dup.sum())
    dx = dx[~dup].merge(adm[["hadm_id", "dischtime"]], on="hadm_id", how="inner")
    note(report, "diagnoses_rows_cohort_patients", len(dx))
    return dx


def main() -> None:
    cfg = load_config()
    raw, out = cfg["paths"]["raw"], cfg["paths"]["processed"]
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {}

    adm = clean_admissions(raw, cfg, report)
    tr = clean_transfers(raw, cfg, report)
    adm, cohort = build_cohort(adm, tr, raw, cfg, report)

    adm_cols = ["subject_id", "hadm_id", "admittime", "dischtime", "has_icu"]
    adm[adm_cols].to_parquet(out / "admissions_all.parquet", index=False)
    cohort.to_parquet(out / "cohort.parquet", index=False)
    tr[tr.hadm_id.isin(cohort.hadm_id)][["hadm_id", "careunit", "intime", "outtime", "is_icu", "is_ward"]] \
        .to_parquet(out / "transfers.parquet", index=False)

    clean_services(raw, cohort, report).to_parquet(out / "services.parquet", index=False)
    clean_omr(raw, cohort, cfg, report).to_parquet(out / "omr.parquet", index=False)
    clean_diagnoses(raw, adm, cohort, report).to_parquet(out / "diagnoses.parquet", index=False)
    clean_prescriptions(raw, cohort, cfg, report).to_parquet(out / "prescriptions.parquet", index=False)
    clean_labs(raw, cohort, cfg, report).to_parquet(out / "labs.parquet", index=False)

    with open(out / "cleaning_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"wrote cleaned tables and cleaning_report.json to {out}")


if __name__ == "__main__":
    main()
