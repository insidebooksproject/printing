"""Follow a spooled job until it leaves the queue, errors, or times out.

``EndDoc`` (or ``lp``) returning only means the spooler accepted the job.
Whether a label actually came out is only visible by watching the job's
status afterwards. The backends report that status as a :class:`JobSnapshot`
whose :class:`JobState` is all this module looks at.
"""

import logging
import time
from typing import Callable, Optional

from ibp_printing.log import (
    describe_exception,
    exception_summary,
    get_logger,
    log_event,
)
from ibp_printing.models import JobOutcome, JobSnapshot, JobState, PrintResult

logger = get_logger(__name__)

# Errors this long in a row are reported; shorter blips (e.g. a busy USB
# port) often clear on their own.
ERROR_GRACE_S = 10.0


def track_job(
    result: PrintResult,
    poll: Callable[[], Optional[JobSnapshot]],
    timeout_s: float,
    interval_s: float = 0.5,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> JobOutcome:
    """Poll the spooler for one job and record each status change.

    Args:
        result: Updated in place with ``job_id``, ``history`` and ``outcome``.
        poll: Returns the job's current snapshot, or None once it is gone.
        timeout_s: How long to wait before giving up (the job is left alone).
    """
    started = clock()
    last: Optional[JobSnapshot] = None
    error_since: Optional[float] = None

    while True:
        snapshot = poll()
        now = clock()

        if snapshot is None:
            outcome = outcome_when_gone(None if last is None else last.state)
            break

        result.job_id = snapshot.job_id
        if last is None or (snapshot.state, snapshot.flags) != (last.state, last.flags):
            _record_change(result, snapshot, now - started)
        last = snapshot

        if snapshot.state is JobState.ERROR:
            error_since = now if error_since is None else error_since
        else:
            error_since = None

        classified = classify_state(
            snapshot.state,
            error_for_s=None if error_since is None else now - error_since,
            elapsed_s=now - started,
            timeout_s=timeout_s,
        )
        if classified is not None:
            outcome = classified
            break
        sleep(interval_s)

    result.outcome = outcome
    log_event(
        logger,
        logging.INFO if outcome.ok else logging.ERROR,
        f"job outcome: {outcome.value}",
        printer=result.printer_name,
        job_id=result.job_id,
        history=result.history,
        waited_s=round(clock() - started, 2),
    )
    return outcome


def tracking_failed(result: PrintResult, exc: BaseException) -> None:
    """Record that following a submitted job raised: ``TRACKING_FAILED``."""
    result.outcome = JobOutcome.TRACKING_FAILED
    result.history.append(f"tracking failed: {exception_summary(exc)}")
    log_event(
        logger,
        logging.WARNING,
        "job tracking failed; the job was spooled but its outcome is unknown",
        printer=result.printer_name,
        job_id=result.job_id,
        history=result.history,
        error=describe_exception(exc),
    )


def outcome_when_gone(last_state: Optional[JobState]) -> JobOutcome:
    """Classify a job that has left the queue, given the last state we saw.

    ``None`` means the job was never observed (it printed before the first poll
    or was never queued under the expected name).
    """
    if last_state is None:
        return JobOutcome.VANISHED_UNSEEN
    if last_state is JobState.DELETED:
        return JobOutcome.DELETED
    if last_state is JobState.ERROR:
        return JobOutcome.ERROR
    return JobOutcome.COMPLETED


def classify_state(
    state: JobState,
    *,
    error_for_s: Optional[float],
    elapsed_s: float,
    timeout_s: float,
) -> Optional[JobOutcome]:
    """Return the final outcome for a job still in the queue, or None to keep waiting.

    Args:
        state: The job's current state.
        error_for_s: How long the job has continuously been in an error state.
        elapsed_s: Time since tracking started.
        timeout_s: Give up after this long.
    """
    if state is JobState.DONE:
        return JobOutcome.COMPLETED
    if state is JobState.DELETED:
        return JobOutcome.DELETED
    if error_for_s is not None and error_for_s >= ERROR_GRACE_S:
        return JobOutcome.ERROR
    if elapsed_s >= timeout_s:
        return JobOutcome.TIMEOUT
    return None


def _record_change(
    result: PrintResult, snapshot: JobSnapshot, elapsed_s: float
) -> None:
    """Append a status change to the result history and log it."""
    label = ",".join(snapshot.flags) or snapshot.state.name
    entry = f"+{elapsed_s:.1f}s {label}"
    if snapshot.status_text:
        entry += f" ({snapshot.status_text})"
    result.history.append(entry)
    log_event(
        logger,
        logging.INFO,
        "job status changed",
        printer=result.printer_name,
        job_id=snapshot.job_id,
        state=snapshot.state.value,
        status=list(snapshot.flags),
        status_text=snapshot.status_text,
        pages=f"{snapshot.pages_printed}/{snapshot.total_pages}",
        t=round(elapsed_s, 2),
    )
