import re

# 1. Update PositionManager.add_trade
with open('position_manager.py', 'r') as f:
    code = f.read()

code = code.replace("def add_trade(self, signal: AresSignal, spot: float, atm: ATMStrikes = None):", "async def add_trade(self, signal: AresSignal, spot: float, atm: ATMStrikes = None) -> tuple[str, str]:")

old_add_trade_return_1 = """            if (existing.get("setup_type") == signal.setup_type.value
                    and existing.get("direction") == signal.direction.value
                    and abs(float(existing.get("entry_price", 0.0)) - float(spot)) <= settings.trade_dedupe_tolerance_pts):
                print(f"[-] PositionManager: Duplicate {signal.setup_type.value} ({signal.direction.value}) trade at {spot:.2f} skipped — already tracking {existing['id']}.")
                return"""
new_add_trade_return_1 = """            if (existing.get("setup_type") == signal.setup_type.value
                    and existing.get("direction") == signal.direction.value
                    and abs(float(existing.get("entry_price", 0.0)) - float(spot)) <= settings.trade_dedupe_tolerance_pts):
                print(f"[-] PositionManager: Duplicate {signal.setup_type.value} ({signal.direction.value}) trade at {spot:.2f} skipped — already tracking {existing['id']}.")
                return "", "DUPLICATE_SKIPPED" """
code = code.replace(old_add_trade_return_1, new_add_trade_return_1)

old_add_trade_body = """        trade_id = str(uuid.uuid4())
        trade_data = {
            "id": trade_id,
            "signal_id": getattr(signal, "signal_id", f"{__import__('random').randint(0, 9999):04d}"),
            "setup_type": signal.setup_type.value,
            "direction": signal.direction.value,
            "entry_price": float(spot),
            "stop_loss": float(signal.stop_loss),
            "target_1": float(signal.target_1),
            "target_2": float(signal.target_2),
            "state": "OPEN",
            # We explicitly set created_at so we can reliably parse it later
            # (especially for multi-day carry).
            "created_at": datetime.now(timezone.utc).isoformat(),
            "added_time_ist": datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%I:%M %p")
        }

        # Fire and forget
        def _insert():
            try:
                self.supabase.table("active_trades").insert(trade_data).execute()
            except Exception as e:
                print(f"Failed to log trade to Supabase: {e}")
                
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _insert)

        self.active_trades.append(trade_data)
        print(f"[+] PositionManager: Trade {trade_id} opened -> {signal.setup_type.value} {signal.direction.value}")

        self.analytics.log_entry(trade_id, signal, spot, atm=atm)"""

new_add_trade_body = """        trade_id = str(uuid.uuid4())
        trade_data = {
            "id": trade_id,
            "signal_id": getattr(signal, "display_id", f"{__import__('random').randint(0, 9999):04d}"),
            "signal_uuid": signal.id,
            "setup_type": signal.setup_type.value,
            "direction": signal.direction.value,
            "entry_price": float(spot),
            "stop_loss": float(signal.stop_loss),
            "target_1": float(signal.target_1),
            "target_2": float(signal.target_2),
            "state": "OPEN",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "added_time_ist": datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%I:%M %p")
        }

        await self.analytics.log_entry_atomic(trade_id, signal, spot, trade_data["added_time_ist"], atm=atm)

        self.active_trades.append(trade_data)
        print(f"[+] PositionManager: Trade {trade_id} opened -> {signal.setup_type.value} {signal.direction.value}")
        return trade_id, "BOUND" """

code = code.replace(old_add_trade_body, new_add_trade_body)

# In position manager, update _initialize_db
code = code.replace('            "signal_id": row.get("signal_id", "N/A"),', '            "signal_id": row.get("signal_id", "N/A"),\n            "signal_uuid": row.get("signal_uuid"),')

with open('position_manager.py', 'w') as f:
    f.write(code)


# 2. Update storage.py
with open('storage.py', 'r') as f:
    code = f.read()

