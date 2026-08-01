from dataclasses import dataclass, field
from typing import List


@dataclass
class MLConfig:

    # Model
    model_path: str = "ml_signal/models/v1.joblib"
    active_model_version: str = "v1"
    # Metrics written by train_offline for the model above. Read at startup so
    # the banner can state what the live model was actually trained on rather
    # than just naming the file.
    model_report_path: str = "reports/ml/task183_offline_metrics.json"

    # Live polling
    poll_interval_seconds: int = 60

    # Labeling
    lookforward_candles: int = 5
    tp_points: float = 35.0
    sl_points: float = 25.0
    label_exclude_inconclusive: bool = True

    # Confidence tiers
    high_threshold: float = 0.70
    medium_threshold: float = 0.55

    # Feature flags
    use_candle_features: bool = True
    use_volume_features: bool = True
    use_iv_features: bool = True
    use_oi_features: bool = True
    use_greek_features: bool = True
    use_structure_features: bool = True
    use_meta_features: bool = True

    # Training
    n_estimators: int = 500
    learning_rate: float = 0.03
    max_depth: int = 5
    subsample: float = 0.8
    colsample_bytree: float = 0.7
    gamma: float = 1.0
    reg_lambda: float = 1.0
    early_stopping_rounds: int = 30
    optuna_trials: int = 50
    test_size_walk_forward: int = 1
    train_size_walk_forward: int = 6

    # Supabase
    supabase_table_predictions: str = "ml_predictions"

    # Signal consumer
    signal_poll_interval_seconds: int = 5

    # Discord
    discord_webhook_url: str = ""
    discord_summary_interval_minutes: int = 15
    discord_min_probability_for_alert: float = 0.70

    # Security
    security_id: str = "13"
    exchange_segment: str = "IDX_I"
    instrument_type: str = "INDEX"


DEFAULT_CONFIG = MLConfig()
