"""Age, relocation, and tab-lifetime regression tests."""
from __future__ import annotations
from pathlib import Path
import pytest

from ai.providers import browser_chat
from recruiter_email.field_registry import detect_fields
from recruiter_email.knowledge_base import load_knowledge
from recruiter_email.service import safety_reasons


def test_age_and_relocation_questions_map_to_facts():
    assert detect_fields("Age") == ["AGE"]
    assert "AGE" in detect_fields("How old are you?")
    assert "INTERNATIONAL_RELOCATION_STATUS" in detect_fields("Date of relocation to US")
    facts=load_knowledge()["facts"]
    assert facts["AGE"] == "25"
    assert "not relocated to the destination country" in facts["INTERNATIONAL_RELOCATION_STATUS"]


def test_known_questions_do_not_get_remapped_as_unknown():
    classification = {
        "intent":"REQUEST_CANDIDATE_DETAILS", "role":"pythondeveloper",
        "confidence":.99, "requires_manual_review":False,
        "unmapped_requests":["Age", "Date of relocation to US", "Passport expiry date"],
        "sensitive_topics":[],
    }
    config = {"threshold":.9, "auto_send_intents":["REQUEST_CANDIDATE_DETAILS"],
              "target_roles":["pythondeveloper"]}
    reasons, unmapped, _ = safety_reasons(classification,
        {"unsupported_requests":[], "sensitive_topics":[]}, config,
        requested=["AGE","INTERNATIONAL_RELOCATION_STATUS"], missing=[], deterministic_only=[])
    assert unmapped == ["Passport expiry date"]
    assert "PERSONAL_AGE_REVIEW_REQUIRED" in reasons
    assert "INTERNATIONAL_IMMIGRATION_REVIEW_REQUIRED" in reasons


class Switch:
    def __init__(self, driver): self.driver=driver
    def new_window(self, kind):
        assert kind == "tab"
        self.driver.window_handles.append("new-ai-tab")
        self.driver.current_window_handle="new-ai-tab"
    def window(self, handle): self.driver.current_window_handle=handle

class FakeDriver:
    def __init__(self):
        self.window_handles=["personal-tab", "user-claude-tab"]
        self.current_window_handle="personal-tab"
        self.current_url="https://claude.ai/chat/new-abc"
        self.switch_to=Switch(self)
        self.closed=[]
    def get(self,url): pass
    def close(self):
        self.closed.append(self.current_window_handle)
        self.window_handles.remove(self.current_window_handle)


@pytest.mark.parametrize("fail", [False, True])
def test_only_automation_created_tab_is_closed_on_success_or_failure(monkeypatch,tmp_path,fail):
    driver=FakeDriver()
    monkeypatch.setattr(browser_chat, "ensure_browser", lambda provider: None)
    monkeypatch.setattr(browser_chat, "attach_driver", lambda provider: driver)
    monkeypatch.setattr(browser_chat, "ensure_inside_project", lambda chat,project,document: None)
    monkeypatch.setattr(browser_chat, "visible_elements", lambda chat,key: [])
    monkeypatch.setattr(browser_chat, "attach_files", lambda chat,paths: None)
    monkeypatch.setattr(browser_chat, "type_and_send", lambda chat,prompt: None)
    monkeypatch.setattr(browser_chat, "wait_for_element", lambda *args,**kw: object())
    monkeypatch.setattr(browser_chat, "check_login", lambda chat: None)
    monkeypatch.setattr(browser_chat.time, "sleep", lambda t: None)
    monkeypatch.setattr(browser_chat, "detach_driver", lambda driver: None)
    if fail:
        monkeypatch.setattr(browser_chat,"wait_for_response", lambda *args: (_ for _ in ()).throw(TimeoutError("provider timeout")))
    else:
        monkeypatch.setattr(browser_chat,"wait_for_response",lambda *args:'{"ok":true}')
    args=dict(provider="claude_web",chat_url="https://claude.ai/new",selectors={},markers=[],
              request_file=tmp_path/'input.md',extra_attachments=[],timeout_seconds=1)
    if fail:
        with pytest.raises(Exception): browser_chat.run_chat_json_request(**args)
    else:
        assert browser_chat.run_chat_json_request(**args)[0] == '{"ok":true}'
    assert driver.closed == ["new-ai-tab"]
    assert driver.window_handles == ["personal-tab","user-claude-tab"]
    assert driver.current_window_handle == "personal-tab"