# Replace log_entry with atomic version
old_log_entry = """    def log_entry(self, trade_id: str, signal: AresSignal, spot: float, atm: Any = None) -> None:
        \"\"\"
        Logs the entry details of a trade to 'trade_analytics'.
        
        Args:
            trade_id: The unique UUID of the trade.
            signal: The AresSignal that generated this trade.
            spot: The spot price at entry.
            atm: The ATMStrikes context at entry (optional).
        \"\"\"
        oi_data = {}
        if atm:
            try:
                # Calculate implied PCR for the ATM strike
                ce_oi = atm.ce.oi
                pe_oi = atm.pe.oi
                pcr = pe_oi / ce_oi if ce_oi > 0 else 0.0
                
                oi_data = {
                    "pcr": round(pcr, 4),
                    "atm_ce_oi": ce_oi,
                    "atm_pe_oi": pe_oi,
                    "ce_oi_change_pct": round(atm.ce.oi_change_pct, 2),
                    "pe_oi_change_pct": round(atm.pe.oi_change_pct, 2)
                }
            except Exception as e:
                print(f"AnalyticsLogger: Failed to parse OI data for entry: {e}")

        db_reasons = list(signal.reasons)
        if getattr(signal, "suggested_lots", None) is not None:
            db_reasons.append(
                f"Option Sizing: {signal.suggested_lots} lots suggested | "
                f"Capital: ₹{signal.capital:,.2f} | Risk: {signal.risk_pct:.1f}% | "
                f"Option SL: ₹{signal.option_sl:.2f} | Option Target: ₹{signal.option_target:.2f} | "
                f"Premium: ₹{signal.option_premium:.2f} | Delta: {signal.option_delta:+.4f}"
            )

        market_context = {
            "reasons": db_reasons,
            "spot_at_signal": float(signal.trigger_price),
            "confidence": signal.confidence,
            "entry_spot": float(spot)
        }

        # Add Option Sizing calculations if populated
        if getattr(signal, "suggested_lots", None) is not None:
            market_context["options_sizing"] = {
                "suggested_lots": signal.suggested_lots,
                "option_sl": signal.option_sl,
                "option_target": signal.option_target,
                "capital": signal.capital,
                "delta": signal.option_delta,
                "premium": signal.option_premium,
                "risk_pct": signal.risk_pct
            }

        # Add OI Wall Telemetry Context (TASK-073)
        if getattr(signal, "oi_wall_context", None) is not None:
            market_context["oi_wall"] = signal.oi_wall_context

        data = {
            "id": trade_id,
            "signal_id": getattr(signal, "db_id", None),  # joins to ares_signals.id (NULL if signal logging failed)
            "setup_type": signal.setup_type.value,
            "direction": signal.direction.value,
            "entry_timestamp": to_utc_iso(signal.timestamp),
            "entry_price": float(spot),
            "result_state": "OPEN",
            "market_context": market_context,
            "oi_data": oi_data
        }

        def _insert():
            self.supabase.table("trade_analytics").insert(data).execute()

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _insert)
        except Exception as e:
            print(f"Failed to log trade analytics entry: {e}")"""

