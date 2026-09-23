from pathlib import Path
import re


MIGRATION = Path("migrations/2026-09-23-task152-exit-timestamp-validation-and-flagging.sql").read_text()


def test_flags_inverted_rows_before_adding_chronology_constraint():
    repair = "exit_timestamp < entry_timestamp"
    constraint = "ADD CONSTRAINT chk_trade_analytics_exit_chronology"
    assert repair in MIGRATION
    assert MIGRATION.index(repair) < MIGRATION.index(constraint)


def test_redefines_installed_entry_rpcs_with_active_entry_timestamp():
    assert "CREATE OR REPLACE FUNCTION create_trade_entry_bridge" in MIGRATION
    assert "CREATE OR REPLACE FUNCTION create_trade_entry_greenfield" in MIGRATION
    assert MIGRATION.count("entry_timestamp IS NOT DISTINCT FROM p_entry_timestamp") >= 4
    assert len(re.findall(r"direction,\s+entry_timestamp,\s+entry_price", MIGRATION)) == 2


def test_active_completeness_constraint_only_targets_terminal_states():
    expected = (
        "state NOT IN ('CLOSED', 'STOPPED_OUT') OR exit_timestamp IS NOT NULL "
        "OR time_metrics_excluded = true"
    )
    assert expected in MIGRATION
    assert "CHECK (state = 'OPEN' OR exit_timestamp IS NOT NULL" not in MIGRATION
