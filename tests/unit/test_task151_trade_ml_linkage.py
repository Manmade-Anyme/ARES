import unittest
import os
import re

class TestTask151TradeMlLinkage(unittest.TestCase):
    def test_a_migration_sql_uses_explicit_cast(self):
        sql_path = "migrations/2026-09-11-task151-trade-analytics-signal-id-text.sql"
        self.assertTrue(os.path.exists(sql_path))
        with open(sql_path, "r") as f:
            content = f.read()
        self.assertIn("USING signal_id::text", content)

    def test_active_trade_terminal_telemetry_has_schema_and_migration(self):
        migration_path = "migrations/2026-09-14-task151-active-trade-terminal-telemetry.sql"
        self.assertTrue(os.path.exists(migration_path))
        with open(migration_path, "r") as f:
            migration = f.read()
        with open("schema.sql", "r") as f:
            schema = f.read()
        with open("README.md", "r") as f:
            readme = f.read()

        for column in ("exit_price", "exit_type", "exit_timestamp", "pnl_points_override"):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", migration)
            self.assertIn(column, schema)
            self.assertIn(column, readme)

    def test_b_collision_resistance_in_ml_collection_update(self):
        with open("storage.py", "r") as f:
            content = f.read()
        self.assertIn('.is_("trade_id", "null")', content)
        self.assertIn('"signal_setup_type", record["setup_type"]', content)
        self.assertIn('.eq("id", closest[1]["id"])', content)

    def test_c_terminal_telemetry_persisted(self):
        with open("position_manager.py", "r") as f:
            content = f.read()
        self.assertIn('update_data["exit_price"] = float(event_price)', content)
        self.assertIn('update_data["exit_type"] = update_type', content)
        self.assertIn('update_data["exit_timestamp"] =', content)

    def test_d_data_type_compatibility(self):
        # 4 digit strings with leading zeros should work as text
        with open("models.py", "r") as f:
            content = f.read()
        self.assertIn("signal_id: str", content)

    def test_e_repair_be_after_t1_uses_market_context(self):
        with open("ml_signal/backfill_labels.py", "r") as f:
            content = f.read()
        self.assertIn('signal_db_id = (trade.get("market_context") or {}).get("signal_db_id")', content)

    def test_f_schema_setup_uses_text(self):
        with open("schema.sql", "r") as f:
            content = f.read()
        # Verify signal_id text is in trade_analytics
        self.assertTrue(re.search(r'CREATE TABLE trade_analytics \([^\)]*signal_id text', content, re.MULTILINE | re.DOTALL) or re.search(r'CREATE TABLE IF NOT EXISTS trade_analytics \([^\)]*signal_id text', content, re.MULTILINE | re.DOTALL))
        
        with open("README.md", "r") as f:
            readme = f.read()
        self.assertIn("signal_id text", readme)

if __name__ == "__main__":
    unittest.main()
