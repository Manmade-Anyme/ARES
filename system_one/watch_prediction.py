"""Typed Jev judgments for an OI watch, separate from signal T1/SL/T2."""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any

from typesafe_sdk import Choice, Noul, RetryPolicy, Score, TypeSafeClient

from oi_watch_context import CONTEXT_VERSION, _timestamp

QUESTION_VERSION = "oi-watch-questions-v1"
FIELD_NAME = "🧠 JEV Prediction"
OUTLOOKS = {
    "established_trend": "Established directional trend",
    "fragile_trend": "Fragile directional trend",
    "range_bound": "Range-bound",
    "opposing_trend": "Opposing trend",
    "insufficient_evidence": "Insufficient evidence",
}
SCORE_KEYS = ("wall_support", "price_support", "structural_runway")


def build_watch_questions() -> dict[str, Any]:
    """Ask five independent judgments over the same frozen public snapshot."""
    return {
        "directional_close": Noul(instructions=(
            "Using only the supplied observation-time evidence, will the final regular-session "
            "one-minute Nifty spot close be strictly beyond `watch.closing_threshold` in "
            "`watch.direction`? BEARISH means below the threshold; BULLISH means above it. "
            "The threshold already includes `watch.minimum_move_points` from watch spot. "
            "Predict the closing outcome, not an intraday touch, uninterrupted trend, option profit "
            "or T1/SL event. Do not assume unavailable history or later market information.")),
        "outlook": Choice(instructions=(
            "Which description best matches the currently observed price structure relative "
            "to `watch.direction`? Use available completed candles, swings and momentum. "
            "Missing or stale price evidence cannot establish a trend; this describes present "
            "structure, not the future closing result."), criteria={
                "established_trend": "Directional swings and consistent follow-through support the watch direction.",
                "fragile_trend": "Progress supports the watch direction but overlapping candles or retracements weaken follow-through.",
                "range_bound": "Adequate available price history shows oscillation with no established directional progress.",
                "opposing_trend": "Established price progress and swings run against the watch direction.",
                "insufficient_evidence": "Price history is absent, stale or too limited to classify current structure.",
            }),
        "wall_support": Score(instructions=(
            "Rate how observed wall behaviour supports `watch.direction`. Use wall size, "
            "growth, relative strength and persistence. OI is positioning, not proof of writer "
            "intent; no unsupplied OI trajectory may be invented. Missing evidence is not positive support."), criteria=[
                "Available wall evidence contradicts directional support, or necessary wall evidence is unavailable.",
                "Wall exists but observed growth/persistence provides little directional support.",
                "Observed wall size and persistence provide mixed or moderate support.",
                "Persistent wall with observed strengthening provides clear directional support.",
                "Strong relative dominance, sustained persistence and strengthening jointly provide exceptional wall support.",
            ]),
        "price_support": Score(instructions=(
            "Rate support of available completed price structure for `watch.direction`. "
            "Use only observed returns and swings; missing return windows cannot be treated as flat."), criteria=[
                "Observed price structure actively opposes direction, or necessary price history is unavailable.",
                "Weak or inconsistent directional progress with frequent opposing moves.",
                "Mixed price structure with some directional progress but limited follow-through.",
                "Consistent directional movement and clear supportive price structure.",
                "Strong efficient directional progress with repeated aligned swings and limited retracement.",
            ]),
        "structural_runway": Score(instructions=(
            "Rate clarity of the path in `watch.direction` before known opposing price levels "
            "and option OI concentrations. The supplied opposing_walls are candidates, not "
            "detector-qualified walls. Use supplied distances relative to `watch.minimum_move_points` "
            "and available volatility. Absence of supplied structure is missing evidence, not open runway."), criteria=[
                "Immediate heavy opposing structure blocks the path, or structural evidence is unavailable.",
                "Nearby opposing structure leaves very little directional room.",
                "Moderate directional room with meaningful opposing structure ahead.",
                "Known opposing structure is sufficiently distant for clear directional room.",
                "Comprehensively supplied nearby structure leaves unusually wide directional room.",
            ]),
    }


