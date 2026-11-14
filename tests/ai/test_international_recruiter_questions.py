"""Country-neutral recruiter question classification and knowledge checks."""
from recruiter_email.field_registry import detect_fields, label_for
from recruiter_email.knowledge_base import load_knowledge
from recruiter_email.service import safety_reasons


def test_country_independent_relocation():
    for q in ("When did you Relocate to US", "When did you relocate to UAE?",
              "When did you move to Saudi Arabia?", "When did you relocate to Japan?",
              "Have you relocated to Qatar?"):
        assert "INTERNATIONAL_RELOCATION_STATUS" in detect_fields(q), q


def test_general_immigration_question_detection():
    cases = {
        "Visa Status": "VISA_STATUS",
        "Do you hold a Japan visa?": "VISA_STATUS",
        "Do you have a passport?": "PASSPORT_AVAILABLE",
        "Would you relocate to UAE?": "WILLING_TO_RELOCATE_INTERNATIONALLY",
        "Do you require visa sponsorship in UAE?": "VISA_SPONSORSHIP_REQUIRED",
        "Are you authorized to work in Japan?": "WORK_AUTHORIZATION_DESTINATION",
    }
    for q, key in cases.items():
        assert key in detect_fields(q), (q, detect_fields(q))


def test_current_authorization_unverified_and_other_facts_present():
    facts = load_knowledge()["facts"]
    assert facts["VISA_STATUS"] == "I do not currently hold a visa for the destination country."
    assert facts["PASSPORT_AVAILABLE"] == "Yes, I have a passport."
    assert "WORK_AUTHORIZATION_DESTINATION" not in facts
    assert "US_RELOCATION_DATE" not in facts


def test_immigration_always_manual_review():
    classification = {"intent": "REQUEST_CANDIDATE_DETAILS", "role": "pythondeveloper",
                      "confidence": 0.99, "requires_manual_review": False,
                      "unmapped_requests": [], "sensitive_topics": []}
    pre = {"unsupported_requests": [], "sensitive_topics": []}
    cfg = {"threshold": 0.9, "auto_send_intents": ["REQUEST_CANDIDATE_DETAILS"],
           "target_roles": ["pythondeveloper"]}
    for key in ("VISA_STATUS", "PASSPORT_AVAILABLE", "INTERNATIONAL_RELOCATION_STATUS",
                "WILLING_TO_RELOCATE_INTERNATIONALLY", "VISA_SPONSORSHIP_REQUIRED",
                "WORK_AUTHORIZATION_DESTINATION"):
        reasons, *_ = safety_reasons(classification, pre, cfg,
                                      requested=[key], missing=[], deterministic_only=[])
        assert "INTERNATIONAL_IMMIGRATION_REVIEW_REQUIRED" in reasons
