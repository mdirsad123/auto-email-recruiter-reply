"""Mock HTTP and fake-router tests. No real Cohere keys, browser or Gmail calls."""
from __future__ import annotations

import json

import httpx
import pytest

from ai.errors import FailureType, ProviderUnavailableError
from ai.provider_result import AIRequest
from ai.providers.cohere_api import call_cohere_api, build_request
from ai.provider_router import execute_ai_task, get_enabled_providers
from ai.settings import load_ai_config, OP_RECRUITER_CLASSIFY, OP_RECRUITER_REPLY
from tests.ai.conftest import fake_provider, unavailable
from recruiter_email.service import IncomingEmail, load_recruiter_config, process_recruiter_email, SEND_REPLY
from tests.ai.test_recruiter_email import classification, GOOD_WORDING, json_engine

REQ = AIRequest(operation=OP_RECRUITER_CLASSIFY, system_prompt="Classify. Return JSON", user_prompt="interview?",
                output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})


def response_json(result={"ok": True}):
    return {"model": "command-a-plus-05-2026", "finish_reason": "COMPLETE",
            "message": {"content": [{"type": "text", "text": json.dumps(result)}]},
            "usage": {"tokens": {"input_tokens": 10, "output_tokens": 8}}}


def test_http_v2_uses_individual_credential_and_json_mode(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "fake-test-token-1")
    monkeypatch.setenv("COHERE_TIMEOUT_SECONDS", "3")
    calls = []
    def endpoint(req):
        calls.append(req)
        return httpx.Response(200, json=response_json())
    client = httpx.Client(transport=httpx.MockTransport(endpoint))
    result = call_cohere_api(REQ, {"default_model": "command-a-plus-05-2026"}, provider="cohere_api_1", client=client)
    assert result["provider"] == "cohere_api_1" and result["data"] == {"ok": True}
    assert calls[0].url.path == "/v2/chat" and calls[0].headers["Authorization"] == "Bearer fake-test-token-1"
    payload = json.loads(calls[0].content)
    assert payload["response_format"] == {"type": "json_object"} and payload["messages"][0]["role"] == "system"
    assert "fake-test-token-1" not in json.dumps(result)
    client.close()


@pytest.mark.parametrize("http_status,failure", [(401,FailureType.AUTH),(403,FailureType.AUTH),
                       (429,FailureType.RATE_LIMIT),(498,FailureType.AUTH),(503,FailureType.SERVER_ERROR)])
def test_http_failures_triggers_next_credential(monkeypatch,http_status,failure):
    monkeypatch.setenv("COHERE_API_KEY", "fake-test-token-1")
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(http_status, json={"message":"error"})))
    with pytest.raises(ProviderUnavailableError) as info:
        call_cohere_api(REQ, {}, provider="cohere_api_1", client=client)
    assert info.value.failure_type == failure and not info.value.retryable
    assert "fake-test-token" not in str(info.value)
    client.close()


def test_provider_priority_and_three_key_fallback(monkeypatch):
    env = {"USE_COHERE_API":"true", "COHERE_API_KEY":"a", "COHERE_API_KEY2":"b", "COHERE_API_KEY3":"c",
           "USE_CLAUDE_API":"false", "USE_CHATGPT_API":"false", "USE_CLAUDE_WEB":"true", "USE_CHATGPT_WEB":"true"}
    config = load_ai_config(env)
    assert get_enabled_providers(config) == ["cohere_api_1", "cohere_api_2", "cohere_api_3", "claude_web", "chatgpt_web"]
    c1 = fake_provider("cohere_api_1", [unavailable(FailureType.RATE_LIMIT)])
    c2 = fake_provider("cohere_api_2", [unavailable(FailureType.AUTH)])
    c3 = fake_provider("cohere_api_3", [{"ok":True}])
    web = fake_provider("claude_web", [{"ok":True}])
    out = execute_ai_task(REQ,config=config,provider_functions={"cohere_api_1":c1,"cohere_api_2":c2,
         "cohere_api_3":c3,"claude_web":web},sleep=lambda _:None,log=lambda _:None)
    assert out["provider"] == "cohere_api_3"
    assert [a["provider"] for a in out["attempts"]] == ["cohere_api_1", "cohere_api_2", "cohere_api_3"]
    assert not web.calls


