"""Offline regression checks for startup recovery, API serialization and Chrome tab cleanup."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from ai.providers import browser_chat

ROOT = Path(__file__).resolve().parents[2]


def test_all_workflow_node_connections_exist():
    w = json.loads((ROOT / 'n8n' / '04_recruiter_email_auto_reply.json').read_text())
    names = {n['name'] for n in w['nodes']}
    assert len(names) == len(w['nodes'])
    assert w['active'] is True
    for src, conn in w['connections'].items():
        assert src in names
        for out in conn['main']:
            for edge in out:
                assert edge['node'] in names
    assert 'N8N_CONCURRENCY_PRODUCTION_LIMIT=1' in (ROOT / 'n8n/docker-compose.yml').read_text()


def test_python_recruiter_endpoint_serializes_across_requests(monkeypatch):
    current = 0
    peak = 0
    mutex = threading.Lock()

    def process(email, **kwargs):
        nonlocal current, peak
        with mutex:
            current += 1
            peak = max(current, peak)
        time.sleep(0.025)
        with mutex:
            current -= 1
        return {'message_id': email.message_id, 'status': 'IGNORED'}

    monkeypatch.setattr('recruiter_email.service.process_recruiter_email', process)
    main.app.dependency_overrides[main.recruiter_dependencies] = lambda: {'engine': object()}
    try:
        with TestClient(main.app) as client:
            def request(i):
                return client.post('/ai/recruiter-email-reply', json={
                    'message_id': f'msg-{i}', 'from': 'test@example.com',
                    'subject': 'test', 'body': 'test',
                })
            with ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(request, range(5)))
        assert all(r.status_code == 200 for r in results)
        assert peak == 1
    finally:
        main.app.dependency_overrides.clear()


def test_claude_tab_is_closed_and_original_tab_restored(monkeypatch, tmp_path):
    class FakeSwitch:
        def __init__(self, driver):
            self.driver = driver
        def new_window(self, kind):
            self.driver.window_handles.append('automation-tab')
            self.driver.current_window_handle = 'automation-tab'
        def window(self, handle):
            self.driver.current_window_handle = handle

    class FakeDriver:
        def __init__(self):
            self.window_handles = ['personal-tab']
            self.current_window_handle = 'personal-tab'
            self.current_url = 'https://claude.ai/chat/mock'
            self.switch_to = FakeSwitch(self)
        def close(self):
            self.window_handles.remove(self.current_window_handle)

    driver = FakeDriver()
    monkeypatch.setattr(browser_chat, 'ensure_browser', lambda provider: None)
    monkeypatch.setattr(browser_chat, 'attach_driver', lambda provider: driver)
    def open_owned_tab(chat, url):
        driver.switch_to.new_window('tab')
        chat['automation_tab_handle'] = driver.current_window_handle
    monkeypatch.setattr(browser_chat, 'open_new_chat', open_owned_tab)
    monkeypatch.setattr(browser_chat, 'ensure_inside_project', lambda chat, project, document: None)
    monkeypatch.setattr(browser_chat, 'visible_elements', lambda chat, key: [])
    monkeypatch.setattr(browser_chat, 'attach_files', lambda chat, paths: None)
    monkeypatch.setattr(browser_chat, 'type_and_send', lambda chat, prompt: None)
    monkeypatch.setattr(browser_chat, 'wait_for_response', lambda chat, baseline, timeout: '{"ok":true}')
    monkeypatch.setattr(browser_chat, 'detach_driver', lambda driver: None)
    text, url = browser_chat.run_chat_json_request(
        provider='claude_web', chat_url='https://claude.ai/new', selectors={}, markers=[],
        request_file=tmp_path/'request.md', extra_attachments=[], timeout_seconds=5,
    )
    assert text == '{"ok":true}'
    assert url == 'https://claude.ai/chat/mock'
    assert driver.window_handles == ['personal-tab']
    assert driver.current_window_handle == 'personal-tab'
