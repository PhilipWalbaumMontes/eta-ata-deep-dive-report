import streamlit as st
import pandas as pd
import zipfile
import io

st.set_page_config(page_title="ETA/ATA Deep-Dive by BOL", layout="centered")
st.title("ETA/ATA Deep-Dive by BOL")

st.markdown(
    """
This app performs a **deep dive** at BOL level for container shipments.

Upload your **shipment CSV** and then map the corresponding column names:

- **Identifier**: container or row identifier
- **Shipment type**: column that contains values like `"CONTAINER_ID"` or `"Container"`
- **BOL ID**
- **ETA (Destination ETA)**: timestamp
- **ATA (Destination ATA)**: timestamp

For **container rows only** (shipment_type matching `CONTAINER` or `CONTAINER_ID`, case-insensitive), the app will:

1. For ETA:
   - Compute the spread in **hours** between the earliest and latest ETA in each BOL.
   - Classify each BOL into:
     - `NO_DIFFERENCE` (spread = 0 h)
     - `BETWEEN_1_AND_24_HOURS` (0 < spread ≤ 24 h)
     - `MORE_THAN_24_HOURS` (spread > 24 h)
     - `NO_VALID_TIMESTAMP` (no parsable ETA for that BOL)

2. For ATA:
   - Same logic and buckets.

3. Summary:
   - Separate counts for ETA and ATA by bucket.
   - Count BOLs with **mixed ATA presence** (some containers with ATA, others without).

4. Deep dive:
   - Details of all containers in BOLs where some containers have ATA and others do not.

All results are packaged as a ZIP containing:
- `bol_eta_spread.csv`
- `bol_ata_spread.csv`
- `bol_mixed_ata_presence_detail.csv`
- `summary.csv`
"""
)

uploaded_file = st.file_uploader("Upload shipment CSV", type=["csv"])


def classify_spread_hours(value):
    """Classify the spread (in hours) into buckets."""
    if value is None or pd.isna(value):
        return "NO_VALID_TIMESTAMP"
    if value == 0:
        return "NO_DIFFERENCE"
    if 0 < value <= 24:
        return "BETWEEN_1_AND_24_HOURS"
    return "MORE_THAN_24_HOURS"


