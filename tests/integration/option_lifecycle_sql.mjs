// Real isolated PostgreSQL engine. Never opens a network/production database.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const { PGlite } = await import(process.env.PGLITE_MODULE_URL || '@electric-sql/pglite');
const db = new PGlite();
const id = '00000000-0000-0000-0000-000000000001';
let checks = 0;
const eq = (actual, expected, reason) => { assert.deepEqual(actual, expected, reason); checks++; };
const rejects = async (fn, pattern) => { await assert.rejects(fn, pattern); checks++; };
const q = (sql, args = []) => db.query(sql, args);
const initial = { unrelated: { preserved: true }, option_trailing: {
  policy_version: 'option_atr_v1', revision: 1, phase: 'PRE_T1', highest_milestone: 0,
  remaining_fraction: 1, original_option_sl: 123.65, original_option_entry: 139.25,
  contract: { security_id: '51333' },
}};
const seed = async () => {
  await db.exec('RESET ROLE; TRUNCATE public.active_trades, public.trade_analytics;');
  await q("INSERT INTO public.active_trades(id,state,stop_loss) VALUES ($1,'OPEN',22577.1)", [id]);
  await q("INSERT INTO public.trade_analytics(id,result_state,market_context) VALUES ($1,'OPEN',$2)", [id, JSON.stringify(initial)]);
};
const transition = async (revision, event, extra = {}) => {
  const payload = { active_state: 'T1_HIT', score: 1, update_type: event,
    option_trailing: { phase: 'POST_T1', highest_milestone: 1, remaining_fraction: .5, locked_option_sl: 127.15 },
    alert_snapshot: { update_type: event, event_at: '2026-10-23T04:40:00Z' }, ...extra };
  return (await q('SELECT public.apply_option_lifecycle_transition($1,$2,$3,$4) AS result',
    [id, revision, event, JSON.stringify(payload)])).rows[0].result;
};
const ack = async (event, status) => (await q('SELECT public.acknowledge_option_lifecycle_alert($1,$2,$3,NULL) AS result',
  [id, event, status])).rows[0].result;
