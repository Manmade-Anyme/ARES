import re
import ast

with open('storage.py', 'r') as f:
    code = f.read()

# Make log_signal return verified success or raise
old_insert = """        def _insert():
            reasons = list(signal.reasons)
            if getattr(signal, "suggested_lots", None) is not None:
                reasons.append(
                    f"Option Sizing: {signal.suggested_lots} lots suggested | "
                    f"Capital: ₹{signal.capital:,.2f} | Risk: {signal.risk_pct:.1f}% | "
                    f"Option SL: ₹{signal.option_sl:.2f} | Option Target: ₹{signal.option_target:.2f} | "
                    f"Premium: ₹{signal.option_premium:.2f} | Delta: {signal.option_delta:+.4f}"
                )
            data = {
                "setup_type": signal.setup_type.value,
                "direction": signal.direction.value,
                "confidence": signal.confidence,
                "trigger_price": float(signal.trigger_price),
                "spot_at_signal": float(spot),
                "stop_loss": float(signal.stop_loss),
                "target_1": float(signal.target_1),
                "target_2": float(signal.target_2),
                "strike": signal.strike_to_trade,
                "option_type": signal.option_type,
                "reasons": reasons,
                "timestamp": to_utc_iso(signal.timestamp)
            }
            if getattr(signal, "oi_wall_context", None) is not None:
                data["oi_wall_context"] = signal.oi_wall_context

            response = self.supabase.table("ares_signals").insert(data).execute()
            if response.data:
                signal.db_id = response.data[0]["id"]
                print(f"[Storage] Logged signal {signal.signal_id} -> ares_signals.id={signal.db_id}")

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _insert)
        except Exception as e:
            print(f"Failed to log signal: {e}")"""

new_insert = """        def _insert():
            reasons = list(signal.reasons)
            if getattr(signal, "suggested_lots", None) is not None:
                reasons.append(
                    f"Option Sizing: {signal.suggested_lots} lots suggested | "
                    f"Capital: ₹{signal.capital:,.2f} | Risk: {signal.risk_pct:.1f}% | "
                    f"Option SL: ₹{signal.option_sl:.2f} | Option Target: ₹{signal.option_target:.2f} | "
                    f"Premium: ₹{signal.option_premium:.2f} | Delta: {signal.option_delta:+.4f}"
                )
            data = {
                "setup_type": signal.setup_type.value,
                "direction": signal.direction.value,
                "confidence": signal.confidence,
                "trigger_price": float(signal.trigger_price),
                "spot_at_signal": float(spot),
                "stop_loss": float(signal.stop_loss),
                "target_1": float(signal.target_1),
                "target_2": float(signal.target_2),
                "strike": signal.strike_to_trade,
                "option_type": signal.option_type,
                "reasons": reasons,
                "timestamp": to_utc_iso(signal.timestamp),
                "signal_uuid": signal.id,
                "display_id": signal.display_id,
            }
            if getattr(signal, "oi_wall_context", None) is not None:
                data["oi_wall_context"] = signal.oi_wall_context

            response = self.supabase.table("ares_signals").insert(data).execute()
            if response.data:
                signal.db_id = response.data[0].get("id")
                print(f"[Storage] Logged signal {signal.display_id} -> ares_signals.id={signal.db_id}")
                return signal.db_id
            raise RuntimeError("Failed to insert signal, no data returned")

        loop = asyncio.get_running_loop()
        db_id = await loop.run_in_executor(None, _insert)
        return True"""

code = code.replace(old_insert, new_insert)
code = code.replace("async def log_signal(self, signal: AresSignal, spot: float) -> None:", "async def log_signal(self, signal: AresSignal, spot: float) -> bool:")

with open('storage.py', 'w') as f:
    f.write(code)
