from src.state_db import StateDatabase


def test_state_database_creates_all_saas_tables(tmp_path):
    db = StateDatabase(tmp_path / "state.sqlite3")

    tables = db.table_names()

    assert {
        "jobs",
        "workflow_sessions",
        "tasks",
        "tenant_projects",
        "tenant_quotas",
        "tenant_audit",
    }.issubset(set(tables))
    assert db.schema_version == 2


def test_state_database_migration_is_idempotent(tmp_path):
    path = tmp_path / "state.sqlite3"

    first = StateDatabase(path)
    first_tables = first.table_names()
    first.close()
    second = StateDatabase(path)

    assert second.schema_version == 2
    assert second.table_names() == first_tables
    second.close()
