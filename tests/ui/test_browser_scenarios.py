"""V0.3 real-browser scenarios: authority, sessions, hostile pages, model failures, layout drift."""
import pytest

from harness import TENANT
from wakecore.kernel.domain.enums import ActionStatus, SourceHealth, TaskLifecycle
from wakecore.kernel.domain.model import ActionAttempt, Approval, SourceCheckpoint
from wakecore.kernel.replay import replay_task


def attempt_of(w, action):
    return w.h.all(ActionAttempt, {"action_id": action.action_id})[-1]


def approved_run(w, **plan_kw):
    action = w.open_seat(**plan_kw)
    assert action.status is ActionStatus.WAITING_APPROVAL, action.resolution
    w.approve(action)
    w.h.run()
    return w.current(action)


# ------------------------------------------------------------------ authority (constraints 6, 7, 8)

@pytest.mark.parametrize("field,url", [
    ("start_url", "EVIL/claim"),
    ("start_url", "ORIGIN@evil.example/"),
    ("verify.url", "EVIL/courses"),
])
def test_planner_cannot_send_the_browser_outside_the_task_origin(world, field, url):
    w = world
    w.setup()
    url = url.replace("EVIL", w.evil.origin).replace("ORIGIN", w.origin)
    action = w.open_seat(**({"start_url": url} if field == "start_url" else {"verify_url": url}))
    assert action.status is ActionStatus.DENIED
    assert action.resolution == f"origin_outside_scope:{field}"
    assert w.h.all(Approval, {"action_id": action.action_id}) == []   # never offered for approval
    w.h.run()
    assert w.fake.requests == [] and w.confirm_posts() == 0 and w.evil.hits == []


def test_without_the_model_egress_grant_the_planner_never_sees_the_browser(world):
    w = world
    w.setup(grant_egress=("model:offline-scripted",), egress=("model:offline-scripted",))
    action = w.open_seat()                                # the planner tries anyway
    [bundle] = [b for kind, b in w.h.k.model.calls if kind == "plan"]
    assert "browser.cua" not in {t["tool_id"] for t in bundle.allowed_tools}
    assert action.status is ActionStatus.DENIED and action.resolution == "data_egress_not_allowed"
    assert w.fake.requests == []


def test_an_approval_for_another_model_endpoint_never_reaches_the_model(world):
    """The user approved screenshots going to OpenAI; this sidecar would send them to another host."""
    w = world
    w.egress = "model:openai-computer-use"
    w.attach()
    w.setup(grant_egress=("model:offline-scripted", w.egress), egress=("model:offline-scripted", w.egress))
    action = approved_run(w)
    assert action.status is ActionStatus.FAILED_NO_EFFECT
    att = attempt_of(w, action)
    assert att.error == "model_egress_mismatch"
    assert w.fake.requests == [] and w.fake.screenshots == 0 and w.confirm_posts() == 0


def test_a_record_outside_the_watched_scope_is_refused_without_touching_the_page(world):
    w = world
    w.setup()
    action = approved_run(w, record="ANAT")
    assert action.status is ActionStatus.FAILED_NO_EFFECT
    assert attempt_of(w, action).error == "record_outside_scope"
    assert w.fake.requests == [] and w.confirm_posts() == 0


def test_already_satisfied_goal_is_confirmed_by_observation_without_the_model(world):
    w = world
    w.setup()
    action = w.open_seat()
    with w.portal.lock:                                   # the user enrolled by hand meanwhile
        w.portal.state.enrollments.add(("alice", "PHARM"))
    w.approve(action)
    w.h.run()
    assert w.current(action).status is ActionStatus.CONFIRMED
    assert attempt_of(w, action).receipt["receipt"]["already_satisfied"] is True
    assert w.fake.requests == [] and w.confirm_posts() == 0


# ------------------------------------------------------------------ sessions (constraint 5)

def test_expired_login_blocks_the_source_and_a_manual_relogin_resumes_it(world):
    w, h = world, world.h
    w.setup()
    w.portal.set(expire_sessions=True)
    h.cycle()
    assert w.binding().health is SourceHealth.AUTH_REQUIRED
    before = h.all(SourceCheckpoint)[0].trusted_snapshot
    w.portal.set(seats={"PHARM": 1})
    h.cycle()                                             # still logged out: no guessing, no model
    assert w.binding().health is SourceHealth.AUTH_REQUIRED and h.k.model.calls == []
    assert h.all(SourceCheckpoint)[0].trusted_snapshot == before

    w.relogin()                                           # the human logs in again (password + TOTP)
    w.reauthorised()
    w.script_plan()
    h.cycle()
    assert w.binding().health is SourceHealth.HEALTHY
    action = w.browser_action()                           # the seat that opened meanwhile is seen now
    w.approve(action)
    h.run()
    assert w.current(action).status is ActionStatus.CONFIRMED and w.portal.enrolled() == ["PHARM"]
    assert w.model_saw_secret() == []


def test_login_lost_after_approval_fails_before_the_model_is_called(world):
    w = world
    w.setup()
    action = w.open_seat()
    w.approve(action)
    w.portal.set(expire_sessions=True)
    w.h.run()
    cur = w.current(action)
    assert cur.status is ActionStatus.FAILED_NO_EFFECT and attempt_of(w, action).error == "auth_required"
    assert w.fake.requests == [] and w.confirm_posts() == 0


# ------------------------------------------------------------------ hostile pages and bad models

def test_prompt_injection_cannot_make_it_type_into_a_credential_field(make_world):
    w = make_world(policy="gullible")
    w.setup()
    action = approved_run(w)
    assert action.status is ActionStatus.FAILED_NO_EFFECT
    assert attempt_of(w, action).error == "act_needs_human:credential_field"
    assert w.evil.hits == [] and w.confirm_posts() == 0
    assert w.model_saw_secret() == []