def test_all_keys_fail_then_claude_web(monkeypatch):
    env = {"USE_COHERE_API":"true", "COHERE_API_KEY":"a", "COHERE_API_KEY2":"b", "COHERE_API_KEY3":"c",
           "USE_CLAUDE_API":"false", "USE_CHATGPT_API":"false", "USE_CLAUDE_WEB":"true", "USE_CHATGPT_WEB":"false"}
    config = load_ai_config(env)
    funcs = {name:fake_provider(name,[unavailable(FailureType.QUOTA_EXHAUSTED, retryable=False)])
             for name in ("cohere_api_1","cohere_api_2","cohere_api_3")}
    funcs["claude_web"] = fake_provider("claude_web",[{"ok":True}])
    out = execute_ai_task(REQ,config=config,provider_functions=funcs, sleep=lambda _:None,log=lambda _:None)
    assert out["provider"] == "claude_web" and len(out["attempts"]) == 4


def test_invalid_cohere_output_falls_back():
    config = load_ai_config({"USE_COHERE_API":"true", "COHERE_API_KEY":"a", "COHERE_API_KEY2":"b",
                            "USE_CLAUDE_API":"false", "USE_CHATGPT_API":"false",
                            "USE_CLAUDE_WEB":"false", "USE_CHATGPT_WEB":"false"})
    # Request validators reject bad model output; try another key instead of stalling.
    from ai.errors import OutputValidationError
    def validator(data,result):
        if not data.get("ok"):
            raise OutputValidationError("bad model data")
        return data
    out = execute_ai_task(REQ, config=config, validator=validator,
        provider_functions={"cohere_api_1":fake_provider("cohere_api_1",[{"ok":False}]),
                            "cohere_api_2":fake_provider("cohere_api_2",[{"ok":True}])},
        sleep=lambda _:None,log=lambda _:None)
    assert out["provider"] == "cohere_api_2" and out["attempts"][0]["result"] == "FALLBACK"


def test_no_fields_interview_forced_send_not_draft():
    provider = fake_provider("cohere_api_1", [lambda req: classification(intent="INTERVIEW_SCHEDULING",
                  requested_fields=[], requires_manual_review=True) if req.operation == OP_RECRUITER_CLASSIFY else GOOD_WORDING])
    config=load_recruiter_config({"AUTO_SEND_RECRUITER_REPLY":"true"})
    item=IncomingEmail(message_id="interview-1",thread_id="thread-1",sender="HR <hr@recruiting.example>",
                       subject="Python Developer opportunity",body="Tomorrow is interview; are you available?")
    from tests.ai.conftest import execute_with
    res=process_recruiter_email(item,engine=json_engine(),config=config,execute=execute_with(provider))
    assert res["action"]==SEND_REPLY and res["auto_send"] is True
    assert "[please fill" not in res["reply_body"]
    assert "Please find my details below" not in res["reply_body"]
    assert "Could you please share the interview" in res["reply_body"]


def test_all_ai_down_forced_send_generic_ack():
    from tests.ai.conftest import execute_with
    provider=fake_provider("cohere_api_1",[unavailable()])
    item=IncomingEmail(message_id="all-down",thread_id="thread-all",sender="HR <hr@recruiting.example>",
                       subject="Python Developer opportunity",body="Please share your resume")
    res=process_recruiter_email(item,engine=json_engine(),config=load_recruiter_config({"AUTO_SEND_RECRUITER_REPLY":"true"}),
                     execute=execute_with(provider))
    assert res["action"]==SEND_REPLY and res["provider"]=="deterministic_fallback"
