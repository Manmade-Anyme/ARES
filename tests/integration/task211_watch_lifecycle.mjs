// Isolated PostgreSQL lifecycle checks. Never uses production credentials.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
const { PGlite } = await import(process.env.PGLITE_MODULE_URL || '@electric-sql/pglite');
const db = new PGlite();
let checks = 0;
const q = (sql, args = []) => db.query(sql, args);
const eq = (a, b, why) => { assert.deepEqual(a, b, why); checks++; };
try {
  await db.exec('CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;');
  const migration = await readFile(new URL('../../migrations/2026-10-08-task211-oi-watch-jev.sql', import.meta.url), 'utf8');
  await db.exec(migration);
  await db.exec(migration); // Safe repeated rollout.
  eq((await q(`SELECT oi_watch_in_session('2026-10-08 03:45:00Z') AS open,
    oi_watch_in_session('2026-10-08 09:59:59Z') AS last,
    oi_watch_in_session('2026-10-08 10:00:00Z') AS closed`)).rows[0],
    {open:true,last:true,closed:false}, 'IST boundaries');
  await db.exec(`CREATE OR REPLACE FUNCTION oi_watch_in_session(p_now timestamptz) RETURNS boolean
    LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=public,pg_temp AS $$ SELECT true $$;
    CREATE OR REPLACE FUNCTION oi_watch_session_end(p_now timestamptz) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=public,pg_temp AS $$ SELECT p_now+interval '1 hour' $$;`);
  const producer = randomUUID();
  const base = {embeds:[{title:'original',fields:[{name:'Time',value:'09:28:33'}],footer:{text:'original'}}]};
  const cancel = {embeds:[{title:'canceled',fields:[{name:'Cancelled at',value:'__WORKER_TIME__'}]}]};
  async function enqueue(age=0, run=producer, id=randomUUID(), state={spot:22489.75}) {
    return (await q(`SELECT * FROM enqueue_oi_watch($1,$2,clock_timestamp()-make_interval(secs=>$3),$4,$5,$6,8,60)`,
      [id,run,age,JSON.stringify(state),JSON.stringify(base),JSON.stringify(cancel)])).rows[0];
  }
  const row = id => q('SELECT * FROM oi_watch_predictions WHERE event_id=$1',[id]).then(r=>r.rows[0]);
  const transition = (id,action,payload=cancel) => q('SELECT * FROM transition_oi_watch($1,$2,$3)',[id,action,JSON.stringify(payload)]);
  const claim = (id,token=randomUUID()) => q('SELECT * FROM claim_oi_watch_inference($1,$2)',[id,token]).then(r=>r.rows.map(x=>x.claim_oi_watch_inference));
  const complete = (id,token,prediction={close_probability:.7},error=null) => q('SELECT * FROM complete_oi_watch_inference($1,$2,$3,$4)',[id,token,prediction===null?null:JSON.stringify(prediction),error]);
  const begin = (id,token=randomUUID(),isc=false,payload=base) => q('SELECT * FROM begin_oi_watch_delivery($1,$2,$3,$4)',[id,token,JSON.stringify(payload),isc]).then(r=>r.rows.map(x=>x.begin_oi_watch_delivery));
  const finish = (id,token,status,isc=false,retry=0) => q('SELECT * FROM finish_oi_watch_delivery($1,$2,$3,$4,$5)',[id,token,status,isc,retry]);
  const polls = () => q('SELECT * FROM poll_oi_watch_delivery()').then(r=>r.rows.map(x=>x.poll_oi_watch_delivery));

  const original=await enqueue();
  await enqueue(0,producer,original.event_id,{spot:1});
  eq((await row(original.event_id)).input_state,{spot:22489.75},'duplicate enqueue cannot replace snapshot');
  await assert.rejects(q('UPDATE oi_watch_predictions SET input_state=$2 WHERE event_id=$1',[original.event_id,'{}']),/immutable/); checks++;
  eq(await enqueue(-30),undefined,'future observations rejected');
  eq(await enqueue(61),undefined,'expired observations rejected');
  eq((await begin(original.event_id)).length,0,'no premature fallback before deadline');
  const token=randomUUID(), owned=(await claim(original.event_id,token))[0];
  eq(owned.remaining_seconds>0,true,'DB returns positive dispatch budget');
  eq((await claim(original.event_id)).length,0,'single inference owner');
  eq((await complete(original.event_id,randomUUID())).rows.length,0,'wrong inference token denied');
  await complete(original.event_id,token);
  await complete(original.event_id,token,{close_probability:.1});
  eq((await row(original.event_id)).prediction,{close_probability:.7},'completion retry preserves first result');
  const sendToken=randomUUID();
  const send=(await begin(original.event_id,sendToken))[0];
  eq(send.remaining_seconds>0,true,'DB returns delivery freshness budget');
  eq((await begin(original.event_id)).length,0,'competing delivery denied');
  await finish(original.event_id,randomUUID(),'SENT');
  eq((await row(original.event_id)).delivery_status,'SENDING','wrong send token denied');
  await finish(original.event_id,sendToken,'SENT');
  eq((await begin(original.event_id)).length,0,'accepted watch never replayed');
  await transition(original.event_id,'CANCEL');
  eq((await polls()).some(x=>x.event_id===original.event_id&&x.is_cancellation),true,'sent watch cancellation eligible');
  const ct=randomUUID(); await begin(original.event_id,ct,true,cancel); await finish(original.event_id,ct,'SENT',true);
  eq((await begin(original.event_id,randomUUID(),true,cancel)).length,0,'cancellation not replayed');

  const early=await enqueue(); await transition(early.event_id,'CANCEL');
  eq((await row(early.event_id)).delivery_status,'SKIPPED','cancel suppresses unsent watch');
  eq((await claim(early.event_id)).length,0,'cancel suppresses inference');
  eq((await begin(early.event_id)).length,0,'cancel cannot leak late watch');
  eq((await begin(early.event_id,randomUUID(),true,cancel)).length,0,'unsent watch needs no cancellation');
  const consumed=await enqueue(); await transition(consumed.event_id,'CONSUME');
  eq((await begin(consumed.event_id)).length,0,'consumed watch suppressed');
  await transition(consumed.event_id,'CANCEL'); eq((await row(consumed.event_id)).lifecycle,'CANCELED','cancel after consume allowed');
  const resolved=await enqueue(); await transition(resolved.event_id,'RESOLVE'); await transition(resolved.event_id,'CANCEL');
  eq((await row(resolved.event_id)).lifecycle,'RESOLVED','cancel never resurrects confirmed signal');

  const fallback=await enqueue(9); const ft=randomUUID();
  eq((await begin(fallback.event_id,ft)).length,1,'deadline enables unavailable fallback');
  await finish(fallback.event_id,ft,'RETRY',false,30);
  eq((await begin(fallback.event_id)).length,0,'explicit rejection backoff honored');
  await q("UPDATE oi_watch_predictions SET next_attempt_at=clock_timestamp()-interval '1 second' WHERE event_id=$1",[fallback.event_id]);
  const rt=randomUUID(); const retry=(await begin(fallback.event_id,rt,false,{content:'replacement'}))[0];
  eq(retry.delivery_payload,base,'explicit retry pins first payload');
  await finish(fallback.event_id,rt,'UNKNOWN');
  eq((await begin(fallback.event_id)).length,0,'ambiguous request never retried');
  const crashed=await enqueue(9); await begin(crashed.event_id);
  await q("UPDATE oi_watch_predictions SET delivery_started_at=clock_timestamp()-interval '11 seconds' WHERE event_id=$1",[crashed.event_id]);
  await polls(); eq((await row(crashed.event_id)).delivery_status,'UNKNOWN','restart recovery never resends crashed transport');
  const recoveredToken=(await row(crashed.event_id)).delivery_token;
  await finish(crashed.event_id,recoveredToken,'SENT');
  eq((await row(crashed.event_id)).delivery_status,'SENT','late confirmed acceptance archives without another POST');
  await transition(crashed.event_id,'CANCEL');
  const crashedCancelToken=randomUUID(); await begin(crashed.event_id,crashedCancelToken,true,cancel);
  await q("UPDATE oi_watch_predictions SET cancellation_started_at=clock_timestamp()-interval '11 seconds' WHERE event_id=$1",[crashed.event_id]);
  await polls(); eq((await row(crashed.event_id)).cancellation_status,'UNKNOWN','crashed cancellation never replayed');
  await finish(crashed.event_id,crashedCancelToken,'SENT',true);
  eq((await row(crashed.event_id)).cancellation_status,'SENT','late known cancellation acceptance can be archived');
  const lateResult=await enqueue(9);
  await q("UPDATE oi_watch_predictions SET prediction_status='INVOKING',invocation_token=$2 WHERE event_id=$1",[lateResult.event_id,token]);
  await complete(lateResult.event_id,token);
  eq((await polls()).find(x=>x.event_id===lateResult.event_id).prediction_available_within_deadline,false,'late SDK response cannot replace deadline fallback');
  await assert.rejects(transition(original.event_id,null),/Invalid watch action/); checks++;
  await assert.rejects(finish(original.event_id,sendToken,null),/Invalid watch delivery/); checks++;

  const race=await enqueue(9), raceToken=randomUUID(); await begin(race.event_id,raceToken); await transition(race.event_id,'CANCEL');
  eq((await begin(race.event_id,randomUUID(),true,cancel)).length,0,'cancellation waits for confirmed watch acceptance');
  await finish(race.event_id,raceToken,'SENT');
  eq((await begin(race.event_id,randomUUID(),true,cancel)).length,1,'in-flight accepted watch receives cancellation');
  const closure={embeds:[{title:'superseded',fields:[{name:'Reason',value:'Watch superseded by confirmed signal'}]}]};
  const overtaken=await enqueue(9), overtakenToken=randomUUID();
  const overtakenClaim=(await begin(overtaken.event_id,overtakenToken))[0];
  eq(overtakenClaim.remaining_seconds<=5,true,'transport claim window capped at five seconds');
  await transition(overtaken.event_id,'CONSUME'); await transition(overtaken.event_id,'RESOLVE',closure);
  eq((await row(overtaken.event_id)).lifecycle,'RESOLVED','confirmed signal prevents new watch dispatch');
  await finish(overtaken.event_id,overtakenToken,'SENT');
  eq((await row(overtaken.event_id)).lifecycle,'CANCELED','late accepted watch after confirmed signal requires closure');
  eq((await row(overtaken.event_id)).cancellation_payload,closure,'late closure explains confirmed-signal supersession');
  eq((await begin(overtaken.event_id,randomUUID(),true,closure)).length,1,'late accepted watch compensation deliverable');
  const uncertain=await enqueue(9), uncertainToken=randomUUID();
  await begin(uncertain.event_id,uncertainToken); await finish(uncertain.event_id,uncertainToken,'UNKNOWN');
  await transition(uncertain.event_id,'RESOLVE',closure); await finish(uncertain.event_id,uncertainToken,'SENT');
  eq((await row(uncertain.event_id)).lifecycle,'CANCELED','same-token late known acceptance after UNKNOWN gets compensation');
  const beforeSignal=await enqueue(9), beforeToken=randomUUID();
  await begin(beforeSignal.event_id,beforeToken); await finish(beforeSignal.event_id,beforeToken,'SENT');
  await transition(beforeSignal.event_id,'RESOLVE',closure);
  eq((await row(beforeSignal.event_id)).lifecycle,'RESOLVED','normally accepted watch resolves without cancellation');
  eq((await row(beforeSignal.event_id)).cancellation_payload,null,'normal resolution never queues compensation');
  const late=await enqueue(), lt=randomUUID(); await claim(late.event_id,lt); await transition(late.event_id,'CANCEL');
  await complete(late.event_id,lt);
  eq((await row(late.event_id)).prediction_status,'COMPLETED','late prediction archived');
  eq((await begin(late.event_id)).length,0,'late prediction never revives canceled alert');
  const invocationCrash=await enqueue(); await claim(invocationCrash.event_id);
  eq((await claim(invocationCrash.event_id)).length,0,'inference crash cannot replay SDK invocation');
  const restart=await enqueue(9), restartToken=randomUUID(); await begin(restart.event_id,restartToken); await finish(restart.event_id,restartToken,'SENT');
  await q('SELECT restart_oi_watches($1)',[randomUUID()]);
  eq((await row(restart.event_id)).lifecycle,'CANCELED','new producer cancels prior unresolved watches');
  eq((await row(restart.event_id)).cancellation_payload,cancel,'restart stores immutable cancellation template');

  const oldFresh=await enqueue(), currentRun=randomUUID();
  // Covers the app reaching readiness before the consumer, and a consumer-only
  // restart. Repeated same-run recovery must preserve current actionable rows.
  const currentFresh=await enqueue(0,currentRun);
  await q('SELECT restart_oi_watches($1)',[currentRun]);
  eq((await row(oldFresh.event_id)).lifecycle,'CANCELED','fresh prior-run watch canceled before workers poll');
  eq((await claim(oldFresh.event_id)).length,0,'prior-run watch cannot reach inference');
  eq((await begin(oldFresh.event_id)).length,0,'prior-run watch cannot reach webhook');
  eq((await row(currentFresh.event_id)).lifecycle,'ACTIVE','late consumer recovery preserves current app watch');
  await q('SELECT restart_oi_watches($1)',[currentRun]);
  eq((await row(currentFresh.event_id)).lifecycle,'ACTIVE','same-run consumer restart preserves current app watch');
  eq((await claim(currentFresh.event_id)).length,1,'current-run inference remains available after recovery');

  await db.exec(`CREATE OR REPLACE FUNCTION oi_watch_in_session(p_now timestamptz) RETURNS boolean
    LANGUAGE sql IMMUTABLE SECURITY INVOKER SET search_path=public,pg_temp AS $$ SELECT false $$;`);
  await polls();
  eq((await row(invocationCrash.event_id)).delivery_status,'SKIPPED','closed session suppresses pending delivery');
  eq((await begin(restart.event_id,randomUUID(),true,cancel)).length,1,'cancellation remains deliverable after close');
  eq((await q("SELECT has_table_privilege('anon','oi_watch_predictions','SELECT') AS allowed")).rows[0].allowed,false,'anon has no watch access');
  eq((await q("SELECT has_function_privilege('authenticated','poll_oi_watch_delivery()','EXECUTE') AS allowed")).rows[0].allowed,false,'authenticated cannot poll watch outbox');
  eq((await q("SELECT has_table_privilege('service_role','oi_watch_predictions','SELECT') AS allowed")).rows[0].allowed,true,'service role has watch access');
  console.log(`TASK-211 watch lifecycle: ${checks} checks passed`);
} finally { await db.close(); }
