with open('main.py', 'r') as f:
    code = f.read()

# Make log_signal return verified success or raise
old_log_signal = """                try:
                    await storage.log_signal(signal, spot)
                except Exception as db_err:
                    print(f"{Y}[{now.strftime('%H:%M:%S')}] ⚠️ Database log failed: {db_err}{RESET}")

                # Every fired signal is a live trade now (TASK-182 removed the
                # observation-only gate).
                try:
                    position_manager.add_trade(signal, spot, atm=atm)
                except Exception as pm_err:
                    print(f"{R}[{now.strftime('%H:%M:%S')}] ⚠️ Position manager add_trade failed: {pm_err}{RESET}")"""

new_log_signal = """                success = False
                try:
                    success = await storage.log_signal(signal, spot)
                except Exception as db_err:
                    print(f"{Y}[{now.strftime('%H:%M:%S')}] ⚠️ Database log failed: {db_err}{RESET}")

                if not success:
                    print(f"{R}[{now.strftime('%H:%M:%S')}] ⚠️ Engine: Failed to log signal, aborting trade entry.{RESET}")
                    signal = None
                else:
                    try:
                        trade_id, binding_status = await position_manager.add_trade(signal, spot, atm=atm)
                        signal.trade_id = trade_id
                        signal.trade_binding_status = binding_status
                    except Exception as pm_err:
                        print(f"{R}[{now.strftime('%H:%M:%S')}] ⚠️ Position manager add_trade failed: {pm_err}{RESET}")"""
code = code.replace(old_log_signal, new_log_signal)

old_ml_collector = """            ml_collector.snapshot(
                candle=candle,
                atm=atm,
                full_chain=full_chain,
                levels=levels,
                spot=spot,
                signal=signal,
                pdh=pdh,
                pdl=pdl,
                is_expiry=is_expiry,
                oi_wall_context=oi_wall_context
            )"""

new_ml_collector = """            await ml_collector.snapshot(
                candle=candle,
                atm=atm,
                full_chain=full_chain,
                levels=levels,
                spot=spot,
                signal=signal,
                pdh=pdh,
                pdl=pdl,
                is_expiry=is_expiry,
                oi_wall_context=oi_wall_context,
                trade_id=getattr(signal, 'trade_id', None) if signal else None,
                trade_binding_status=getattr(signal, 'trade_binding_status', None) if signal else None
            )"""
code = code.replace(old_ml_collector, new_ml_collector)

with open('main.py', 'w') as f:
    f.write(code)
