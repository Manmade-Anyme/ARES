// Isolated PostgreSQL contract for the one-time 26–30 Oct 2026 backfill.
// Uses PGlite only; this test never connects to production Supabase.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const { PGlite } = await import(process.env.PGLITE_MODULE_URL || '@electric-sql/pglite');
const db = new PGlite();
const rpc = async (name, args) => (await db.query(
  `SELECT public.${name}(${args.map((_, i) => `$${i + 1}`).join(',')}) AS result`, args,
)).rows[0].result;

const ids = Array.from({length: 16}, (_, index) =>
  `00000000-0000-0000-0000-${String(index + 100).padStart(12, '0')}`);
const now = new Date().toISOString();
const timestamp = (day, minute) => `2026-10-${day}T${minute}+05:30`;
const pin = (index) => ({
  security_id: String(70000 + index), exchange_segment: 'NSE_FNO',
  expiry_date: '2026-11-03', strike: 22400 + index * 50,
  option_type: index % 2 ? 'CE' : 'PE', lot_size: 65,
});
const evidence = (index, eventAt, price, provider = 'dhan_intraday_minute') => {
  const event = new Date(eventAt);
  event.setSeconds(0, 0);
  const barStart = event.toISOString();
  const observedAt = new Date(event.getTime() + 60000).toISOString();
  const provenance = {
    provider,
    requested_security_id: String(70000 + index),
    requested_exchange_segment: 'NSE_FNO',
    requested_expiry_date: '2026-11-03',
    requested_strike: 22400 + index * 50,
    requested_option_type: index % 2 ? 'CE' : 'PE',
    requested_lot_size: 65,
    bar_start: barStart,
    sampled_at: now,
    complete: true,
    open: price - 1,
    high: price + 1,
    low: price - 2,
    close: price,
    volume: 100,
  };
  if (provider === 'dhan_expired_rolling_options') {
    Object.assign(provenance, {
      underlying_security_id: '13', expiry_flag: 'WEEK', expiry_code: 1,
      moneyness: 'ATM', returned_strike: 22400 + index * 50,
    });
  }
  return {
    price, observed_at: observedAt, source: 'completed_1m_close', approximate: true,
    provenance,
  };
};
const payload = ids.map((id, index) => {
  const entryAt = timestamp(26 + Math.floor(index / 4), '09:20:00');
  const exitAt = timestamp(26 + Math.floor(index / 4), '09:40:00');
  return {
    id, contract: pin(index), entry_event_at: entryAt, exit_event_at: exitAt,
    exit_reason: index % 2 ? 'ATR_SL_HIT' : 'SL_HIT',
    entry: evidence(index, entryAt, 100 + index),
    exit: evidence(index, exitAt, 110 + index,
      index === 0 ? 'dhan_expired_rolling_options' : 'dhan_intraday_minute'),
  };
});

