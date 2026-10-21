# Anonymous read-only LAN access

The user approved access without login for company LAN visitors on 2026-10-21.
The visit at11:16 Shanghai from192.168.35.227 failed with Nginx403 (`access forbidden by rule`), before Teable authentication.

## Access policy

- Known company networks:192.168.32.0/24 and192.168.35.0/24. Other networks retain `deny all`.
- Entry: https://192.168.32.84/read . Exact `/` redirects to this entry after installation.
- Seven native Teable grid views named `局域网只读`, one per active table, require no account.
- Existing visible business columns are included. Hidden source/import/automation columns are excluded.
- Sharing flags: `allowCopy=false`, `includeHiddenField=false`, `includeRecords=false`, `allowEdit=false` where supported.
  The installed Community build omits the newer `allowEdit` flag; its shared grid route is read-only.
- Editing still requires an authenticated staff account with an existing Teable role. No collaborator roles or accounts were changed.
- New visitors in the two permitted subnets need no individual-IP approval. Additional company subnets require an explicit network update.
- LAN restriction comes from Nginx/firewall, not the share URL. Keep the Docker app bound to127.0.0.1:3000.

## Setup and activation

Run `scripts/setup-lan-reader.py` on the server to preview; add `--apply` to create/reuse the views and verify anonymous access.
It reads the existing token from its private server file, never embeds it in HTML, and saves before-state and share IDs privately in
`/home/jjj/db_server_setup/lan-reader-20261021/`. A failed per-view verification disables that share.

The root installer defaults to a diff-only preview. Activation needs an interactive administrator terminal:

```sh
sudo python3 /home/jjj/db_server_setup/install-lan-reader.py --apply
```

It copies the token-free index to `/var/www/teable-read/index.html`, adds exact reader routes, replaces the one-client rule with35/24,
validates and reloads Nginx, then adds scoped UFW rules for TCP80/443. TLS and existing proxy routes remain in place.
Nginx and any prior HTML are backed up under `/etc/teable/config-backups/lan-reader-*`;
validation/reload failure restores the previous configuration and HTML. Firewall failures are reported separately.

At preparation time, all seven shared-view APIs were verified anonymously; hidden-column projections were blocked or omitted,
and all seven unauthenticated record-create probes returned401. Re-running setup reused the same views.
Five configuration tests passed: subnet/deny rules, idempotence, TLS/proxy preservation, unexpected config rejection, and conflicting-route rejection.
The final root activation was still pending user authentication when this note was written.

After activation, verify `/` redirects to `/read`, the reader returns200 over trusted HTTPS, staff base pages still load,
and the newly permitted visitor can open a share. Browser trust in the existing local certificate authority remains a separate requirement.

Activation follow-up: the entry was subsequently verified at200 over trusted HTTPS and contains the intended reader iframe. All seven anonymous views were rechecked after the V3.3 identifier migration.
