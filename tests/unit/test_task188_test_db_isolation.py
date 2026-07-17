"""TASK-188: unit tests must never be able to write to the production database.

tests/unit/test_storage.py calls Storage().log_signal(...) with fixture values
(SetupType.OI_WALL_REJECTION, BULLISH, trigger 24000.0, spot 24001.0). It
relies on an import-order-dependent patch of supabase.create_client. When that
patch lost the race, those fixtures were inserted into the live tables as
ares_signals ids 167-170 and nine trade_analytics rows, silently corrupting the
OI-wall P&L history.

conftest.pytest_sessionstart now points the Supabase credentials at unroutable
dummies for the whole session, so a real client cannot be built by accident.
These tests fail if that guard is removed or weakened.
"""
import unittest

from config import settings


class TestProductionDBIsolation(unittest.TestCase):

    def test_supabase_url_is_not_a_live_project(self):
        url = settings.supabase_url
        self.assertIn("invalid", url, f"tests must not point at a live Supabase project (got {url!r})")
        self.assertNotIn("supabase.co", url)

    def test_supabase_key_is_a_dummy(self):
        self.assertIn("test-key", settings.supabase_key)

    def test_dhan_credentials_are_dummies(self):
        self.assertTrue(settings.dhan_client_id.startswith("test-"))
        self.assertTrue(settings.dhan_access_token.startswith("test-"))

    def test_conftest_does_not_import_storage(self):
        """conftest must not import storage at session start.

        Importing it unmocked is the actual leak vector: it binds a real
        supabase client before tests/unit/test_storage.py can patch
        create_client, so that module's log_signal() fixtures land in
        production. Checked against the source rather than by import, since
        other test modules legitimately import storage later.
        """
        import pathlib
        conftest = (pathlib.Path(__file__).parent.parent / "conftest.py").read_text()
        code = "\n".join(
            line for line in conftest.splitlines() if not line.strip().startswith("#")
        )
        # Strip the module docstring, which names the offending call on purpose.
        body = code.split('"""')[-1]
        self.assertNotIn("from storage import", body)
        self.assertNotIn("import storage", body)


if __name__ == "__main__":
    unittest.main()
