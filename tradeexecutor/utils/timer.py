import datetime
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def timed_task(task_name: str, **context_info):
    """A simple context manger to measure the duration of different tasks.

    Can be later plugged in to a metrics system like Statsd / Grafana / Datadog.
    """
    started = datetime.datetime.utcnow()
    logger.info("Starting task %s at %s, context is %s", task_name, started, context_info)

    try:
        yield
    finally:
        duration = datetime.datetime.utcnow() - started
        logger.info("Ended task %s, took %s", task_name, duration)