def _finite(value, low=0.0, high=1.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError("invalid typed watch probability or score")
    return float(value)


def _distribution(probabilities, expected):
    if not isinstance(probabilities, dict) or set(probabilities) != set(expected):
        raise ValueError("incomplete typed watch distribution")
    result = {k: _finite(v) for k, v in probabilities.items()}
    if not math.isclose(sum(result.values()), 1.0, abs_tol=0.005):
        raise ValueError("watch distribution does not sum to one")
    return result


def _validate_state(state):
    if not isinstance(state, dict) or state.get("context_version") != CONTEXT_VERSION:
        raise ValueError("missing or unsupported watch context")
    watch = state["watch"]
    direction = watch["direction"]
    if direction not in ("BULLISH", "BEARISH"):
        raise ValueError("invalid watch direction")
    spot = _finite(watch["spot"], 0.0001, math.inf)
    minimum = _finite(watch["minimum_move_points"], 0.0001, math.inf)
    threshold = _finite(watch["closing_threshold"], 0.0001, math.inf)
    if not math.isclose(threshold, spot + (-minimum if direction == "BEARISH" else minimum), abs_tol=.0001):
        raise ValueError("watch closing threshold disagrees with context")
    observed = _timestamp(state["observed_at"])
    if observed >= _timestamp(watch["session_close"]):
        raise ValueError("watch session already closed")
    json.dumps(state, allow_nan=False)


def _validate_prediction(prediction):
    _finite(prediction["close_probability"])
    if prediction["context_version"] != CONTEXT_VERSION or prediction["question_version"] != QUESTION_VERSION:
        raise ValueError("unsupported prediction version")
    _distribution(prediction["outlook_distribution"], OUTLOOKS)
    if prediction["outlook"] not in OUTLOOKS:
        raise ValueError("unknown watch outlook")
    for key in SCORE_KEYS:
        value = prediction["scores"][key]
        if value is not None:
            _finite(value, 0, 4)


def invoke_watch_jev(state, *, timeout=5.0, dispatch_deadline=None):
    """Invoke once under an absolute monotonic deadline; return auditable JSON.

    Callers own the database inference claim. Invalid or partial typed responses
    raise ValueError so delivery can append an unavailable assessment instead.
    """
    _validate_state(state)
    timeout = _finite(timeout, .0001, math.inf)
    start = time.monotonic()
    if dispatch_deadline is not None:
        if not isinstance(dispatch_deadline, (int, float)) or not math.isfinite(dispatch_deadline) or dispatch_deadline <= start:
            raise ValueError("watch invocation dispatch window elapsed")
        timeout = min(timeout, dispatch_deadline-start)
    with TypeSafeClient(model=os.environ.get("TYPESAFE_MODEL", "jev-1.13.0"),
                        retry=RetryPolicy(max_retries=0), timeout=timeout) as client:
        if dispatch_deadline is not None:
            remaining = dispatch_deadline-time.monotonic()
            if remaining <= 0:
                raise ValueError("watch invocation dispatch window elapsed during SDK setup")
            timeout = min(timeout, remaining)
        response = client.system_one(state=state, questions=build_watch_questions(), timeout=timeout)
    probability = _finite(response.nouls["directional_close"].noul)
    outlook = response.choices["outlook"]
    distribution = _distribution(dict(outlook.probabilities), OUTLOOKS)
    _finite(outlook.confidence)
    if outlook.choice not in OUTLOOKS or distribution[outlook.choice] < max(distribution.values()) - .005:
        raise ValueError("invalid selected watch outlook")
    scores = {}
    for key in SCORE_KEYS:
        answer = response.scores[key]
        scores[key] = _finite(answer.score, 0, 4)
        _finite(answer.confidence)
        probs = _distribution(dict(answer.probabilities), range(5))
        if set(answer.legend) != set(range(5)) or not math.isclose(scores[key], sum(k*v for k,v in probs.items()), abs_tol=.025):
            raise ValueError("invalid watch Score levels or weighted value")
    wall, price = state.get("wall", {}), state.get("price", {})
    if wall.get("wall_oi") is None or wall.get("wall_oi_change_pct") is None:
        scores["wall_support"] = None
    if (price.get("contiguous_completed_count", 0) < 2
            or price.get("source_age_seconds") is None or price["source_age_seconds"] > 120
            or price.get("completed_source_age_seconds") is None or price["completed_source_age_seconds"] > 120):
        scores["price_support"] = None
    if not state.get("structure", {}).get("nearest_directional_level") and not state.get("options", {}).get("opposing_walls"):
        scores["structural_runway"] = None
    # Pydantic's dump preserves all SDK response fields, including request/model
    # metadata and every answer's legend, confidence and full distribution.
    raw = response.model_dump(mode="json")
    request_id = getattr(response, "request_id", None)
    if isinstance(request_id, str):
        raw["request_id"] = request_id
    if not isinstance(response.model, str) or not response.model:
        raise ValueError("missing actual Jev model metadata")
    result = {"close_probability": probability, "outlook": outlook.choice,
              "outlook_distribution": distribution, "scores": scores,
              "raw_response": raw, "model_name": response.model,
              "context_version": CONTEXT_VERSION, "question_version": QUESTION_VERSION,
              "latency_ms": round((time.monotonic()-start)*1000, 1)}
    json.dumps(result, allow_nan=False)
    return result


def format_watch_prediction(state, prediction=None):
    """Compose exactly one Discord field from typed judgments and source facts."""
    try:
        observed = _timestamp(state["observed_at"]).strftime("%d-%b-%Y %H:%M:%S IST")
    except (KeyError, ValueError, TypeError):
        observed = "Unavailable"
    suffix = f"**Assessment time:** {observed}\n*Experimental model estimate.*"
    unavailable = f"Assessment unavailable.\n{suffix}"
    try:
        _validate_state(state)
        if prediction is None:
            return unavailable
        _validate_prediction(prediction)
        if prediction["outlook"] == "insufficient_evidence" or prediction["scores"]["price_support"] is None:
            return f"Insufficient evidence for closing probability.\n**Outlook:** Insufficient evidence\n{suffix}"
        watch = state["watch"]
        bearish = watch["direction"] == "BEARISH"
        relation = "below" if bearish else "above"
        outlook = OUTLOOKS[prediction["outlook"]]
        if prediction["outlook"] in ("established_trend", "fragile_trend"):
            outlook = ("Established" if prediction["outlook"] == "established_trend" else "Fragile") + (" bearish trend" if bearish else " bullish trend")
        wall, price = state["wall"], state["price"]
        evidence = []
        if prediction["scores"]["wall_support"] is not None:
            evidence.append((prediction["scores"]["wall_support"],
                f"Wall {wall['wall_oi']/100000:.1f}L OI, {wall['wall_oi_change_pct']:+.1f}% change; {wall['persistence_snapshots']:g} evaluations"))
        returns = price.get("returns_points", {})
        window = next((w for w in ("30m", "15m", "5m") if returns.get(w) is not None), None)
        if window and prediction["scores"]["price_support"] is not None:
            evidence.append((prediction["scores"]["price_support"], f"{window} spot movement {returns[window]:+.2f} points"))
        nearest = state["structure"].get("nearest_directional_level")
        if nearest and prediction["scores"]["structural_runway"] is not None:
            evidence.append((prediction["scores"]["structural_runway"],
                f"Nearest directional level {nearest['price']:.2f}, {nearest['distance_points']:.2f} points away"))
        facts = "; ".join(text for _,text in sorted(evidence,key=lambda item:item[0],reverse=True)[:3]) or "Limited supplied evidence"
        return (f"**Chance of closing {relation} {watch['closing_threshold']:.2f}: "
                f"{prediction['close_probability']*100:.0f}%**\n"
                f"*{watch['minimum_move_points']:g}+ points {relation} watch price.*\n"
                f"**Outlook:** {outlook}\n**Supporting evidence:** {facts}\n{suffix}")
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return unavailable
