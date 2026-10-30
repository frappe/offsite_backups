# Offsite Backups

Automate offsite backups for Frappe and ERPNext sites to cloud storage providers.

Supported storage backends:
- **S3 & Compatible Object Storage**:
  - **Oracle Cloud Infrastructure (OCI) Object Storage**
  - **Amazon S3**
  - **Cloudflare R2**
  - **MinIO** (Self-hosted)
  - **Wasabi**
  - **Backblaze B2**
  - **DigitalOcean Spaces**
  - **Google Cloud Storage** (via S3 Interoperability)
  - **Custom / Self-hosted S3-compatible Storage** (Ceph, Scaleway, etc.)
- **Google Drive**
- **Dropbox**

---

## Installation

Install using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/frappe/offsite_backups --branch develop
bench install-app offsite_backups
```

---

## New Features & Recent Fixes

### 1. Universal S3-Compatible Object Storage & Oracle Cloud (OCI) Support
- **Support for Any S3 Service**: Native compatibility with Oracle Cloud Object Storage, Cloudflare R2, MinIO, Wasabi, Backblaze B2, and standard AWS S3.
- **Configurable Addressing Style**: Supports both `path` (strictly required for OCI & MinIO) and `virtual` hosted addressing.
- **Region & Checksum Handling**: Configurable `region_name` and compatible botocore checksum parameters to prevent OCI `MissingContentLength` and SigV4 signature mismatches.
- **Test Connection Button**: Immediately verify credentials and bucket access from the Desk form without waiting for scheduled runs.

### 2. Backup Rotation (Automated Retention Policy)
- Automatically deletes older backup sets from S3 / Object Storage buckets based on a configurable **Retention Count**, preventing unbounded storage growth and unexpected cloud costs.

### 3. Flexible Backup Frequencies
- Added **Hourly**, **Daily**, **Weekly**, and **Monthly** schedules across S3, Dropbox, and Google Drive integrations, hooked directly into Frappe's background scheduler.

### 4. Async Execution from Desk UI
- The **Take Backup Now** button queues backups as asynchronous background jobs in Frappe's worker queue (`queue="long"`), preventing browser timeouts and HTTP 504 Gateway errors during large site dumps.

### 5. Multi-Tenant Site Identification in Alerts
- Email notifications now include the active site name (`[sitename]`) in both the email subject and body, making it easy to identify which ERPNext instance generated the notification.

### 6. Dropbox & Google Drive Fixes
- **Google Drive OAuth Callback Fix**: Corrected OAuth callback domain to `google_drive` to comply with modern Frappe callback domain validation.
- **Dropbox Orphaned File Cleanup**: Deletion of old database backups in Dropbox now cleans up both `.sql.gz` database dumps and matching `.json` site configuration backups.
- **Dropbox Account Re-sync**: Changing the Dropbox app key or re-authorizing resets the local file upload state so all files synchronize to the new destination.

### 7. Security Hardening
- **Access Control**: Enforced strict `System Manager` role checks on all whitelisted backup triggers, OAuth flows, and connection test endpoints.
- **Secure Token Storage**: Google Drive refresh tokens are stored using Frappe's encrypted `Password` fieldtype.
- **Path Traversal Protection**: Bucket paths are sanitized to prevent directory traversal escapes.
- **HTML Escaped Email Logs**: Error tracebacks sent via email notifications are HTML-escaped to prevent script injection.

---

## Configuring S3 & Object Storage Backups

Open ERPNext / Frappe Desk and search for **S3 Backup Settings**.

### Oracle Cloud Infrastructure (OCI) Object Storage Setup

Oracle Cloud Object Storage provides an Amazon S3 Compatibility API that works seamlessly with this app.

#### Step A: Generate Customer Secret Key in OCI
1. Log in to the **Oracle Cloud Infrastructure (OCI) Console**.
2. Click your user profile icon (top right) and choose **User Settings** (or **My Profile**).
3. Under the **Resources** menu on the left, click **Customer Secret Keys**.
4. Click **Generate Secret Key**, enter a description (e.g. `erpnext-backup`), and click **Generate Secret Key**.
5. Copy the generated **Secret Key** immediately (it is only displayed once) and the generated **Access Key**.

#### Step B: Find your Namespace and Region
1. In the OCI Console, click your profile icon and select **Tenancy: <tenancy-name>**.
2. Note your **Object Storage Namespace** (e.g., `axxxxxxxx`).
3. Note your Region identifier from the top navigation bar (e.g., `us-ashburn-1`, `ap-mumbai-1`, `eu-frankfurt-1`).

#### Step C: Create a Bucket
1. Navigate to **Storage** > **Object Storage & Archive Storage** > **Buckets**.
2. Click **Create Bucket**, give it a name (e.g. `erpnext-backups`), and create it.

#### Step D: Configure in Frappe / ERPNext
In **S3 Backup Settings**:
- **Enable Automatic Backup**: Checked
- **Storage Service Provider**: `Oracle Cloud Infrastructure (OCI)`
- **Access Key ID**: Your OCI Customer Secret Key (Access Key)
- **Access Key Secret**: Your OCI Customer Secret Key (Secret Key)
- **Bucket Name**: Your OCI bucket name (e.g. `erpnext-backups`)
- **Endpoint URL**: `https://<namespace>.compat.objectstorage.<region>.oraclecloud.com`
  *(Replace `<namespace>` with your tenancy namespace and `<region>` with your region code, e.g. `https://axxxxxxxx.compat.objectstorage.us-ashburn-1.oraclecloud.com`)*
