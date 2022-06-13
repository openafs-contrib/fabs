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

from fnmatch import fnmatch
import json
import time

from fabs.dumpjob import DumpJob

import fabs.server
import fabs.report as report
import fabs.util as util
import fabs.err as err
import fabs.db as db
import fabs.config as config
import fabs.bstore as bstore
import fabs.log
log = fabs.log.getLogger(__name__)

class BackupRun:
    """
    A BackupRun represents a fabs backup run, a single run through the cell,
    backing up a single volume or a set of volumes at a particular time.

    Don't instantiate a BackupRun directly. You can create a new one with
    BackupRun.create(), or pull existing ones from the db with
    BackupRun.find().
    """

    @classmethod
    def create(cls, cell, note, volume, injected_blob=None):
        """
        Create a new BackupRun.

        Args:
            cell (str): The cell to backup.
            note (str): Human-readable note (e.g. "Daily backup for xxx").
            volume (str or None): The name of the volume to backup, for a
                single-volume backup. Give `None` to backup all volumes
                according to the fabs config.
            injected_blob (`fabs.bstore.DumpBlob`, optional): If provided, this
                brun will use the given dump blob instead of dumping the volume
                from the fileserver.

        Returns:
            `BackupRun`: The created backup run.
        """
        iblob_storid = None
        iblob_spath = None
        iblob_cksum = None

        if injected_blob:
            if not volume:
                raise err.InternalError("injected_blob without volume")

            iblob_storid = injected_blob.storid()
            iblob_spath = injected_blob.rel_path()
            iblob_cksum = injected_blob.checksum()
            injected_blob.no_delete()

        brid = db.brun.create(cell=cell, note=note, volume=volume,
                              injected_blob_storid=iblob_storid,
                              injected_blob_spath=iblob_spath,
                              injected_blob_checksum=iblob_cksum)
        return cls.find(id=brid)[0]

    @classmethod
    def _binfo_status(cls, binfo):
        ret = ''

        data = {}
        data.update(binfo)

        if data['state_last'] is not None:
            data['state'] += "/%s" % data['state_last']

        if data['volume'] is None:
            data['volume'] = '*'

        for field in ('start', 'end'):
            if int(data[field]) > 0:
                data[field] = util.time_unix2str(int(data[field]))

        data['jobs_total'] = len(data['jobs'])

        ret += """Backup run %(id)d [%(state)s]: %(note)s
  status.%(dv)d: %(state_descr)s
  cell: %(cell)s, volume: %(volume)s, errors: %(errors)d
  start: %(start)s
  end:   %(end)s
  jobs: %(jobs_total)d total
""" % data

        collapse_states = ('NEW', 'CLONE_PENDING', 'CLONE_DONE',
                           'DUMP_PENDING', 'DUMP_SKIPPED', 'DUMP_DONE',
                           'DONE')

        state_summary = {}
        expand_jobs = {}

        jobs = binfo['jobs']
        for job in jobs:
            if job['state'] in collapse_states:
                state_summary[job['state']] = state_summary.get(job['state'], 0) + 1
            else:
                key = job['server_addr']
                expand_jobs.setdefault(key, [])
                expand_jobs[key].append(job)

        for state in collapse_states:
            numjobs = state_summary.get(state, 0)
            if numjobs > 0:
                ret += "  [%d job(s) in state %s]\n" % (numjobs, state)

        ret += "\n"

        for server_addr, jobs in expand_jobs.items():

            ret += "  Server %s, %d job(s):\n" % (server_addr, len(jobs))

            for job in jobs:
                data = {}
                data.update(job)
                if data['state_last'] is not None:
                    data['state'] += "/%s" % data['state_last']

                for field in ('ctime', 'mtime'):
                    if int(data[field]) > 0:
                        data[field] = time.ctime(int(data[field]))

                ret += """    vol %(name)s job %(vl_id)d.%(dv)d [%(state)s]: %(state_descr)s (%(state_source)s)
      ctime: %(ctime)s, rwid: %(rwid)d, part: vicep%(partition)s
      mtime: %(mtime)s, timeout: %(timeout)d, errors: %(errors)d

""" % data

        return ret

    @classmethod
    def status(cls, brid=None, failed=False):
        """
        Get the status of the specified backup runs.

        Args:
            brid (int, optional): If given, return the status of the brun with
                the given brid. Otherwise, return the status of failed or
                active bruns (depending on the other arguments given).
            failed (bool, optional): If True, return the status of all failed
                bruns. Otherwise, and if `brid` is not given, return the status
                of active bruns. Defaults to False.

        Returns:
            dict: A dictionary containing status info for the requested backup
            runs.
        """

        infos = []

        with db.connect(lock=False):
            if brid is not None:
                brun_list = cls.find(id=brid)
            elif failed:
                brun_list = cls.find(state='FAILED')
            else:
                brun_list = cls.find(state_not=['DONE', 'FAILED'])

            for brun in brun_list:
                bruninfo = {}
                # pylint: disable=not-an-iterable
                for col in db.model.backup_runs.columns:
                    attr = col.name
                    bruninfo[attr] = getattr(brun, attr)

                jobs = [jobinfo for jobinfo in DumpJob.describe(brun)]
                bruninfo['jobs'] = jobs

                infos.append(bruninfo)

        return {'status': infos}

    @classmethod
    def status_str(cls, info):
        """
        Translate the info from `BackupRun.status` into a human-readable
        string.

        Args:
            info (dict): A status dict returned from `BackupRun.status`.

        Returns:
            str: A human-readable formatted status.
        """

        info_list = info['status']
        ret = ''
        ret += "Found %d backup run(s)\n" % len(info_list)
        for binfo in info_list:
            ret += cls._binfo_status(binfo)
        return ret

    @classmethod
    def find(cls, **kwargs):
        """
        Find an existing BackupRun in the database.

        Args:
            **kargs: Search criteria. Any BackupRun database column can be
            specified; common criteria are, for example:

            * id=5

            * state='DONE'

            * state_not=['DONE','FAILED']

            See `fabs.db._BackupRunDb.find` for specifics on criteria that can
            be given.

        Returns:
            list of BackupRun: A list of matching bruns. Note that a list is
            returned even if you're just looking for a single brun by brid.
            (You get a list of length 1 in that case).

            If no bruns are found, and you didn't search for a specific id, an
            empty list is returned.

        Raises:
            `fabs.err.BadBridError`: If you searched for a specific id, but
            that id doesn't exist.
        """
        rows = db.brun.find(**kwargs)

        # If we were given an id, that backuprun should really exist.
        if 'id' in kwargs and not rows:
            raise err.BadBridError("Backup run id %d does not seem to exist" % kwargs['id'])

        return [BackupRun(row) for row in rows]

    @classmethod
    def find_update(cls, cell, from_state, to_state, descr):
        """
        Transition all bruns from one state to another state, and return all
        bruns in the new state.

        Args:
            cell (str):       Only update bruns for this cell.
            from_state (str): Search for bruns in this state.
            to_state (str):   Transition matching bruns to this state.
            descr (str):      For bruns in `from_state`, set the status description
                to this when transitioning to `to_state`.

        Returns:
            list of BackupRun: All bruns in `to_state`. This may include bruns
            that were already in `to_state`; we didn't necessarily transition
            them to `to_state` in this function.
        """

        bruns = cls.find(cell=cell, state=from_state)

        if to_state is None:
            return bruns

        with db.connect():
            for brun in bruns:
                if descr is None:
                    brun.update(state=to_state)
                else:
                    brun.update(state=to_state, state_descr=descr)

        bruns = cls.find(cell=cell, state=to_state)
        return bruns

    @classmethod
    def _filter_dups(cls, bruns):
        # Check if we have duplicates, so we don't run the same backup run more
        # than once in parallel

        # Keep track of a dict mapping 'volume' to the brun for that volume
        # (remember, a volume of None means the brun is for all volumes)
        vol_map = {}
        for brun in bruns:
            conflict_brun = vol_map.get(brun.volume, None)

            if conflict_brun is not None:
                brun.update(state='FAILED',
                            state_descr="Conflict with brid %d in state %s" % (
                                         conflict_brun.id, conflict_brun.state))
            else:
                vol_map[brun.volume] = brun

        # When we're done, all of the bruns in the vol_map should be unique wrt
        # volume
        return list(vol_map.values())

    @classmethod
    def _process(cls, cell, from_state, to_state, start_state, func, descr):
        """
        Process backup runs in a given state.

        For each brun in the given cell and `from_state`, transition them to
        `to_state` and set the status description to `descr`.

        Then for each brun in `to_state`, spawn a child process, transition the
        brun to `start_state`, and run `func`.

        Args:
            cell (str):        Match bruns in this cell.
            from_state (str):  Match bruns in this state.
            to_state (str):    Transition matching bruns to this state.
            start_state (str): Transition bruns to this state in child process.
            func (function):   Function to run in the child process.
            descr (str):       For bruns in `from_state`, set the status description
                to this when transitioning to `to_state`.
        """

        bruns = cls.find_update(cell, from_state, to_state, descr)
        log.d("Found %d bruns in state %s -> %s" % (len(bruns), from_state, to_state))

        bruns = cls._filter_dups(bruns)
        if not bruns:
            # no matching bruns; nothing to do
            return

        procs = []

        with db.connect_bulk() as tx:
            for brun in bruns:
                if start_state is not None:
                    brun.update(state=start_state)
                    tx.entry_done()

                if func:
                    procs.append(util.FabsProcess(target=brun.process_child, args=(func,)))

        for proc in procs:
            proc.start()

    @classmethod
    def process_jobs(cls):
        """
        Process work that needs to be done for backup runs.
        """

        cell = config.get('afs/cell')

        cls._process(cell, 'NEW', 'INIT_START', 'INIT_WORK', cls.do_init,
                     "Starting backup run initialization")

        # Scan the vldb for fileserver addresses, and store the addresses
        cls._process(cell, 'INIT_DONE', 'VLADDR_START', 'VLADDR_WORK', cls.do_vladdr,
                     "Starting to scan vldb server addresses")

        # Scan the vldb for volume entries, and store the ones we care about
        cls._process(cell, 'VLADDR_DONE', 'VLENTRY_START', 'VLENTRY_WORK',
                     cls.do_vlentry, "Starting to scan vldb entries")

        # Create our jobs to clone volumes. (Not scheduled, just created in
        # the db.)
        cls._process(cell, 'VLENTRY_DONE', 'BRUN_CLCREATE_START', 'BRUN_CLCREATE_WORK',
                     cls.create_clones, "Starting volume clones")

        # Schedule any pending clone jobs. This runs repeatedly, scheduling
        # more jobs as we are able, respecting our various parallel quotas.
        cls._process(cell, 'BRUN_CLCREATE_DONE', 'BRUN_CLSCHED_START', 'BRUN_CLSCHED_WORK',
                     cls.sched_clones, "About to schedule volume clones")

        # Create our jobs to dump volumes. (Not scheduled, just created in the
        # db.)
        cls._process(cell, 'BRUN_CLSCHED_DONE', 'BRUN_DUMPCREATE_START', 'BRUN_DUMPCREATE_WORK',
                     cls.create_dumps, "Starting volume dumps")

        # Schedule any pending dump jobs. This runs repeatedly, scheduling more
        # jobs as we are able, respecting our various parallel quotas.
        cls._process(cell, 'BRUN_DUMPCREATE_DONE', 'BRUN_DUMPSCHED_START', 'BRUN_DUMPSCHED_WORK',
                     cls.sched_dumps, "About to schedule volume dumps")

        # Send a report out about this finished backup run, including what
        # backups succeeded, failed, etc.
        cls._process(cell, 'BRUN_DUMPSCHED_DONE', 'BRUN_REPORT_START', 'BRUN_REPORT_WORK',
                     cls.do_report, "About to report on finished backup run")

        cls._process(cell, 'BRUN_REPORT_DONE', 'BRUN_CLEANUP_START', 'BRUN_CLEANUP_WORK',
                     cls.do_cleanup, "Cleaning up finished backup run")

        # BRUN_REPORT_DONE is the last stage; we don't have anything more to
        # do. So move the brun into the DONE stage to indicate there's nothing
        # more. Don't use _process for this, since we don't want to find all
        # DONE bruns, and we don't need to spawn a new process just for this
        # final transition.
        cls._finish_bruns(cell, 'BRUN_CLEANUP_DONE')

        # Retry stages that failed with an error
        cls._process(cell, 'ERROR', 'BRUN_RECOVER_START', 'BRUN_RECOVER_WORK',
                     cls.do_recover, None)

        # For recent runs, check if they have any dangling vl_entry rows or
        # other cruft.
        for brun in cls.cleanable_bruns(recent=True):
            brun.clean_cruft()

    @classmethod
    def _finish_bruns(cls, cell, state):
        for brun in cls.find(cell=cell, state=state):
            brun.update(state='DONE', state_descr="Done")
            log.info('brun_done', "Backup run %d finished" % brun.id)

    @classmethod
    def cleanable_bruns(cls, recent=False, all_=False):
        """
        Find backup runs that are eligible for cleaning up.

        You must specify either `recent` or `all_`.

        Args:
            recent (bool, optional): If True, only look for recent bruns
                (started in the last 12 hours). If there are any active bruns,
                don't find any bruns (since fabs is busy running those bruns).
                Defaults to False.
            all_ (bool, optional): If True, look for all bruns that aren't
                actively running. Defaults to False.

        Returns:
            list of `BackupRun`: A list of matching bruns.
        """

        if recent:
            active_bruns = cls.find(state_not=['DONE','FAILED'])
            if active_bruns:
                brun = active_bruns[0]
                log.d("Not cleaning bruns, brun %d is active" % (brun.id))
                return []

            now = int(time.time())
            interval = 60*60*12 # twelve hours
            start_time = now-interval

            return cls.find(start_after=start_time)

        elif all_:
            return cls.find(state=['DONE','FAILED'])

        raise ValueError("We're not cleaning all backup_runs, but no criteria given?")

    def __init__(self, row):
        """
        Initialize a BackupRun object.

        Don't run this directly; use `BackupRun.create`, or `BackupRun.find`,
        or something else.

        Args:
            row (`sqlalchemy.engine.Row`): The row from the database that
                represents the BackupRun to instantiate.
        """
        for col in row.keys():
            setattr(self, col, row[col])

    def describe_dict(self):
        """
        Returns:
            dict: A dictionary of info describing this brun.
        """
        brinfo = {}
        # pylint: disable=not-an-iterable
        for col in db.model.backup_runs.columns:
            attr = col.name
            brinfo[attr] = getattr(self, attr)
        return brinfo

    def describe(self, from_dict=None):
        """
        Args:
            from_dict (dict, optional): A dict from `BackupRun.describe_dict`,
                describing this brun. If not given, we'll just call
                `BackupRun.describe_dict` to get one.

        Returns:
            str: A human-readable formatted string describing this brun.
        """
        if from_dict is None:
            from_dict = self.describe_dict()
        data = {}
        data.update(from_dict)
        for field in ('start', 'end'):
            if int(data[field]) > 0:
                data[field] = util.time_unix2str(int(data[field]))
        return """Backup run %(id)d [%(state)s]: %(note)s
  status.%(dv)d: %(state_descr)s
  cell: %(cell)s, volume: %(volume)s, errors: %(errors)d
  start: %(start)s
  end:   %(end)s""" % data

    def delete(self):
        """
        Delete this brun from the database.

        This will delete all associated VL data for the backup run, but will
        not delete any existing dumps. If there are any dumps associated with
        this brun, this will fail due to database constraints.
        """
        db.brun.delete(self.id)

    def is_empty(self):
        """
        Returns:
            bool: True if this backup run is empty; that is, it has no volume
            dumps associated with it (and the caller can delete it). False
            otherwise.
        """
        for _ in db.voldump.finditer(brid=self.id):
            return False
        return True

    def update(self, **kwargs):
        """
        Update data for this brun in the db.

        Args:
            **kwargs: Columns to update in the database, and the values to
                update them to. See `fabs.db._BackupRunDb.update` for
                specifics.
        """
        db.brun.update(self, **kwargs)

    def error(self):
        """
        Flag this brun as encountering an error.

        Typically this function will be called when an exception is caught
        while trying to do something for this brun, or in general an error is
        encountered.

        This function will put this brun in an error state, unless it's already
        trying to recover from an error, in which case it will be put
        immediately into FAILED state.
        """

        if self.state in ('ERROR', 'BRUN_RECOVER_START', 'BRUN_RECOVER_WORK'):
            log.error('dump_err_meta', ("Backup run %d encountered an error " +
                      "while trying to recover from a previous error. It " +
                      "will not be retried.") % self.id)
            self.update(state='FAILED')
        else:
            db.brun.error(self)

    def kill(self, note):
        """
        Kill this brun.

        This puts the brun immediately into FAILED state.

        Args:
            note (str): A human-readable note to say why this brun was killed.
        """
        db.brun.kill(self, note)

    def retry(self):
        """
        Retry this failed/error'd brun.

        This tries to get this brun to run again, after encountering errors.
        This resets the error counter, and puts the brun in BRUN_RECOVER_START
        state, where it can go through the error recovery process.
        """

        if self.state not in ('ERROR', 'FAILED'):
            raise err.RetryStateError(("Backup run %d is not in an error " +
                                       "state (it's in %s)") % (
                                       self.id, self.state))
        self.update(state='BRUN_RECOVER_START', errors=0)

    def process_child(self, func):
        try:
            func(self)
        except Exception:
            log.exception('brun_child_err', "Error when processing backup run %d" % self.id)
            self.error()

    def do_recover(self):
        """
        Go through the error recovery process for this brun.

        This looks at what the last state was for this brun, and resets the
        brun back to the appropriate state so the relevant work can be retried.
        """

        ret_table = dict(
            VLADDR_WORK='VLADDR_START',
            VLENTRY_WORK='VLENTRY_START',
            BRUN_CLCREATE_WORK='BRUN_CLCREATE_START',
            BRUN_CLSCHED_WORK='BRUN_CLSCHED_START',
            BRUN_DUMPCREATE_WORK='BRUN_DUMPCREATE_START',
            BRUN_DUMPSCHED_WORK='BRUN_DUMPSCHED_START',
            BRUN_REPORT_WORK='BRUN_REPORT_START',
            BRUN_CLEANUP_WORK='BRUN_CLEANUP_START',

            # These are states where we can stay in the same state to recover.
            # That is, e.g. VLADDR_START just gets transitioned back to
            # VLADDR_START; we don't need to change to a new state.
            NEW=None,
            VLADDR_START=None,
            VLADDR_DONE=None,
            VLENTRY_START=None,
            VLENTRY_DONE=None,
            BRUN_CLCREATE_START=None,
            BRUN_CLCREATE_DONE=None,
            BRUN_CLSCHED_START=None,
            BRUN_CLSCHED_DONE=None,
            BRUN_DUMPCREATE_START=None,
            BRUN_DUMPCREATE_DONE=None,
            BRUN_DUMPSCHED_START=None,
            BRUN_DUMPSCHED_DONE=None,
            BRUN_REPORT_START=None,
            BRUN_REPORT_DONE=None,
            BRUN_CLEANUP_START=None,
            BRUN_CLEANUP_DONE=None,
        )

        failed = False

        if self.errors >= config.get('dump/error_limit'):
            log.error('brun_err_limit', ("Backup run %d has failed %d " +
                      "times, which exceeds our retry limit") % (
                      self.id, self.errors))
            failed = True

        elif self.state_last not in ret_table:
            log.error('brun_err_unknown', ("Backup run %d encountered " +
                      "an error in state %s, which we do not know how to " +
                      "retry") % (self.id, self.state_last))
            failed = True

        if failed:
            self.update(state='FAILED')
        else:
            retry_state = ret_table[self.state_last]
            if retry_state is None:
                retry_state = self.state_last

            log.warn('brun_retry', "Retrying backup run %d %s -> %s" % (
                     self.id, self.state_last, retry_state))
            self.update(state=retry_state, state_last=None)

            fabs.server.made_progress()

    def injected_blob(self):
        """
        Returns:
            `fabs.bstore.DumpBlob` or None: The injected dump blob for this
            brun, or None if there was none.
        """
        return bstore.blob_from_db(self.injected_blob_storid,
                                   self.injected_blob_spath,
                                   self.injected_blob_checksum)

    def do_cleanup(self):
        dblob = self.injected_blob()
        if dblob:
            # Get rid of our injected dump blob if it's still around
            dblob.delete()

        self.clean_cruft(active=True)

        self.update(state='BRUN_CLEANUP_DONE',
                    state_descr="Done cleaning up backup run")
        fabs.server.made_progress()

    def do_report(self):
        report.report_success(self)

        self.update(state='BRUN_REPORT_DONE', state_descr="Done sending report(s)")
        fabs.server.made_progress()

    def do_init(self):
        try:
            bstore.periodic_cleaning()
        except Exception:
            log.exception('bstore_cleanup_err', ("Error while performing " +
                          "periodic cleaning of blob storage dirs"))

        log.info('brun_start', "Starting backup run %d, backing up volumes: %s" % (
                 self.id, '*' if self.volume is None else self.volume))

        self.update(state='INIT_DONE', state_descr="Done initializing backup run")

        fabs.server.made_progress()

    def do_vladdr(self):

        self.update(state_descr="Scanning VLDB server addresses")

        vos = util.vos_unauth(cell=self.cell)
        res = vos.listaddrs(printuuid=True, noresolv=True).run()

        log.d("Adding %d vldb servers for brun %d" % (len(res.servers), self.id))

        db.vladdr.create_many(self, res.servers)

        self.update(state='VLADDR_DONE', state_descr="Done scanning VLDB server addresses")

        # Tell the server that we have made progress, so it will immediately
        # check for more stuff to do.
        fabs.server.made_progress()

    def _include_volume(self, vlentry, rw_fsid, inc_vol, exc_vol, inc_fs, exc_fs):
        """
        Determine whether or not to include the given volume in this backup
        run.

        This checks the vlentry for a volume, and sees if we should include it
        in the volumes to backup during this backup run, based on the
        per-volume and per-fileserver lists given.

        Args:
            vlentry (`fabs.cmd.VLDBEntry`): The VL entry to check.
            rw_fsid (int): ID number of the fileserver for the vol's RW site.
            inc_vol (list of str): fnmatch patterns of volume names to include.
            exc_vol (list of str): fnmatch patterns of volume names to exclude.
            inc_fs (list of int): Fileserver IDs to include.
            exc_fs (list of int): Fileserver IDs to exclude.

        Returns:
            bool: True if the volume should be backed up, False if the volume
            should be ignored.
        """

        matched = False

        if rw_fsid in inc_fs:
            log.d("volume %s matched inc_fs %r" % (vlentry.name, inc_fs))
            matched = True

        if not matched:
            for pat in inc_vol:
                if fnmatch(vlentry.name, pat):
                    log.d("volume %s matched vol pattern %s" % (vlentry.name, pat))
                    matched = True
                    break

        if not matched:
            log.d("Ignoring volume %s (not in include pattern)" % vlentry.name)
            return False

        if rw_fsid in exc_fs:
            log.d("Ignoring volume %s because it matched exc_fs %r" % (vlentry.name, exc_fs))
            return False

        for pat in exc_vol:
            if fnmatch(vlentry.name, pat):
                log.d("Ignoring volume %s (in exclude pattern %s)" % (vlentry.name, pat))
                return False

        log.d("brun %s including volume %s" % (self.id, vlentry.name))
        return True

    def _get_fsid(self, addrmap, server):
        fsid = addrmap.get(server, None)
        if fsid is None:
            log.warn('vlentry_noaddr',
                     "Server %s is unknown, creating a non-uuid entry for it" %
                     server)
            db.vladdr.create_nonuuid(self, addr=server)

            # Update our addrmap with whatever's in the db now. (Make sure to
            # alter addrmap, and not just assign to it, so our caller sees our
            # changes.)
            addrmap.clear()
            addrmap.update(db.vladdr.get_addrmap(self))

            fsid = addrmap.get(server, None)
            if fsid is None:
                raise err.VLFsIdError("Failed to create non-uuid entry for %s" %
                                      server)
        return fsid

    # "cludes" as in, 'cludes, as in includes/excludes.
    def _get_cludes(self):
        self.update(state_descr="Processing includes/excludes")

        cludes = {
            ('include', 'volumes'): [],
            ('exclude', 'volumes'): [],
            ('include', 'fileservers'): [],
            ('exclude', 'fileservers'): [],
        }

        # First, read cludes from e.g. config.get('dump/include/volumes')
        for (clude, clude_type) in cludes.keys():
            cludes[(clude, clude_type)].extend(config.get('dump/%s/%s' % (clude, clude_type)))

        # Then read in any cludes from our configured command, if any
        cmd = config.get('dump/filter_cmd/json')
        if cmd:
            raw_data = util.run_config_cmd(cmd)

            try:
                data = json.loads(raw_data)

                for (clude, clude_type) in cludes.keys():
                    if clude in data and clude_type in data[clude]:
                        cludes[(clude, clude_type)].extend(data[clude][clude_type])

            except Exception as exc:
                raise err.ConfigCmdError("Error processing data from filter command '%s': %s" % (
                                         cmd, type(exc).__name__ + str(exc)))

        log.info('filter', ("Backup run %d including volumes [%s], " +
                            "excluding volumes [%s], including " +
                            "fileservers [%s], excluding fileservers " +
                            "[%s]") % (
                            self.id,
                            ', '.join(cludes[('include', 'volumes')]),
                            ', '.join(cludes[('exclude', 'volumes')]),
                            ', '.join(cludes[('include', 'fileservers')]),
                            ', '.join(cludes[('exclude', 'fileservers')])))

        # Translate fileserver names into vl_fileservers ids
        uuidmap = db.vladdr.get_uuidmap(self)
        for (clude, clude_type) in [('include', 'fileservers'), ('exclude', 'fileservers')]:
            cludes[(clude, clude_type)] = self._fs2id(uuidmap, cludes[(clude, clude_type)])

        return (cludes[('include', 'volumes')],
                cludes[('exclude', 'volumes')],
                cludes[('include', 'fileservers')],
                cludes[('exclude', 'fileservers')],)

    def _fs2id(self, uuidmap, fslist):
        """
        Translate a list of fileserver names/addresses into their corresponding
        ids in the vl_fileservers table.

        We do this by running 'vos listaddrs' for each address to get the
        corresponding uuid, and then lookup the uuid in the given uuidmap.
        """
        vos = util.vos_unauth(cell=self.cell)

        ids = []

        for fs in fslist:
            log.d("Running listaddrs for fileserver %s" % fs)
            res = vos.listaddrs(host=fs, printuuid=True, noresolv=True).run()
            log.d("res: %r" % res)
            uuid = res.servers[0].uuid
            fs_id = uuidmap[uuid]
            ids.append(fs_id)
            log.d("Looked up fileserver %s as uuid %s fs_id %d" % (fs, uuid, fs_id))

        return ids

    def do_vlentry(self):

        (inc_vol, exc_vol, inc_fs, exc_fs) = self._get_cludes()

        self.update(state_descr="Scanning VLDB volume entries")

        vos = util.vos_unauth(cell=self.cell)
        if self.volume:
            cursor = vos.listvldb(name=self.volume, noresolv=True)
        else:
            cursor = vos.listvldb(noresolv=True)

        # Ideally we should probably have streaming results from this, but eh,
        # not now.
        res = cursor.run()

        log.d("Processing %d vldb entries" % len(res.entries))

        if not self.volume:
            log.d("Checking volumes against patterns include: %r exclude: %r" % (
                  config.get('dump/include/volumes'), config.get('dump/exclude/volumes')))

        addrmap = db.vladdr.get_addrmap(self)

        with db.connect_bulk() as tx:
            for entry in res.entries:

                rw_fsid = None
                for site in entry.sites:
                    if site.vtype == 'RW':
                        rw_fsid = self._get_fsid(addrmap, site.server)
                        break
                if rw_fsid is None:
                    log.warn('vlentry_norw', ("Cannot find an RW site for " +
                                              "volume %s; skipping it") % entry.name)
                    continue

                if not self.volume and not self._include_volume(entry, rw_fsid,
                                                                inc_vol,
                                                                exc_vol,
                                                                inc_fs,
                                                                exc_fs):
                    continue

                vl_id = db.vlentry.create(self, entry)
                vlsites = []

                for site in entry.sites:
                    fsid = self._get_fsid(addrmap, site.server)
                    part = site.partition.lower()
                    if part.startswith('/vicep'):
                        part = part[6:]

                    siteinfo = dict(
                        vl_id=vl_id,
                        type=site.vtype,
                        server_id=fsid,
                        partition=part
                    )

                    vlsites.append(siteinfo)
                db.vlentry.create_sites(entry, vlsites)

                tx.entry_done()

        self.update(state='VLENTRY_DONE', state_descr="Done scanning VLDB volume entries")

        fabs.server.made_progress()

    def create_clones(self):
        self.update(state='BRUN_CLCREATE_WORK', state_descr="Creating clone jobs")

        DumpJob.clear(self)

        with db.connect_bulk() as tx:
            for row in db.vlentry.finditer(self):
                rwsite = None
                for site in row['sites']:
                    if site['type'] == 'RW':
                        rwsite = site
                        break

                if rwsite is None:
                    log.warn('no_rw', "Volume %s has no RW site; not backing it up" %
                                      row['name'])
                    continue

                iblob_storid = None
                iblob_spath = None
                iblob_cksum = None
                if self.injected_blob_spath:
                    # We have an injected dump blob. We had better be processing
                    # the volume that this dump blob is for, otherwise something is
                    # screwy.
                    if row['name'] != self.volume:
                        raise err.InternalError("vl_entry name (%s) != brun.volume (%s)" % (
                                                row['name'], self.volume))
                    iblob_storid = self.injected_blob_storid
                    iblob_spath = self.injected_blob_spath
                    iblob_cksum = self.injected_blob_checksum

                DumpJob.create(cell=self.cell,
                               vl_id=row['id'],
                               server_id=rwsite['server_id'],
                               partition=rwsite['partition'],
                               injected_blob_storid=iblob_storid,
                               injected_blob_spath=iblob_spath,
                               injected_blob_checksum=iblob_cksum,
                               state='CLONE_PENDING',
                               state_descr="Waiting for clone to be scheduled")

                tx.entry_done()

        self.update(state='BRUN_CLCREATE_DONE', state_descr="Dump jobs created")

        fabs.server.made_progress()

    def sched_clones(self):
        self.update(state_descr="Scheduling volume clones")

        done = self._sched(descr="clone",
                           pend_state='CLONE_PENDING',
                           start_state='CLONE_READY')

        if done:
            self.update(state='BRUN_CLSCHED_DONE',
                        state_descr="Done running volume clones")
            fabs.server.made_progress()

        else:
            self.update(state='BRUN_CLSCHED_START',
                        state_descr="Continuing to schedule volume clones")

    def create_dumps(self):
        self.update(state_descr="Creating dump jobs")

        DumpJob.update_all(self,
                           from_state='CLONE_DONE', to_state='DUMP_PENDING',
                           timeout=0,
                           state_descr="Waiting for dump to be scheduled")

        self.update(state='BRUN_DUMPCREATE_DONE', state_descr="Dump jobs created")
        fabs.server.made_progress()

    def sched_dumps(self):
        self.update(state_descr="Scheduling volume dumps")

        done = self._sched(descr="dump",
                           pend_state='DUMP_PENDING',
                           start_state='DUMP_READY')

        if done:
            self.update(state='BRUN_DUMPSCHED_DONE',
                        state_descr="Done running volume dumps",
                        end=int(time.time()))
            fabs.server.made_progress()

        else:
            self.update(state='BRUN_DUMPSCHED_START',
                        state_descr="Continuing to run volume dumps")

    def _sched(self, descr, pend_state, start_state):
        """
        Schedule tasks for this brun (e.g. cloning volumes, dumping volumes, etc).

        Returns:
            bool: True if we are done scheduling the relevant tasks; there's no
            more cloning/dumping to do. False if there are still jobs running
            or waiting to be scheduled.
        """

        # See how many jobs are scheduled or running
        counts = DumpJob.get_counts(self, running=True)

        if not counts.total_quota_ok():
            # If we are at our total max quota, don't even try processing anything
            log.d("Skipping scheduling %s jobs, total quota at max" % descr)
            return False

        # Have we scheduled at least one new job?
        did_schedule = False

        with db.connect_bulk() as tx:
            for job in DumpJob.finditer(self, state=pend_state):
                if not counts.quota_ok(job):
                    # Skip this job
                    continue

                log.d("Scheduling %s job for vl_id %d, server_id %s partition %s" % (
                      descr, job.vl_id, job.server_id, job.partition))
                job.update(state=start_state, state_descr="About to start %s" % descr,
                           running=True)
                tx.entry_done()

                did_schedule = True

                if not counts.total_quota_ok():
                    # We've hit our total quota, so we can't schedule any more jobs
                    log.d("Not scheduling any more %s jobs; total quota full" % descr)
                    break

        if counts.total_count == 0:
            # We didn't find any NEW jobs, or any jobs that are in progress.
            # Check once more in the db just to be certain...
            pend_counts = DumpJob.get_counts(self, state=pend_state)
            run_counts = DumpJob.get_counts(self, running=True)
            if pend_counts.total_count == 0 and run_counts.total_count == 0:
                # Indeed, we cannot find any relevant jobs to run or that are
                # running. So, we are done with this stage of jobs
                # into the next state
                return True

        if did_schedule:
            log.d("made_progress in _sched")
            fabs.server.made_progress()

        # We either have more pending jobs that are waiting to be scheduled, or
        # there are some jobs still running. Either way, we are not done yet.
        return False

    def clean_cruft(self, active=False):
        """
        Clean database cruft for this brun.

        Args:
            active (bool, optional): If True, we're running this while running
                this backup run. If False (the default), make sure this brun
                isn't active, and throw an error if it is.

        Returns:
            dict: A dict indicating how many items were deleted while cleaning
            this brun. For example, {'dumpjobs': 5, 'vlentries': 3} means that
            we deleted 5 dumpjobs and 3 vlentries.

        Raises:
            `fabs.err.BrunActiveError`: This brun is actively running, and
            `active` is not True.

        """
        if not active and self.state not in ('DONE','FAILED'):
            raise err.BrunActiveError("Cannot clean active brun %d (state %s)" % (
                                      self.id, self.state))

        log.d("Cleaning cruft for brun %d" % self.id)

        ret = {}

        n_dumpjobs = db.dumpjob.clear_jobs(self)
        if n_dumpjobs > 0:
            ret['dumpjobs'] = n_dumpjobs

        n_vlentries = db.vlentry.delete_cruft(self.id)
        if n_vlentries > 0:
            ret['vlentries'] = n_vlentries

        if self.is_empty():
            self.delete()
            ret['brun'] = 1

        return ret

find = BackupRun.find
status = BackupRun.status
status_str = BackupRun.status_str
cleanable_bruns = BackupRun.cleanable_bruns

def backup_now(note, volume=None, volume_blob=None):
    return BackupRun.create(config.get('afs/cell'), note, volume, volume_blob)

def kill(brid, note):
    brun = BackupRun.find(id=brid)[0]
    brun.kill(note)

def retry(brid):
    brun = BackupRun.find(id=brid)[0]
    brun.retry()