if uploaded_file is not None:
    try:
        # Read every column as string and normalize blanks
        df = pd.read_csv(uploaded_file, dtype=str)
        df = df.fillna("")

        st.write(f"Detected **{df.shape[0]} rows** and **{df.shape[1]} columns**.")

        # Let the user map columns by name
        st.subheader("Map CSV Columns")

        cols = list(df.columns)

        identifier_col = st.selectbox("Identifier column", options=cols)
        shipment_type_col = st.selectbox("Shipment type column", options=cols)
        bol_id_col = st.selectbox("BOL ID column", options=cols)
        eta_col = st.selectbox("ETA (Destination ETA) column", options=cols)
        ata_col = st.selectbox("ATA (Destination ATA) column", options=cols)

        if st.button("Run Deep-Dive Analysis and Generate ZIP"):
            # Normalized working columns
            work_df = df.copy()
            work_df["identifier"] = work_df[identifier_col].astype(str)
            work_df["shipment_type"] = work_df[shipment_type_col].astype(str)
            work_df["bol_id"] = work_df[bol_id_col].astype(str)
            work_df["eta"] = work_df[eta_col].astype(str)
            work_df["ata"] = work_df[ata_col].astype(str)

            # Filter container rows: shipment_type in {CONTAINER, CONTAINER_ID} (case-insensitive, trimmed)
            containers = work_df[
                work_df["shipment_type"]
                .str.strip()
                .str.upper()
                .isin(["CONTAINER", "CONTAINER_ID"])
            ].copy()

            if containers.empty:
                st.warning(
                    "No container rows found. I looked for values 'Container' or 'CONTAINER_ID' "
                    f"in column '{shipment_type_col}'."
                )
            else:
                st.info(
                    f"{len(containers)} rows detected as containers using column '{shipment_type_col}'."
                )

                # Parse timestamps (no guessing: non-parsable remain NaT and will not contribute to spreads)
                containers["eta_dt"] = pd.to_datetime(containers["eta"], errors="coerce")
                containers["ata_dt"] = pd.to_datetime(containers["ata"], errors="coerce")

                # Group by BOL
                grouped = containers.groupby("bol_id", dropna=False)

                # Aggregate per BOL: counts and min/max timestamps
                bol_spread = grouped.agg(
                    n_containers=("identifier", "size"),
                    n_eta_present=("eta", lambda s: (s.str.strip() != "").sum()),
                    n_eta_missing=("eta", lambda s: (s.str.strip() == "").sum()),
                    n_ata_present=("ata", lambda s: (s.str.strip() != "").sum()),
                    n_ata_missing=("ata", lambda s: (s.str.strip() == "").sum()),
                    eta_min=("eta_dt", "min"),
                    eta_max=("eta_dt", "max"),
                    ata_min=("ata_dt", "min"),
                    ata_max=("ata_dt", "max"),
                )

                # Compute spreads in hours
                # If min or max is NaT (no valid timestamps), result will be NaN
                bol_spread["eta_spread_hours"] = (
                    (bol_spread["eta_max"] - bol_spread["eta_min"])
                    .dt.total_seconds()
                    / 3600.0
                )
                bol_spread["ata_spread_hours"] = (
                    (bol_spread["ata_max"] - bol_spread["ata_min"])
                    .dt.total_seconds()
                    / 3600.0
                )

                # Classify spreads into buckets
                bol_spread["eta_spread_bucket"] = bol_spread["eta_spread_hours"].apply(
                    classify_spread_hours
                )
                bol_spread["ata_spread_bucket"] = bol_spread["ata_spread_hours"].apply(
                    classify_spread_hours
                )

                # Reset index so bol_id is a column
                bol_spread = bol_spread.reset_index()

                # Separate views for ETA and ATA
                bol_eta_spread = bol_spread[
                    [
                        "bol_id",
                        "n_containers",
                        "n_eta_present",
                        "n_eta_missing",
                        "eta_min",
                        "eta_max",
                        "eta_spread_hours",
                        "eta_spread_bucket",
                    ]
                ].copy()

                bol_ata_spread = bol_spread[
                    [
                        "bol_id",
                        "n_containers",
                        "n_ata_present",
                        "n_ata_missing",
                        "ata_min",
                        "ata_max",
                        "ata_spread_hours",
                        "ata_spread_bucket",
                    ]
                ].copy()

                # Identify BOLs with mixed ATA presence:
                # at least one container with ATA, at least one without
                mixed_ata_bols = bol_spread.loc[
                    (bol_spread["n_ata_present"] > 0)
                    & (bol_spread["n_ata_missing"] > 0),
                    "bol_id",
                ]

                bol_mixed_ata_presence_detail = containers[
                    containers["bol_id"].isin(mixed_ata_bols)
                ][["bol_id", "identifier", "shipment_type", "eta", "ata"]].copy()

                # Build summary table
                rows = []
                bucket_descriptions = {
                    "NO_DIFFERENCE": "Spread = 0 hours",
                    "BETWEEN_1_AND_24_HOURS": "Spread > 0 and ≤ 24 hours",
                    "MORE_THAN_24_HOURS": "Spread > 24 hours",
                    "NO_VALID_TIMESTAMP": "No parsable timestamps for this metric in the BOL",
                    "MIXED_PRESENT_AND_MISSING": "BOLs where some containers have ATA and others do not",
                }

                # ETA summary
                eta_counts = (
                    bol_spread["eta_spread_bucket"]
                    .value_counts(dropna=False)
                    .to_dict()
                )
                for bucket, count in eta_counts.items():
                    rows.append(
                        {
                            "metric": "ETA",
                            "bucket": bucket,
                            "description": bucket_descriptions.get(
                                bucket, bucket
                            ),
                            "count": int(count),
                        }
                    )

                # ATA summary
                ata_counts = (
                    bol_spread["ata_spread_bucket"]
                    .value_counts(dropna=False)
                    .to_dict()
                )
                for bucket, count in ata_counts.items():
                    rows.append(
                        {
                            "metric": "ATA",
                            "bucket": bucket,
                            "description": bucket_descriptions.get(
                                bucket, bucket
                            ),
                            "count": int(count),
                        }
                    )

                # Mixed ATA presence summary
                rows.append(
                    {
                        "metric": "ATA_MIXED_PRESENCE",
                        "bucket": "MIXED_PRESENT_AND_MISSING",
                        "description": bucket_descriptions["MIXED_PRESENT_AND_MISSING"],
                        "count": int(len(mixed_ata_bols)),
                    }
                )

                summary_df = pd.DataFrame(
                    rows, columns=["metric", "bucket", "description", "count"]
                )

                # === Build ZIP in memory ===
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(
                    zip_buffer, "w", compression=zipfile.ZIP_DEFLATED
                ) as zf:
                    zf.writestr(
                        "bol_eta_spread.csv",
                        bol_eta_spread.to_csv(index=False).encode("utf-8"),
                    )
                    zf.writestr(
                        "bol_ata_spread.csv",
                        bol_ata_spread.to_csv(index=False).encode("utf-8"),
                    )
                    zf.writestr(
                        "bol_mixed_ata_presence_detail.csv",
                        bol_mixed_ata_presence_detail.to_csv(index=False).encode(
                            "utf-8"
                        ),
                    )
                    zf.writestr(
                        "summary.csv",
                        summary_df.to_csv(index=False).encode("utf-8"),
                    )

                zip_buffer.seek(0)

                st.success("Deep-dive analysis completed. Download the ZIP below.")
                st.download_button(
                    label="Download Deep-Dive ETA/ATA ZIP",
                    data=zip_buffer,
                    file_name="eta_ata_deep_dive_report.zip",
                    mime="application/zip",
                )

                st.subheader("Summary (preview)")
                st.dataframe(summary_df)

                st.subheader("ETA spread by BOL (preview)")
                st.dataframe(bol_eta_spread.head(50))

                st.subheader("ATA spread by BOL (preview)")
                st.dataframe(bol_ata_spread.head(50))

                if not bol_mixed_ata_presence_detail.empty:
                    st.subheader("Mixed ATA presence – detailed BOLs (preview)")
                    st.dataframe(bol_mixed_ata_presence_detail.head(50))

    except Exception as e:
        st.error(f"Error processing file: {e}")

else:
    st.info("Upload a CSV file to begin.")
