with open('position_manager.py', 'r') as f:
    code = f.read()

code = code.replace('"signal_id": getattr(signal, "display_id", f"{__import__(\'random\').randint(0, 9999):04d}"),\\n            "signal_uuid": signal.id,',
                    '"signal_id": getattr(signal, "display_id", f"{__import__(\'random\').randint(0, 9999):04d}"),\\n            "display_id": signal.display_id,\\n            "signal_uuid": signal.id,')

with open('position_manager.py', 'w') as f:
    f.write(code)
