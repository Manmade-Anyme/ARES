"""Public worker contracts with isolated RPC/HTTP/inference substitutes."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from threading import Barrier, Event, Lock, current_thread
import pytest
import httpx

from system_one import watch_consumer as worker


class FakeDB:
    """In-memory RPC boundary; production clients are never constructed."""
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls = []

    def rpc(self, name, params=None):
        self.calls.append((name, deepcopy(params or {})))
        value = self.rows.get(name, [{}] if name == "complete_oi_watch_inference" else [])
        def execute():
            if isinstance(value, Exception):
                raise value
            return SimpleNamespace(data=deepcopy(value))
        return SimpleNamespace(execute=execute)


def watch(**updates):
    data = {"event_id": "event", "input_state": {"spot": 22489.75},
            "prediction_status": "COMPLETED", "prediction": {"close_probability": .7},
            "base_payload": {"embeds": [{"title": "original", "fields": [{"name": "Time", "value": "09:28:33"}],
                                          "footer": {"text": "original footer"}}]},
            "remaining_seconds": 5.0, "is_cancellation": False,
            "prediction_available_within_deadline": True}
    data.update(updates)
    return data


@pytest.fixture
def formatter(monkeypatch):
    monkeypatch.setattr(worker, "format_watch_prediction", lambda state, prediction=None: "prediction" if prediction else "unavailable")


def test_combined_watch_preserves_every_original_field(formatter):
    row = watch(); original = deepcopy(row["base_payload"])
    payload = worker.compose_watch_payload(row)
    assert row["base_payload"] == original
    assert payload["embeds"][0]["fields"][:-1] == original["embeds"][0]["fields"]
    assert payload["embeds"][0]["fields"][-1] == {"name": "🧠 JEV Prediction", "value": "prediction", "inline": False}
    assert payload["embeds"][0]["footer"] == original["embeds"][0]["footer"]
    assert payload["embeds"][0]["title"] == "original"
    assert worker.compose_watch_payload(watch(prediction_status="INVOKING"))["embeds"][0]["fields"][-1]["value"] == "unavailable"
    assert worker.compose_watch_payload(watch(prediction_available_within_deadline=False))["embeds"][0]["fields"][-1]["value"] == "unavailable"


def test_cancellation_payload_preserves_actual_time_and_stamps_restart_only():
    actual = {"embeds": [{"fields": [{"name": "Cancelled at", "value": "08-Oct-2026 09:29:00 IST"}]}]}
    assert worker.compose_watch_payload(watch(is_cancellation=True,cancellation_payload=actual)) == actual
    restart = deepcopy(actual); restart["embeds"][0]["fields"][0]["value"] = "__WORKER_TIME__"
    result = worker.compose_watch_payload(watch(is_cancellation=True,cancellation_payload=restart))
    assert result["embeds"][0]["fields"][0]["value"].endswith(" IST")
    assert "__WORKER_TIME__" not in str(result)
    assert restart["embeds"][0]["fields"][0]["value"] == "__WORKER_TIME__"


@pytest.mark.parametrize("budget", [0,-1,float("nan"),float("inf"),"invalid",None])
def test_inference_invalid_db_budget_never_calls_jev(monkeypatch,budget):
    db=FakeDB({"claim_oi_watch_inference":[watch(remaining_seconds=budget)]})
    monkeypatch.setattr(worker,"invoke_watch_jev",lambda *a,**kw: pytest.fail("SDK invoked"))
    assert worker.process_watch_inference(db,watch()) == "FAILED"
    assert db.calls[-1][1]["p_prediction"] is None


def test_inference_single_dispatch_and_same_result_persistence_retry(monkeypatch):
    db=FakeDB({"claim_oi_watch_inference":[watch()]})
    supplied=[]
    def invoke(state,**kwargs):
        supplied.append((state,kwargs)); return {"close_probability": .7}
    monkeypatch.setattr(worker,"invoke_watch_jev",invoke)
    assert worker.process_watch_inference(db,watch()) == "COMPLETED"
    assert supplied[0][0] == {"spot":22489.75}
    assert 0 < supplied[0][1]["timeout"] <=5
    assert supplied[0][1]["dispatch_deadline"] >0
    assert db.calls[-1][1]["p_prediction"] == {"close_probability":.7}
    assert worker.process_watch_inference(FakeDB(),watch()) == "NONE"


def test_inference_failure_and_unacknowledged_completion(monkeypatch):
    db=FakeDB({"claim_oi_watch_inference":[watch()],"complete_oi_watch_inference":RuntimeError("DB")})
    monkeypatch.setattr(worker,"invoke_watch_jev",lambda *a,**kw: {"close_probability":.7})
    assert worker.process_watch_inference(db,watch()) == "UNKNOWN"
    monkeypatch.setattr(worker,"invoke_watch_jev",lambda *a,**kw: (_ for _ in ()).throw(ValueError("bad output")))
    assert worker.process_watch_inference(db,watch()) == "FAILED"
    assert db.calls[-1][1]["p_error"] == "ValueError"
    assert worker.process_watch_inference(FakeDB({"claim_oi_watch_inference":RuntimeError("DB")}),watch()) == "NONE"


class FakeHTTP:
    def __init__(self,response=None,error=None): self.response=response; self.error=error; self.sent=[]
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def post(self,url,**kwargs):
        self.sent.append((url,kwargs))
        if self.error: raise self.error
        return self.response


@pytest.mark.parametrize("response,status",[
    (httpx.Response(200,json={"id":"discord-id"}),"SENT"),
    (httpx.Response(200,json={}),"UNKNOWN"),
    (httpx.Response(204),"UNKNOWN"),
    (httpx.Response(500),"UNKNOWN"),
    (httpx.Response(400),"UNKNOWN"),
    (httpx.Response(429,json={"retry_after":2.5}),"RETRY"),
    (httpx.Response(429,json={"retry_after":-1}),"UNKNOWN"),
])
def test_delivery_archives_known_acceptance_or_ambiguity(monkeypatch,formatter,response,status):
    payload={"content":"pinned"}; row=watch(delivery_payload=payload)
    db=FakeDB({"begin_oi_watch_delivery":[row],"finish_oi_watch_delivery":[{}]})
    http=FakeHTTP(response); monkeypatch.setattr(worker.httpx,"Client",lambda **kwargs:http)
    assert worker.deliver_watch(db,watch(),webhook_url="https://discord.invalid") == status
    assert http.sent[0][1]["json"] == payload
    assert http.sent[0][1]["params"] == {"wait":"true"}
    assert db.calls[-1][1]["p_status"] == status
    if status=="RETRY": assert db.calls[-1][1]["p_retry_after_seconds"] ==2.5


def test_delivery_network_failure_or_lost_ack_never_posts_again(monkeypatch,formatter):
    db=FakeDB({"begin_oi_watch_delivery":[watch(delivery_payload={})],"finish_oi_watch_delivery":RuntimeError("DB")})
    http=FakeHTTP(error=httpx.ReadTimeout("timeout")); monkeypatch.setattr(worker.httpx,"Client",lambda **kwargs:http)
    assert worker.deliver_watch(db,watch(),webhook_url="https://discord.invalid") =="UNKNOWN"
    assert len(http.sent)==1
    http.error=None; http.response=httpx.Response(200,json={"id":"id"})
    assert worker.deliver_watch(db,watch(),webhook_url="https://discord.invalid") =="UNKNOWN"
    assert len(http.sent)==2 # Two distinct invocations here; never retried within either call.


def test_delivery_gates_no_webhook_no_claim_or_expired(monkeypatch,formatter):
    monkeypatch.setattr(worker,"WEBHOOK_URL","")
    assert worker.deliver_watch(FakeDB(),watch())=="NONE"
    http=FakeHTTP(); monkeypatch.setattr(worker.httpx,"Client",lambda **kwargs:http)
    assert worker.deliver_watch(FakeDB(),watch(),webhook_url="https://discord.invalid")=="NONE"
    db=FakeDB({"begin_oi_watch_delivery":[watch(remaining_seconds=0)],"finish_oi_watch_delivery":[{}]})
    assert worker.deliver_watch(db,watch(),webhook_url="https://discord.invalid")=="UNKNOWN"
    assert http.sent==[]


def test_delivery_processing_pause_cannot_renew_claim_window(monkeypatch,formatter):
    # Older/invalid metadata cannot authorize a transport start after 5 seconds.
    db=FakeDB({"begin_oi_watch_delivery":[watch(remaining_seconds=60, delivery_payload={"content": "frozen"})],"finish_oi_watch_delivery":[{}]})
    http=FakeHTTP(); monkeypatch.setattr(worker.httpx,"Client",lambda **kwargs:http)
    clock=iter([0.0,.1,5.1]); monkeypatch.setattr(worker.time,"monotonic",lambda:next(clock))
    assert worker.deliver_watch(db,watch(),webhook_url="https://discord.invalid")=="UNKNOWN"
    assert http.sent==[]
    assert db.calls[-1][1]["p_status"]=="UNKNOWN"


def test_disabled_worker_starts_no_threads(monkeypatch):
    monkeypatch.setattr(worker, "OI_WATCH_JEV_CONFIG",
                        replace(worker.OI_WATCH_JEV_CONFIG, oi_watch_jev_enabled=False))
    assert worker.start_watch_workers(Event(),lambda:pytest.fail("client"))==[]


def test_unacknowledged_claim_never_starts_http(monkeypatch,formatter):
    http=FakeHTTP(); monkeypatch.setattr(worker.httpx,"Client",lambda **kwargs:http)
    db=FakeDB({"begin_oi_watch_delivery":RuntimeError("DB")})
    assert worker.deliver_watch(db,watch(),webhook_url="https://discord.invalid")=="NONE"
    assert http.sent==[]


def test_workers_own_clients_and_independent_loops(monkeypatch):
    stop=Event(); barrier=Barrier(2); lock=Lock(); clients={}; processed=[]
    def factory():
        name=current_thread().name
        db=FakeDB({"poll_oi_watch_inference":[watch()],"poll_oi_watch_delivery":[watch()]})
        clients[name]=db
        barrier.wait(timeout=3)
        return db
    def process(db,row):
        with lock:
            processed.append((current_thread().name,db,row))
            if len(processed)==2: stop.set()
    monkeypatch.setattr(worker,"process_watch_inference",process)
    monkeypatch.setattr(worker,"deliver_watch",process)
    threads=worker.start_watch_workers(stop,factory)
    for thread in threads: thread.join(timeout=3)
    assert all(not thread.is_alive() for thread in threads)
    assert set(clients)=={"jev-watch-inference","jev-watch-delivery"}
    assert len({id(db) for db in clients.values()})==2
    assert len(processed)==2


def test_missing_migration_fails_soft_and_backs_off(monkeypatch):
    stop=Event(); delays=[]
    def wait(delay): delays.append(delay); stop.set(); return True
    monkeypatch.setattr(stop,"wait",wait)
    db=FakeDB({"poll_oi_watch_inference":RuntimeError("missing RPC"),"poll_oi_watch_delivery":RuntimeError("missing RPC")})
    threads=worker.start_watch_workers(stop,lambda:db)
    for thread in threads: thread.join(timeout=3)
    assert delays==[30]


def test_stop_after_poll_does_not_dispatch(monkeypatch):
    stop=Event()
    class StoppingDB:
        def __init__(self): self.polls=0
        def rpc(self,name,params):
            def execute():
                self.polls+=1
                if self.polls==1: return SimpleNamespace(data=[])
                stop.set(); return SimpleNamespace(data=[watch()])
            return SimpleNamespace(execute=execute)
    monkeypatch.setattr(worker,"process_watch_inference",lambda *a:pytest.fail("inference"))
    monkeypatch.setattr(worker,"deliver_watch",lambda *a:pytest.fail("delivery"))
    threads=worker.start_watch_workers(stop,StoppingDB)
    for thread in threads: thread.join(timeout=3)
    assert all(not thread.is_alive() for thread in threads)