new_log_entry = """    async def log_entry_atomic(self, trade_id: str, signal: AresSignal, spot: float, added_time_ist: str, atm: Any = None) -> None:
        oi_data = {}
        if atm:
            try:
                ce_oi = atm.ce.oi
                pe_oi = atm.pe.oi
                pcr = pe_oi / ce_oi if ce_oi > 0 else 0.0
                
                oi_data = {
                    "pcr": round(pcr, 4),
                    "atm_ce_oi": ce_oi,
                    "atm_pe_oi": pe_oi,
                    "ce_oi_change_pct": round(atm.ce.oi_change_pct, 2),
                    "pe_oi_change_pct": round(atm.pe.oi_change_pct, 2)
                }
            except Exception as e:
                print(f"AnalyticsLogger: Failed to parse OI data for entry: {e}")

        db_reasons = list(signal.reasons)
        if getattr(signal, "suggested_lots", None) is not None:
            db_reasons.append(
                f"Option Sizing: {signal.suggested_lots} lots suggested | "
                f"Capital: ₹{signal.capital:,.2f} | Risk: {signal.risk_pct:.1f}% | "
                f"Option SL: ₹{signal.option_sl:.2f} | Option Target: ₹{signal.option_target:.2f} | "
                f"Premium: ₹{signal.option_premium:.2f} | Delta: {signal.option_delta:+.4f}"
            )

        market_context = {
            "reasons": db_reasons,
            "spot_at_signal": float(signal.trigger_price),
            "confidence": signal.confidence,
            "entry_spot": float(spot),
            "signal_display_id": signal.display_id
        }
        if getattr(signal, "suggested_lots", None) is not None:
            market_context["options_sizing"] = {
                "suggested_lots": signal.suggested_lots,
                "option_sl": signal.option_sl,
                "option_target": signal.option_target,
                "capital": signal.capital,
                "delta": signal.option_delta,
                "premium": signal.option_premium,
                "risk_pct": signal.risk_pct
            }
        if getattr(signal, "oi_wall_context", None) is not None:
            market_context["oi_wall"] = signal.oi_wall_context

        def _rpc_call():
            params = {
                "p_trade_id": trade_id,
                "p_signal_id": getattr(signal, "db_id", None) or 0,
                "p_signal_uuid": signal.id,
                "p_setup_type": signal.setup_type.value,
                "p_direction": signal.direction.value,
                "p_entry_price": float(spot),
                "p_stop_loss": float(signal.stop_loss),
                "p_target_1": float(signal.target_1),
                "p_target_2": float(signal.target_2),
                "p_added_time_ist": added_time_ist,
                "p_market_context": market_context,
                "p_oi_data": oi_data,
                "p_entry_timestamp": to_utc_iso(signal.timestamp)
            }
            res = self.supabase.rpc("create_trade_entry_bridge", params).execute()
            return res

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _rpc_call)
        except Exception as e:
            print(f"Storage: Failed to commit atomic trade entry: {e}")
            raise"""

code = code.replace(old_log_entry, new_log_entry)

# Update log_exit
old_log_exit_query = '            response = self.supabase.table("trade_analytics").select("entry_price", "direction", "signal_id").eq("id", trade_id).execute()'
new_log_exit_query = '            response = self.supabase.table("trade_analytics").select("entry_price", "direction", "signal_id", "signal_uuid").eq("id", trade_id).execute()'
code = code.replace(old_log_exit_query, new_log_exit_query)

old_ml_update = """            signal_id = record.get("signal_id")
            if signal_id is None:
                return
            try:
                self.supabase.table("ml_collection").update({
                    "trade_id": trade_id,
                    "trade_outcome": final_state,
                    "trade_pnl": pnl,
                    "trade_score": score,
                }).eq("signal_id", str(signal_id)).execute()
            except Exception as ml_err:
                # Never let a labelling failure lose the trade exit above.
                print(f"Failed to back-fill ml_collection label: {ml_err}")"""

new_ml_update = """            signal_uuid = record.get("signal_uuid")
            if signal_uuid is None:
                # Fallback to signal_id for legacy rows
                signal_id = record.get("signal_id")
                if signal_id is None:
                    return
                try:
                    res = self.supabase.table("ml_collection").update({
                        "trade_id": trade_id,
                        "trade_outcome": final_state,
                        "trade_pnl": pnl,
                        "trade_score": score,
                    }).eq("signal_id", str(signal_id)).execute()
                    if not res.data:
                        print(f"Storage: zero rows updated in ml_collection for signal_id {signal_id}")
                except Exception as ml_err:
                    print(f"Failed to back-fill ml_collection label: {ml_err}")
                return

            try:
                res = self.supabase.table("ml_collection").update({
                    "trade_outcome": final_state,
                    "trade_pnl": pnl,
                    "trade_score": score,
                }).eq("trade_id", trade_id).eq("signal_uuid", signal_uuid).execute()
                if not res.data:
                    print(f"Storage: zero rows updated in ml_collection for trade {trade_id}")
            except Exception as ml_err:
                print(f"Failed to back-fill ml_collection label: {ml_err}")"""
