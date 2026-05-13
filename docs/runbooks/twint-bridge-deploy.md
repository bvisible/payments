# Runbook — Deploy / Update the TWINT PHP Bridge on neoservice

Mirrors the EBICS deployment workflow (`scripts/update-neoservice.sh` already runs `composer install` for `ebics-client-php`; the TWINT bridge follows the same pattern via `scripts/install_twint.sh`).

## Initial install (one-time)

1. **Clone the branch** on neoservice:
   ```bash
   ssh neoservice
   cd /home/neoffice/frappe-bench/apps/neoffice_devops
   git fetch origin
   git checkout feat/twint-php-bridge   # or version-15 once merged
   git pull --ff-only
   ```

2. **Install PHP dependencies**:
   ```bash
   cd /home/neoffice/frappe-bench/apps/neoffice_devops
   ./scripts/install_twint.sh
   ```
   This runs `composer install --no-dev --optimize-autoloader --prefer-dist`
   and ensures `/home/frappe/twint-certs` exists with `0700` permissions.

3. **Add the TWINT Service User role** on the neoservice site:
   ```bash
   bench --site neoservice.neoffice.me add-user-role twint-bridge@example.com "TWINT Service User"
   ```
   (Or create the role first if not present.)

4. **Set site_config keys** on every client site that calls the bridge:
   ```bash
   bench --site <client> set-config twint_service_url "https://neoservice.neoffice.me"
   bench --site <client> set-config twint_api_key "<frappe-token-key>"
   bench --site <client> set-config twint_api_secret "<frappe-token-secret>"
   ```
   Alternatively, store these in the `Payment Provider.credentials_json` of each
   client (per-provider override).

## Health check

From neoservice itself:
```bash
ssh neoservice
cd /home/neoffice/frappe-bench/apps/neoffice_devops
php twint_bridge.php '{"command":"health_check","config":{"certificate_path":"/nonexistent","certificate_password":""},"params":{}}'
```

Expected response:
```json
{"success":false,"error":"Certificate path missing or unreadable"}
```
(That's healthy — the bridge loaded, parsed JSON, dispatched to `health_check`,
and properly reported the missing cert.)

From a client site:
```bash
bench --site <client> execute neoffice_devops.api.twint.execute --kwargs '{
  "command": "health_check",
  "merchant_uuid": "<existing_merchant>",
  "store_uuid": "<store_uuid>",
  "environment": "production",
  "params": "{}"
}'
```

Expected: `{"success": true, "sdk_loaded": true, "cert_file_exists": true}`.

## Update (when the bridge code changes upstream)

```bash
ssh neoservice
cd /home/neoffice/frappe-bench/apps/neoffice_devops
git fetch origin
git pull --ff-only
./scripts/install_twint.sh
```

The script is idempotent and re-runs Composer.

## Monitoring

- **Logs**: `frappe.log_error` captures any non-success response in
  `Stripe Webhook Log` (re-used for cross-provider logs) and `Error Log`.
- **Prometheus alert** (recommended once `Phase 7 monitoring` is wired):
  - latency p95 > 3 s
  - error rate > 1 %
  - bridge unavailable for > 60 s (status 5xx or connection refused)

## References

- ADR-002 (`docs/adr/ADR-002-twint-via-php-bridge.md`)
- `neoffice_devops/api/twint.py` (proxy)
- `neoffice-devops/twint_bridge.php` (CLI)
- `neoffice-devops/scripts/install_twint.sh` (deploy script)
- Pattern reference: EBICS bridge `neoffice-devops/ebics_bridge.php`
