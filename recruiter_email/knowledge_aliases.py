"""
recruiter_email/knowledge_aliases.py
====================================

Plain data tables used by the knowledge-base parser, the deterministic field
detector and the retrieval step:

* SKILLS      - technical skills -> fact key + aliases ("gen ai" == "GenAI").
* ROLE_AREAS  - role-level experience areas ("AWS Data Engineering") -> fact key.
* ROLE_RELEVANT_KEYS - which fact answers "Relevant Experience" for a role.

Extend these tables when you add a new skill or role; nothing else needs to
change. Aliases are matched on whole, normalised list items ("AWS Data
Engineering" is a role area, it is NOT read as the skill "AWS").
"""

from __future__ import annotations

import re

# key -> (label, aliases). Keys ending in _YEARS get " years" appended to numbers.
ROLE_AREAS: dict[str, tuple[str, tuple[str, ...]]] = {
    "AWS_DATA_ENGINEERING_EXPERIENCE_YEARS": (
        "AWS Data Engineering Experience",
        ("aws data engineering", "aws data engineer", "data engineering on aws", "aws data engineers"),
    ),
    "AZURE_DATA_ENGINEERING_EXPERIENCE_YEARS": (
        "Azure Data Engineering Experience",
        ("azure data engineering", "azure data engineer", "data engineering on azure", "azure data engineers"),
    ),
    "AI_ENGINEERING_EXPERIENCE_YEARS": (
        "AI Engineering Experience",
        ("ai engineering", "ai engineer", "ai/ml engineering", "genai engineering", "gen ai engineering",
         "generative ai engineering", "ai engineers"),
    ),
    "PYTHON_DEVELOPMENT_EXPERIENCE_YEARS": (
        "Python Development Experience",
        ("python development", "python developer", "python backend development", "python software development",
         "python developers"),
    ),
    "DATA_ENGINEERING_EXPERIENCE_YEARS": (
        "Data Engineering Experience",
        ("data engineering", "data engineer", "big data engineering", "data engineers"),
    ),
}

SKILLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "PYTHON_EXPERIENCE_YEARS": ("Python Experience", ("python", "core python", "python programming")),
    "PYSPARK_EXPERIENCE_YEARS": ("PySpark Experience", ("pyspark", "py spark", "spark", "apache spark")),
    "AWS_EXPERIENCE_YEARS": ("AWS Experience", ("aws", "amazon web services", "aws cloud")),
    "AZURE_EXPERIENCE_YEARS": ("Azure Experience", ("azure", "microsoft azure", "azure cloud")),
    "DATABRICKS_EXPERIENCE_YEARS": ("Databricks Experience", ("databricks", "azure databricks", "aws databricks")),
    "SQL_EXPERIENCE_YEARS": ("SQL Experience", ("sql", "t-sql", "tsql", "spark sql")),
    "FASTAPI_EXPERIENCE_YEARS": ("FastAPI Experience", ("fastapi", "fast api")),
    "REST_API_EXPERIENCE_YEARS": ("REST API Experience",
                                  ("rest api", "rest apis", "restful api", "restful apis", "api development",
                                   "api integration", "api integrations")),
    "BACKEND_EXPERIENCE_YEARS": ("Backend Development Experience", ("backend", "backend development")),
    "LLM_EXPERIENCE_YEARS": ("LLM Experience", ("llm", "llms", "large language model", "large language models")),
    "GENAI_EXPERIENCE_YEARS": ("GenAI Experience",
                               ("genai", "gen ai", "gen-ai", "generative ai", "generative artificial intelligence")),
    "AI_AGENT_EXPERIENCE_YEARS": ("AI Agent Experience",
                                  ("ai agent", "ai agents", "agentic ai", "ai agent development", "agents",
                                   "autonomous agents", "multi-agent systems")),
    "LANGGRAPH_EXPERIENCE": ("LangGraph Experience", ("langgraph", "lang graph")),
    "LANGCHAIN_EXPERIENCE_YEARS": ("LangChain Experience", ("langchain", "lang chain")),
    "AGENTCORE_EXPERIENCE": ("AgentCore Experience",
                             ("agentcore", "agent core", "bedrock agentcore", "aws bedrock agentcore")),
    "CLAUDE_EXPERIENCE_YEARS": ("Claude Experience", ("claude", "anthropic claude")),
    "MACHINE_LEARNING_EXPERIENCE_YEARS": ("Machine Learning Experience", ("machine learning", "ml")),
    "AIRFLOW_EXPERIENCE_YEARS": ("Airflow Experience", ("airflow", "apache airflow", "mwaa")),
    "KAFKA_EXPERIENCE_YEARS": ("Kafka Experience", ("kafka", "apache kafka")),
    "SNOWFLAKE_EXPERIENCE_YEARS": ("Snowflake Experience", ("snowflake",)),
    "AWS_GLUE_EXPERIENCE_YEARS": ("AWS Glue Experience", ("glue", "aws glue")),
    "AZURE_DATA_FACTORY_EXPERIENCE_YEARS": ("Azure Data Factory Experience", ("adf", "azure data factory")),
    "DOCKER_EXPERIENCE_YEARS": ("Docker Experience", ("docker",)),
    "KUBERNETES_EXPERIENCE_YEARS": ("Kubernetes Experience", ("kubernetes", "k8s")),
    "OCR_EXPERIENCE_YEARS": ("OCR Experience", ("ocr", "ocr automation")),
    "SERVERLESS_EXPERIENCE_YEARS": ("Serverless Experience", ("serverless", "aws serverless", "aws lambda")),
}