- **Region**: Your OCI region identifier (e.g. `us-ashburn-1`)
- **Addressing Style**: `path` *(OCI strictly requires path-style addressing; selected automatically)*
- **Backup Frequency**: Hourly, Daily, Weekly, or Monthly
- **Enable Backup Rotation**: Checked (set desired retention count, e.g. 5)
- **Send Notifications To**: Your alert email address
- Click **Save**, then click **Test Connection** to verify access!

---

### Other Object Storage Providers

#### Cloudflare R2
- **Storage Service Provider**: `Cloudflare R2`
- **Endpoint URL**: `https://<account_id>.r2.cloudflarestorage.com`
- **Region**: `auto`
- **Addressing Style**: `auto`
- **Credentials**: Created under Cloudflare Dashboard > R2 > Manage R2 API Tokens.

#### MinIO (Self-Hosted)
- **Storage Service Provider**: `MinIO`
- **Endpoint URL**: `http(s)://<minio-server-host>:9000`
- **Region**: `us-east-1`
- **Addressing Style**: `path`
- **Verify SSL Certificate**: Uncheck if using internal self-signed SSL certificates.

#### Wasabi
- **Storage Service Provider**: `Wasabi`
- **Endpoint URL**: `https://s3.<region>.wasabisys.com` (e.g., `https://s3.us-east-1.wasabisys.com`)
- **Region**: e.g., `us-east-1`, `eu-central-1`

#### Backblaze B2
- **Storage Service Provider**: `Backblaze B2`
- **Endpoint URL**: `https://s3.<region>.backblazeb2.com` (e.g., `https://s3.us-west-004.backblazeb2.com`)
- **Region**: e.g., `us-west-004`
- **Credentials**: Application Key ID & Application Key (with S3 API enabled).

#### Amazon S3
- **Storage Service Provider**: `Amazon S3`
- **Endpoint URL**: `https://s3.amazonaws.com` (or regional endpoint)
- **Region**: e.g. `us-east-1`, `eu-west-1`, `ap-south-1`
- **Addressing Style**: `auto`

---

## Contributing

This app uses `pre-commit` for code formatting and linting:

```bash
cd apps/offsite_backups
pre-commit install
```

Tools configured:
- ruff
- eslint
- prettier
- pyupgrade

---

## License

MIT