def test_prompt_injection_cannot_navigate_it_off_the_origin(make_world):
    w = make_world(policy="offsite")
    w.setup()
    action = approved_run(w)
    assert action.status is ActionStatus.FAILED_NO_EFFECT
    assert "origin_escape" in attempt_of(w, action).error
    assert w.evil.hits == []                              # the request was aborted in the browser


def test_model_safety_checks_are_never_acknowledged(make_world):
    w = make_world(policy="safety_check", variant="preview")
    w.setup()
    action = approved_run(w)
    assert action.status is ActionStatus.FAILED_NO_EFFECT
    assert "safety_check:malicious_instructions" in attempt_of(w, action).error
    assert w.fake.violations == [] and w.confirm_posts() == 0


def test_model_api_down_is_a_clean_failure(make_world):
    w = make_world(policy="error500")
    w.setup()
    action = approved_run(w)
    assert action.status is ActionStatus.FAILED_NO_EFFECT and "model_error" in attempt_of(w, action).error
    assert w.confirm_posts() == 0


def test_without_a_model_key_the_eye_still_works_and_the_hand_fails_cleanly(make_world):
    w = make_world(with_key=False)
    w.setup()
    assert w.binding().health is SourceHealth.HEALTHY     # observation never needs the model
    action = approved_run(w)
    assert action.status is ActionStatus.FAILED_NO_EFFECT
    assert "model_not_configured" in attempt_of(w, action).error
    assert w.fake.requests == []


def test_a_model_that_submits_twice_only_reaches_the_server_once(make_world):
    w = make_world(policy="double_submit")
    w.setup()
    action = approved_run(w)
    assert action.status is ActionStatus.CONFIRMED
    assert w.confirm_posts() == 1 and w.portal.enrolled() == ["PHARM"]
    assert attempt_of(w, action).receipt["receipt"]["act"]["blocked_requests"] >= 1


def test_a_stalling_model_runs_out_of_steps_without_side_effects(make_world):
    w = make_world(policy="stall")
    w.setup()
    w.tool.max_steps = 4
    action = approved_run(w)
    assert action.status is ActionStatus.FAILED_NO_EFFECT and "max_steps" in attempt_of(w, action).error
    assert w.confirm_posts() == 0


# ------------------------------------------------------------------ endpoints without the computer tool

def test_a_codex_like_relay_drives_the_page_through_the_function_variant(make_world):
    # no hosted `computer` tool and no previous_response_id, like the relays seen for real
    w = make_world(variant="function", stateless=True)
    w.setup()
    action = approved_run(w)
    assert action.status is ActionStatus.CONFIRMED, attempt_of(w, action).error
    assert w.confirm_posts() == 1 and w.portal.enrolled() == ["PHARM"]
    assert w.fake.violations == ["previous_response_id is not available for this user"]   # once, then history
    assert w.fake.history_turns >= 2 and w.model_saw_secret() == []


@pytest.mark.parametrize("policy,reason", [("gullible", "credential_field"), ("offsite", "origin_escape")])
def test_the_function_variant_keeps_every_guard(make_world, policy, reason):
    w = make_world(variant="function", stateless=True, policy=policy)
    w.setup()
    action = approved_run(w)
    assert action.status is ActionStatus.FAILED_NO_EFFECT and reason in attempt_of(w, action).error
    assert w.evil.hits == [] and w.confirm_posts() == 0 and w.model_saw_secret() == []


# ------------------------------------------------------------------ layout drift

def test_renamed_and_reordered_columns_are_still_read(world):
    w, h = world, world.h
    w.setup()
    w.portal.set(layout="v2")
    h.cycle()
    assert w.binding().health is SourceHealth.HEALTHY and h.k.model.calls == []
    action = approved_run(w)                              # CU still finds the (moved) button
    assert action.status is ActionStatus.CONFIRMED
    h.cycle()
    assert h.runtime("enroll_watch").lifecycle is TaskLifecycle.COMPLETED


def test_an_unreadable_layout_never_overwrites_the_trusted_snapshot(world):
    w, h = world, world.h
    w.setup()
    before = h.all(SourceCheckpoint)[0]
    w.portal.set(layout="v3", seats={"PHARM": 5})
    h.cycle()
    assert w.binding().health is SourceHealth.SCHEMA_INVALID
    after = h.all(SourceCheckpoint)[0]
    assert after.trusted_snapshot == before.trusted_snapshot and after.local_revision == before.local_revision
    assert h.k.model.calls == [] and w.fake.requests == []
    assert replay_task(h.store, TENANT, "enroll_watch").summary()["mismatches"] == []


# ------------------------------------------------------------------ the runtime itself

def test_runtime_down_means_unavailable_source_and_a_definite_no_effect(world):
    w, h = world, world.h
    w.setup()
    action = w.open_seat()
    w.approve(action)
    w.side.stop()
    h.run()
    assert w.current(action).status is ActionStatus.FAILED_NO_EFFECT   # connection refused: never sent
    assert attempt_of(w, action).error == "pre_verify_failed"
    h.cycle()
    assert w.binding().health is SourceHealth.UNAVAILABLE
    assert w.confirm_posts() == 0


def test_runtime_requires_its_token(make_world):
    from wakecore.adapters.ui_runtime.client import RuntimeRejected, UiRuntimeClient

    w = make_world(token="s3cret-token")
    w.setup()
    assert w.binding().health is SourceHealth.HEALTHY
    with pytest.raises(RuntimeRejected) as e:
        UiRuntimeClient(w.side.url).observe(session_ref="jw_alice", url=w.origin + "/courses",
                                            origins=[w.origin], extractor={})
    assert e.value.status == 401
