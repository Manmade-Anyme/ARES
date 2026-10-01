"""system_one.jev — TypeSafe Jev inference client.

Batches five independent questions over one compact state to the Jev model.
Maps typed responses into probability outputs, regime classification,
setup quality, and trap assessment.

This module must NOT import from main.py, ml_signal, SignalPredictor,
storage.py, or alerts.py.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from typesafe_sdk import (
    Choice,
    Noul,
    RetryPolicy,
    Score,
    TypeSafeClient,
)
from typesafe_sdk import TypeSafeError, TypeSafeAPIError, TypeSafeAPITimeoutError

from . import QUESTION_VERSION

logger = logging.getLogger("system_one.jev")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
DEFAULT_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-1.13.0")
DEFAULT_TIMEOUT = float(os.environ.get("TYPESAFE_TIMEOUT", "5.0"))


# ---------------------------------------------------------------------------
# Question definitions — versioned with QUESTION_VERSION
# ---------------------------------------------------------------------------
def build_questions() -> Dict[str, Any]:
    """Build the five Jev questions for a signal evaluation.

    All questions evaluate the same state in parallel and cannot see
    each other's outputs. Question IDs are for code consumption only
    and are not sent to the model.
    """
    return {
        "first_barrier": Choice(
            instructions=(
                "Which event occurs first for this spot-price signal "
                "before session close?"
            ),
            criteria={
                "t1_first": (
                    "Target 1 (`signal.target_1`) touches before the original "
                    "stop loss (`signal.original_stop_loss`)."
                ),
                "sl_first": (
                    "The original stop loss (`signal.original_stop_loss`) "
                    "touches before Target 1 (`signal.target_1`)."
                ),
                "neither_by_close": (
                    "Neither barrier touches before session close."
                ),
            },
        ),
        "t2_given_t1": Noul(
            instructions=(
                "Assuming Target 1 touched before the original stop loss, "
                "ARES then moves the stop to `signal.post_t1_stop_price` "
                "(the entry price). Does Target 2 (`signal.target_2`) touch "
                "before this post-T1 breakeven stop or session close? "
                "A touch of the breakeven stop ends the trade; ignore any "
                "later Target 2 touch."
            ),
        ),
        "market_regime": Choice(
            instructions="Classify the observed signal-time market regime.",
            criteria={
                "trending": (
                    "Sustained directional movement with confirming "
                    "participation."
                ),
                "range_choppy": (
                    "Range-bound or reversing movement with weak "
                    "follow-through."
                ),
                "volatile_event": (
                    "Event volatility dominates the observed structure."
                ),
                "insufficient_evidence": (
                    "The supplied snapshot does not support a classification."
                ),
            },
        ),
        "setup_quality": Score(
            instructions=(
                "Rate the observed setup quality using only supplied evidence."
            ),
            criteria=[
                "Unusable: No discernible edge or contradictory signals.",
                "Weak: Marginal setup with significant uncertainty.",
                "Mixed: Some supporting evidence but notable concerns.",
                "Strong: Clear setup with aligned market factors.",
                "Exceptional: Textbook setup with overwhelming confluence.",
            ],
        ),
        "is_trap": Noul(
            instructions=(
                "Does the supplied evidence indicate a false break or "
                "stop sweep that would invalidate the trade thesis?"
            ),
        ),
    }


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class JevResult:
    """Structured result from a Jev inference call."""

    # Probabilities (0-1)
    t1_hit_prob: float
    t2_hit_prob: float  # P(T1 first) × P(T2 | T1)
    sl_hit_prob: float

    # Regime
    regime: str
    regime_distribution: Dict[str, float]
    regime_confidence: float

    # Quality (0-10 scale)
    setup_quality: float
    setup_quality_confidence: float

    # Trap
    is_trap_prob: float

    # Model metadata
    engine_name: str
    question_version: str

    # Raw response for archival
    raw_response: Dict[str, Any] = field(default_factory=dict)

    # Timing
    latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------
def invoke_jev(
    state: Dict[str, Any],
    model: Optional[str] = None,
    timeout: Optional[float] = None,
) -> JevResult:
    """Call TypeSafe Jev with the prepared signal context.

    Sends one request with five batched questions. No automatic retries
    (max_retries=0). The caller must already own an acknowledged INVOKING
    job before calling this function.

    Args:
        state: Exact context dict from context.build_context().
        model: Jev model identifier. Defaults to TYPESAFE_MODEL env.
        timeout: Request timeout in seconds. Defaults to TYPESAFE_TIMEOUT env.

    Returns:
        JevResult with all probability outputs and metadata.

    Raises:
        TypeSafeError: On API key or validation errors.
        TypeSafeAPIError: On API response errors.
        TypeSafeAPITimeoutError: On request timeout.
    """
    model = model or DEFAULT_MODEL
    timeout = timeout or DEFAULT_TIMEOUT
    questions = build_questions()

    start = time.monotonic()

    with TypeSafeClient(
        model=model,
        retry=RetryPolicy(max_retries=0),
        timeout=timeout,
    ) as client:
        response = client.system_one(state=state, questions=questions)

    elapsed_ms = (time.monotonic() - start) * 1000

    # Extract first_barrier Choice distribution
    barrier = response.choices["first_barrier"]
    p_t1 = barrier.probabilities.get("t1_first", 0.0)
    p_sl = barrier.probabilities.get("sl_first", 0.0)

    # T2 conditional probability
    p_t2_given_t1 = response.nouls["t2_given_t1"].noul
    p_t2 = p_t1 * p_t2_given_t1  # Joint: P(T1 first) × P(T2 | T1)

    # Regime
    regime_answer = response.choices["market_regime"]

    # Setup quality: Score returns a continuous value across levels.
    # 5 levels (0-4), scale to 0-10.
    quality_answer = response.scores["setup_quality"]
    quality_scaled = quality_answer.score * (10.0 / (len(quality_answer.legend) - 1))

    # Trap probability
    trap_prob = response.nouls["is_trap"].noul

    # Build serializable raw response for archival
    raw = {
        "first_barrier": {
            "choice": barrier.choice,
            "confidence": barrier.confidence,
            "probabilities": dict(barrier.probabilities),
        },
        "t2_given_t1": {
            "noul": p_t2_given_t1,
        },
        "market_regime": {
            "choice": regime_answer.choice,
            "confidence": regime_answer.confidence,
            "probabilities": dict(regime_answer.probabilities),
        },
        "setup_quality": {
            "score": quality_answer.score,
            "confidence": quality_answer.confidence,
            "probabilities": {str(k): v for k, v in quality_answer.probabilities.items()},
            "legend": {str(k): v for k, v in quality_answer.legend.items()},
        },
        "is_trap": {
            "noul": trap_prob,
        },
        "model": response.model,
        "request_id": response.request_id,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }

    return JevResult(
        t1_hit_prob=round(p_t1, 6),
        t2_hit_prob=round(p_t2, 6),
        sl_hit_prob=round(p_sl, 6),
        regime=regime_answer.choice,
        regime_distribution=dict(regime_answer.probabilities),
        regime_confidence=regime_answer.confidence,
        setup_quality=round(quality_scaled, 2),
        setup_quality_confidence=quality_answer.confidence,
        is_trap_prob=round(trap_prob, 6),
        engine_name=response.model,
        question_version=QUESTION_VERSION,
        raw_response=raw,
        latency_ms=round(elapsed_ms, 1),
    )
