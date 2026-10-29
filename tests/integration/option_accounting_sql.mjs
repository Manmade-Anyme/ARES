// Isolated PostgreSQL checks; never connects to production.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const { PGlite } = await import(process.env.PGLITE_MODULE_URL || '@electric-sql/pglite');
const db = new PGlite();
const id = '00000000-0000-0000-0000-000000000001';
const q = (sql, args = []) => db.query(sql, args);
const rpc = async (name, args) => (await q(`SELECT public.${name}(${args.map((_, i) => `$${i + 1}`).join(',')}) AS result`, args)).rows[0].result;
const correction = (revision, event, accounting, spot = null, option = null) => rpc('apply_option_accounting_correction', [id, revision, event, JSON.stringify(accounting), spot, option]);
try {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
    CREATE TABLE public.active_trades(id uuid primary key,state text,stop_loss numeric,
      exit_price numeric,exit_type text,exit_timestamp timestamptz,pnl_points_override numeric,
      entry_price numeric,direction text,target_1 numeric,target_2 numeric,signal_uuid uuid,signal_id text);
    CREATE TABLE public.trade_analytics(id uuid primary key,score integer,result_state text,
      market_context jsonb,exit_price numeric,exit_timestamp timestamptz,pnl_points numeric,signal_uuid uuid,signal_id text);
    CREATE TABLE public.ml_collection(id integer primary key,trade_id uuid,signal_uuid uuid,signal_id text,trade_outcome text,trade_score integer,trade_pnl numeric);
    GRANT USAGE ON SCHEMA public TO service_role,anon,authenticated;
    GRANT SELECT,UPDATE ON public.active_trades,public.trade_analytics,public.ml_collection TO service_role;`);
  for (const name of ['20261023_option_lifecycle_transition.sql', '20261029_option_trade_accounting.sql']) {
    const sql = await readFile(new URL(`../../migrations/${name}`, import.meta.url), 'utf8');
    await db.exec(sql); await db.exec(sql);
  }
  const initial = {option_trailing: { policy_version: 'option_atr_v1', accounting_formula_version: 'one_lot_reference_v1', revision: 1, phase: 'PRE_T1',
    highest_milestone: 0, remaining_fraction: 1, original_option_sl: 90, original_option_entry: 100,
    contract: {security_id: '123'}, accounting: {legs: {}, spot_booked_points: null, option_booked_points: null} }};
  await q("INSERT INTO public.active_trades(id,state,stop_loss,entry_price,direction,signal_uuid) VALUES ($1,'OPEN',22000,22000,'BULLISH','00000000-0000-0000-0000-000000000099')", [id]);
  await q("INSERT INTO public.trade_analytics(id,result_state,market_context,signal_uuid) VALUES ($1,'OPEN',$2,'00000000-0000-0000-0000-000000000099')", [id, JSON.stringify(initial)]);
  await q("INSERT INTO public.ml_collection(id,trade_id,signal_uuid) VALUES (1,$1,'00000000-0000-0000-0000-000000000099'), (2,$1,'00000000-0000-0000-0000-000000000098'), (3,NULL,'00000000-0000-0000-0000-000000000099')", [id]);
  for (const role of ['anon', 'authenticated']) {
    await db.exec(`SET ROLE ${role}`);
    await assert.rejects(() => rpc('option_reference_accounting_capabilities', []), /permission denied/);
    await assert.rejects(() => correction(1, 'denied', {legs: {}}), /permission denied/);
    await assert.rejects(() => rpc('list_incomplete_option_accounting', [100, 0]), /permission denied/);
    await assert.rejects(() => rpc('sync_option_reference_ml_label', [id]), /permission denied/);
    await db.exec('RESET ROLE');
  }
  await db.exec('SET ROLE service_role');
  assert.deepEqual(await rpc('option_reference_accounting_capabilities', []), {schema_version: 'option_reference_accounting_v1', ready: true});
  await db.exec('RESET ROLE; REVOKE EXECUTE ON FUNCTION public.acknowledge_option_lifecycle_alert(uuid,text,text,text) FROM service_role; SET ROLE service_role;');
  assert.equal((await rpc('option_reference_accounting_capabilities', [])).ready, false);
  await db.exec('RESET ROLE; GRANT EXECUTE ON FUNCTION public.acknowledge_option_lifecycle_alert(uuid,text,text,text) TO service_role; SET ROLE service_role;');
  await db.exec('RESET ROLE; REVOKE UPDATE ON public.ml_collection FROM service_role; SET ROLE service_role;');
  assert.equal((await rpc('option_reference_accounting_capabilities', [])).ready, false);
  await db.exec('RESET ROLE; GRANT UPDATE ON public.ml_collection TO service_role; SET ROLE service_role;');
  const t1 = {active_state: 'T1_HIT', score: 1, option_trailing: {phase: 'POST_T1', highest_milestone: 1,
    remaining_fraction: .5, accounting: {legs: {T1: {event_at: '2026-10-23T04:40:00Z', spot_price: 22100,
      spot_source: 'spot_target', option_price: null, option_source: null}}, spot_booked_points: 50, option_booked_points: null}}};
  const callTransition = (revision, event, transition) => rpc('apply_option_lifecycle_transition', [id, revision, event, JSON.stringify(transition)]);
  const first = await callTransition(1, 'T1', t1);
  assert.equal(first.result_state, 'OPEN'); assert.equal(first.score, 1);
  assert.equal((await q('SELECT trade_outcome FROM public.ml_collection WHERE id=1')).rows[0].trade_outcome, null);
  await assert.rejects(() => rpc('sync_option_reference_ml_label', [id]), /closed one-lot/);
  await assert.rejects(() => correction(2, 'open', {legs: {}}), /closed trade/);
  const exit = {active_state: 'CLOSED', score: 1, exit_reason: 'ATR_SL_HIT', exit_timestamp: '2026-10-23T05:30:00Z',
    pnl_points: null, option_pnl_points: null, option_trailing: {phase: 'CLOSED', highest_milestone: 1,
    remaining_fraction: 0, accounting: {legs: {T1: {event_at: '2026-10-23T04:40:00Z', spot_price: 22100,
    spot_source: 'spot_target', option_price: null, option_source: null}, EXIT: {event_at: '2026-10-23T05:30:00Z',
    spot_price: null, option_price: 120, option_source: 'option_5m_close'}}, spot_booked_points: 50, option_booked_points: null}}};
  await assert.rejects(() => callTransition(2, 'wrong-formula', {...exit, option_trailing: {...exit.option_trailing, accounting_formula_version: 'other'}}), /formula version is immutable/);
  await assert.rejects(() => callTransition(2, 'incomplete', {...exit, option_pnl_points: 10}), /Option points require/);
  await db.exec(`RESET ROLE;
    CREATE FUNCTION public.reject_ml_label() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected ML failure'; END $$;
    CREATE TRIGGER reject_ml_label BEFORE UPDATE ON public.ml_collection FOR EACH ROW EXECUTE FUNCTION public.reject_ml_label(); SET ROLE service_role;`);
  await assert.rejects(() => callTransition(2, 'EXIT', exit), /injected ML failure/);
  assert.equal((await q('SELECT state FROM public.active_trades WHERE id=$1', [id])).rows[0].state, 'T1_HIT');
  assert.equal((await q('SELECT result_state FROM public.trade_analytics WHERE id=$1', [id])).rows[0].result_state, 'OPEN');
  await db.exec('RESET ROLE; DROP TRIGGER reject_ml_label ON public.ml_collection; DROP FUNCTION public.reject_ml_label(); SET ROLE service_role;');
  await callTransition(2, 'EXIT', exit);
  const initialLabel = (await q('SELECT trade_outcome,trade_score,trade_pnl FROM public.ml_collection WHERE id=1')).rows[0];
  assert.deepEqual(initialLabel, {trade_outcome: 'ATR_SL_HIT', trade_score: 1, trade_pnl: null});
  assert.equal((await q('SELECT trade_outcome FROM public.ml_collection WHERE id=2')).rows[0].trade_outcome, null);
  assert.equal((await q('SELECT trade_outcome FROM public.ml_collection WHERE id=3')).rows[0].trade_outcome, null);
  const missing = {legs: {T1: {option_price: 110, option_source: 'option_1m_close', approximate: true},
    EXIT: {spot_price: 22040, spot_source: 'spot_1m_close'}}, spot_booked_points: null, option_booked_points: 5};
  await assert.rejects(() => correction(3, 'bad', {legs: {T1: {spot_price: 999}}}), /Cannot overwrite/);
  await assert.rejects(() => correction(3, 'missing', {legs: {EXIT: {spot_price: 22040}}}, 70, 15), /Option points require/);
  await assert.rejects(() => correction(3, 'wrong-spot', missing, 71, 15), /Spot total does not match/);
  await assert.rejects(() => correction(3, 'wrong-option', missing, 70, 16), /Option total does not match/);
  assert.equal((await rpc('list_incomplete_option_accounting', [100, 0])).length, 1);
  await assert.rejects(() => rpc('list_incomplete_option_accounting', [501, 0]), /limit/);
  await db.exec(`RESET ROLE;
    CREATE FUNCTION public.reject_repair() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'injected write failure'; END $$;
    CREATE TRIGGER reject_repair BEFORE UPDATE ON public.trade_analytics
      FOR EACH ROW EXECUTE FUNCTION public.reject_repair(); SET ROLE service_role;`);
  await assert.rejects(() => correction(3, 'recover', missing, 70, 15), /injected write failure/);
  assert.equal((await q('SELECT pnl_points_override FROM public.active_trades WHERE id=$1', [id])).rows[0].pnl_points_override, null);
  await db.exec('RESET ROLE; DROP TRIGGER reject_repair ON public.trade_analytics; DROP FUNCTION public.reject_repair(); SET ROLE service_role;');
  const repaired = await correction(3, 'recover', missing, 70, 15);
  assert.equal(repaired.revision, 4); assert.equal(Number(repaired.option_pnl_points), 15);
  assert.equal((await correction(3, 'recover', missing, 70, 15)).idempotent_skip, true);
  await assert.rejects(() => correction(4, 'recover', {legs: {}}), /different evidence/);
  await assert.rejects(() => correction(3, 'stale', {legs: {}}), /Revision conflict/);
  const row = (await q('SELECT score,result_state,pnl_points,option_pnl_points,market_context FROM public.trade_analytics WHERE id=$1', [id])).rows[0];
  assert.equal(row.score, 1); assert.equal(row.result_state, 'ATR_SL_HIT');
  assert.equal((await rpc('list_incomplete_option_accounting', [100, 0])).length, 0);
  assert.equal(Number((await q('SELECT exit_price FROM public.trade_analytics WHERE id=$1', [id])).rows[0].exit_price), 22040);
  assert.equal(Number((await q('SELECT trade_pnl FROM public.ml_collection WHERE id=1')).rows[0].trade_pnl), 70);
  assert.equal(Number(row.pnl_points), 70); assert.equal(Number(row.option_pnl_points), 15);
  assert.equal(row.market_context.option_trailing.accounting.legs.T1.option_source, 'option_1m_close');
  assert.equal(row.market_context.option_trailing.alert_events && Object.keys(row.market_context.option_trailing.alert_events).length, 0);
  assert.equal(Number((await q('SELECT pnl_points_override FROM public.active_trades WHERE id=$1', [id])).rows[0].pnl_points_override), 70);
  // A snapshot that binds after closure remains discoverable and can be retried.
  await q('UPDATE public.ml_collection SET trade_id=NULL, trade_outcome=NULL, trade_score=NULL, trade_pnl=NULL WHERE id=1');
  assert.equal((await rpc('list_incomplete_option_accounting', [100, 0])).length, 1);
  assert.equal((await rpc('sync_option_reference_ml_label', [id])).matched, 0);
  await q('UPDATE public.ml_collection SET trade_id=$1 WHERE id=1', [id]);
  assert.equal((await rpc('sync_option_reference_ml_label', [id])).matched, 1);
  assert.equal(Number((await q('SELECT trade_pnl FROM public.ml_collection WHERE id=1')).rows[0].trade_pnl), 70);
  assert.equal((await rpc('list_incomplete_option_accounting', [100, 0])).length, 0);
  assert.equal((await rpc('sync_option_reference_ml_label', [id])).matched, 0);
  assert.equal((await q('SELECT trade_outcome FROM public.ml_collection WHERE id=3')).rows[0].trade_outcome, null);

  await assert.rejects(() => correction(4, 'overwrite', {legs: {}}, 71, null), /Cannot overwrite/);
  // A later stale client snapshot cannot erase or rewrite an already committed leg.
  await db.exec('RESET ROLE; TRUNCATE public.active_trades, public.trade_analytics, public.ml_collection;');
  await q("INSERT INTO public.active_trades(id,state,stop_loss,entry_price,direction,signal_uuid) VALUES ($1,'OPEN',22000,22000,'BULLISH','00000000-0000-0000-0000-000000000099')", [id]);
  const ambiguousInitial = structuredClone(initial);
  ambiguousInitial.option_trailing.ambiguous_index_touch = {status: 'ORDER_UNKNOWN', source_minute: '2026-10-23T04:40:00Z'};
  await q("INSERT INTO public.trade_analytics(id,result_state,market_context,signal_uuid) VALUES ($1,'OPEN',$2,'00000000-0000-0000-0000-000000000099')", [id, JSON.stringify(ambiguousInitial)]);
  await q("INSERT INTO public.ml_collection(id,trade_id,signal_uuid) VALUES (1,$1,'00000000-0000-0000-0000-000000000099'), (2,$1,'00000000-0000-0000-0000-000000000098'), (3,NULL,'00000000-0000-0000-0000-000000000099')", [id]);
  await db.exec('SET ROLE service_role;');
  const withEvidence = structuredClone(t1);
  withEvidence.option_trailing.accounting.legs.T1.option_price = 110;
  withEvidence.option_trailing.accounting.legs.T1.option_source = 'quote';
  withEvidence.option_trailing.accounting.option_booked_points = 5;
  await callTransition(1, 'evidenced-T1', withEvidence);
  const stale = await callTransition(2, 'stale-snapshot', t1);
  assert.equal(stale.option_trailing.accounting.legs.T1.option_price, 110);
  assert.equal(stale.option_trailing.accounting.legs.T1.option_source, 'quote');
  assert.equal(stale.option_trailing.accounting.option_booked_points, 5);
  const conflicting = structuredClone(t1);
  conflicting.option_trailing.accounting.legs.T1.option_price = 111;
  await assert.rejects(() => callTransition(3, 'conflicting-snapshot', conflicting), /Cannot overwrite existing accounting evidence/);
  const staleT2 = {active_state: 'T2_REACHED', score: 2, option_trailing: {phase: 'POST_T2', highest_milestone: 2,
    remaining_fraction: .2, ambiguous_index_touch: null,
    accounting: {legs: {T2: {spot_price: 22200, option_price: null}},
    spot_booked_points: 110, option_booked_points: null}}};
  const mergedT2 = await callTransition(3, 'stale-T2', staleT2);
  assert.equal(mergedT2.option_trailing.ambiguous_index_touch, null);
  assert.equal(mergedT2.option_trailing.accounting.legs.T1.option_price, 110);
  assert.equal(mergedT2.option_trailing.accounting.legs.T2.spot_price, 22200);
  assert.equal(mergedT2.option_trailing.accounting.option_booked_points, null);
  // Bearish spot and PE premium use opposite spot sign, but the same positive premium sign.
  await db.exec('RESET ROLE; TRUNCATE public.active_trades, public.trade_analytics, public.ml_collection;');
  await q("INSERT INTO public.active_trades(id,state,stop_loss,entry_price,direction) VALUES ($1,'CLOSED',22000,22000,'BEARISH')", [id]);
  const pe = structuredClone(initial);
  pe.option_trailing = {...pe.option_trailing, phase: 'CLOSED', highest_milestone: 2,
    remaining_fraction: 0, revision: 5, accounting: {legs: {
      T1: {spot_price: 21900, option_price: null}, T2: {spot_price: 21800, option_price: null},
      EXIT: {spot_price: 21850, option_price: null}}, spot_booked_points: null, option_booked_points: null}};
  await q("INSERT INTO public.trade_analytics(id,result_state,score,market_context) VALUES ($1,'ATR_SL_HIT',2,$2)", [id, JSON.stringify(pe)]);
  await q("INSERT INTO public.ml_collection(id,trade_id) VALUES (1,$1)", [id]);
  await db.exec('SET ROLE service_role');
  const peEvidence = {legs: {T1: {option_price: 110}, T2: {option_price: 125}, EXIT: {option_price: 120}}};
  await assert.rejects(() => correction(5, 'bad-pe', peEvidence, 140, 15), /Option total does not match/);
  const peRecovered = await correction(5, 'recover-pe', peEvidence, 140, 16.5);
  assert.equal(Number(peRecovered.pnl_points), 140);
  assert.equal(Number(peRecovered.option_pnl_points), 16.5);
  assert.equal((await rpc('list_incomplete_option_accounting', [100, 0])).length, 0);
  // Old trades stay out of the recovery scan, even if they have accounting data.
  await db.exec('RESET ROLE;');
  await q("UPDATE public.trade_analytics SET market_context = market_context #- '{option_trailing,accounting_formula_version}', pnl_points = NULL WHERE id=$1", [id]);
  await db.exec('SET ROLE service_role;');
  assert.equal((await rpc('list_incomplete_option_accounting', [100, 0])).length, 0);
  await assert.rejects(() => correction(6, 'old-trade', {legs: {}}), /not enrolled in one-lot/);
  await db.exec('RESET ROLE; REVOKE UPDATE ON public.trade_analytics FROM service_role; SET ROLE service_role;');
  assert.equal((await rpc('option_reference_accounting_capabilities', [])).ready, false);
  console.log('Option accounting isolated PostgreSQL checks passed');
} finally { await db.close(); }
