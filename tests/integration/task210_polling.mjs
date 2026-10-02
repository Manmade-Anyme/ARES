// Database contracts for bounded polling and UUID cutover compatibility.
// PGLITE_MODULE_URL may point to an isolated temporary PGlite installation.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
const { PGlite } = await import(process.env.PGLITE_MODULE_URL || '@electric-sql/pglite');
const migration = await readFile(new URL('../../migrations/2026-10-01-task210-llm-predictions.sql', import.meta.url), 'utf8');
let checks = 0;
function equal(actual, expected, label) {
  assert.deepEqual(actual, expected, label);
  checks++;
}
for (const mode of ['bridge', 'greenfield', 'cutover-after-apply']) {
  const db = new PGlite();
  try {
    const greenfield = mode === 'greenfield';
    let parent = greenfield ? 'id' : 'signal_uuid';
    let child = greenfield ? 'signal_id' : 'signal_uuid';
    await db.exec(`
      CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
      CREATE TABLE ares_signals (${greenfield ? 'id uuid PRIMARY KEY, legacy_id bigint' :
        'id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, signal_uuid uuid NOT NULL UNIQUE'});
      CREATE TABLE ml_collection (snapshot_uuid uuid PRIMARY KEY,
        ${greenfield ? 'signal_id uuid, legacy_signal_id bigint' : 'signal_uuid uuid, signal_id bigint'},
        timestamp timestamptz);
      GRANT SELECT ON ares_signals, ml_collection TO service_role;
    `);
    await db.exec(migration);
    if (mode === 'cutover-after-apply') {
      // The TASK-150 column renames, including retained legacy numeric IDs.
      await db.exec(`ALTER TABLE ares_signals RENAME COLUMN id TO legacy_id;
        ALTER TABLE ares_signals RENAME COLUMN signal_uuid TO id;
        ALTER TABLE ares_signals DROP CONSTRAINT ares_signals_pkey;
        ALTER TABLE ares_signals ADD PRIMARY KEY(id);
        ALTER TABLE ml_collection RENAME COLUMN signal_id TO legacy_signal_id;
        ALTER TABLE ml_collection RENAME COLUMN signal_uuid TO signal_id;`);
      parent = 'id'; child = 'signal_id';
    }
    equal((await db.query("SELECT jev_signal_uuid_column('ares_signals') AS col")).rows[0].col, parent, mode + ': parent UUID');
    equal((await db.query("SELECT jev_signal_uuid_column('ml_collection') AS col")).rows[0].col, child, mode + ': child UUID');
    // Eligibility boundaries use actual database time; isolate from wall-clock session hours.
    await db.exec(`CREATE OR REPLACE FUNCTION jev_in_session(p_now timestamptz)
      RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY INVOKER AS $$ SELECT true $$;
      INSERT INTO llm_consumer_state(consumer_id, live_from) VALUES ('jev-primary', clock_timestamp() - interval '1 day');
      INSERT INTO ares_signals(${parent}) SELECT md5('expired-' || i)::uuid FROM generate_series(1,200) i;
      INSERT INTO ml_collection(snapshot_uuid, ${child}, timestamp)
        SELECT md5('snapshot-' || i)::uuid, md5('expired-' || i)::uuid,
        clock_timestamp() - interval '1 hour' FROM generate_series(1,200) i;`);
    async function snapshot(age) {
      const signal = randomUUID(), snap = randomUUID();
      await db.query(`INSERT INTO ares_signals(${parent}) VALUES ($1)`, [signal]);
      await db.query(`INSERT INTO ml_collection(snapshot_uuid, ${child}, timestamp)
        VALUES ($1,$2,clock_timestamp()-make_interval(secs=>$3))`, [snap, signal, age]);
      return { signal, snap };
    }
    async function poll(consumer = 'jev-primary') {
      const rows = (await db.query("SELECT poll_jev_signals($1,'v1.2','v1.1','jev') AS snapshot", [consumer])).rows;
      return rows.map(row => row.snapshot);
    }
    async function claim(row) {
      return (await db.query(`SELECT * FROM claim_llm_prediction_job(
        $1,$2,30,$3,'v1.2','v1.1','jev',$4,999,'jev-primary')`,
        [row.signal_uuid, randomUUID(), row.snapshot_uuid, row.timestamp])).rows;
    }
    equal(await poll(), [], mode + ': expired history is not returned');
    await snapshot(-30); // Future time is also ineligible.
    await snapshot(90000); // Before the persisted rollout cutoff.
    for (let i = 0; i < 11; i++) await snapshot(20 + i);
    const first = await poll();
    equal(first.length, 10, mode + ': ten fresh distinct signals');
    equal(new Set(first.map(row => row.signal_uuid)).size, 10, mode + ': normalized UUIDs');
    equal(first.every(row => Date.now() - Date.parse(row.timestamp) < 60000), true, mode + ': only fresh snapshots');
    for (const row of first) equal((await claim(row)).length, 1, mode + ': cutover-aware claim');
    const remaining = await poll();
    equal(remaining.length, 1, mode + ': older eleventh signal survives claimed batch');
    equal(first.some(row => row.signal_uuid === remaining[0].signal_uuid), false, mode + ': active claims excluded');
    const read = (await db.query('SELECT read_jev_signal($1) AS signal', [remaining[0].signal_uuid])).rows;
    equal(read[0].signal.signal_uuid, remaining[0].signal_uuid, mode + ': signal read normalized');
    await db.query('UPDATE llm_prediction_jobs SET lease_expires_at=clock_timestamp()-interval \'1 second\' WHERE signal_uuid=$1', [first[0].signal_uuid]);
    equal((await poll()).length, 2, mode + ': expired uninvoked lease is recoverable');
    await db.query("UPDATE llm_prediction_jobs SET status='INVOKING', invocation_started_at=clock_timestamp() WHERE signal_uuid=$1", [first[0].signal_uuid]);
    equal((await poll()).length, 1, mode + ': dispatched claim never re-polled');
    // Ten rejected alerts in backoff must not hide the later pending result.
    equal((await claim(remaining[0])).length, 1, mode + ': claim pending alert fixture');
    await db.exec(`INSERT INTO llm_predictions(signal_uuid,t1_hit_prob,t2_hit_prob,sl_hit_prob,
      regime,engine_name,context_version,question_version,input_state,raw_response,signal_timestamp)
      SELECT signal_uuid,.6,.3,.2,'trending','jev','v1.2','v1.1','{}','{}',event_at
      FROM llm_prediction_jobs;
      UPDATE llm_prediction_jobs j SET status='COMPLETED',alert_status='RETRYABLE',
        prediction_id=p.id,alert_backoff_until=clock_timestamp()+interval '30 seconds'
      FROM llm_predictions p WHERE p.signal_uuid=j.signal_uuid;`);
    await db.query("UPDATE llm_prediction_jobs SET alert_status='PENDING',alert_backoff_until=NULL WHERE signal_uuid=$1", [remaining[0].signal_uuid]);
    const alerts = async () => (await db.query("SELECT * FROM poll_jev_alert_jobs('jev-primary')")).rows;
    equal((await alerts()).map(row => row.signal_uuid), [remaining[0].signal_uuid], mode + ': backoff filtered before limit');
    await db.query("UPDATE llm_prediction_jobs SET alert_backoff_until=clock_timestamp()-interval '1 second', expires_at=clock_timestamp()+interval '10 seconds' WHERE signal_uuid=$1", [first[0].signal_uuid]);
    equal((await alerts()).map(row => row.signal_uuid), [first[0].signal_uuid, remaining[0].signal_uuid], mode + ': ready retry ordered by earliest expiry');
    await db.query("UPDATE llm_prediction_jobs SET expires_at=statement_timestamp()+interval '20 seconds' WHERE signal_uuid=ANY($1::uuid[])", [[first[0].signal_uuid, remaining[0].signal_uuid]]);
    equal((await alerts()).map(row => row.signal_uuid), [first[0].signal_uuid, remaining[0].signal_uuid], mode + ': equal expiry ordered by job ID');
    await db.query("UPDATE llm_prediction_jobs SET expires_at=clock_timestamp() WHERE signal_uuid=$1", [first[0].signal_uuid]);
    equal((await alerts()).map(row => row.signal_uuid), [remaining[0].signal_uuid], mode + ': expired retry excluded');
    // Preserve an unclaimed older event for the persisted-policy assertion below.
    await snapshot(20);
    await db.exec('UPDATE llm_consumer_state SET max_signal_age_seconds=2');
    equal(await poll(), [], mode + ': persisted smaller age policy wins');
    equal(await poll('missing'), [], mode + ': absent consumer fails closed');
    await db.exec('UPDATE llm_consumer_state SET max_signal_age_seconds=0');
    equal(await poll(), [], mode + ': invalid age policy fails closed');
    await db.exec(`UPDATE llm_consumer_state SET max_signal_age_seconds=60;
      CREATE OR REPLACE FUNCTION jev_in_session(p_now timestamptz)
      RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY INVOKER AS $$ SELECT false $$;`);
    equal(await poll(), [], mode + ': session close fails closed');
    equal(await alerts(), [], mode + ': alert recovery closed session');
    await db.exec('SET ROLE anon');
    await assert.rejects(poll(), /permission denied/); checks++;
    await assert.rejects(alerts(), /permission denied/); checks++;
    await assert.rejects(db.query('SELECT read_jev_signal($1)', [remaining[0].signal_uuid]), /permission denied/); checks++;
    await db.exec('RESET ROLE; SET ROLE service_role');
    equal(await poll(), [], mode + ': service role may call bounded poll');
    equal(await alerts(), [], mode + ': service role may call alert recovery');
  } finally {
    await db.close();
  }
}
console.log(`${checks} polling/UUID contract assertions passed`);
