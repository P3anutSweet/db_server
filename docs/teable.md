# Teable on thunderobotdb

Deployment status: **running; server-side LAN health and login page verified after Nginx restart**.
Target URL: `http://192.168.32.84` from the Windows computer on the same LAN. SSH and RDP are not needed for everyday use.

Server review on 2026-10-20 (`TEABLE-SERVER-20261020-C`): all 19 storage/deployment/backup tests pass;
standalone Compose 2.40.3 validation passes, including loopback-only port and persistent-mount checks.
The user ran the installer interactively. Docker, PostgreSQL, Redis and Teable started successfully.
At 13:06 UTC, the export restored successfully into the isolated check database: 49 source tables and 49 restored tables.
The WDC backup service completed successfully at 13:06:23 UTC, producing
`/srv/backups/db-server/backup-20261020T130622.810492Z` (confirmed by the service journal).
The operator cannot directly inspect root-only WDC archives or Docker state without sudo; archive checksums
and current container health have not been independently rechecked outside the installer's successful service run.

The original final HTTP check failed because reloading Nginx retained its old wildcard listener, which conflicted
with the new LAN-specific address. The user restarted Nginx; subsequent checks returned HTTP 200 for both
`http://192.168.32.84/health` and `/auth/login`, and `/` redirected to the Teable login page.
Only `192.168.32.84:80` and `127.0.0.1:3000` are listening; neither PostgreSQL nor Redis publishes a host port.
Nginx access logs also confirm actual cross-machine LAN access from `192.168.32.174`, including HTTP 200
for `/auth/login` and successful static-resource requests (no business data or request secrets were recorded here).
The installer now uses Nginx restart for this listener transition. No installation or backup was repeated by this chat.

`/var/lib/db-server/teable-export-status.json` exists and records the successful restore check.
The root-written `/var/lib/db-server/teable-status.json` is still absent because the original installer exited
at its final HTTP check. That missing summary does not prevent application use. Do not rerun the full installer
solely to generate this summary. The existing daily 03:00 Asia/Shanghai WDC timer remains enabled.

This installs the open-source Community edition using the official container, PostgreSQL 15 and Redis 7.4.
It does not install the optional AI/App Builder runtime or activate paid features.
The Windows chat is drafting the business structure from the two new Excel inputs independently;
the later [seven-market import](operations/2026-10-20-order-import.md) deployed the basic business tables and
historical order ledger through the supported API. Formal reconciliation and controlled fulfillment posting remain pending.

## Deploy

The storage setup must already be complete. The installer is restricted to `thunderobotdb`, validates both storage UUIDs,
checks the LAN IP and rejects conflicting ports on a fresh installation. It never formats disks.

From a checkout containing these files, at the Ubuntu terminal:

```bash
python3 scripts/deploy-teable.py
sudo python3 scripts/deploy-teable.py --apply
```

The first command shows the target without changing the server. The second installs Ubuntu's `docker.io`,
`docker-compose-v2` and `nginx` packages. It generates root-only credentials in `/etc/teable/.env`, pulls the images,
locks their immutable digests, starts the stack and runs the first database export/restore validation and WDC backup.
The packaged Nginx default-site symlink is disabled so that only the intended LAN listener remains.
The approved database and backup UUIDs are checked against the storage configuration before installation.
Dedicated attachment, mail-token, personal-access-token and database-URL encryption values are generated on this server,
instead of falling back to public upstream development defaults. All encryption values are preserved on re-runs;
an incomplete existing environment stops installation for review rather than silently rotating keys.
Re-running preserves existing credentials and pinned images. It is not an automatic image upgrade procedure.
Secrets and rendered `docker compose config` output must never be committed or pasted into chat.

Offline configuration validation, without reading `/etc/teable/.env` or contacting Docker:

```bash
python3 scripts/validate-teable-compose.py --compose-binary /home/jjj/db_server_setup/compose-validation/extracted/usr/libexec/docker/cli-plugins/docker-compose
python3 -m unittest discover -s tests -v
```