try {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
    CREATE TABLE public.active_trades(id uuid primary key,state text,stop_loss numeric,
      exit_price numeric,exit_type text,exit_timestamp timestamptz,pnl_points_override numeric);
    CREATE TABLE public.trade_analytics(id uuid primary key,score integer,result_state text,
      market_context jsonb,exit_price numeric,exit_timestamp timestamptz,pnl_points numeric);
    GRANT USAGE ON SCHEMA public TO service_role,anon,authenticated;
    GRANT SELECT,UPDATE ON public.active_trades,public.trade_analytics TO service_role;`);
  const migration = await readFile(new URL('../../migrations/20261023_option_lifecycle_transition.sql', import.meta.url), 'utf8');
  await db.exec(migration);
  await db.exec(migration);
  await seed();
  for (const role of ['anon', 'authenticated']) {
    await db.exec(`SET ROLE ${role}`);
    await rejects(() => transition(1, 'denied'), /permission denied/);
    await rejects(() => ack('denied', 'DELIVERED'), /permission denied/);
    await db.exec('RESET ROLE');
  }
  await db.exec('SET ROLE service_role');
  const first = await transition(1, 'T1');
  eq([first.revision, first.score, first.result_state], [2, 1, 'OPEN'], 'T1 commits milestone while runner open');
  eq(first.option_trailing.alert_events.T1.status, 'PENDING', 'outbox snapshot committed with transition');
  eq(first.option_trailing.alert_events.T1.snapshot.update_type, 'T1', 'immutable alert payload persisted');
  eq((await transition(1, 'T1')).idempotent_skip, true, 'lost commit response replay is idempotent');
  await rejects(() => transition(1, 'stale'), /Revision conflict/);
  await rejects(() => transition(null, 'missing_revision'), /positive expected revision/);
  await rejects(() => transition(2, ''), /stable event id/);
  eq((await ack('T1', 'SENDING')).option_trailing.revision, 2, 'send claim never increments lifecycle revision');
  eq((await ack('T1', 'SENDING')).claimed, false, 'second sender cannot claim an in-flight alert');
  await ack('T1', 'AMBIGUOUS');
  eq((await ack('T1', 'FAILED')).option_trailing.alert_events.T1.status, 'AMBIGUOUS', 'ambiguous delivery cannot become retryable');
  await rejects(() => transition(2, 'lower_stop', { option_trailing: { locked_option_sl: 126 } }), /cannot decrease/);
  await rejects(() => transition(2, 'lower_score', { score: 0 }), /Score must equal/);
  await rejects(() => transition(2, 'wrong_contract', { option_trailing: { contract: { security_id: '51331' } } }), /immutable/);
  const second = await transition(2, 'T2', { active_state: 'T2_REACHED', score: 2,
    option_trailing: { phase: 'POST_T2', highest_milestone: 2, remaining_fraction: .2, locked_option_sl: 150 } });
  eq([second.revision, second.score, second.result_state], [3, 2, 'OPEN'], 'T2 remains open and records score2');
  eq(second.option_trailing.alert_events.T1.status, 'AMBIGUOUS', 'later snapshot cannot erase acknowledgement');
  await ack('T2', 'SENDING'); await ack('T2', 'DELIVERED');
  eq((await ack('T2', 'FAILED')).option_trailing.alert_events.T2.status, 'DELIVERED', 'late failure cannot downgrade successful delivery');
  const closed = await transition(3, 'floor_exit', { active_state: 'CLOSED', score: 2,
    option_trailing: { phase: 'CLOSED', highest_milestone: 2, remaining_fraction: 0, locked_option_sl: 150 },
    exit_reason: 'OPTION_SL_FLOOR_HIT', exit_price: null, pnl_points: null, exit_timestamp: '2026-10-23T05:30:00Z' });
  eq([closed.revision, closed.score, closed.result_state], [4, 2, 'OPTION_SL_FLOOR_HIT'], 'terminal floor reason retains T2 score');
  const terminal = (await q('SELECT exit_price,pnl_points,market_context FROM public.trade_analytics WHERE id=$1', [id])).rows[0];
  eq([terminal.exit_price, terminal.pnl_points], [null, null], 'unknown index mark/PnL stay unavailable');
  eq(terminal.market_context.unrelated, { preserved: true }, 'unrelated context survives lifecycle writes');
  await rejects(() => transition(4, 'reopen'), /Terminal trade/);
  await seed();
  await db.exec(`CREATE FUNCTION public.reject_analytics_update() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'injected analytics write failure'; END $$;
    CREATE TRIGGER reject_analytics BEFORE UPDATE ON public.trade_analytics
    FOR EACH ROW EXECUTE FUNCTION public.reject_analytics_update();`);
  await rejects(() => transition(1, 'T1'), /injected analytics write failure/);
  eq((await q('SELECT state FROM public.active_trades')).rows[0].state, 'OPEN', 'second table failure rolls back first table update');
  const unchanged = (await q('SELECT score,result_state,market_context FROM public.trade_analytics')).rows[0];
  eq([unchanged.score, unchanged.result_state, unchanged.market_context.option_trailing.revision], [null, 'OPEN', 1], 'analytics/outbox/revision roll back together');
  await db.exec('DROP TRIGGER reject_analytics ON public.trade_analytics; DROP FUNCTION public.reject_analytics_update();');
  await seed();
  await transition(1, 'T1');
  await transition(2, 'index_be', { active_state: 'CLOSED', score: 1,
    option_trailing: { phase: 'CLOSED', highest_milestone: 1, remaining_fraction: 0 },
    exit_reason: 'STOPPED_OUT_AT_BE', exit_price: 22552.1, pnl_points: 12.5,
    exit_timestamp: '2026-10-23T05:30:00Z' });
  eq(Number((await q('SELECT pnl_points_override FROM public.active_trades')).rows[0].pnl_points_override), 12.5,
    'weighted allocation PnL is available for crash repair');
  eq(Number((await q('SELECT pnl_points FROM public.trade_analytics')).rows[0].pnl_points), 12.5,
    'analytics weighted allocation PnL commits atomically');
  console.log(`Option lifecycle isolated PostgreSQL: ${checks} checks passed`);
} finally { await db.close(); }