code = code.replace(old_ml_update, new_ml_update)

with open('storage.py', 'w') as f:
    f.write(code)


# 3. Update main.py
with open('main.py', 'r') as f:
    code = f.read()

old_main_log = """            storage.log_signal(signal, spot_price)
            position_manager.add_trade(signal, spot_price, atm_strikes)
            
            # Record ML data unconditionally when a valid setup triggers.
            # TASK-161: Pass option targets explicitly if configured for 1-lot tests
            target_distance = signal.target_1 - spot_price if signal.direction.value == "BULLISH" else spot_price - signal.target_1
            ml_collector.snapshot(
                spot_price,
                atm_strikes,
                signal_type=signal.setup_type.value,
                signal_direction=signal.direction.value,
                signal_confidence=signal.confidence,
                signal_id=signal.signal_id,
                trade_target_pts=target_distance,
                trade_sl_pts=abs(spot_price - signal.stop_loss)
            )"""

new_main_log = """            success = await storage.log_signal(signal, spot_price)
            if not success:
                print("Engine: Failed to log signal, aborting trade entry.")
                continue

            trade_id, binding_status = await position_manager.add_trade(signal, spot_price, atm_strikes)
            
            target_distance = signal.target_1 - spot_price if signal.direction.value == "BULLISH" else spot_price - signal.target_1
            await ml_collector.snapshot(
                spot_price,
                atm_strikes,
                signal_type=signal.setup_type.value,
                signal_direction=signal.direction.value,
                signal_confidence=signal.confidence,
                signal_id=signal.id, # canonical UUID
                signal_display_id=signal.display_id,
                trade_id=trade_id if trade_id else None,
                trade_binding_status=binding_status,
                trade_target_pts=target_distance,
                trade_sl_pts=abs(spot_price - signal.stop_loss)
            )"""
code = code.replace(old_main_log, new_main_log)

with open('main.py', 'w') as f:
    f.write(code)


# 4. Update ml_signal/collector.py
with open('ml_signal/collector.py', 'r') as f:
    code = f.read()

code = code.replace("def snapshot(", "async def snapshot(")
code = code.replace("signal_id: Optional[str] = None,", "signal_id: Optional[str] = None,\n        signal_display_id: Optional[str] = None,\n        trade_id: Optional[str] = None,\n        trade_binding_status: Optional[str] = None,")
code = code.replace('            "signal_generated": True if signal_type else False,\n            "signal_id": signal_id,', '            "signal_generated": True if signal_type else False,\n            "signal_uuid": signal_id,\n            "signal_display_id": signal_display_id,\n            "trade_id": trade_id,\n            "trade_binding_status": trade_binding_status,')
code = code.replace("            loop.run_in_executor(None, _insert)", "            await loop.run_in_executor(None, _insert)")

with open('ml_signal/collector.py', 'w') as f:
    f.write(code)

# 5. Update alerts.py
with open('alerts.py', 'r') as f:
    code = f.read()
code = code.replace('trade_data.get("signal_id", "N/A")', 'trade_data.get("signal_id", "N/A") if trade_data.get("signal_uuid") is None else trade_data.get("display_id", "N/A")')
# Wait, active_trades doesn't fetch display_id in PositionManager unless we add it to trade_data. 
# We already set `getattr(signal, "display_id", ...)` to `signal_id` in trade_data for bridge mode!
# So trade_data["signal_id"] will contain the 4 digit display_id natively in active_trades in memory for new trades.
# But alerts.py needs to read display_id if available, otherwise signal_id.
code = code.replace('trade_data.get("signal_id", "N/A")', 'trade_data.get("display_id", trade_data.get("signal_id", "N/A"))')

with open('alerts.py', 'w') as f:
    f.write(code)

print("Patching complete.")