Persistent storage:

| Content | Location |
| --- | --- |
| PostgreSQL data | `/srv/db/teable/postgres` |
| Local attachments | `/srv/db/teable/assets` |
| Redis persistence | `/srv/db/teable/redis` |
| Root-only deployment and credentials | `/etc/teable` |
| Latest consistent export bundle | `/srv/db/exports/teable-latest.tar.gz` |
| Public deployment report | `/var/lib/db-server/teable-status.json` |
| Public export/restore-check report | `/var/lib/db-server/teable-export-status.json` |

Docker images and engine metadata remain on the operating-system disk. Database files, attachments and Redis persistence
use the dedicated 200GiB filesystem. The app uses local attachment storage; include it in every recovery plan.

## Browser access and operations

Open `http://192.168.32.84` and register your own first account. No application account password is generated or recorded by this installer.
The host Nginx proxy listens on the LAN IP, allows `192.168.32.0/24`, and proxies to Docker's loopback-only port 3000.
PostgreSQL and Redis have no published host ports. UFW is given a LAN-only HTTP rule; its existing rules remain.
Reserve the server's IP in DHCP before relying on this address. This is LAN HTTP; browser features requiring a secure
context, including some clipboard operations, need a later HTTPS/domain configuration.

```bash
sudo systemctl status teable nginx db-server-backup.timer --no-pager
sudo docker compose --project-directory /etc/teable --env-file /etc/teable/.env -f /etc/teable/compose.yaml ps
sudo systemctl restart teable
sudo systemctl start db-server-backup.service
```

The `teable` systemd unit depends on the database mount. Docker itself also has a mount guard, because container restart
policies can start containers before the app unit. If the expected disk is missing, containers must not write to the root disk.
Do not use `docker compose down -v`, delete data directories or replace encryption keys as routine troubleshooting.

## Backups and recovery limits

The existing daily 03:00 Asia/Shanghai backup timer (up to five minutes delay, seven completed generations) invokes
`teable-export` before archiving exports to the WDC disk. The app briefly stops while PostgreSQL and attachments are captured;
it restarts in a `finally` block even if stopping the app partially fails, or if a dump or restore check fails. A reserved `teable_restore_check` temporary database is
created, restored with `pg_restore --exit-on-error`, compared by table count and dropped. An existing database with that
name is preserved and aborts the export. The existing completed export is replaced atomically only after success.

The export includes a custom-format PostgreSQL dump, global roles, attachments, compose configuration and secrets.
Redis sessions/caches and queued jobs are not part of the recovery bundle. The WDC generation also contains `/etc`, package inventory,
recent logs, manifests and checksums. A failure before export completion fails the backup service; existing exports and
previous completed WDC generations remain available.

For recovery, verify the WDC generation's `SHA256SUMS`, extract its `database-exports.tar.gz` into a private staging directory,
and extract the Teable bundle there. Retain `deployment/.env` and its pinned images/encryption keys. On a clean matching
deployment, start PostgreSQL, recreate the application database and restore `teable.dump` as the database administrator.
Restore local attachments with UID/GID 1001 before starting the app; restore configuration and mount guards as well.
Check sign-in, record relationships and attachment downloads in the browser before accepting recovery.
Do not restore over a live working database. A full application-level recovery rehearsal is still required;
the automated test validates PostgreSQL restore, not every application workflow. An off-machine backup is still recommended.

## Sources

- [Official Docker deployment and Community image](https://help.teable.ai/en/deploy/docker)
- [Official Teable source](https://github.com/teableio/teable), including `apps/nestjs-backend/src/configs/storage.ts` and `scripts/start.sh`
- [Upstream encryption-secret configuration](https://github.com/teableio/teable/blob/develop/apps/nestjs-backend/src/configs/secrets/secret-specs.ts)
- [Docker and UFW behavior](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
