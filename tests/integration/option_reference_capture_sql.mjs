// Isolated PostgreSQL migration/RPC checks; never connects to production.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const { PGlite } = await import(process.env.PGLITE_MODULE_URL || '@electric-sql/pglite');
const db = new PGlite();
const ids = [
  '00000000-0000-0000-0000-000000000001',
  '00000000-0000-0000-0000-000000000002',
];
const q = (sql, args = []) => db.query(sql, args);
const rpc = async (name, args) => (await q(
  `SELECT public.${name}(${args.map((_, i) => `$${i + 1}`).join(',')}) AS result`, args,
)).rows[0].result;
const reference = (entryAt) => ({
  formula_version: 'one_lot_hold_to_exit_v1', revision: 1, status: 'TRACKING',
  reference_lots: 1,
  contract: {security_id: '51333', exchange_segment: 'NSE_FNO', expiry_date: '2026-11-03',
    strike: 22550, option_type: 'PE', lot_size: 65},
  entry_event_at: entryAt, entry: null, milestones: {}, exit_event_at: null,
  exit_reason: null, exit: null, premium_points: null, applied_events: {},
});
const apply = (id, revision, eventId, event) => rpc(
  'apply_option_reference_capture', [id, revision, eventId, JSON.stringify(event)],
);
const evidence = (price, observedAt, source = 'dhan_last_trade', approximate = false) => ({
  price, observed_at: observedAt, source, approximate,
});

