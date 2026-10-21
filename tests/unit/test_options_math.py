"""Observable MANM-176 pricing, sizing and provider-boundary regressions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from config_profiles import TuningConfig
from models import AresSignal, Direction, OptionGreeks, SetupType
from options_math import (
    _expected_move_diagnostics, build_option_bracket, calculate_affordable_lots,
    calculate_greek_premium_translation, calculate_lots, calculate_points,
    calculate_risk_amount, fetch_dhan_capital, find_optimal_strike,
    option_sizing_context, option_sizing_summary, process_options_calculation,
    project_option_premium, round_to_tick, translate_to_premium, validate_capital,
)


def make_signal(direction=Direction.BULLISH):
    sign = 1 if direction == Direction.BULLISH else -1
    return AresSignal(SetupType.FAILED_BREAKOUT, direction, 24000, (23995, 24005),
                      24000 - sign * 10, 24000 + sign * 20, 24000 + sign * 40,
                      'HIGH', [], datetime(2026, 10, 15, 10), 24000,
                      'CE' if sign == 1 else 'PE')


def make_row(strike=24000, delta=.5, **overrides):
    row = {'strike': strike, 'expiry_date': '2026-10-15', 'underlying_spot': 24000}
    for side, sign in [('ce', 1), ('pe', -1)]:
        row.update({f'{side}_security_id': '40710' if sign == 1 else '40711',
                    f'{side}_ltp': 100, f'{side}_delta': sign * delta,
                    f'{side}_gamma': .002, f'{side}_theta': -10,
                    f'{side}_vega': 4, f'{side}_iv': 15,
                    f'{side}_bid': 99.95, f'{side}_ask': 100.05})
    row.update(overrides)
    return row


@pytest.mark.parametrize('direction', [Direction.BULLISH, Direction.BEARISH])
def test_atm_selection_is_closest_absolute_delta_without_otm_bias(direction):
    result = find_optimal_strike(direction.value, [make_row(24050, .45), make_row(24000, .51)])
    assert result[0] == 24000
    assert result[3].delta == (.51 if direction == Direction.BULLISH else -.51)


def test_deep_itm_is_eligible_if_no_nearer_atm_exists():
    assert find_optimal_strike('BULLISH', [make_row(delta=.8)])[0] == 24000


@pytest.mark.parametrize('spot,delta,expected', [(50,.5,27.5),(-50,-.5,27.5),(-10,.5,-4.9),(10,-.5,-4.9)])
def test_signed_second_order_projection(spot, delta, expected):
    assert calculate_greek_premium_translation(spot, delta, .002) == pytest.approx(expected)


@pytest.mark.parametrize('capital', [None, -39.19, 0, float('nan'), float('inf'), True, 'invalid'])
def test_invalid_capital_never_produces_lots(capital):
    assert validate_capital(capital) == (0.0, False)
    assert calculate_risk_amount(capital, 4) == 0
    assert calculate_lots(capital, 5, 65) == 0
    assert calculate_affordable_lots(capital, 100, 65) == 0
    bracket = build_option_bracket(make_signal(), [make_row()], capital, TuningConfig())
    assert (bracket.is_valid, bracket.lots, bracket.rejection_reason) == (False, 0, 'NO_AVAILABLE_CAPITAL')


@pytest.mark.parametrize('direction', [Direction.BULLISH, Direction.BEARISH])
@pytest.mark.parametrize('capital', [0, None, -39.19, float('nan'), float('inf'), True, 'invalid', 1])
def test_option_prices_do_not_depend_on_available_capital(direction, capital):
    signal = make_signal(direction)
    funded = build_option_bracket(signal, [make_row()], 100000, TuningConfig())
    unfunded = build_option_bracket(signal, [make_row()], capital, TuningConfig())
    for field in ('security_id', 'expiry_date', 'strike', 'greeks', 'entry_limit',
                  'catastrophic_opt_sl', 'target_1', 'target_2', 'risk_loss_pts'):
        assert getattr(unfunded, field) == getattr(funded, field)
    assert (unfunded.lots, unfunded.affordable_lots, unfunded.is_valid) == (0, 0, False)


@pytest.mark.asyncio
@pytest.mark.parametrize('response, status', [
    ({'status': 'success', 'data': {'availabelBalance': 0}}, 'NO_AVAILABLE_CAPITAL'),
    ({'status': 'failure'}, 'NO_AVAILABLE_CAPITAL'),
    ({'status': 'success', 'data': {'availabelBalance': 1}}, 'INSUFFICIENT_CAPITAL_OR_RISK_BUDGET'),
])
async def test_unfunded_signal_retains_prices_in_context_and_summary(response, status):
    client = MagicMock()
    client.get_fund_limits.return_value = response
    signal = make_signal()
    await process_options_calculation(signal, [make_row()], client, TuningConfig())
    context = signal.market_context['options_sizing']
    assert (context['entry_limit'], signal.option_sl, signal.option_target,
            context['target_2']) == (100.05, 95.1, 110.4, 121.6)
    assert context['suggested_lots'] == 0
    assert signal.options_sizing_status == status
    summary = option_sizing_summary(signal)
    assert 'Option Sizing: 0 lots' in summary
    for price in ('100.05', '95.10', '110.40', '121.60'):
        assert price in summary


@pytest.mark.parametrize('direction', [Direction.BULLISH, Direction.BEARISH])
def test_complete_bracket_uses_signed_greeks_ticks_and_risk_floor(direction):
    bracket = build_option_bracket(make_signal(direction), [make_row()], 100000, TuningConfig())
    assert bracket.is_valid
    assert bracket.entry_limit == 100.05
    assert bracket.catastrophic_opt_sl == 95.1
    assert bracket.target_1 == 110.4
    assert bracket.target_2 == 121.6
    assert bracket.risk_loss_pts >= 5
    for price in (bracket.entry_limit, bracket.catastrophic_opt_sl, bracket.target_1, bracket.target_2):
        assert price / .05 == pytest.approx(round(price / .05))
    assert 0 <= bracket.lots <= 10


@pytest.mark.parametrize('field,value', [('ce_delta',None),('ce_delta',-.5),('ce_delta',1.1),
    ('ce_ltp',0),('ce_ltp',float('nan')),('ce_gamma',None),('ce_gamma',-1),
    ('ce_theta',float('inf')),('ce_vega',None),('ce_iv',None),('ce_security_id',None),
    ('expiry_date',None),('expiry_date','2026-10-14')])
@pytest.mark.parametrize('capital', [0, 100000])
def test_bad_quote_is_rejected_without_fabrication(field,value,capital):
    bracket = build_option_bracket(make_signal(), [make_row(**{field:value})], capital, TuningConfig())
    assert not bracket.is_valid
    assert bracket.lots == 0
    assert bracket.rejection_reason == 'INVALID_DATA'


def test_nearest_current_expiry_is_selected_before_delta_ranking():
    rows = [make_row(24050,.48), make_row(24000,.5,expiry_date='2026-10-22')]
    assert build_option_bracket(make_signal(), rows, 100000, TuningConfig()).strike == 24050


def test_entry_cap_and_spread_are_included_in_risk():
    bracket = build_option_bracket(make_signal(), [make_row(ce_ask=101,ce_bid=99)], 100000, TuningConfig())
    assert bracket.entry_limit == 100.5
    assert bracket.risk_loss_pts >= 7.4


@pytest.mark.parametrize('direction', [Direction.BULLISH, Direction.BEARISH])
@pytest.mark.parametrize('capital', [0, 100000])
def test_theta_rr_does_not_reject_prices_or_lot_suggestion(direction, capital):
    signal = make_signal(direction)
    baseline = build_option_bracket(signal, [make_row()], capital, TuningConfig())
    side = signal.option_type.lower()
    bracket = build_option_bracket(signal, [make_row(**{f'{side}_theta': -200})], capital, TuningConfig())
    assert bracket.projected_target_1 - bracket.entry_limit < bracket.risk_loss_pts
    for field in ('entry_limit', 'catastrophic_opt_sl', 'target_1', 'target_2',
                  'lots', 'is_valid', 'rejection_reason'):
        assert getattr(bracket, field) == getattr(baseline, field)


def test_low_premium_cannot_create_inverted_stop():
    bracket = build_option_bracket(make_signal(), [make_row(ce_ltp=1,ce_ask=1.05,ce_bid=.95)],100000,TuningConfig())
    assert bracket.rejection_reason == 'INVALID_BRACKET_ORDERING'


@pytest.mark.parametrize('field,value', [('options_max_lots_cap',-1),('options_max_lots_cap',float('inf')),
    ('nifty_lot_size',0),('risk_per_trade_pct',float('nan')),('options_slippage_buffer_pts',-1)])
def test_invalid_sizing_configuration_is_fail_closed(field,value):
    config = TuningConfig()
    setattr(config,field,value)
    assert not build_option_bracket(make_signal(),[make_row()],100000,config).is_valid


@pytest.mark.asyncio
async def test_capital_zero_is_not_overwritten_by_alias_or_default():
    client = MagicMock()
    client.get_fund_limits.return_value = {'status':'success','data':{'availabelBalance':0,'availableBalance':10000}}
    assert await fetch_dhan_capital(client) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [{'status':'failure'},{'status':'success','data':{}},
    {'status':'success','data':{'availabelBalance':'nan'}}])
async def test_capital_failure_has_no_unverified_default(response):
    client=MagicMock()
    client.get_fund_limits.return_value=response
    assert await fetch_dhan_capital(client) is None


@pytest.mark.asyncio
async def test_process_attaches_serializable_sizing_and_clears_stale_values():
    client = MagicMock()
    client.get_fund_limits.return_value={'status':'success','data':{'availabelBalance':100000}}
    signal=make_signal()
    await process_options_calculation(signal,[make_row()],client,TuningConfig())
    assert signal.options_sizing_status == 'VALID'
    assert signal.suggested_lots == signal.bracket.lots
    assert option_sizing_context(signal)['greeks']['gamma'] == .002
    await process_options_calculation(signal,[],client,TuningConfig())
    assert signal.options_sizing_status == 'INVALID_DATA'
    assert signal.suggested_lots == 0
    assert signal.option_premium is None


def test_tick_half_rounds_up():
    assert round_to_tick(100.025) == 100.05


@pytest.mark.parametrize('capital,risk,premium,lot_size', [(100000,4,100,65),(-39.19,4,100,65),(100000,0,100,65),(100000,float('inf'),100,65),(100000,4,0,65),(100000,4,100,0),(100000,4,100,65.5)])
def test_sizing_helpers_never_return_negative_or_fractional_lots(capital,risk,premium,lot_size):
    lots = calculate_lots(calculate_risk_amount(capital,risk),5,lot_size)
    affordable = calculate_affordable_lots(capital,premium,lot_size)
    assert isinstance(lots,int) and lots >= 0
    assert isinstance(affordable,int) and affordable >= 0


def test_lots_obey_cap_risk_and_affordability():
    bracket = build_option_bracket(make_signal(),[make_row()],1000000,TuningConfig(options_max_lots_cap=2))
    assert bracket.lots == 2
    assert bracket.uncapped_suggested_lots > bracket.lots
    assert bracket.lots * bracket.lot_size * bracket.entry_limit <= bracket.capital
    assert bracket.lots * bracket.lot_size * bracket.risk_loss_pts <= bracket.capital * .04


def test_invalid_current_expiry_does_not_fall_through_to_valid_next_expiry():
    rows=[make_row(ce_gamma=None),make_row(expiry_date='2026-10-22')]
    assert not build_option_bracket(make_signal(),rows,100000,TuningConfig()).is_valid


def test_projection_references_chain_spot_and_leaves_engine_geometry_unchanged():
    signal=make_signal()
    bracket=build_option_bracket(signal,[make_row(underlying_spot=24001)],100000,TuningConfig())
    assert bracket.target_1 == round_to_tick(100 + .5 * 19 + .001 * 19 ** 2)
    assert signal.stop_loss == 23990
    assert signal.target_1 == 24020


def test_missing_book_uses_ltp_buffer_without_fabricating_quotes():
    bracket=build_option_bracket(make_signal(),[make_row(ce_ask=None,ce_bid=None)],100000,TuningConfig())
    assert bracket.entry_limit == 100.5
    assert bracket.reference_premium == 100


@pytest.mark.parametrize('updates',[{'ce_bid':101,'ce_ask':100},{'ce_bid':float('nan')}, {'ce_ask':0}])
def test_bad_book_rejects_bracket(updates):
    assert not build_option_bracket(make_signal(),[make_row(**updates)],100000,TuningConfig()).is_valid


def test_rounding_cannot_collapse_targets_into_an_executable_bracket():
    signal=make_signal()
    signal.target_1=24000.01
    signal.target_2=24000.02
    assert build_option_bracket(signal,[make_row()],100000,TuningConfig()).rejection_reason == 'INVALID_BRACKET_ORDERING'


def test_extreme_finite_values_fail_closed_without_projection_overflow():
    signal=make_signal()
    signal.trigger_price=1e308
    signal.stop_loss=1e307
    signal.target_1=1.1e308
    signal.target_2=1.2e308
    bracket=build_option_bracket(signal,[make_row(underlying_spot=1e308,ce_gamma=1e308)],100000,TuningConfig())
    assert not bracket.is_valid


@pytest.mark.asyncio
async def test_provider_exception_is_unavailable_capital():
    client=MagicMock()
    client.get_fund_limits.side_effect=RuntimeError('offline')
    assert await fetch_dhan_capital(client) is None


def test_legacy_sizing_snapshot_preserves_report_inputs_without_bracket():
    signal = make_signal()
    signal.suggested_lots = 2
    signal.capital = 10000
    signal.option_delta = .5
    signal.option_premium = 100
    signal.option_sl = 95
    signal.option_target = 110
    signal.risk_pct = 4
    context = option_sizing_context(signal)
    assert {key: context[key] for key in ('capital', 'delta', 'premium', 'option_sl', 'option_target', 'risk_pct')} == {
        'capital': 10000, 'delta': .5, 'premium': 100, 'option_sl': 95, 'option_target': 110, 'risk_pct': 4,
    }
    assert 'Option Sizing: 2 lots' in option_sizing_summary(signal)


@pytest.mark.parametrize('lots', [None, -1, float('nan'), float('inf'), 1.5, True, '2'])
def test_legacy_snapshot_rejects_invalid_lots_and_handles_absent_status(lots):
    signal = make_signal()
    signal.suggested_lots = lots
    signal.options_sizing_status = None
    assert option_sizing_context(signal)['suggested_lots'] == 0
    assert 'Option Sizing: 0 lots' in option_sizing_summary(signal)


@pytest.mark.asyncio
async def test_bracket_snapshot_exposes_legacy_report_aliases():
    signal = make_signal()
    client = MagicMock()
    client.get_fund_limits.return_value = {'status': 'success', 'data': {'availableBalance': 100000}}
    await process_options_calculation(signal, [make_row()], client, TuningConfig())
    context = option_sizing_context(signal)
    assert context['delta'] == context['option_delta'] == .5
    assert context['premium'] == context['option_premium'] == 100
    assert f'Option Sizing: {signal.bracket.lots} lots' in option_sizing_summary(signal)


def test_high_premium_overflow_fails_closed_without_decimal_exception():
    bracket = build_option_bracket(make_signal(), [make_row(ce_ltp=1e308, ce_ask=None, ce_bid=None)], 100000, TuningConfig())
    assert not bracket.is_valid
    assert bracket.lots == 0


def test_theta_horizon_only_gates_feasibility_without_changing_instantaneous_risk():
    immediate = build_option_bracket(make_signal(), [make_row()], 100000, TuningConfig(options_holding_horizon_minutes=0))
    later = build_option_bracket(make_signal(), [make_row()], 100000, TuningConfig(options_holding_horizon_minutes=30))
    assert immediate.is_valid and later.is_valid
    assert immediate.projected_target_1 > later.projected_target_1
    assert (immediate.lots, immediate.risk_loss_pts, immediate.target_1) == (later.lots, later.risk_loss_pts, later.target_1)


def test_scenario_projection_applies_theta_and_vega_without_mutating_greeks():
    greeks = OptionGreeks(-.5, .002, -10, 4, 15)
    assert project_option_premium(-20, greeks, .04, -2) == pytest.approx(2)
    assert project_option_premium(-20, greeks) == pytest.approx(10.4)


@pytest.mark.parametrize('days,iv_change', [(-1, 0), (float('nan'), 0), (0, float('inf'))])
def test_invalid_scenario_inputs_are_rejected(days, iv_change):
    with pytest.raises(ValueError):
        project_option_premium(20, OptionGreeks(.5, .002, -10, 4, 15), days, iv_change)


def test_expiry_day_iv_diagnostic_uses_fractional_calendar_time_and_is_non_blocking():
    signal = make_signal()
    bracket = build_option_bracket(signal, [make_row()], 100000, TuningConfig())
    assert bracket.expected_move_iv == pytest.approx(24000 * .15 * (5.5 / (365 * 24)) ** .5)
    assert bracket.expected_move_straddle == 250
    assert bracket.iv_feed_warning == 'IV_FEED_STALE'
    assert bracket.is_valid
    signal.bracket = bracket
    assert option_sizing_context(signal)['iv_feed_warning'] == 'IV_FEED_STALE'


def test_diagnostic_timezone_conversion_preserves_same_instant():
    signal = make_signal()
    naive = build_option_bracket(signal, [make_row()], 100000, TuningConfig())
    signal.timestamp = signal.timestamp.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))).astimezone(timezone.utc)
    aware = build_option_bracket(signal, [make_row()], 100000, TuningConfig())
    assert aware.expected_move_iv == naive.expected_move_iv


def test_missing_iv_keeps_straddle_diagnostic_without_fabricated_execution_iv():
    bracket = build_option_bracket(make_signal(), [make_row(ce_iv=None)], 100000, TuningConfig())
    assert bracket.expected_move_straddle == 250
    assert bracket.expected_move_iv is None
    assert bracket.greeks is None
    assert not bracket.is_valid


def test_contract_cannot_execute_at_or_after_expiry_close():
    signal = make_signal()
    signal.timestamp = datetime(2026, 10, 15, 15, 30)
    bracket = build_option_bracket(signal, [make_row()], 100000, TuningConfig())
    assert not bracket.is_valid
    assert bracket.expected_move_iv is None


def test_entry_tick_rounding_never_exceeds_slippage_ceiling():
    config = TuningConfig(options_slippage_buffer_pts=.53)
    result = build_option_bracket(make_signal(),[make_row(ce_ask=101,ce_bid=100)],100000,config)
    assert result.is_valid
    assert result.entry_limit == 100.5


def test_calculate_points():
    assert calculate_points(100.0, 95.0) == 5.0
    assert calculate_points(95.0, 100.0) == 5.0


def test_translate_to_premium():
    assert translate_to_premium(10.0, 0.5) == 5.0
    assert translate_to_premium(10.0, -0.5) == 5.0


@pytest.mark.parametrize("spot_delta,delta,gamma", [
    (float("nan"), 0.5, 0.002),
    (10.0, float("inf"), 0.002),
    (10.0, 0.5, -0.001),
])
def test_calculate_greek_premium_translation_invalid(spot_delta, delta, gamma):
    with pytest.raises(ValueError, match="requires finite inputs and nonnegative gamma"):
        calculate_greek_premium_translation(spot_delta, delta, gamma)


@pytest.mark.parametrize("price,tick_size", [
    (float("nan"), 0.05),
    (100.0, float("nan")),
    (100.0, 0.0),
    (100.0, -0.05),
])
def test_round_to_tick_invalid(price, tick_size):
    with pytest.raises(ValueError, match="Price and positive tick size must be finite"):
        round_to_tick(price, tick_size)


@pytest.mark.asyncio
async def test_fetch_dhan_capital_data_not_dict():
    client = MagicMock()
    client.get_fund_limits.return_value = {"status": "success", "data": "invalid_not_a_dict"}
    assert await fetch_dhan_capital(client) is None


@pytest.mark.parametrize("direction,chain_arg", [
    ("SIDEWAYS", [make_row()]),
    ("BULLISH", "not_a_list"),
])
def test_find_optimal_strike_invalid_direction_or_chain(direction, chain_arg):
    assert find_optimal_strike(direction, chain_arg) == (None, None, None, None, None)


def test_expected_move_diagnostics_edge_cases():
    signal = make_signal()
    # 1. full_chain not a list (line 173)
    assert _expected_move_diagnostics(signal, "not_a_list") == {}

    # 2. No matching expiry rows with strike > 0 (line 184)
    row_no_strike = {"strike": 0, "expiry_date": "2026-10-15"}
    assert _expected_move_diagnostics(signal, [row_no_strike]) == {}

    # 3. underlying_spot non-finite or <= 0 (line 188)
    row_bad_spot = make_row(underlying_spot=-100)
    assert _expected_move_diagnostics(signal, [row_bad_spot]) == {}

    # 4. Non-finite iv_move (line 196)
    row_huge = make_row(underlying_spot=1e300, ce_iv=1e300)
    diag = _expected_move_diagnostics(signal, [row_huge])
    assert diag.get("expected_move_iv") is None


def test_build_option_bracket_invalid_levels_and_geometry():
    # line 226: non-finite or non-positive levels
    sig_bad_sl = make_signal()
    sig_bad_sl.stop_loss = -10
    bracket_bad = build_option_bracket(sig_bad_sl, [make_row()], 100000, TuningConfig())
    assert bracket_bad.rejection_reason == "INVALID_DATA"

    # line 231: misordered geometry
    sig_bad_geo = make_signal()
    sig_bad_geo.stop_loss = 24005
    bracket_geo = build_option_bracket(sig_bad_geo, [make_row()], 100000, TuningConfig())
    assert bracket_geo.rejection_reason == "INVALID_BRACKET_ORDERING"


def test_option_sizing_summary_invalid_bracket():
    signal = make_signal()
    signal.bracket = replace(build_option_bracket(signal, [make_row()], 100000, TuningConfig()),
                             is_valid=False, rejection_reason="INSUFFICIENT_OPTION_RR")
    assert "Limit 100.05" in option_sizing_summary(signal)


def test_summary_reports_genuinely_unavailable_quote_data():
    signal = make_signal()
    signal.bracket = build_option_bracket(signal, [], 100000, TuningConfig())
    assert option_sizing_summary(signal) == 'Option sizing unavailable: INVALID_DATA'


@pytest.mark.parametrize('capital', [0, 100000])
@pytest.mark.parametrize('theta', [-1e308, 1e308])
def test_overflowed_diagnostic_does_not_reject_or_hide_option_values(capital, theta):
    import json
    signal = make_signal()
    config = TuningConfig(options_holding_horizon_minutes=1e308)
    baseline = build_option_bracket(signal, [make_row()], capital, TuningConfig())
    bracket = build_option_bracket(signal, [make_row(ce_theta=theta)], capital, config)
    assert bracket.projected_target_1 is None
    for field in ('entry_limit', 'catastrophic_opt_sl', 'target_1', 'target_2',
                  'lots', 'is_valid', 'rejection_reason'):
        assert getattr(bracket, field) == getattr(baseline, field)
    signal.bracket = bracket
    json.dumps(option_sizing_context(signal), allow_nan=False)
    assert 'Limit 100.05' in option_sizing_summary(signal)


@pytest.mark.parametrize('spot_ordering', [True, False])
def test_ordering_check_does_not_hide_calculated_prices_or_lots(spot_ordering):
    signal = make_signal()
    row = make_row()
    if spot_ordering:
        signal.stop_loss = 24005
    else:
        row.update(ce_ltp=1, ce_ask=1.05, ce_bid=.95)
    signal.bracket = build_option_bracket(signal, [row], 100000, TuningConfig())
    assert signal.bracket.rejection_reason == 'INVALID_BRACKET_ORDERING'
    assert signal.bracket.lots == 10
    assert not signal.bracket.is_valid
    assert '10 lots' in option_sizing_summary(signal)
