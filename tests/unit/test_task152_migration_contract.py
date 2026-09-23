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


def test_active_exit_type_backfill_does_not_mark_open_trades_terminal():
    terminal_states = (
        "'CLOSED', 'T2_HIT', 'SL_HIT', 'STOPPED_OUT',\n"
        "                'STOPPED_OUT_AT_BE'"
    )
    assert f"analytics.result_state IN (\n                {terminal_states}" in MIGRATION
    assert "coalesce(active.exit_type, analytics.result_state)" not in MIGRATION


def test_analytics_exclusions_propagate_to_matching_active_rows():
    analytics_repair = "UPDATE trade_analytics\nSET time_metrics_excluded = true"
    propagation = """UPDATE active_trades AS active
SET time_metrics_excluded = true
FROM trade_analytics AS analytics
WHERE active.id = analytics.id
  AND analytics.time_metrics_excluded = true;"""
    active_constraint = "ADD CONSTRAINT chk_active_trades_exit_chronology"

    assert propagation in MIGRATION
    assert MIGRATION.index(analytics_repair) < MIGRATION.index(propagation)
    assert MIGRATION.index(propagation) < MIGRATION.index(active_constraint)
