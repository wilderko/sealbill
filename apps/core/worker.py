"""A tiny job registry and loop. Jobs run in this one process, one at a time."""
import logging
import signal
import time

from django.utils import timezone

logger = logging.getLogger(__name__)

_jobs: dict[str, dict] = {}
_stop = False
_wall: dict[str, str] = {}  # job name -> ISO time of its last run, for the heartbeat


def register(name, every_s, fn):
    _jobs[name] = {"every_s": every_s, "fn": fn, "last_run": None}


def reset():
    global _stop
    _jobs.clear()
    _wall.clear()
    _stop = False


def request_stop(*_args):
    global _stop
    _stop = True


def stop_requested() -> bool:
    return _stop


def run_due(now) -> list[str]:
    """Run each job that is due and return the names. A failing job never stops the rest."""
    ran = []
    for name, job in list(_jobs.items()):
        last = job["last_run"]
        if last is not None and last + job["every_s"] > now:
            continue
        job["last_run"] = now  # set first: a failure is rescheduled, not retried at once
        ran.append(name)
        _wall[name] = timezone.now().isoformat()
        beat()  # a long job must not make a live worker look dead
        try:
            job["fn"]()
        except Exception as exc:  # noqa: BLE001 - the loop must survive any job
            # type only: the message of a failing job may carry client data
            logger.error("worker job %s failed: %s", name, type(exc).__name__)
    return ran


def beat():
    """Record that the loop is alive. Plaintext only (works sealed); a failure is logged by type
    and swallowed, because a broken heartbeat must never stop the worker."""
    try:
        from apps.core.models import WorkerHeartbeat

        WorkerHeartbeat.objects.update_or_create(pk=1, defaults={"beat_at": timezone.now(), "jobs": dict(_wall)})
    except Exception as exc:  # noqa: BLE001
        logger.error("worker heartbeat failed: %s", type(exc).__name__)


def serve(clock=time.monotonic, sleep=time.sleep, tick_s=1.0):
    """Loop until SIGTERM/SIGINT; the job in hand always finishes first."""
    global _stop
    _stop = False
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not _stop:
        run_due(clock())
        beat()
        if _stop:
            break
        sleep(tick_s)
