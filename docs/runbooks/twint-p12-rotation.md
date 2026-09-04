<!-- //// Neoffice — added file (no upstream equivalent). Rotating a TWINT merchant P12. The one
     //// fact worth reading twice: the certificate lives on neoservice ONLY — client
     //// instances hold its password (encrypted) and never the file. That split is why
     //// `Twint Bridge Settings` looks the way it does.
     //// Commits: 7cfe7fa 2026-05-13 "Phase 7 runbooks". -->
# Runbook — Rotate a TWINT Merchant P12 certificate

Applies when TWINT issues a new certificate (annual renewal or compromise).

The P12 lives on **neoservice.neoffice.me** only — never on client instances.
Client instances store the *password* (encrypted Frappe Password field) but
never the certificate file.

## Pre-requisites

- SSH access to `neoservice.neoffice.me` (TransHub server `neoservice`).
- A `Twint Bridge Settings` record on each client site using this merchant.
- The new P12 file + its password from TWINT.

## Step 1 — Backup the existing certificate

```bash
ssh neoservice
cd /home/frappe/twint-certs
mkdir -p .archive
cp -p <merchant_uuid>.p12 .archive/<merchant_uuid>-$(date +%F).p12
```

## Step 2 — Drop in the new certificate

```bash
# From your local machine, upload via TransHub MCP or scp:
scp ~/Downloads/new_<merchant_uuid>.p12 neoservice:/tmp/

ssh neoservice
sudo install -m 0600 -o frappe -g frappe /tmp/new_<merchant_uuid>.p12 /home/frappe/twint-certs/<merchant_uuid>.p12
shred -u /tmp/new_<merchant_uuid>.p12
```

Verify:
```bash
ls -l /home/frappe/twint-certs/<merchant_uuid>.p12
# -rw------- 1 frappe frappe ... <merchant_uuid>.p12
```

## Step 3 — Update the password on every client site

For each ERPNext client site that uses this merchant_uuid:

```bash
bench --site <site> execute frappe.client.set_value --kwargs '{
  "doctype": "Twint Bridge Settings",
  "name": "<merchant_uuid>",
  "fieldname": "p12_password",
  "value": "<new_password>"
}'
```

The password is encrypted on insertion (Frappe Password field).

## Step 4 — Verify

On any client site:

```bash
bench --site <site> execute neoffice_devops.api.twint.execute --kwargs '{
  "command": "health_check",
  "merchant_uuid": "<merchant_uuid>",
  "store_uuid": "<store_uuid>",
  "environment": "production",
  "params": "{}"
}'
```

Should return `{"success": true, "bridge_version": "...", "sdk_loaded": true, "cert_file_exists": true}`.

Then run a tiny live test transaction (CHF 0.50 if production, or sandbox amount):

```bash
bench --site <site> execute payments.api.intent.create_intent --kwargs '{
  "provider": "<your_twint_provider_name>",
  "channel": "qr_bridge",
  "amount": 50,
  "currency": "CHF",
  "metadata": "{\"twint_merchant_uuid\": \"<merchant_uuid>\"}"
}'
```

Scan the QR with the TWINT app, watch `payments.api.twint.poll_pending_twint_transactions` advance the FSM to `succeeded` within ~30 seconds.

## Rollback

If the new P12 doesn't work, restore the archived one:

```bash
ssh neoservice
sudo install -m 0600 -o frappe -g frappe \
  /home/frappe/twint-certs/.archive/<merchant_uuid>-YYYY-MM-DD.p12 \
  /home/frappe/twint-certs/<merchant_uuid>.p12
```

Then revert the password change on the client sites.

## References

- ADR-002 (`docs/adr/ADR-002-twint-via-php-bridge.md`)
- `neoffice_devops/api/twint.py` (proxy + permissions check)
- `neoffice-devops/twint_bridge.php` (CLI wrapper, reads `certificate_path`)
