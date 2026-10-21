# Source-field cleanup and per-record change backups

User policy: keep current business records, remove original import/source copies from the active tables,
and back up each subsequent record change independently. The seven-table structure and existing VNM/PHL order-number primary remain.

## Prepared deployment

The scripts are staged in `/home/jjj/db_server_setup/record-changes-20261021/`.
At the time this note was written, production activation was pending interactive sudo authentication.
The command was queued in the existing server chat:

```sh
sudo python3 /home/jjj/db_server_setup/record-changes-20261021/record-change-backup.py install --apply --cleanup
```

The installer first enables and verifies capture, exports a current-business baseline to WD,
then invokes the separately reviewed cleanup through the Teable API as user jjj.
No original source cell values are copied into the new business baseline or change journal.
Existing historical archives and the user's original workbook files are outside this cleanup; this is not a forensic purge.

## Capture and delivery

- PostgreSQL triggers capture each committed INSERT, UPDATE and DELETE, including successive edits within one transaction.
- BEFORE TRUNCATE triggers capture each removed row when a watched table is cleared.
- Seven business tables and five existing many-to-many junction tables are covered. Field/table metadata is also journaled for these tables.
- Each event contains a stable eventID, timestamp, transactionID, table/record identity, and before/after values.
- Record projections include active business columns, system identity columns and foreign keys; removed/orphaned source columns are excluded.
  New ordinary fields in the same watched tables are picked up through active field metadata.
- A failed database transaction rolls back its audit rows too. Successful business writes and their local journal commit together.
- Root's systemd exporter copies durable event files to `/srv/backups/teable-record-changes/<instance>/YYYY/MM/DD/`.
- Files and directories are fsynced before per-event delivery acknowledgements are written to PostgreSQL.
  A crash before acknowledgement safely retries the same file. A conflicting file is never silently overwritten.
- Delivery uses outstanding eventIDs, not a maximum-ID checkpoint: a lowerID committing late is still delivered.
- The timer runs15seconds after the previous exporter completes. This is immediate transactional capture plus asynchronous WD copying,
  not a claim that a separate-disk copy exists at the instant of every commit.
- On WD/mount/export failure, local committed journal events remain pending for retry. There is no automatic journal pruning.

The database, WD mount UUIDs and free space are checked. Private event files are root-only; no backup API tokens are embedded.
The existing nightly consistent full-database/attachment export and restore validation remain enabled.
The row journal complements that backup; it is not an automatic one-click application recovery system.

## Precise cleanup

`tools/schema/source_cleanup.py` defines the explicit retirement policy; unknown user-added fields are protected.
The current preflight identifies45 fields: original quantities/states/dates/EAN/notes, raw row/plan JSON, import keys,
old source groupings, and migration/source markers. Current quantities, prices, forecasts, statuses, followups,
customer requirements, SKU/customer/market/tag fields, order numbers and reservation inputs remain.

`scripts/remove-source-fields.py` requires root-owned successful capture/export status for all seven tables.
It checks business-field and view dependencies, locks the regular refresher, saves a current-business-only baseline,
installs the source-independent refresher, removes the source view and approved columns, and verifies every protected value and record/linkID.
It renames `全部订单台账` to `全部订单`; `原始资料` is no longer recreated.
An incomplete cleanup blocks regular refresh and can be resumed after review. A completed cleanup can be rerun without comparing later edits to an old baseline.

Original import files are no longer required by routine operation. Do not run the historical importer or old migrations to recreate retired source columns.
When adding a new business table or a new many-to-many relationship, update and install the capture manifest before using its new physical table.
The current manifest is restricted to the approved seven-table database.

## Verification completed before administrator activation

- Dependency preflight:45 source-only fields, no retained field or business view dependency.
- Four retirement-policy tests protect business values, identifier inputs and user-added fields.
- In-memory cleaned-schema validation:96 orders and77 monthly records refreshed with zero application writes.
- An isolated unprivileged PostgreSQL18 cluster, with TCP disabled, tested real SQL and real fsynced file export.
  Passed: baseline/idempotence, rapid INSERT/UPDATE/UPDATE/DELETE, rollback, links, TRUNCATE,
  source exclusion after physical-column rename, new ordinary fields, crash before acknowledgement, late commit and repeat export.
- Production uses PostgreSQL15. Its installer runs rollback-only trigger tests before declaring readiness and before removing source fields.
- CI runs the integration harness on an isolated PostgreSQL16 cluster. It never points at a production database.

After activation inspect `/var/lib/db-server/record-change-status.json`, `db-record-changes.timer` and
the private cleanup result. `actor_hint` comes from the record's metadata; it does not reliably identify the person deleting a row.
Database timestamps include their offset; convert to Asia/Shanghai for user-facing reports.
