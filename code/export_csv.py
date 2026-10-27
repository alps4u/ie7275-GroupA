"""Export every processed parquet table to plain CSV for SQL tools.

Timestamps are written as 'YYYY-MM-DD HH:MM:SS' and booleans as 0/1, which SQL
importers (DBeaver, SQL Server, PostgreSQL COPY, SQLite) read without extra settings.

Run: python -m icu_forecast.export_csv
"""

import pandas as pd

from icu_forecast.config import load_config


def main() -> None:
    cfg = load_config()
    src, dst = cfg["paths"]["processed"], cfg["paths"]["processed_csv"]
    dst.mkdir(parents=True, exist_ok=True)

    for path in sorted(src.glob("*.parquet")):
        df = pd.read_parquet(path)
        bools = df.select_dtypes("bool").columns
        df[bools] = df[bools].astype("int8")
        out = dst / f"{path.stem}.csv"
        df.to_csv(out, index=False, date_format="%Y-%m-%d %H:%M:%S", chunksize=500_000)
        print(f"{out.name}: {len(df):,} rows x {df.shape[1]} columns, {out.stat().st_size / 1e6:,.0f} MB")


if __name__ == "__main__":
    main()
