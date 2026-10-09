"""Public watch forecasting behavior; SDK and market inputs are isolated."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo
import json

import pytest
from typesafe_sdk import Choice, Noul, Score

from oi_watch_context import build_watch_context, build_watch_candles
from system_one.watch_prediction import build_watch_questions, invoke_watch_jev, format_watch_prediction

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 8, 9, 28, 33, tzinfo=IST)


def candle(at, close):
    return SimpleNamespace(timestamp=at, open=close + 1, high=close + 2,
                           low=close - 2, close=close, volume=100, vwap=close,
                           sampled_at=NOW,complete=at+timedelta(minutes=1)<=NOW)


def state(direction="BEARISH"):
    bias = SimpleNamespace(wall_key="CE:22500", wall_strike=22500,
        wall_option_type="CE", direction=direction, wall_oi=5090000,
        wall_oi_change_pct=9.5, relative_percentile=99, persistence_snapshots=3,
        persistence_duration_seconds=120, first_seen=NOW-timedelta(minutes=2),
        last_seen=NOW, favourable_excursion_pts=20)
    bars = [candle(NOW.replace(minute=15+i, second=0), 22520-i*2) for i in range(13)]
    return build_watch_context(bias, 22489.75, NOW, candles=bars,
        full_chain=[{"strike":22400,"pe_oi":7000000,"ce_oi":1000000},
                    {"strike":22500,"ce_oi":5090000,"pe_oi":500000}],
        levels=[SimpleNamespace(price=22430,source="PDL",strength=3)],
        pdh=22600,pdl=22430,expiry_date="2026-10-13")


def response():
    choices = {"established_trend":.7,"fragile_trend":.2,"range_bound":.05,
               "opposing_trend":.04,"insufficient_evidence":.01}
    score = lambda: SimpleNamespace(score=3,confidence=.9,
        probabilities={0:0,1:0,2:0,3:1,4:0},legend={i:str(i) for i in range(5)})
    raw = {"model":"jev-actual", "answers":{"exact":"archive"}, "request_id":"r1"}
    return SimpleNamespace(nouls={"directional_close":SimpleNamespace(noul=.68)},
        choices={"outlook":SimpleNamespace(choice="established_trend",probabilities=choices,confidence=.8)},
        scores={name:score() for name in ("wall_support","price_support","structural_runway")},
        model="jev-actual",request_id="r1",model_dump=lambda **_:raw)


def test_context_excludes_future_other_day_and_incomplete_window():
    s=state()
    assert s["watch"]["closing_threshold"] == 22464.75
    assert s["price"]["returns_points"]["5m"] == -10
    assert s["price"]["returns_points"]["15m"] is None
    assert s["session_so_far"]["opening_range_15m"] is None
    assert s["unavailable"]["vwap"]
    assert s["unavailable"]["oi_history"]
    json.dumps(s,allow_nan=False)
    bars=[candle(NOW-timedelta(days=1),30000),candle(NOW+timedelta(minutes=1),40000),
          candle(NOW.replace(second=0),22490)]
    empty=build_watch_context(SimpleNamespace(**{**s["wall"],"direction":"BEARISH"}),
        22489.75,NOW,candles=bars,full_chain=[],levels=[],pdh=None,pdl=None,expiry_date=None)
    assert empty["price"]["completed_candle_count"] == 0
    assert empty["price"]["latest_candle"]["complete"] is False
    assert empty["session_so_far"]["high"] is None


def test_bullish_threshold_and_input_immutability():
    assert state("BULLISH")["watch"]["closing_threshold"] == 22514.75
    s=state()
    assert s["structure"]["nearest_directional_level"]["price"] == 22430
    assert s["options"]["opposing_walls"][0]["strike"] == 22400
    assert "capital" not in json.dumps(s)


def test_questions_are_independent_typed_and_bound_to_closing_threshold():
    q=build_watch_questions()
    assert isinstance(q["directional_close"],Noul)
    assert "closing_threshold" in q["directional_close"].instructions
    assert isinstance(q["outlook"],Choice)
    assert "insufficient_evidence" in q["outlook"].criteria
    scores=[v for v in q.values() if isinstance(v,Score)]
    assert len(scores)==3 and all(len(v.criteria)==5 for v in scores)


def test_invocation_bounded_no_retry_actual_model_full_response_and_format():
    s=state()
    with patch("system_one.watch_prediction.TypeSafeClient") as sdk, \
         patch("system_one.watch_prediction.time.monotonic",return_value=100):
        sdk.return_value.__enter__.return_value.system_one.return_value=response()
        p=invoke_watch_jev(s,timeout=5,dispatch_deadline=101)
        assert sdk.call_args.kwargs["retry"].max_retries==0
        assert sdk.return_value.__enter__.return_value.system_one.call_args.kwargs["timeout"]==1
    assert p["model_name"]=="jev-actual" and p["raw_response"]["answers"]=={"exact":"archive"}
    text=format_watch_prediction(s,p)
    assert "22464.75" in text and "68%" in text and "25+ points below" in text
    assert "09:28:33 IST" in text and "Experimental" in text
    assert "50.9L" in text and "9.5%" in text


@pytest.mark.parametrize("bad",[float("nan"),float("inf"),-.1,1.1])
def test_invalid_probability_rejected_and_formatter_never_shows_fake_number(bad):
    r=response(); r.nouls["directional_close"].noul=bad
    with patch("system_one.watch_prediction.TypeSafeClient") as sdk:
        sdk.return_value.__enter__.return_value.system_one.return_value=r
        with pytest.raises(ValueError): invoke_watch_jev(state())
    assert "unavailable" in format_watch_prediction(state(),{"close_probability":bad}).lower()


@pytest.mark.parametrize("deadline",[99,100,float("nan"),float("inf")])
def test_expired_budget_does_not_construct_sdk(deadline):
    with patch("system_one.watch_prediction.TypeSafeClient") as sdk, \
         patch("system_one.watch_prediction.time.monotonic",return_value=100):
        with pytest.raises(ValueError): invoke_watch_jev(state(),dispatch_deadline=deadline)
        sdk.assert_not_called()


def test_missing_snapshot_and_insufficient_outlook_hide_probability():
    assert "%" not in format_watch_prediction({},None)
    with patch("system_one.watch_prediction.TypeSafeClient") as sdk:
        r=response(); r.choices["outlook"].choice="insufficient_evidence"
        r.choices["outlook"].probabilities={k:float(k=="insufficient_evidence") for k in r.choices["outlook"].probabilities}
        sdk.return_value.__enter__.return_value.system_one.return_value=r
        p=invoke_watch_jev(state())
    assert "%" not in format_watch_prediction(state(),p)


def test_bad_distribution_and_bad_score_fail_closed():
    for mutate in [lambda r:r.choices["outlook"].probabilities.update(established_trend=.1),
                   lambda r:setattr(r.scores["wall_support"],"score",9)]:
        r=response(); mutate(r)
        with patch("system_one.watch_prediction.TypeSafeClient") as sdk:
            sdk.return_value.__enter__.return_value.system_one.return_value=r
            with pytest.raises(ValueError): invoke_watch_jev(state())


def run_prediction(s=None, r=None):
    with patch("system_one.watch_prediction.TypeSafeClient") as sdk:
        sdk.return_value.__enter__.return_value.system_one.return_value=r or response()
        return invoke_watch_jev(s or state())


@pytest.mark.parametrize("mutation",[
    lambda s:s["watch"].update(direction="UNKNOWN"),
    lambda s:s["watch"].update(closing_threshold=22000),
    lambda s:s.update(observed_at=s["watch"]["session_close"]),
    lambda s:s.update(context_version="future-version"),
    lambda s:s["watch"].update(spot=True),
    lambda s:s["watch"].update(minimum_move_points="25"),
])
def test_invalid_snapshot_fails_before_any_network(mutation):
    s=state(); mutation(s)
    with patch("system_one.watch_prediction.TypeSafeClient") as sdk:
        with pytest.raises(ValueError): invoke_watch_jev(s)
        sdk.assert_not_called()
    assert "unavailable" in format_watch_prediction(s).lower()


@pytest.mark.parametrize("mutation",[
    lambda r:r.choices["outlook"].probabilities.pop("range_bound"),
    lambda r:setattr(r.choices["outlook"],"choice","unknown"),
    lambda r:setattr(r.choices["outlook"],"choice","fragile_trend"),
    lambda r:r.scores["wall_support"].legend.pop(0),
    lambda r:setattr(r.scores["wall_support"],"score",2),
    lambda r:setattr(r,"model",""),
    lambda r:setattr(r,"model",None),
])
def test_malformed_complete_response_cannot_be_displayed(mutation):
    r=response(); mutation(r)
    with pytest.raises(ValueError): run_prediction(r=r)


def test_deadline_can_expire_during_sdk_setup():
    with patch("system_one.watch_prediction.TypeSafeClient") as sdk, \
         patch("system_one.watch_prediction.time.monotonic",side_effect=[100,102]):
        with pytest.raises(ValueError,match="during SDK setup"):
            invoke_watch_jev(state(),dispatch_deadline=101)
        sdk.return_value.__enter__.return_value.system_one.assert_not_called()


@pytest.mark.parametrize("mutation",[
    lambda s:s["wall"].update(wall_oi=None),
    lambda s:s["wall"].update(wall_oi_change_pct=None),
    lambda s:s["price"].update(contiguous_completed_count=0),
    lambda s:s["price"].update(source_age_seconds=None),
    lambda s:s["price"].update(source_age_seconds=121),
    lambda s:s["price"].update(completed_source_age_seconds=None),
    lambda s:s["price"].update(completed_source_age_seconds=121),
    lambda s:(s["structure"].update(nearest_directional_level=None),s["options"].update(opposing_walls=[])),
])
def test_missing_evidence_does_not_become_positive_score(mutation):
    s=state(); mutation(s)
    p=run_prediction(s)
    assert None in p["scores"].values()
    text=format_watch_prediction(s,p)
    if p["scores"]["price_support"] is None:
        assert "%" not in text
    else:
        assert "68%" in text


@pytest.mark.parametrize("mutation",[
    lambda p:p.update(context_version="other"),
    lambda p:p.update(question_version="other"),
    lambda p:p.update(outlook="unknown"),
    lambda p:p.update(outlook_distribution=[]),
])
def test_archived_prediction_version_and_type_guard(mutation):
    p=run_prediction(); mutation(p)
    assert "unavailable" in format_watch_prediction(state(),p).lower()


def test_format_direction_outlook_grounded_fact_selection_and_no_evidence():
    s=state("BULLISH"); p=run_prediction(s)
    p["outlook"]="fragile_trend"
    text=format_watch_prediction(s,p)
    assert "above 22514.75" in text and "Fragile bullish trend" in text
    p["outlook"]="range_bound"
    assert "Range-bound" in format_watch_prediction(s,p)
    p["scores"]["wall_support"]=None
    p["scores"]["structural_runway"]=None
    s["price"]["returns_points"]={}
    assert "Limited supplied evidence" in format_watch_prediction(s,p)
    assert "Assessment unavailable" in format_watch_prediction(state(),None)


def test_context_rejects_nonpublic_future_invalid_and_missing_market_data():
    s=state(); bias=SimpleNamespace(**s["wall"],direction="BEARISH")
    bars=[candle(NOW-timedelta(days=1),30000),candle(NOW+timedelta(minutes=1),40000),
          candle(NOW.replace(minute=25,second=0),22490),candle(NOW.replace(minute=26,second=0),22491),
          SimpleNamespace(timestamp=None),candle(NOW.replace(minute=24,second=0),float("nan"))]
    context=build_watch_context(bias,22489.75,NOW.astimezone(ZoneInfo("UTC")),candles=bars,
        full_chain=[{"strike":None},{"strike":22400,"pe_oi":None}],levels=[],pdh=None,pdl=None,
        expiry_date=None)
    assert context["price"]["completed_candle_count"]==2
    assert context["price"]["returns_points"]["5m"] is None
    assert context["session_so_far"]["covers_session_open"] is False
    assert context["options"]["oi_totals"]=={"ce":None,"pe":None}
    assert context["observed_at"].endswith("+05:30")
    with pytest.raises(ValueError):
        build_watch_context(bias,True,NOW,candles=[],full_chain=[],levels=[],pdh=None,pdl=None,expiry_date=None)


@pytest.mark.parametrize("bad_oi",[None,-1,True,float("nan")])
def test_partial_chain_never_implies_complete_totals_or_pcr(bad_oi):
    s=state(); bias=SimpleNamespace(**s["wall"],direction="BEARISH")
    context=build_watch_context(bias,22489.75,NOW,candles=[],
        full_chain=[{"strike":22400,"ce_oi":100,"pe_oi":200},
                    {"strike":22500,"ce_oi":bad_oi,"pe_oi":300}],
        levels=[],pdh=None,pdl=None,expiry_date=None)
    assert context["options"]["oi_totals"]=={"ce":None,"pe":500}
    assert context["options"]["pcr"] is None
    assert context["options"]["missing_oi_strike_counts"]=={"ce":1,"pe":0}
    assert "not detector-qualified" in context["options"]["opposing_walls_basis"]


def test_raw_response_without_request_header_id_still_archived():
    r=response(); r.request_id=None
    assert run_prediction(r=r)["model_name"]=="jev-actual"


def test_fresh_partial_bar_cannot_refresh_stale_completed_evidence():
    s=state(); bias=SimpleNamespace(**s["wall"],direction="BEARISH")
    context=build_watch_context(bias,22489.75,NOW,candles=[
        candle(NOW.replace(minute=15,second=0),22520),
        candle(NOW.replace(minute=16,second=0),22510),
        candle(NOW.replace(minute=28,second=0),22490)],
        full_chain=[],levels=[],pdh=None,pdl=None,expiry_date=None)
    assert context["price"]["source_age_seconds"]==33
    assert context["price"]["completed_source_age_seconds"]==753
    p=run_prediction(context)
    assert p["scores"]["price_support"] is None
    assert "%" not in format_watch_prediction(context,p)


@pytest.mark.parametrize("interaction_at",[NOW-timedelta(minutes=2),NOW+timedelta(minutes=1),None,"invalid",NOW-timedelta(days=1)])
def test_frozen_interaction_evidence_is_contemporaneous(interaction_at):
    s=state()
    bias=SimpleNamespace(**{**s["wall"],"direction":"BEARISH",
        "initial_interaction_timestamp":interaction_at,"initial_interaction_price":22500.25})
    context=build_watch_context(bias,22489.75,NOW,candles=[],full_chain=[],levels=[],pdh=None,pdl=None,expiry_date=None)
    assert context["watch"]["setup_type"]=="OI_WALL_RETEST_READY"
    if interaction_at==NOW-timedelta(minutes=2):
        assert context["wall"]["initial_interaction_timestamp"]==interaction_at.isoformat()
        assert context["wall"]["initial_interaction_price"]==22500.25
        assert context["unavailable"]["initial_interaction"] is None
    else:
        assert context["wall"]["initial_interaction_timestamp"] is None
        assert context["wall"]["initial_interaction_price"] is None
        assert context["unavailable"]["initial_interaction"]


def raw_data(times, *, closes=None):
    closes=closes or [22500]*len(times)
    return {"timestamp":times,"open":[v+1 for v in closes],"high":[v+2 for v in closes],
            "low":[v-2 for v in closes],"close":closes,"volume":[100]*len(times)}


@pytest.mark.parametrize("encoding",["seconds","milliseconds","string","epoch_string"])
def test_same_raw_provider_history_supports_completed_windows(encoding):
    ats=[NOW.replace(minute=15+i,second=0) for i in range(14)]
    encode={"seconds":lambda at:at.timestamp(),"milliseconds":lambda at:at.timestamp()*1000,
            "string":lambda at:at.isoformat(),"epoch_string":lambda at:str(at.timestamp())}[encoding]
    data=raw_data([encode(at) for at in ats],closes=[22520-i*2 for i in range(14)])
    if encoding=="milliseconds":
        data["start_Time"]=data.pop("timestamp")
    bars=build_watch_candles(data,NOW.astimezone(ZoneInfo("UTC")))
    assert len(bars)==14 and bars[-1]["complete"] is False
    assert all(bar["complete"] for bar in bars[:-1])
    json.dumps(bars,allow_nan=False)
    s=state(); bias=SimpleNamespace(**s["wall"],direction="BEARISH")
    context=build_watch_context(bias,22489.75,NOW,candles=bars,
        full_chain=[],levels=[],pdh=None,pdl=None,expiry_date=None)
    assert context["price"]["completed_candle_count"]==13
    assert context["price"]["returns_points"]["5m"]==-10
    assert context["session_so_far"]["opening_range_15m"] is None
    assert run_prediction(context)["scores"]["price_support"] is not None


def test_aged_partial_candle_never_becomes_completed_without_new_provider_sample():
    sampled=NOW.replace(minute=19,second=30)
    bar_at=sampled.replace(second=0)
    bars=build_watch_candles(raw_data([bar_at.timestamp()]),sampled)
    s=state(); bias=SimpleNamespace(**s["wall"],direction="BEARISH")
    def context_at(candles):
        return build_watch_context(bias,22489.75,sampled+timedelta(minutes=1),candles=candles,
            full_chain=[],levels=[],pdh=None,pdl=None,expiry_date=None)
    old=context_at(bars)
    assert old["price"]["latest_candle"]["complete"] is False
    assert old["price"]["completed_candle_count"]==0
    refreshed=build_watch_candles(raw_data([bar_at.timestamp()],closes=[22490]),sampled+timedelta(minutes=1))
    revised=context_at(refreshed)
    assert revised["price"]["completed_candle_count"]==1
    assert revised["price"]["latest_candle"]["close"]==22490
    assert revised["price"]["latest_candle"]["complete"] is True


@pytest.mark.parametrize("provenance",[{}, {"complete":True},
    {"complete":True,"sampled_at":NOW.replace(minute=19,second=30)},
    {"complete":True,"sampled_at":NOW+timedelta(minutes=1)},
    {"complete":True,"sampled_at":"invalid"}])
def test_completion_requires_verified_contemporaneous_sample(provenance):
    bar={"timestamp":NOW.replace(minute=19,second=0),"open":22500,"high":22510,
         "low":22490,"close":22495,"volume":100,**provenance}
    s=state(); bias=SimpleNamespace(**s["wall"],direction="BEARISH")
    context=build_watch_context(bias,22489.75,NOW,candles=[bar],
        full_chain=[],levels=[],pdh=None,pdl=None,expiry_date=None)
    assert context["price"]["completed_candle_count"]==0
    if provenance.get("sampled_at")==NOW+timedelta(minutes=1):
        assert context["price"]["latest_candle"] is None


@pytest.mark.parametrize("bad_data",[None,{}, {"timestamp":"bad"},
    {"timestamp":[1],"open":[],"high":[],"low":[],"close":[],"volume":[]}])
def test_malformed_raw_provider_arrays_mean_unavailable_history(bad_data):
    assert build_watch_candles(bad_data,NOW)==[]


def test_raw_provider_invalid_future_previous_session_rows_are_excluded():
    times=["invalid",(NOW+timedelta(minutes=1)).timestamp(),(NOW-timedelta(days=1)).timestamp(),
           NOW.replace(hour=8).timestamp(),NOW.replace(minute=20,second=0).timestamp(),
           NOW.replace(minute=21,second=0).timestamp()]
    data=raw_data(times)
    data["close"][-2]=float("nan")
    data["volume"][-1]=-1
    assert build_watch_candles(data,NOW)==[]
    assert build_watch_candles(raw_data([NOW.timestamp()]),None)==[]
