#!/bin/bash
set -e
# Redirect all output and errors to a bootstrap log
exec > /var/log/bootstrap.log 2>&1

echo "=== System update and package install ==="
apt-get update -y
apt-get install -y git python3 python3-pip python3-venv

echo "=== Set BUCKET_NAME environment variable ==="
# Available immediately in this script (templatefile substitutes the value at plan time)
export BUCKET_NAME="${bucket_name}"
# Persist for all future logins and reboots
echo "BUCKET_NAME=${bucket_name}" >> /etc/environment

echo "=== Clone forked repository ==="
git clone ${git_repo_url} /opt/anomaly-detection

echo "=== Create Python virtual environment ==="
python3 -m venv /opt/anomaly-detection/venv

echo "=== Install Python dependencies ==="
/opt/anomaly-detection/venv/bin/pip install --upgrade pip
/opt/anomaly-detection/venv/bin/pip install -r /opt/anomaly-detection/requirements.txt

echo "=== Start FastAPI ==="
nohup /opt/anomaly-detection/venv/bin/fastapi run /opt/anomaly-detection/app.py --host 0.0.0.0 > /var/log/fastapi-stdout.log 2>&1 &

echo "=== Bootstrap complete ==="
