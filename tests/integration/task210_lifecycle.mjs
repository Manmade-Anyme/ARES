// Execute the migration against PostgreSQL in PGlite, with no production data.
// PGLITE_MODULE_URL can point to an isolated temporary installation (see assessment).
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';

const { PGlite } = await import(process.env.PGLITE_MODULE_URL || '@electric-sql/pglite');
const db = new PGlite();
const query = (sql, args = []) => db.query(sql, args);
let checks = 0;
function equal(actual, expected, message) {
  assert.deepEqual(actual, expected, message);
  checks += 1;
}

try {
  await db.exec(`
    CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE TABLE ares_signals (signal_uuid uuid PRIMARY KEY, created_at timestamptz DEFAULT clock_timestamp());
    CREATE TABLE ml_collection (snapshot_uuid uuid PRIMARY KEY, signal_uuid uuid, timestamp timestamptz);
    GRANT SELECT ON ares_signals, ml_collection TO service_role;
  `);
  await db.exec(await readFile(new URL('../../migrations/2026-10-01-task210-llm-predictions.sql', import.meta.url), 'utf8'));
  equal((await query(`SELECT jev_in_session('2026-10-02 03:45:00Z') AS open,
    jev_in_session('2026-10-02 09:59:59Z') AS last,
    jev_in_session('2026-10-02 10:00:00Z') AS closed`)).rows[0],
    { open: true, last: true, closed: false }, 'IST session boundaries');
  // All other gates use actual database time. Make trading hours independent
  // of when this test runs; exercise closed-session rejection separately below.
  await db.exec(`CREATE OR REPLACE FUNCTION jev_in_session(p_now timestamptz)
    RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY INVOKER AS $$ SELECT true $$;
    INSERT INTO llm_consumer_state(consumer_id, live_from) VALUES ('jev-primary', clock_timestamp() - interval '1 day');`);

  async function snapshot(age = 0) {
    const signal = randomUUID(), snap = randomUUID();
    await query('INSERT INTO ares_signals(signal_uuid) VALUES ($1)', [signal]);
    await query(`INSERT INTO ml_collection VALUES ($1, $2, clock_timestamp() - make_interval(secs => $3))`, [snap, signal, age]);
    return { signal, snap };
  }
  async function claim(s, owner = randomUUID(), eventOverride = null) {
    // The caller's 999-second setting must never extend the persisted 60-second policy.
    const result = await query(`SELECT * FROM claim_llm_prediction_job($1,$2,30,$3,'v1.1','v1.1','jev',
      coalesce($4::timestamptz,(SELECT timestamp FROM ml_collection WHERE snapshot_uuid=$3)),999,'jev-primary')`,
      [s.signal, owner, s.snap, eventOverride]);
    return result.rows;
  }
  async function completed(s) {
    const job = (await claim(s))[0];
    await query(`INSERT INTO llm_predictions(signal_uuid,t1_hit_prob,t2_hit_prob,sl_hit_prob,regime,
      engine_name,context_version,question_version,input_state,raw_response,signal_timestamp)
      VALUES ($1,.6,.3,.2,'trending','jev','v1.1','v1.1','{}','{}',$2)`, [s.signal, job.event_at]);
    return (await query(`UPDATE llm_prediction_jobs SET status='COMPLETED',alert_status='PENDING',
      prediction_id=(SELECT id FROM llm_predictions WHERE signal_uuid=$1) WHERE id=$2 RETURNING *`, [s.signal, job.id])).rows[0];
  }
  async function begin(s, job, token = randomUUID(), payload = { content: 'original' }) {
    return (await query(`SELECT * FROM begin_jev_alert_attempt($1,$2,$3,'test-owner',$4,'https://example.invalid/webhook')`,
      [s.signal, job.id, token, JSON.stringify(payload)])).rows;
  }

  const delayed = await snapshot(61);
  equal((await claim(delayed)).length, 0, 'new insert cannot refresh a 61-second-old event');
  equal((await query('SELECT count(*)::int AS count FROM llm_prediction_jobs WHERE signal_uuid=$1', [delayed.signal])).rows[0].count, 0, 'stale signal never claimed');
  equal((await claim(await snapshot(-30))).length, 0, 'future-dated event rejected');
  const mismatched = await snapshot();
  equal((await claim(mismatched, randomUUID(), '2026-01-01T00:00:00Z')).length, 0, 'caller cannot replace snapshot event time');
  const prerollout = await snapshot(1);
  await query(`UPDATE llm_consumer_state SET live_from=clock_timestamp()`);
  equal((await claim(prerollout)).length, 0, 'pre-rollout event rejected despite later insertion');
  await query(`UPDATE llm_consumer_state SET live_from=clock_timestamp()-interval '1 day'`);

  const active = await snapshot(), owner = randomUUID();
  const job = (await claim(active, owner))[0];
  equal((new Date(job.expires_at) - new Date(job.event_at))/1000, 60, 'database policy controls expiry');
  equal((await claim(active)).length, 0, 'competing inference owner rejected');
  await query(`UPDATE llm_prediction_jobs SET expires_at=clock_timestamp() WHERE id=$1`, [job.id]);
  equal((await query(`SELECT * FROM begin_llm_invocation($1,$2,$3)`, [active.signal, owner, randomUUID()])).rows.length, 0, 'expiry while context builds prevents invocation');

  const leased = await snapshot(), oldOwner = randomUUID();
  const oldJob = (await claim(leased, oldOwner))[0];
  await query(`UPDATE llm_prediction_jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=$1`, [oldJob.id]);
  equal((await query(`SELECT * FROM begin_llm_invocation($1,$2,$3)`, [leased.signal, oldOwner, randomUUID()])).rows.length, 0, 'expired lease cannot invoke');
  const replacement = (await claim(leased))[0];
  equal(replacement.event_at, oldJob.event_at, 'recovery pins original event');
  equal(replacement.expires_at, oldJob.expires_at, 'recovery never renews expiry');
  equal((await query(`SELECT * FROM begin_llm_invocation($1,$2,$3)`, [leased.signal, oldOwner, randomUUID()])).rows.length, 0, 'superseded owner cannot invoke');
  equal((await query(`SELECT * FROM begin_llm_invocation($1,$2,$3)`, [leased.signal, replacement.owner_token, randomUUID()])).rows.length, 1, 'new owner invokes once');
  equal((await query(`SELECT * FROM begin_llm_invocation($1,$2,$3)`, [leased.signal, replacement.owner_token, randomUUID()])).rows.length, 0, 'invocation marker cannot be replayed');

  const archived = await snapshot(), archiveOwner = randomUUID(), invocation = randomUUID();
  const archiveJob = (await claim(archived, archiveOwner))[0];
  await query('SELECT * FROM begin_llm_invocation($1,$2,$3)', [archived.signal, archiveOwner, invocation]);
  const prediction = { signal_uuid: archived.signal, snapshot_uuid: archived.snap,
    t1_hit_prob: .6, t2_hit_prob: .3, sl_hit_prob: .2, regime: 'trending', regime_distribution: { trending: 1 },
    engine_name: 'jev', context_version: 'v1.1', question_version: 'v1.1',
    input_state: {}, raw_response: { conditional_t2: .5 } };
  const complete = (token = invocation, payload = prediction) => query(
    'SELECT * FROM complete_llm_prediction_job($1,$2,$3)', [archived.signal, token, JSON.stringify(payload)]);
  equal((await complete(randomUUID())).rows.length, 0, 'wrong invocation cannot publish');
  await assert.rejects(complete(invocation, { ...prediction, t1_hit_prob: 2 }), /check constraint/);
  checks += 1;
  equal((await query('SELECT status FROM llm_prediction_jobs WHERE id=$1', [archiveJob.id])).rows[0].status,
    'INVOKING', 'failed result insert rolls back completion');
  const saved = (await complete()).rows[0];
  equal((await query('SELECT status,alert_status,prediction_id FROM llm_prediction_jobs WHERE id=$1', [archiveJob.id])).rows[0],
    { status: 'COMPLETED', alert_status: 'PENDING', prediction_id: saved.id }, 'archive and job complete atomically');
  await query("UPDATE llm_prediction_jobs SET alert_status='SENT' WHERE id=$1", [archiveJob.id]);
  equal((await complete()).rows[0].id, saved.id, 'lost commit acknowledgment returns original result');
  equal((await query('SELECT alert_status FROM llm_prediction_jobs WHERE id=$1', [archiveJob.id])).rows[0].alert_status,
    'SENT', 'persistence retry never resets delivery');

  const delivery = await snapshot(), deliveryJob = await completed(delivery), attemptToken = randomUUID();
  equal((await begin(delivery, deliveryJob, attemptToken)).length, 1, 'first sender owns durable attempt');
  equal((await begin(delivery, deliveryJob)).length, 0, 'second sender cannot take SENDING');
  equal((await query(`SELECT check_jev_alert_freshness($1,$2,$3) AS gate`, [delivery.signal, deliveryJob.id, randomUUID()])).rows[0].gate.is_fresh, false, 'wrong attempt cannot send');
  equal((await query(`SELECT check_jev_alert_freshness($1,$2,$3) AS gate`, [delivery.signal, deliveryJob.id, attemptToken])).rows[0].gate.is_fresh, true, 'matching owner passes final gate');
  await query(`UPDATE llm_prediction_jobs SET alert_attempt_deadline_at=clock_timestamp()-interval '1 second' WHERE id=$1`, [deliveryJob.id]);
  await query(`SELECT recover_llm_prediction_jobs('jev-primary')`);
  equal((await query(`SELECT alert_status FROM llm_prediction_jobs WHERE id=$1`, [deliveryJob.id])).rows[0].alert_status, 'DELIVERY_UNKNOWN', 'abandoned send becomes terminal unknown');
  equal((await begin(delivery, deliveryJob)).length, 0, 'restart cannot resend uncertain delivery');
  equal((await query(`SELECT count(*)::int AS count FROM llm_predictions WHERE signal_uuid=$1`, [delivery.signal])).rows[0].count, 1, 'prediction survives uncertain delivery');

  const rejected = await snapshot(), retryJob = await completed(rejected);
  await begin(rejected, retryJob);
  await query(`UPDATE llm_prediction_jobs SET alert_status='RETRYABLE',alert_backoff_until=clock_timestamp()+interval '10 seconds' WHERE id=$1`, [retryJob.id]);
  equal((await begin(rejected, retryJob)).length, 0, 'backoff blocks early retry');
  await query(`UPDATE llm_prediction_jobs SET alert_backoff_until=clock_timestamp()-interval '1 second',
    alert_attempt_deadline_at=clock_timestamp()-interval '1 second' WHERE id=$1`, [retryJob.id]);
  const retried = (await begin(rejected, retryJob, randomUUID(), { content: 'changed' }))[0];
  equal(retried.alert_payload, { content: 'original' }, 'retry preserves pinned payload');
  equal((await begin(rejected, retryJob)).length, 0, 'retry still has only one owner');

  const expiredRetry = await snapshot(), expiredJob = await completed(expiredRetry);
  await query(`UPDATE llm_prediction_jobs SET alert_status='RETRYABLE',expires_at=clock_timestamp() WHERE id=$1`, [expiredJob.id]);
  equal((await begin(expiredRetry, expiredJob)).length, 0, 'exact expiry rejects retry');
  equal((await query(`SELECT alert_status FROM llm_prediction_jobs WHERE id=$1`, [expiredJob.id])).rows[0].alert_status, 'SUPPRESSED_EXPIRED', 'expiry suppression is durable');

  const session = await snapshot(), sessionJob = await completed(session);
  await db.exec(`CREATE OR REPLACE FUNCTION jev_in_session(p_now timestamptz)
    RETURNS boolean LANGUAGE sql IMMUTABLE SECURITY INVOKER AS $$ SELECT false $$;`);
  equal((await begin(session, sessionJob)).length, 0, 'session close prevents delivery');
  equal((await claim(await snapshot())).length, 0, 'session close prevents new inference');
  await db.exec('SET ROLE anon');
  await assert.rejects(query(`SELECT check_jev_alert_freshness($1,$2)`, [session.signal, sessionJob.id]), /permission denied/);
  checks += 1;
  await db.exec('RESET ROLE; SET ROLE service_role');
  equal((await query(`SELECT check_jev_alert_freshness($1,$2) AS gate`, [session.signal, sessionJob.id])).rows[0].gate.is_fresh, false, 'service role can execute gate');
  console.log(`${checks} PostgreSQL lifecycle assertions passed`);
} finally {
  await db.close();
}
