<!-- //// Neoffice — added file (no upstream equivalent). Rotating a TWINT merchant P12. Since
     //// 19.10.2026 (maintenance#1721) the certificate lives in two places: encrypted on each client
     //// instance, for its own TWINT engine (payments.twint_engine), and on neoservice, for the hub's
     //// PHP bridge (the way back). The instance holds its password (encrypted) too.
     //// Commits: 7cfe7fa 2026-05-13 "Phase 7 runbooks". -->
# Runbook — Rotate a TWINT Merchant P12 certificate

Applies when TWINT issues a new certificate (annual renewal or compromise).

The P12 lives on **each client instance**, encrypted with the site's key (for the instance's own TWINT
engine, `payments.twint_engine`, the default since 19.10.2026), and on **neoservice.neoffice.me** (for the
hub's PHP bridge, the way back: `twint_local_engine: false`). Client instances also store the *password*
(encrypted Frappe Password field).

A certificate replaced on neoservice by hand (the steps below) reaches the instances by itself: a copy kept
on an instance that no longer opens with the record's password is replaced, at the next TWINT call, by the
one neoservice stores, which neoservice hands over only for the password that opens it. So the order is: the
file on neoservice, then the new password on each record (Step 4).

## Pre-requisites

- SSH access to `neoservice.neoffice.me` (the `neoservice` server of the ssh-manager).
- A `Twint Bridge Settings` record on each client site using this merchant.
- The new P12 file + its password from TWINT.

## Normal way: the form

Open the merchant's `Twint Bridge Settings`, attach the new P12 and type the new password, save. The form
keeps the file encrypted on the instance, pushes it to neoservice and removes the attached file. Replacing a certificate that is already stored needs
the **password of the stored one**: the form sends it by itself (it is the password the record held before
this save), and neoservice refuses the replacement otherwise (the service key is the same on every till, so
it does not say whose certificate a call is about). The same goes for deleting the record.

If the form is refused with "A certificate is already stored for this merchant_uuid" (the record was deleted
or retyped while the file stayed on neoservice, or the stored password was never right), a person with SSH
access removes the stored file (Step 1 keeps a copy) and saves the form again. The steps below are that
manual way, and the way back.

## Step 1 — Backup the existing certificate

```bash
ssh neoservice
cd /home/neoffice/twint-certs
mkdir -p .archive
cp -p <merchant_uuid>.p12 .archive/<merchant_uuid>-$(date +%F).p12
```

## Step 2 — Drop in the new certificate

```bash
# From your local machine, upload with the ssh-manager or scp:
scp ~/Downloads/new_<merchant_uuid>.p12 neoservice:/tmp/

ssh neoservice
sudo install -m 0600 -o neoffice -g neoffice /tmp/new_<merchant_uuid>.p12 /home/neoffice/twint-certs/<merchant_uuid>.p12
shred -u /tmp/new_<merchant_uuid>.p12
```

Verify:
```bash
ls -l /home/neoffice/twint-certs/<merchant_uuid>.p12
# -rw------- 1 neoffice neoffice ... <merchant_uuid>.p12
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
sudo install -m 0600 -o neoffice -g neoffice \
  /home/neoffice/twint-certs/.archive/<merchant_uuid>-YYYY-MM-DD.p12 \
  /home/neoffice/twint-certs/<merchant_uuid>.p12
```

Then revert the password change on the client sites.

## References

- ADR-002 (`docs/adr/ADR-002-twint-via-php-bridge.md`)
- `neoffice_devops/api/twint.py` (proxy + permissions check)
- `neoffice-devops/twint_bridge.php` (CLI wrapper, reads `certificate_path`)
