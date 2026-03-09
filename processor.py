#!/usr/bin/env python3
import json
import io
import boto3
import pandas as pd
import logging
from datetime import datetime

from baseline import BaselineManager
from detector import AnomalyDetector

s3 = boto3.client("s3")

logger = logging.getLogger(__name__)

NUMERIC_COLS = ["temperature", "humidity", "pressure", "wind_speed"]  # students configure this

def process_file(bucket: str, key: str):
    print(f"Processing: s3://{bucket}/{key}")

    try:
        # 1. Download raw file
        try:
            response = s3.get_object(Bucket=bucket, Key=key)
            df = pd.read_csv(io.BytesIO(response["Body"].read()))

            print(f"  Loaded {len(df)} rows, columns: {list(df.columns)}")
            logger.info(f"Loaded {len(df)} rows, columns: {list(df.columns)}")
        except Exception as e:
            logger.error(f"Unexpected error reading CSV {key}: {e}")
            return

        # 2. Load current baseline
        try:
            baseline_mgr = BaselineManager(bucket=bucket)
            baseline = baseline_mgr.load()
        except Exception as e:
            logger.error(f"Failed to load baseline: {e}")
            baseline = {}

        # 3. Update baseline with values from this batch BEFORE scoring
        #    (use only non-null values for each channel)
        try:
            for col in NUMERIC_COLS:
                if col in df.columns:
                    clean_values = df[col].dropna().tolist()
                    if clean_values:
                        baseline = baseline_mgr.update(baseline, col, clean_values)
        except Exception as e:
            logger.error(f"Error updating baseline: {e}")

        # 4. Run detection
        try:
            detector = AnomalyDetector(z_threshold=3.0, contamination=0.05)
            scored_df = detector.run(df, NUMERIC_COLS, baseline, method="both")
        except Exception as e:
            logger.error(f"Anomaly detection failed for file {key}: {e}")
            return

        # 5. Write scored file to processed/ prefix
        try:
            output_key = key.replace("raw/", "processed/")
            csv_buffer = io.StringIO()
            scored_df.to_csv(csv_buffer, index=False)
            s3.put_object(
                Bucket=bucket,
                Key=output_key,
                Body=csv_buffer.getvalue(),
                ContentType="text/csv"
            )

            logger.info(f"Processed CSV written to s3://{bucket}/{output_key}")
        except Exception as e:
            logger.error(f"Failed to write processed CSV to S3: {e}")
            return
        

        # 6. Save updated baseline back to S3
        baseline_mgr.save(baseline)

        # 7. Build and return a processing summary
        anomaly_count = int(scored_df["anomaly"].sum()) if "anomaly" in scored_df else 0
        summary = {
            "source_key": key,
            "output_key": output_key,
            "processed_at": datetime.utcnow().isoformat(),
            "total_rows": len(df),
            "anomaly_count": anomaly_count,
            "anomaly_rate": round(anomaly_count / len(df), 4) if len(df) > 0 else 0,
            "baseline_observation_counts": {
                col: baseline.get(col, {}).get("count", 0) for col in NUMERIC_COLS
            }
        }

        # Write summary JSON alongside the processed file
        try:
            summary_key = output_key.replace(".csv", "_summary.json")
            s3.put_object(
                Bucket=bucket,
                Key=summary_key,
                Body=json.dumps(summary, indent=2),
                ContentType="application/json"
            )

            logger.info(f"Processing summary written to s3://{bucket}/{summary_key}")
            
        except Exception as e:
            logger.error(f"Unexpected error writing summary JSON to s3: {e}")

        print(f"  Done: {anomaly_count}/{len(df)} anomalies flagged")
        return summary
    except Exception as e:
        logger.error(f"Fatal error in process_file for {key}: {e}")
        return
