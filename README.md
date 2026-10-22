<!-- OneDrive SharePoint Backup - Revision 1 - 2026-10-22 09:10:00 -->

### Offsite Backups

Various apps to backup frappe

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app offsite_backups
```

ERPNext v16 uses the `version-16` branch. The `develop` branch tracks the next major version.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/offsite_backups
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade
### CI

This app can use GitHub Actions for CI. The following workflows are configured:

- CI: Installs this app and runs unit tests on every push to `develop` branch.
- Linters: Runs [Frappe Semgrep Rules](https://github.com/frappe/semgrep-rules) and [pip-audit](https://pypi.org/project/pip-audit/) on every pull request.


### OneDrive and SharePoint

OneDrive Settings backs up a Frappe site to the signed-in user's OneDrive or to a SharePoint document library. It follows the same pattern as Dropbox, S3, and Google Drive: a System Manager configures a single, authorizes the provider, and the long scheduler uploads the standard Frappe backup files.

Each successful run creates a dated folder:

```text
ERPNext Backup/20261022_090000/
    20261022_090000-<site>-database.sql.gz
    20261022_090000-<site>-site_config_backup.json
    20261022_090000-<site>-files.tar
    20261022_090000-<site>-private-files.tar
```

The database archive and the site config backup are always uploaded. Public and private file archives are uploaded only when **File Backup** is checked. When **Encrypt Backup** is enabled, Frappe inserts `-enc` before the extension. Compressed file archives use `.tgz` instead of `.tar`. The site config backup contains database credentials and other secrets. Keep the destination folder or library limited to the people who are allowed to restore the site.

Failed backups email the address in **Send Notification To**. Successful backups email that address only when **Send Email for Successful backup** is checked.

#### Azure app registration

1. Open [Microsoft Entra admin center](https://entra.microsoft.com) as someone who can register applications.
2. Go to **Identity > Applications > App registrations > New registration**.
3. Name the app `ERPNext Offsite Backup`.
4. For OneDrive personal and work accounts, choose **Accounts in any organizational directory and personal Microsoft accounts**. For a SharePoint site in one tenant, **Accounts in this organizational directory only** is the tighter choice.
5. Set the redirect platform to **Web** and paste the redirect URI shown on OneDrive Settings. It looks like `https://<your-site>/api/method/offsite_backups.offsite_backups.doctype.onedrive_settings.onedrive_settings.microsoft_oauth_callback`.
6. Register the app. Copy the **Application (client) ID** and **Directory (tenant) ID**.
7. Open **Certificates & secrets**, create a client secret, and copy the secret value immediately. It is shown only once.
8. Open **API permissions > Add a permission > Microsoft Graph > Delegated permissions** and add:
    - `offline_access`
    - `User.Read`
    - `Files.ReadWrite` for OneDrive
    - `Files.ReadWrite.All` and `Sites.Read.All` as well, when the target is SharePoint
9. For SharePoint, choose **Grant admin consent**. Those two extra permissions require an administrator. OneDrive's `Files.ReadWrite` usually does not.
10. In ERPNext, open **Offsite Backups > OneDrive Settings**. Enter the tenant, client ID, and client secret, or set them in `site_config.json` as `onedrive_tenant_id`, `onedrive_client_id`, and `onedrive_client_secret`. Values in site config override the form, and the secret fields are hidden when the client ID and secret are configured there.
11. Choose **OneDrive** or **SharePoint**. For SharePoint, paste the site URL, for example `https://contoso.sharepoint.com/sites/Backups`, and the document library name. The default library is `Documents`.
12. Enter the backup folder. `ERPNext Backup` or `ERPNext/Daily` are both valid. The folder is created if it is missing.
13. Choose Daily, Weekly, or Monthly, enable the single, save, and click **Authorize OneDrive Access**. Sign in as the account that should own the files. For SharePoint, that account must be able to write to the library.
14. Use **Take Backup** to run one upload immediately. After it finishes, **Last Backup On** is set and the dated folder is in the drive.

Switching between OneDrive and SharePoint, or changing the tenant or client ID, clears the saved sign-in. Authorize again so the token has the right permissions. Changing `onedrive_client_id`, `onedrive_client_secret`, or `onedrive_tenant_id` in site config does not clear a token by itself. Authorize again after those values change.

#### Frappe Cloud

Install `offsite_backups` on the private bench from the `version-16` branch, then install it on the site. Frappe Cloud sets `host_name` to the public `https` site URL. The Azure redirect URI must use that exact URL.

The scheduler is already running. Enable the single and pick a frequency. The upload runs on the long queue with the same 1500 second limit as Dropbox, S3, and Google Drive. A very large file archive can need a longer long-queue timeout in the bench config.

Outbound HTTPS must be allowed to `login.microsoftonline.com`, `graph.microsoft.com`, and the site's `sharepoint.com` (or `sharepoint.us` / `sharepoint.cn`) host. Frappe Cloud allows normal outbound HTTPS.

#### Retention

Turn on **Limit Number of Backups** to keep only the newest dated groups. The default keep count is 14. Files in the backup folder that do not use the Frappe timestamp prefix are left in place. Retention runs after the new backup has uploaded. A failure while deleting old folders is logged and does not mark the new backup as failed.

#### Security

- The client secret and refresh token are Password fields. Frappe encrypts them in `__Auth` with the site encryption key. A placeholder is stored on the Single so the form can show that a value exists.
- Sign-in uses authorization code with PKCE. The browser is sent back to the desk, and the code is exchanged by a POST from the logged-in System Manager. The code is not put in the desk URL.
- The OAuth `state` is random, stored server-side for 10 minutes, and deleted when it is used. It is bound to the user who started sign-in.
- Only a System Manager can authorize the app or queue a backup. The DocType permission is System Manager.
- Graph calls send the access token only to `graph.microsoft.com`. Upload slices go to the pre-authenticated Microsoft upload URL without that token. Redirects are not followed.
- Upload URLs and token values are redacted from the Error Log text this app writes.
- Prefer a dedicated backup account that can write only to the backup folder or SharePoint site. Do not point a SharePoint target at a personal OneDrive host (`*-my.sharepoint.com`).
- Restore the site config backup with the same care as `site_config.json`.

#### Restore

Download the dated folder and restore it with bench. Use the private file archive only when file backup was enabled:

```bash
bench --site <site> restore /path/to/database.sql.gz \
    --with-public-files /path/to/files.tar \
    --with-private-files /path/to/private-files.tar
```

If **Encrypt Backup** is enabled in System Settings, the site's backup encryption key is required before those archives can be unpacked. Copy `site_config_backup.json` back only when you intend to restore those secrets.

#### Troubleshooting

- **Redirect URI mismatch.** The Web redirect URI on the Azure app must match the value on OneDrive Settings, including `https`.
- **AADSTS65001 or admin consent.** SharePoint needs a tenant admin to grant `Files.ReadWrite.All` and `Sites.Read.All`.
- **Authorization expired.** The sign-in is single-use and lasts 10 minutes. Click **Authorize OneDrive Access** again.
- **No document library matches.** Use the library name shown in SharePoint, usually `Documents`.
- **Upload timed out.** The job retries twice. For large file archives, raise the long queue timeout or leave **File Backup** unchecked.
- **Personal Microsoft account and SharePoint.** SharePoint needs a work or school account. Use the OneDrive target for a personal Microsoft account.

### License

mit
