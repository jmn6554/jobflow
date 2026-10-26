"""Interrupt synchronous application work even inside broad Exception handlers."""
from contextlib import contextmanager
import signal
import threading


class ApplicationTimeLimit(BaseException):
    pass


@contextmanager
def application_phase_budget(seconds):
    """Give a bounded slow phase its own time, preserving outer work time."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Application phases must run on the main thread')
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    previous = signal.getsignal(signal.SIGALRM)
    def expired(signum, frame):
        raise ApplicationTimeLimit('Application phase exceeded its wall-clock budget')
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.01, float(seconds)))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if remaining:
            signal.setitimer(signal.ITIMER_REAL, remaining, interval)


@contextmanager
def application_time_limit(seconds):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Bounded application worker must run on the main thread')
    if signal.getitimer(signal.ITIMER_REAL)[0]:
        raise RuntimeError('Cannot replace an existing process timer')
    previous = signal.getsignal(signal.SIGALRM)
    def expired(signum, frame):
        raise ApplicationTimeLimit('Application exceeded its wall-clock budget')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.01, float(seconds)))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
