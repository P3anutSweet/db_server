# Storage and backup operations

This procedure is specific to **thunderobotdb**, Ubuntu user `jjj`, at `192.168.32.84`.
The existing 100GiB root logical volume stays in place. All sizes below use binary GiB.

## Target layout

| Purpose | Device | Size | Filesystem | Mount |
| --- | --- | --- | --- | --- |
| Ubuntu root | `ubuntu-vg/ubuntu-lv` | 100GiB, existing | ext4 | `/` |
| Database | `ubuntu-vg/database-data` on the NVMe SSD | 200GiB, new | ext4, 1% reserved | `/srv/db` |
| Backup | Western Digital disk, serial `WD-WX92AC2FMJ29` | Entire 1TB disk | GPT + one ext4 partition, no reserved percentage | `/srv/backups` |

The database volume is allocated only from the existing NVMe LVM physical volume.
Approximately 174GiB should remain in the volume group; the privileged preflight and final report confirm the exact amount.
Filesystem overhead and reserved blocks make filesystem available space smaller than logical-volume size.
Database engine files will live under `/srv/db/data`; completed database exports go under `/srv/db/exports`.
The database data directory starts root-owned; configure its ownership and service mount dependency when installing the chosen database engine.

**Applying this procedure clears the old Western Digital BitLocker partition and all files on it.**
Formatting creates an unencrypted ext4 backup filesystem; no BitLocker unlock/recovery password is required to replace it.
This is a filesystem reset, not a secure overwrite of every disk sector.

## Apply

Keep all files from this repository together. Both scripts and the `systemd/` files are required.
Run the script as root because reading LVM metadata and changing filesystems requires administrator access.
Without `--apply`, the script performs a read-only privileged preflight:

```bash
sudo python3 scripts/setup-storage.py
```

To perform the authorized setup:

```bash
sudo python3 scripts/setup-storage.py --apply --erase-backup WD-WX92AC2FMJ29
```

The script verifies the hostname, exact WDC serial/capacity, original BitLocker UUID,
SSD LVM physical-volume UUID, free space, existing mounts and any conflicting fstab entries before changes.
It refuses to format an existing database volume. Successful reruns reuse the recorded filesystem UUIDs.
An interrupted format that leaves unknown filesystem state requires inspection rather than automatic retry.

It saves the original fstab, LVM metadata and before/after inventories in
`/var/backups/db-server-setup/<UTC timestamp>/`. These are configuration records, not a copy of the old WDC data.
The script mounts both filesystems by UUID, installs the backup service and timer, and runs the first backup.
No reboot is required. A public, credential-free result is written to `/var/lib/db-server/storage-status.json`.

## Backup schedule and contents

- Daily at **03:00 Asia/Shanghai**, with up to five minutes of scheduling jitter.
- `Persistent=true` catches up a missed run when the server next becomes available.
- Retain the **latest seven completed backups**, under `/srv/backups/db-server/`.
- A backup disk with less than 5GiB free causes failure and preserves existing backups.
- Each backup includes `/etc`, the backup program, installed-package inventory,
  the last 24 hours of system journal, and completed files from `/srv/db/exports`.
- In-progress `.partial`, `.tmp` and hidden export files are excluded. Export producers should write a temporary file and atomically rename it only after successful completion.
- A manifest, archive readability checks and SHA-256 checksums accompany each completed backup.
- Backups and configuration archives are root-only because they can contain credentials.
- Both filesystem UUIDs must match the recorded configuration; a missing WDC mount cannot silently fill the SSD/root filesystem.

**Database dump generation and database restore testing are not configured yet.**
The database engine has not been selected or installed. The first backup therefore protects configuration and logs;
an empty export archive does not mean database contents have been backed up.
Add an engine-specific consistent export (`pg_dump`, `mysqldump`, or an appropriate physical-backup tool)
before treating this as database recovery protection. Do not tar a live database data directory as a substitute.
The WDC disk is an on-machine copy; off-machine backups are a separate future step.

## Check and restore

```bash
df -hT / /srv/db /srv/backups
sudo vgs --units g
cat /var/lib/db-server/storage-status.json
systemctl list-timers db-server-backup.timer
sudo journalctl -u db-server-backup.service -n 60 --no-pager
sudo systemctl start db-server-backup.service
```

Inspect a completed backup as root. Use the actual timestamp directory returned by the backup job:

```bash
cd /srv/backups/db-server/backup-ACTUAL_TIMESTAMP
sudo sha256sum -c SHA256SUMS
sudo tar -tzf configuration.tar.gz
sudo tar -tzf database-exports.tar.gz
```

For restore drills, extract to a new empty directory and inspect it before replacing live configuration.
Do not extract `/etc` directly onto a running system. Database exports need the matching engine's restore procedure.
After changing filesystem UUIDs during a recovery, update fstab and `/etc/db-server/storage.json` together.

## Sources

- [Ubuntu LVM administration](https://ubuntu.com/server/docs/how-to/storage/manage-logical-volumes/)
- [Ubuntu lvcreate reference](https://manpages.ubuntu.com/manpages/noble/man8/lvcreate.8.html)
- [systemd timers](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)