try {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role;
    CREATE TABLE public.trade_analytics(
      id uuid PRIMARY KEY, entry_timestamp timestamptz, exit_timestamp timestamptz,
      result_state text, score integer, pnl_points numeric, option_pnl_points numeric,
      exit_price numeric, time_metrics_excluded boolean, market_context jsonb);
    GRANT USAGE ON SCHEMA public TO service_role,anon,authenticated;
    GRANT SELECT,UPDATE ON public.trade_analytics TO service_role;`);
  const migration = await readFile(new URL('../../migrations/20261030_option_reference_pnl.sql', import.meta.url), 'utf8');
  await db.exec(migration);
  await db.exec(migration);

  for (let i = 0; i < ids.length; i++) {
    const entryAt = payload[i].entry_event_at;
    const exitAt = payload[i].exit_event_at;
    await db.query(`INSERT INTO public.trade_analytics
      (id,entry_timestamp,exit_timestamp,result_state,score,pnl_points,option_pnl_points,
       exit_price,time_metrics_excluded,market_context)
      VALUES ($1,$2,$3,$4,2,12.5,77,21800,false,$5)`,
    [ids[i], entryAt, exitAt, payload[i].exit_reason,
      JSON.stringify({options_sizing: pin(i), unrelated: {keep: true}})]);
  }
  // This closed row is outside the report denominator and must not be required.
  const excludedId = '00000000-0000-0000-0000-000000000999';
  await db.query(`INSERT INTO public.trade_analytics
    (id,entry_timestamp,exit_timestamp,result_state,pnl_points,time_metrics_excluded,market_context)
    VALUES ($1,'2026-10-30T09:20:00+05:30','2026-10-30T09:40:00+05:30','SL_HIT',1,true,'{}')`, [excludedId]);

  assert.deepEqual(await rpc('option_reference_capture_capabilities', []), {
    schema_version: 'one_lot_hold_to_exit_v1', ready: true,
  });
  for (const role of ['anon', 'authenticated']) {
    await db.exec(`SET ROLE ${role}`);
    await assert.rejects(
      () => rpc('apply_option_reference_weekly_backfill', [JSON.stringify(payload)]),
      /permission denied/,
    );
    await db.exec('RESET ROLE');
  }

  await db.exec('SET ROLE service_role');
  await assert.rejects(
    () => rpc('apply_option_reference_weekly_backfill', [JSON.stringify(payload.slice(0, 15))]),
    /exactly 16/,
  );
  const badStrike = structuredClone(payload);
  badStrike[15].entry.provenance.returned_strike = badStrike[15].contract.strike + 50;
  badStrike[15].entry.provenance.provider = 'dhan_expired_rolling_options';
  badStrike[15].entry.provenance.underlying_security_id = '13';
  badStrike[15].entry.provenance.expiry_flag = 'WEEK';
  badStrike[15].entry.provenance.expiry_code = 1;
  badStrike[15].entry.provenance.moneyness = 'ATM';
  await assert.rejects(
    () => rpc('apply_option_reference_weekly_backfill', [JSON.stringify(badStrike)]),
    /returned strike/,
  );
  assert.equal((await db.query(`SELECT count(*) AS n FROM public.trade_analytics
    WHERE market_context->'option_reference' IS NOT NULL`)).rows[0].n, 0);

  for (const [field, mutate] of [
    ['sampled_at', (row) => { delete row.entry.provenance.sampled_at; }],
    ['volume', (row) => { delete row.entry.provenance.volume; }],
  ]) {
    const missingEvidence = structuredClone(payload);
    mutate(missingEvidence[15]);
    await assert.rejects(
      () => rpc('apply_option_reference_weekly_backfill', [JSON.stringify(missingEvidence)]),
      /missing or malformed timestamp, request, or OHLCV provenance/,
      `missing ${field} must be rejected before writes`,
    );
    assert.equal((await db.query(`SELECT count(*) AS n FROM public.trade_analytics
      WHERE market_context->'option_reference' IS NOT NULL`)).rows[0].n, 0,
    `missing ${field} must not partially apply the cohort`);
  }

  const wrongExpiryCode = structuredClone(payload);
  wrongExpiryCode[0].contract.expiry_date = '2026-10-27';
  wrongExpiryCode[0].entry.provenance.requested_expiry_date = '2026-10-27';
  wrongExpiryCode[0].exit.provenance.requested_expiry_date = '2026-10-27';
  await db.query(`UPDATE public.trade_analytics SET market_context=jsonb_set(
      market_context,'{options_sizing,expiry_date}','"2026-10-27"'::jsonb) WHERE id=$1`, [ids[0]]);
  await assert.rejects(
    () => rpc('apply_option_reference_weekly_backfill', [JSON.stringify(wrongExpiryCode)]),
    /expired rolling bar returned strike\/provenance/,
  );
  assert.equal((await db.query(`SELECT market_context->'option_reference' FROM public.trade_analytics WHERE id=$1`,
    [ids[0]])).rows[0].option_reference, undefined);
  await db.query(`UPDATE public.trade_analytics SET market_context=jsonb_set(
      market_context,'{options_sizing,expiry_date}','"2026-11-03"'::jsonb) WHERE id=$1`, [ids[0]]);

  const result = await rpc('apply_option_reference_weekly_backfill', [JSON.stringify(payload)]);
  assert.equal(result.success, true);
  assert.equal(result.backfilled, 16);
  assert.equal(result.idempotent, false);
  const stored = (await db.query(`SELECT id,result_state,score,pnl_points,option_pnl_points,
      exit_price,exit_timestamp,option_reference_pnl_points,market_context
    FROM public.trade_analytics WHERE id=$1`, [ids[0]])).rows[0];
  assert.equal(Number(stored.option_reference_pnl_points), 10);
  assert.deepEqual([stored.result_state, stored.score, Number(stored.pnl_points),
    Number(stored.option_pnl_points), Number(stored.exit_price)], ['SL_HIT', 2, 12.5, 77, 21800]);
  assert.equal(stored.market_context.unrelated.keep, true);
  assert.equal(stored.market_context.option_reference.status, 'CLOSED');
  assert.equal(stored.market_context.option_reference.entry.approximate, true);
  assert.equal(stored.market_context.option_reference.exit.provenance.complete, true);
  assert.equal((await db.query(`SELECT count(*) AS n FROM public.trade_analytics
    WHERE market_context->'option_reference'->>'formula_version'='one_lot_hold_to_exit_v1'`)).rows[0].n, 16);
  assert.equal((await db.query(`SELECT market_context->'option_reference' FROM public.trade_analytics WHERE id=$1`,
    [excludedId])).rows[0].option_reference, undefined);

  const replay = await rpc('apply_option_reference_weekly_backfill', [JSON.stringify(payload)]);
  assert.equal(replay.success, true);
  assert.equal(replay.backfilled, 16);
  assert.equal(replay.idempotent, true);
  const conflicting = structuredClone(payload);
  conflicting[0].entry.price = 500;
  conflicting[0].entry.provenance.open = 500;
  conflicting[0].entry.provenance.high = 501;
  conflicting[0].entry.provenance.low = 499;
  conflicting[0].entry.provenance.close = 500;
  await assert.rejects(
    () => rpc('apply_option_reference_weekly_backfill', [JSON.stringify(conflicting)]),
    /Capture event id already used for different evidence|Cannot overwrite existing option entry evidence/,
  );
  console.log('Weekly option-reference backfill isolated PostgreSQL checks passed');
} finally {
  await db.close();
}
