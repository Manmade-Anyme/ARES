def round_to_tick(val: float, tick_size: float = 0.05) -> float:
    return round(val / tick_size) * tick_size

print(f"{round_to_tick(24087.44921875):.2f}")
