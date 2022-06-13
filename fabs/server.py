#
# Copyright (c) 2015-2022, Sine Nomine Associates ("SNA")
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND SNA DISCLAIMS ALL WARRANTIES WITH REGARD
# TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND
# FITNESS. IN NO EVENT SHALL SNA BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR
# CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE,
# DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS
# ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS
# SOFTWARE.

import multiprocessing
import signal

import fabs.err as err
import fabs.brun
import fabs.dumpjob
import fabs.restore
import fabs.util
import fabs.const
import fabs.log
log = fabs.log.getLogger(__name__)

do_quit = False

def _int_handler(signum, frame):
    # pylint: disable=unused-argument
    global do_quit
    do_quit = True

def run(once=False):
    """
    Run the server main loop.

    Args:
        once (bool, optional): If True, just run the main loop exactly once,
            then return. Otherwise, run forever until interrupted.
    """

    log.info('start', "Starting fabs server v%s" % fabs.const.VERSION)
    int_sleep.setup()

    if not once:
        signal.signal(signal.SIGINT, _int_handler)
        signal.signal(signal.SIGTERM, _int_handler)

    while not do_quit:
        try:
            fabs.brun.BackupRun.process_jobs()
        except Exception:
            log.exception('bruns_err', "Error while processing backup runs")

        try:
            fabs.dumpjob.DumpJob.process_jobs()
        except Exception:
            log.exception('dumpjobs_err', "Error while processing dump jobs")

        try:
            fabs.restore.RestoreRequest.process_jobs()
        except Exception:
            log.exception('restore_err', "Error while processing restore requests")

        if once:
            break

        int_sleep.sleep(60)

    log.info('stop', "Fabs server shutting down")

def made_progress():
    """
    In a child process, indicate that we have made some forward process.

    This wakes the parent process (the server process), so it will immediately
    check the db to see if there is more work to do.
    """
    int_sleep.wake_parent()

class _InterruptibleSleep:
    """
    Lets the caller sleep for a certain amount of time, but can be woken up
    early by child processes calling `made_progress`.
    """

    def __init__(self):
        self.event = None

    def setup(self):
        if self.event is not None:
            # We've already been setup
            return
        self.event = multiprocessing.Event()

    def wake_parent(self):
        if self.event is None:
            raise err.InternalError("int sleep event is not set, but we called wake_parent")
        self.event.set()

    def sleep(self, seconds):
        """
        Sleep for approximately the given number of seconds, unless woken up
        early.

        Args:
            seconds (int): The number of seconds to sleep.
        """

        if not self.event.is_set():
            log.d("Waiting up to %d seconds for an event to trigger" % seconds)

        # Wait to see if self.event is triggered, but we also want to check if
        # do_quit is set. Python has various issues with triggering an Event in
        # a signal handler, so sidestep the whole issue and check out do_quit
        # flag here.
        for _ in range(seconds):
            self.event.wait(1)
            if self.event.is_set():
                break
            if do_quit:
                break

        # Periodically clean up any lingering zombies created from FabsProcess
        # jobs
        # pylint: disable=not-callable
        multiprocessing.active_children()

        self.event.clear()

int_sleep = _InterruptibleSleep()
