"""System One: Jev forward predictor for ARES signals.

This package runs as a separate process group in the same Fly app.
It must NOT import from main.py, ml_signal, SignalPredictor, storage.py, or alerts.py.
"""

CONTEXT_VERSION = "v1"
QUESTION_VERSION = "v1"
