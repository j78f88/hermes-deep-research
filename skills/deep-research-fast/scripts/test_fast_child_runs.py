"""Child runs, controller exclusion and source-kind migration, without live workers."""
import fcntl
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from fast_research import FastResearch, main
from fast_quote_check import check_answer
from fast_store import Store
from fast_test_support import FakeHermes, small_config

SEARCH = {"q1": ["https://example.invalid/docs"]}


def child(root, fake, **kwargs):
    return FastResearch(root, "Child?", config=small_config(), skip_preflight=True, **fake.hooks(), **kwargs)


def test_child_stops_after_research(tmp_path):
    fake = FakeHermes(SEARCH)
    fr = child(tmp_path, fake, run_id="child", parent_run_id="parent")
    rec = fr.run(stop_after="research")
    assert rec["status"] == "completed", rec["error"]
    assert fake.count("preflight") == fake.count("writer") == fake.count("review") == 0
    assert fr.store.phase(fr.run_id, "preflight")["data"] == {"skipped": "covered by parent run"}
    assert fr._admitted()
    assert fr.source_run_ids() == ["child"]
    assert fr.store.child_runs("parent")[0]["id"] == "child"


def test_run_lock_rejects_without_changing_state(tmp_path):
    fake = FakeHermes(SEARCH)
    fr = child(tmp_path, fake, run_id="locked")
    before = fr.store.get_run("locked")
    with open(fr.path(".controller.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another controller is running run locked"):
            fr.run()
        assert fr.store.get_run("locked") == before
        assert not fr.store.phases("locked")
        assert not fake.sessions
        assert main(["run", "--question", "Child?", "--root", str(tmp_path), "--run-id", "locked"]) == 1
    assert fr.run(stop_after="research")["status"] == "completed"


def test_different_run_locks_do_not_block(tmp_path):
    fake = FakeHermes(SEARCH)
    a, b = (child(tmp_path, fake, run_id=name) for name in ("a", "b"))
    with open(a.path(".controller.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert b.run(stop_after="research")["status"] == "completed"


def test_multiple_source_runs_are_explicit(tmp_path):
    fake = FakeHermes(SEARCH)
    a, b = (child(tmp_path, fake, run_id=name) for name in ("a", "b"))
    for fr in (a, b):
        assert fr.run(stop_after="research")["status"] == "completed"
    claims = [a._admitted()[0], b._admitted()[0]]
    text, cites, _ = a.render("One.[C%d] Two.[C%d]" % tuple(c["id"] for c in claims), {c["id"]: c for c in claims})
    assert check_answer(a.store, "a", text, cites, run_ids=["a", "b"])["verdict"] == "pass"
    assert check_answer(a.store, "a", text, cites, run_ids=["a"])["verdict"] == "fail"


@pytest.mark.parametrize("kind,expected", [("self", "self"), ("primary", "primary"), ("independent", "independent"),
                                         ("discovery", "discovery"), ("nonsense", "unknown"), ({}, "unknown")])
def test_meaning_check_source_kind_never_changes_verdict(tmp_path, kind, expected):
    fake = FakeHermes(SEARCH)
    original = fake.oneshot
    def reply(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if prompt.startswith("You check findings"):
            parsed = json.loads(res.stdout)
            for row in parsed["checks"]:
                row["source_kind"] = kind
            res.stdout = json.dumps(parsed)
        return res
    fr = child(tmp_path, fake, run_id="kind")
    fr.oneshot = reply
    assert fr.run(stop_after="research")["status"] == "completed"
    supports = fr.store.supports("kind")
    assert supports and all(s["source_kind"] == expected and s["verdict"] == "supported" for s in supports)


def test_controller_sets_slot_count_but_child_does_not_reset_it(tmp_path):
    import fast_worker as fw
    fake = FakeHermes(SEARCH)
    config = small_config(host_slots=50)
    parent = FastResearch(tmp_path, "Parent?", run_id="slot-parent", config=config, **fake.hooks())
    assert parent.run(stop_after="preflight")["status"] == "completed"
    assert fw.HOST_SLOTS == 50
    assert child(tmp_path, fake, run_id="slot-child").run(stop_after="research")["status"] == "completed"
    assert fw.HOST_SLOTS == 50


def test_new_prompts_include_untrusted_data_guard():
    import fast_prompts as prompts
    assert all(prompts.GUARD in text for text in (prompts.FOLLOWUPS, prompts.SYNTHESIS))


def test_old_support_table_migrates_concurrently(tmp_path):
    path = tmp_path / "fast.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE claim_support (claim_id INTEGER PRIMARY KEY, run_id TEXT, verdict TEXT, supported_text TEXT, "
                  "reason TEXT, model TEXT, session_id TEXT, checked_at REAL)")
        c.execute("INSERT INTO claim_support VALUES (1, 'old', 'supported', NULL, 'ok', NULL, NULL, 1)")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Store(path), range(4)))
    assert Store(path).support(1)["verdict"] == "supported"
    assert "source_kind" in Store(path).support(1)
