import signal
import time
import pytest
from jobflow.application_budget import application_time_limit, application_phase_budget, ApplicationTimeLimit


def test_slow_phase_does_not_consume_form_budget():
    with application_time_limit(.15):
        with application_phase_budget(.6):
            time.sleep(.2)
        assert signal.getitimer(signal.ITIMER_REAL)[0] > .1
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_phase_still_has_a_deadline_and_restores_outer_budget():
    with application_time_limit(1):
        with pytest.raises(ApplicationTimeLimit):
            with application_phase_budget(.02):
                time.sleep(.1)
        assert signal.getitimer(signal.ITIMER_REAL)[0] > .8


def test_phase_restores_timer_on_interruption():
    with application_time_limit(1):
        with pytest.raises(KeyboardInterrupt):
            with application_phase_budget(5):
                raise KeyboardInterrupt
        assert signal.getitimer(signal.ITIMER_REAL)[0] > .8


def test_interrupts_broad_exception_handlers_and_restores_timer():
    original = signal.getsignal(signal.SIGALRM)
    start = time.monotonic()
    with pytest.raises(ApplicationTimeLimit):
        with application_time_limit(.03):
            try:
                time.sleep(1)
            except Exception:
                pytest.fail('Deadline swallowed by generic handler')
    assert time.monotonic()-start < .5
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    assert signal.getsignal(signal.SIGALRM) == original


def test_timer_cleared_on_success_and_can_process_next_job():
    with application_time_limit(.1):
        pass
    with application_time_limit(.1):
        pass
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


def test_question_precheck_stops_model_calls_after_first_unknown(monkeypatch):
    from jobflow.autopilot import AssistedApplicationService, ApplicationProfile
    fields = [dict(question=q, kind='text', selected=False, options=[]) for q in ['Unknown one', 'Unknown two']]
    monkeypatch.setattr(AssistedApplicationService, '_inspect_required', lambda surface: fields)
    calls = []
    def answer(question, options):
        calls.append(question)
        return None
    unknown = AssistedApplicationService._preflight_unknown(None,
        dict(id=1,company='Example',location='Canada',country='CA',description=''),
        ApplicationProfile({}), answer_for=answer, stop_after_first=True)
    assert unknown == ['Unknown one']
    assert calls == ['Unknown one']
