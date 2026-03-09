# app.py
import io
import json
import os
import boto3
import pandas as pd
import requests
import logging
from datetime import datetime
from fastapi import FastAPI, BackgroundTasks, Request, HTTPException
from baseline import BaselineManager
from processor import process_file

# ── Logging Setup ────────────────────────────────────────────────────────────

LOG_FILE = "/var/log/anomly_detection.log"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE)
    ]
)

logger = logging.getLogger(__name__)

# ── App Initialization ────────────────────────────────────────────────────────

app = FastAPI(title="Anomaly Detection Pipeline")

try:
    BUCKET_NAME = os.environ["BUCKET_NAME"]
except KeyError:
    logger.error("Environment variable BUCKET_NAME is not set.")
    raise

s3 = boto3.client("s3")

logger.info(f"Application starting with bucket: {BUCKET_NAME}")

# ── SNS subscription confirmation + message handler ──────────────────────────

@app.post("/notify")
async def handle_sns(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
        msg_type = request.headers.get("x-amz-sns-message-type")

        logger.info(f"SNS message received. Type: {msg_type}")

        # SNS sends a SubscriptionConfirmation before it will deliver any messages.
        # Visiting the SubscribeURL confirms the subscription.
        if msg_type == "SubscriptionConfirmation":
            confirm_url = body["SubscribeURL"]

            logger.info("Confirming SNS subscription.")

            try:
                requests.get(confirm_url)
                logger.info("SNS subscription confirmed.")
            except Exception as e:
                logger.error(f"Failed to confirm SNS subscription: {e}")
                raise

            return {"status": "confirmed"}

        if msg_type == "Notification":
            # The SNS message body contains the S3 event as a JSON string
            try:
                s3_event = json.loads(body["Message"])
            except Exception as e:
                logger.error(f"Failed to parse SNS message body: {e}")
                return {"status": "error"}
            
            for record in s3_event.get("Records", []):
                try:
                    key = record["s3"]["object"]["key"]
                    logger.info(f"S3 event received for object: {key}")

                    if key.startswith("raw/") and key.endswith(".csv"):
                        logger.info(f"Queueing processing task for file: {key}")
                        background_tasks.add_task(process_file, BUCKET_NAME, key)
                    else:
                        logger.info(f"Ignoring non-target object: {key}")
                except KeyError as e:
                    logger.error(f"Malformed S3 event record: {e}")

        return {"status": "ok"}
    
    except Exception as e:
        logger.error(f"Error handling SNS notification: {e}")
        raise HTTPException(status_code=500, detail="SNS processing failed")


# ── Query endpoints ───────────────────────────────────────────────────────────

@app.get("/anomalies/recent")
def get_recent_anomalies(limit: int = 50):
    """Return rows flagged as anomalies across the 10 most recent processed files."""
    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix="processed/")

        keys = sorted(
            [
                obj["Key"]
                for page in pages
                for obj in page.get("Contents", [])
                if obj["Key"].endswith(".csv")
            ],
            reverse=True,
        )[:10]

        all_anomalies = []

        for key in keys:
            try:
                response = s3.get_object(Bucket=BUCKET_NAME, Key=key)
                df = pd.read_csv(io.BytesIO(response["Body"].read()))

                if "anomaly" in df.columns:
                    flagged = df[df["anomaly"] == True].copy()
                    flagged["source_file"] = key
                    all_anomalies.append(flagged)

            except Exception as e:
                logger.error(f"Failed to process processed file {key}: {e}")

        if not all_anomalies:
            return {"count": 0, "anomalies": []}

        combined = pd.concat(all_anomalies).head(limit)

        return {"count": len(combined), "anomalies": combined.to_dict(orient="records")}

    except Exception as e:
        logger.error(f"Error retrieving recent anomalies: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve anomalies")


@app.get("/anomalies/summary")
def get_anomaly_summary():
    """Aggregate anomaly rates across processed files."""
    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix="processed/")

        summaries = []

        for page in pages:
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("_summary.json"):
                    try:
                        response = s3.get_object(Bucket=BUCKET_NAME, Key=obj["Key"])
                        summaries.append(json.loads(response["Body"].read()))
                    except Exception as e:
                        logger.error(f"Failed to read summary file {obj['Key']}: {e}")

        if not summaries:
            return {"message": "No processed files yet."}

        total_rows = sum(s["total_rows"] for s in summaries)
        total_anomalies = sum(s["anomaly_count"] for s in summaries)

        return {
            "files_processed": len(summaries),
            "total_rows_scored": total_rows,
            "total_anomalies": total_anomalies,
            "overall_anomaly_rate": round(total_anomalies / total_rows, 4) if total_rows > 0 else 0,
            "most_recent": sorted(summaries, key=lambda x: x["processed_at"], reverse=True)[:5],
        }

    except Exception as e:
        logger.error(f"Error generating anomaly summary: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate summary")


@app.get("/baseline/current")
def get_current_baseline():
    """Show the current per-channel statistics."""
    try:
        baseline_mgr = BaselineManager(bucket=BUCKET_NAME)
        baseline = baseline_mgr.load()

        channels = {}

        for channel, stats in baseline.items():
            if channel == "last_updated":
                continue

            channels[channel] = {
                "observations": stats["count"],
                "mean": round(stats["mean"], 4),
                "std": round(stats.get("std", 0.0), 4),
                "baseline_mature": stats["count"] >= 30,
            }

        return {
            "last_updated": baseline.get("last_updated"),
            "channels": channels,
        }

    except Exception as e:
        logger.error(f"Failed to load baseline: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve baseline")


@app.get("/health")
def health():
    try:
        return {"status": "ok", "bucket": BUCKET_NAME, "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail="Health check failed")
