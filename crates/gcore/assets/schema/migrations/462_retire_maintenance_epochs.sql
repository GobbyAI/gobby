DO $migration$
BEGIN
    EXECUTE FORMAT(
        'DROP EVENT TRIGGER IF EXISTS %I',
        'gobby_maintenance_epoch_login_' || SUBSTRING(MD5(current_schema()) FOR 16)
    );
END;
$migration$;

DROP FUNCTION gobby_maintenance_epoch_login_guard();

DROP TABLE destructive_batches;

DROP TABLE maintenance_epochs;
