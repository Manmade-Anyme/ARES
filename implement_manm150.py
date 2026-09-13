import re
import os

def update_schema_sql():
    with open('schema.sql', 'r') as f:
        content = f.read()

    # Add signal_uuid bridge column to ares_signals
    content = content.replace("timestamp timestamptz,", "signal_uuid uuid UNIQUE,\n  display_id text,\n  timestamp timestamptz,")
    
    # Update active_trades
    content = content.replace("signal_id text,", "signal_id text,\n  signal_uuid uuid,")
    
    # Update trade_analytics
    content = content.replace("signal_id bigint, -- Optional link to ares_signals", "signal_id bigint, -- Optional link to ares_signals\n  signal_uuid uuid,")

    with open('schema.sql', 'w') as f:
        f.write(content)
        
def update_ml_schema():
    with open('ml_signal/schema.sql', 'r') as f:
        content = f.read()
    content = content.replace("signal_id text,", "signal_id text,\n  signal_uuid uuid,\n  signal_display_id text,\n  trade_binding_status text,")
    with open('ml_signal/schema.sql', 'w') as f:
        f.write(content)

update_schema_sql()
update_ml_schema()
print("Schemas updated.")
