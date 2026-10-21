# Order identifiers and primary field V3.3

The user requested all historical and future country codes to follow the edited market directory:
Vietnam `VIE → VNM`, Philippines `PHI → PHL`. The order table primary must be `订单号`; `记录名称` is no longer needed.

## Live result

- Seven tables retained;96 order records,67 numbered and29 awaiting real order dates.
- All28 affected historical order numbers now useVNM/PHL, preserving their date segments and six-digit sequences.
- Country reservation keys were migrated transactionally, with collision detection and high-water preservation.
  Vietnam remains at5, Philippines at23; future allocation continues from those values.
- Batch names and country-specific order filters useVNM/PHL. Raw imported source snapshots remain original evidence.
- The existing primary field was converted to the order-number formula in place. This preserves linked-record identities.
  The obsolete second order-number formula was removed after dependency and value checks.
- State maps only `order_lines.order_number` to this primary field; no redundant `order_lines.__label` alias.
- Missing real order dates still produce blank order numbers. No replacement dates or surrogate order numbers were invented.
- Every recordID, link identity, sequence, date, quantity, price, business status, forecast and other protected business value was compared with the backup.
- All seven anonymous LAN views still read successfully; the order share displays the new primary and no `记录名称`.

## Migration and recovery

`scripts/migrate-order-identifiers.py` previews by default. It uses the normal `operations.lock`, saves a private seven-table snapshot,
backs up SQLite through its backup API, and stages the updated refresher/rules while holding the lock.
It sets `identifier_migration_pending` before mutations; the new refresher refuses pending migrations.
After a failure, inspect the private backups and rerun the same staged migration to resume. Do not delete reservations or reset counters.

Server staging: `/home/jjj/db_server_setup/identifiers-20261021/`.
Backups in `/home/jjj/db_server_setup/orders-import-20261020/`:

- `before-identifier-v33-private.json`: all original records, fields, views and state, including old primary labels.
- `before-identifier-v33.sqlite3`: original country counters and reservations.
- `before-identifier-v33-order_numbers.py` and `before-identifier-v33-refresh-order-operations.py`: previous runtime code.
- `identifier-v33-result-private.json`: verification summary.

These files stay private and are not committed. Field conversion and API writes are not one distributed transaction;
the lock, pending marker, backups and resumable checks cover partial failure rather than claiming atomic database rollback.

## Validation

16 numbering/aggregation tests passed, including code validation, reservation migration and collision rollback.
The live migration passed full protected-value verification and was rerun successfully without changing identities or sequences.
A subsequent stable refresh made sevenGET requests and zero application writes. The existing cron and night backup schedules remain unchanged.
