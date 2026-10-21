"""Explicit source-only field retirement policy; current business fields stay."""
REMOVE = {
    'order_lines': {'__key', 'line_number', 'description_snapshot', 'ordered_qty', 'base_uom_snapshot',
        'estimated_price', 'price_currency_state', 'review_state', 'review_notes',
        'migration_marker', 'followup_ids'},
    'order_batches': {'__key', 'source_batch_label', 'channel'},
    'markets': {'__key', 'review_state', 'region_provenance'},
    'skus': {'__key', 'pid_raw', 'review_state'},
    'customers': {'__key'}, 'tags': {'__key'},
    'monthly_overview': {'__key', 'forecast_source', 'forecast_initialized'},
}


def retired(table, key):
    return bool(key) and (key.startswith('source_') or key in REMOVE.get(table, set()))
