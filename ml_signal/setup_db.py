"""
Run this script to create the ml_collection table in Supabase.

Usage:
    python ml_signal/setup_db.py
    
If you don't have the database password, open your Supabase Dashboard
SQL Editor and paste the contents of ml_signal/schema.sql instead.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_via_psql():
    password = os.getenv("SUPABASE_DB_PASSWORD", "")
    if not password:
        print("=" * 60)
        print("  SUPABASE_DB_PASSWORD not set.")
        print()
        print("  To create the table, either:")
        print("  1. Open Supabase Dashboard -> SQL Editor")
        print("     Paste the contents of ml_signal/schema.sql and run it.")
        print()
        print("  2. Set SUPABASE_DB_PASSWORD and re-run this script:")
        print("     export SUPABASE_DB_PASSWORD='your-password'")
        print("     python ml_signal/setup_db.py")
        print("=" * 60)
        return False

    import psycopg2

    project_ref = "mgenubvjbatpcpntlgav"
    host = f"db.{project_ref}.supabase.co"
    user = "postgres"
    dbname = "postgres"

    schema_path = os.path.join(
        os.path.dirname(__file__), "schema.sql"
    )
    with open(schema_path) as f:
        sql = f.read()

    try:
        conn = psycopg2.connect(
            host=host,
            port=5432,
            user=user,
            password=password,
            dbname=dbname,
            connect_timeout=10,
        )
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        cur.close()
        conn.close()
        print("Table 'ml_collection' created successfully.")
        return True
    except Exception as e:
        print(f"Failed to create table: {e}")
        return False


if __name__ == "__main__":
    run_via_psql()
