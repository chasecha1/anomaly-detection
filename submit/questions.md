# Answers to discussion questions

### 1. Technical challenges
The main challenge I faced in translating the template came from the lack of the `!Sub` in terraform. This made it very difficult to correctly write the bootstrap data for my EC2 instance. To solve this, I used `templatefile()` and added a `userdata.sh` file to the directory so that variables like my github url and the bucketname could be substituted into the template. I also ran into some trouble trying to set the AMI ID to the latest version of Ubuntu. I ultimately had to add a data source section to resolve this issue.

### 2. Access Permissions
The FastAPI accepts traffic on port 8000 from any IP address, so nothing specifically grants the SNS subscription permission to send messages since the security group allows traffic from anywhere. This ingress rule for the security group is found in `build.yaml` on lines 101-104 and specifies an IP of "0.0.0.0/0", or from anywhere. The subscription itself knows to send messages to the specific `/notify` endpoint because of line 209-212 of the same file, where I specify the endpoint and protocol.

### 3. Event flow and reliability
When a single csv is uploaded to `raw/` in s3, s3 detects the object created event and publishes a notification to the SNS ds5220-dp1 topic. SNS then sends this message out to the confirmed subscription of our `/notify` endpoint. FastAPI then receives this message, parses it, extracts the s3 object key, and begins to process the file.

If the EC2 instance is down or `/notify` returns an error, SNS has a built in retry policy and will retry with exponential backoff. After exhausting all retries, the message will be discarded from SNS as there is no dead letter queue for HTTP subscriptions.

If this needed to be production grade, we could introduce an SQS queue so instead of SNS pushing directly to the EC2 instance, it would push to SQS and the instance would poll the queue so that messages are durably stored even if the instance is down. 

### 4. IAM and least privilege
The application only performs three S3 operations, including `GetObject`, `PutObject`, `ListBucket`. In order to read raw csv files that are pushed to the bucket, the application needs `GetObject` permissions in order to pull these files. After processing, the application writes summaries, baseline, and the log file back to the bucket so it needs `PutObject` permissions. In addition, for the `/anomalies/recent` endpoint the application must be able to list the bucket in order to see the 10 most recent processed csv files.

The application never deletes objects and never updates versioning or bucket location, so these actions are not needed. Thus, you could replace the full access policy with a minimal set of permissions. To do so, you would need to remove any unnecessary actions from lines 66-72 of `build.yaml`

### 5. Architecture and scaling
If we needed to handle 100x more csv files an hour, several bottlenecks would emerge from the current architecture. Mainly, the single EC2 instance would be overwhelmed and notifications would arrive while the instance is still processing past files, leading to requests timing out or being dropped. Thus, the first thing I would do would be to decouple ingestion from processing by introducing an SQS queue between SNS and the compute layer. This would allow for horizontal scaling and multiple instances to poll the queue concurrently, scaling to accomodate the higher traffic.

However, with multiple consumers the shared `baseline.json` becomes a race condition with multiple instances trying to update the same file. To address this, we could keep the baseline state in a database such as DynamoDB with conditional writes to implement locking. We could also simply stop sharing the baseline state entirely and have each instance track its own local baseline, periodically merging statistics back to a central store.

Overall, the queue allows for horizontal scaling and concurrency when processing messages from SNS, however the increased number of instances does make tracking the shared statistics of `baseline.json` quite challenging.