# Which fact answers a generic "Relevant Experience" question for each role.
# The first key that has a verified value wins; nothing else is ever used.
ROLE_RELEVANT_KEYS: dict[str, tuple[str, ...]] = {
    "awsdataengineer": ("AWS_DATA_ENGINEERING_EXPERIENCE_YEARS",),
    "azuredataengineer": ("AZURE_DATA_ENGINEERING_EXPERIENCE_YEARS",),
    "aiengineer": ("AI_ENGINEERING_EXPERIENCE_YEARS",),
    "pythondeveloper": ("PYTHON_DEVELOPMENT_EXPERIENCE_YEARS", "PYTHON_EXPERIENCE_YEARS"),
    "dataengineer": ("DATA_ENGINEERING_EXPERIENCE_YEARS",),
}

ROLE_LABELS = {
    "aiengineer": "AI Engineer", "awsdataengineer": "AWS Data Engineer",
    "azuredataengineer": "Azure Data Engineer", "dataengineer": "Data Engineer",
    "pythondeveloper": "Python Developer",
}

# A generic data-engineer hint never contradicts a cloud-specific one.
COMPATIBLE_ROLES = {
    frozenset({"dataengineer", "awsdataengineer"}),
    frozenset({"dataengineer", "azuredataengineer"}),
}

_FILLER = re.compile(r"^(?:and|or|the|in|with|on|of|also|including|e\.g\.|like|my|our)\s+|\s+experience$|\s+(?:skills?)$")


def normalise_item(text: str) -> str:
    """'  and AWS Data Engineering. ' -> 'aws data engineering'."""
    value = re.sub(r"[*_`\"'()\[\]]", "", text or "").strip().lower()
    value = re.sub(r"\s+", " ", value).strip(" .,:;-")
    previous = None
    while previous != value:
        previous = value
        value = _FILLER.sub("", value).strip(" .,:;-")
    return value


def _index(table: dict[str, tuple[str, tuple[str, ...]]]) -> dict[str, str]:
    return {alias: key for key, (_label, aliases) in table.items() for alias in aliases}


ROLE_AREA_BY_ALIAS = _index(ROLE_AREAS)
SKILL_BY_ALIAS = _index(SKILLS)


def key_for_item(item: str) -> str | None:
    """Exact alias lookup for one list item. Role areas win over single skills."""
    norm = normalise_item(item)
    return ROLE_AREA_BY_ALIAS.get(norm) or SKILL_BY_ALIAS.get(norm)


def aliases_for(key: str) -> tuple[str, ...]:
    entry = ROLE_AREAS.get(key) or SKILLS.get(key)
    return entry[1] if entry else ()


def label_for_key(key: str) -> str | None:
    entry = ROLE_AREAS.get(key) or SKILLS.get(key)
    return entry[0] if entry else None


def alias_pattern(aliases: tuple[str, ...]) -> str:
    """Regex alternation for aliases, longest first, whole words."""
    parts = sorted({re.escape(a).replace(r"\ ", r"\s*-?\s*") for a in aliases}, key=len, reverse=True)
    return r"(?<![\w-])(?:" + "|".join(parts) + r")(?![\w-])"


def skills_mentioned(text: str) -> list[str]:
    """Fact keys of skills / role areas whose alias appears anywhere in the text."""
    lowered = (text or "").lower()
    found = []
    for table in (ROLE_AREAS, SKILLS):
        for key, (_label, aliases) in table.items():
            if re.search(alias_pattern(aliases), lowered):
                found.append(key)
    return list(dict.fromkeys(found))
