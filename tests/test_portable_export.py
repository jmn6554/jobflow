import importlib
import json
import pkgutil
from pathlib import Path
from unittest.mock import Mock

import pytest

import jobflow
from jobflow.autopilot import ApplicationProfile, AssistedApplicationService, PlaywrightFormAdapter
from jobflow.cli import parser
from jobflow.policy import settings, us_office
from jobflow.resume import configured_tailoring_provider


def test_every_module_imports_without_local_model():
    names = [m.name for m in pkgutil.iter_modules(jobflow.__path__)]
    assert not any(name.startswith('local_') for name in names)
    for name in names:
        importlib.import_module('jobflow.' + name)


@pytest.mark.parametrize('question', [
    'Why do you want to work for us?', 'What is your gender?',
    'Are you a veteran?', 'Do you have a disability?',
    'Are you legally authorized to work in Canada?',
    'Will you require sponsorship?', 'Do you accept the privacy notice?',
    'How many years of SQL experience do you have?',
    'Tell us about your ETL experience', 'What is your notice period?',
])
def test_missing_facts_never_inherit_original_applicant_answers(question):
    assert ApplicationProfile({}).answer(question, 'CA') is None


def test_employer_specific_answers_take_precedence():
    profile = ApplicationProfile({
        'additional_answers': {'Notice period?': 'Four weeks'},
        'employer_answers': {'Example': {'Notice period?': 'Six weeks'}},
    })
    assert profile.answer('Notice period?', 'CA', 'Example') == 'Six weeks'
    assert profile.answer('Notice period?', 'CA', 'Another') == 'Four weeks'


def test_identity_and_authorization_are_explicit():
    profile = ApplicationProfile({
        'identity': {'full_name': 'Alex Example'},
        'work_authorization': {'CA': {'authorized': True}, 'US': {'authorized': False}},
    })
    assert profile.answer('Full name', 'CA') == 'Alex Example'
    assert profile.answer('Are you legally authorized to work in Canada?', 'CA') == 'Yes'
    assert profile.answer('Are you legally authorized to work in Canada?', 'US') is None
    assert profile.answer('Are you legally authorized to work in the United States?', 'US') == 'No'
    assert profile.answer('Will you require sponsorship?', 'US') is None


def test_application_uses_unchanged_resume_snapshot(tmp_path):
    from jobflow.repository import Repository
    from jobflow.models import JobInput
    repo = Repository(str(tmp_path / 'jobs.db'))
    repo.init()
    job_id, _, _ = repo.upsert_job(JobInput('Data Analyst', 'Example', 'Canada', 'https://jobs.example/1', 'Analyze data.'))
    resume = tmp_path / 'resume.docx'
    resume.write_bytes(b'unchanged original file')
    service = AssistedApplicationService(repo, browser_profile=tmp_path / 'browser')
    snapshot = service._prepare_form_resume({'id': job_id}, resume)
    assert Path(snapshot).read_bytes() == resume.read_bytes()
    assert Path(snapshot).resolve() != resume.resolve()


def test_defaults_do_not_enable_model_or_submit(monkeypatch):
    monkeypatch.delenv('JOBFLOW_RESUME_PROVIDER', raising=False)
    with pytest.raises(ValueError, match='disabled'):
        configured_tailoring_provider()
    assert parser().parse_args(['assist', '1']).auto_submit is False
    assert settings()['application_approval_required'] is True
    assert not us_office('')


def test_blank_profile_has_no_contact_or_authorization():
    profile = json.loads(Path('examples/application-profile.example.json').read_text())
    assert not any(profile['identity'].values())
    assert profile['work_authorization'] == {}
    assert profile['additional_answers'] == {}


def test_dashboard_routes_load_with_empty_database(tmp_path):
    from fastapi.testclient import TestClient
    from jobflow.dashboard import create_app
    app = create_app(str(tmp_path / 'jobs.db'))
    with TestClient(app) as client:
        for route in ('/', '/daily', '/settings', '/exceptions'):
            assert client.get(route).status_code == 200, route
