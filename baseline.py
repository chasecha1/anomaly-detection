#!/usr/bin/env python3
import json
import math
import boto3
import logging
from datetime import datetime
from typing import Optional

s3 = boto3.client("s3")

LOG_FILE = "/var/log/anomaly_detection.log"

logger = logging.getLogger(__name__)


class BaselineManager:
    """
    Maintains a per-channel running baseline using Welford's online algorithm,
    which computes mean and variance incrementally without storing all past data.
    """

    def __init__(self, bucket: str, baseline_key: str = "state/baseline.json"):
        self.bucket = bucket
        self.baseline_key = baseline_key

    def load(self) -> dict:
        try:
            response = s3.get_object(Bucket=self.bucket, Key=self.baseline_key)
            logger.info("Baseline successfully loaded.")
            return json.loads(response["Body"].read())
        except s3.exceptions.NoSuchKey:
            return {}
        except Exception as e:
            logger.error(f"Unexpected error loading baseline: {e}")
            return {}

    def save(self, baseline: dict):
        try:
            baseline["last_updated"] = datetime.utcnow().isoformat()
            s3.put_object(
                Bucket=self.bucket,
                Key=self.baseline_key,
                Body=json.dumps(baseline, indent=2),
                ContentType="application/json"
            )
            logger.info("Baseline successfully saved.")
            self.sync_logs_to_s3()
        except Exception as e:
            logger.error(f"Unexpected error saving baseline: {e}")

    def sync_logs_to_s3(self):
        '''
        Upload the local application log file to S3.
        This runs whenever baseline.json is updated.
        '''
        try:
            log_key = "logs/anomaly_detection.log"

            logger.info(
                f"Uploading log file to s3://{self.bucket}/{log_key}"
            )

            s3.upload_file(LOG_FILE, self.bucket, log_key)

            logger.info("Log file successfully synced to S3.")
        except Exception as e:
            logger.error(f"Unexpected error uploading log file: {e}")

    def update(self, baseline: dict, channel: str, new_values: list[float]) -> dict:
        """
        Welford's online algorithm for numerically stable mean and variance.
        Each channel tracks: count, mean, M2 (sum of squared deviations).
        Variance = M2 / count, std = sqrt(variance).
        """
        try:
            if channel not in baseline:
                baseline[channel] = {"count": 0, "mean": 0.0, "M2": 0.0}

            state = baseline[channel]

            for value in new_values:
                state["count"] += 1
                delta = value - state["mean"]
                state["mean"] += delta / state["count"]
                delta2 = value - state["mean"]
                state["M2"] += delta * delta2

            # Only compute std once we have enough observations
            if state["count"] >= 2:
                variance = state["M2"] / state["count"]
                state["std"] = math.sqrt(variance)
            else:
                state["std"] = 0.0

            baseline[channel] = state
            return baseline
        except Exception as e:
            logger.error(f"Failed updating baseline for channel {channel}: {e}")
            return baseline


    def get_stats(self, baseline: dict, channel: str) -> Optional[dict]:
        return baseline.get(channel)
