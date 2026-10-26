"""Public tests must not inherit developer credentials or submit applications."""
import os
import pytest


@pytest.fixture(autouse=True)
def isolate_credentials(monkeypatch):
    for key in list(os.environ):
        if key.endswith(('API_KEY', 'APP_KEY', 'TOKEN')) or key.startswith('JOBFLOW_'):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('JOBFLOW_APPLICATION_AI_ENABLED', 'false')