try {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
    CREATE TABLE public.trade_analytics(
      id uuid PRIMARY KEY, entry_timestamp timestamptz, exit_timestamp timestamptz,
      result_state text, score integer, pnl_points numeric, option_pnl_points numeric,
      exit_price numeric, market_context jsonb);
    GRANT USAGE ON SCHEMA public TO service_role,anon,authenticated;
    GRANT SELECT,UPDATE ON public.trade_analytics TO service_role;`);
  const migration = await readFile(new URL('../../migrations/20261030_option_reference_pnl.sql', import.meta.url), 'utf8');
  await db.exec(migration);
  await db.exec(migration);

  const start = '2026-10-30T09:20:00+05:30';
  const exitAt = '2026-10-30T09:40:00+05:30';
  await q(`INSERT INTO public.trade_analytics
    (id,entry_timestamp,result_state,score,pnl_points,option_pnl_points,exit_price,market_context)
    VALUES ($1,$2,'OPEN',2,12.5,77,21800,$3)`,
  [ids[0], start, JSON.stringify({option_reference: reference(start), unrelated: {keep: true}})]);
  await q(`INSERT INTO public.trade_analytics
    (id,entry_timestamp,result_state,market_context)
    VALUES ($1,$2,'OPEN',$3)`,
  [ids[1], start, JSON.stringify({option_reference: reference(start)})]);

  for (const role of ['anon', 'authenticated']) {
    await db.exec(`SET ROLE ${role}`);
    await assert.rejects(() => rpc('option_reference_capture_capabilities', []), /permission denied/);
    await assert.rejects(() => apply(ids[0], 1, 'denied', {kind: 'MILESTONE'}), /permission denied/);
    await assert.rejects(() => rpc('list_incomplete_option_reference', [100, 0]), /permission denied/);
    await db.exec('RESET ROLE');
  }

  await db.exec('SET ROLE service_role');
  assert.deepEqual(await rpc('option_reference_capture_capabilities', []), {
    schema_version: 'one_lot_hold_to_exit_v1', ready: true,
  });
  const entry = {kind: 'ENTRY_CAPTURE', event_at: start,
    evidence: evidence(140.25, '2026-10-30T09:20:01+05:30')};
  const entryResult = await apply(ids[0], 1, 'entry-1', entry);
  assert.equal(entryResult.revision, 2);
  assert.equal(entryResult.option_reference_pnl_points, null);
  assert.equal((await apply(ids[0], 1, 'entry-1', entry)).idempotent_skip, true);
  await assert.rejects(() => apply(ids[0], 2, 'entry-1', {...entry,
    evidence: evidence(141, '2026-10-30T09:20:01+05:30')}), /different evidence/);
  await assert.rejects(() => apply(ids[0], 2, 'bad-quote', {...entry,
    evidence: evidence(141, '2026-10-30T09:20:06+05:30')}), /within five seconds/);

  const milestone = {kind: 'MILESTONE', name: 'T1', milestone: {
    event_at: '2026-10-30T09:30:00+05:30', spot_target: 22870,
    spot_price: null, source: 'completed_candle_touch', time_basis: 'candle_start',
  }};
  const milestoneResult = await apply(ids[0], 2, 'milestone-t1', milestone);
  assert.equal(milestoneResult.revision, 3);
  await assert.rejects(() => apply(ids[0], 3, 'missing-time-basis', {
    ...milestone, milestone: {...milestone.milestone, time_basis: null},
  }), /requires T1\/T2/);
  await assert.rejects(() => apply(ids[0], 3, 'malformed-spot-price', {
    ...milestone, milestone: {...milestone.milestone, spot_price: '22870'},
  }), /numeric-or-null spot price/);
  await assert.rejects(() => apply(ids[0], 3, 'invalid-milestone', {...milestone, name: 'T3'}), /Spot milestone/);

  await q(`UPDATE public.trade_analytics SET result_state='SL_HIT',exit_timestamp=$2 WHERE id=$1`, [ids[0], exitAt]);
  const marker = {kind: 'EXIT_MARKER', event_at: exitAt, exit_reason: 'SL_HIT'};
  const markerResult = await apply(ids[0], 3, 'exit-marker', marker);
  assert.equal(markerResult.option_reference.status, 'EXIT_PENDING');
  const exit = {kind: 'EXIT_CAPTURE', event_at: exitAt, exit_reason: 'SL_HIT',
    evidence: evidence(149.75, '2026-10-30T09:40:02+05:30')};
  const exitResult = await apply(ids[0], 4, 'exit-1', exit);
  assert.equal(exitResult.option_reference.status, 'CLOSED');
  assert.equal(Number(exitResult.option_reference_pnl_points), 9.5);
  const row = (await q(`SELECT result_state,score,pnl_points,option_pnl_points,exit_price,
    exit_timestamp,option_reference_pnl_points,market_context FROM public.trade_analytics WHERE id=$1`, [ids[0]])).rows[0];
  assert.deepEqual([row.result_state, row.score, Number(row.pnl_points), Number(row.option_pnl_points),
    Number(row.exit_price), Number(row.option_reference_pnl_points)], ['SL_HIT', 2, 12.5, 77, 21800, 9.5]);
  assert.deepEqual(row.market_context.unrelated, {keep: true});
  assert.equal(row.market_context.option_reference.milestones.T1.source, 'completed_candle_touch');
  assert.equal((await rpc('list_incomplete_option_reference', [100, 0])).length, 1);

  // The independent formula accepts only the matching completed-minute close
  // when an exact LTP is not available.
  const approximateEntry = {kind: 'ENTRY_CAPTURE', event_at: start,
    evidence: evidence(140, '2026-10-30T09:21:00+05:30', 'completed_1m_close', true)};
  await assert.rejects(() => apply(ids[1], 1, 'bad-close', {...approximateEntry,
    evidence: evidence(140, '2026-10-30T09:22:00+05:30', 'completed_1m_close', true)}), /matching completed/);
  await apply(ids[1], 1, 'entry-approx', approximateEntry);
  await q(`UPDATE public.trade_analytics SET result_state='SL_HIT',exit_timestamp=$2 WHERE id=$1`, [ids[1], exitAt]);
  await apply(ids[1], 2, 'exit-marker-approx', {kind: 'EXIT_MARKER', event_at: exitAt, exit_reason: 'SL_HIT'});
  const approximateExit = await apply(ids[1], 3, 'exit-approx', {kind: 'EXIT_CAPTURE', event_at: exitAt,
    exit_reason: 'SL_HIT', evidence: evidence(135, '2026-10-30T09:41:00+05:30', 'completed_1m_close', true)});
  assert.equal(Number(approximateExit.option_reference_pnl_points), -5);
  assert.equal((await rpc('list_incomplete_option_reference', [100, 0])).length, 0);
  await q(`UPDATE public.trade_analytics
    SET market_context=jsonb_set(market_context,'{option_reference_milestones}',
      '{"T2":{"event_at":"2026-10-30T09:35:00+05:30","spot_target":22900,
        "spot_price":22899,"source":"spot_observation","time_basis":"spot_observation"}}'::jsonb,true)
    WHERE id=$1`, [ids[0]]);
  const recoveredMilestones = await rpc('list_incomplete_option_reference', [100, 0]);
  assert.equal(recoveredMilestones.length, 1);
  assert.equal(recoveredMilestones[0].market_context.option_reference_milestones.T2.spot_target, 22900);
  console.log('Option reference capture isolated PostgreSQL checks passed');
} finally {
  await db.close();
}
