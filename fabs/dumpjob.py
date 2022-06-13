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

import contextlib
import os
import os.path
import time
import hashlib

import fabs.server
import fabs.err as err
import fabs.voldump as voldump
import fabs.bstore as bstore
import fabs.util as util
import fabs.db as db
import fabs.config as config
import fabs.log
log = fabs.log.getLogger(__name__)

class DumpJob:
    """
    A DumpJob represents a task that handles dumping a volume to disk.

    Don't instantiate a DumpJob directly. Instead, create a new job with
    `create`, or find existing jobs with `find`.
    """

    # If a volume appears to have changed less than FUDGE_TIME seconds since
    # before the last backup, we assume that backup didn't catch it, so we
    # assume the volume has changed, even though it looks like it hasn't
    # changed since the last backup. We do this just to try to be on the safe
    # side.
    FUDGE_TIME = 3

    @classmethod
    def finditer(cls, brun, **kwargs):
        """
        Find dump jobs for the given brun and given criteria.

        Args:
            `fabs.brun.BackupRun`: The backup run to search in.
            **kwargs: Search criteria. See `db.dumpjob.finditer`.

        Yields:
            `DumpJob`: Dump jobs that match the given criteria.
        """
        for row in db.dumpjob.finditer(brun, **kwargs):
            yield DumpJob(row)

    @classmethod
    def find(cls, **kwargs):
        """
        Find dump jobs that match the given criteria.

        Args:
            **kwargs: Search criteria. See `db.dumpjob.find`.

        Returns:
            list of `DumpJob`: Matching dump jobs.
        """
        rows = db.dumpjob.find(**kwargs)
        return [DumpJob(row) for row in rows]

    @classmethod
    def describe(cls, brun, **kwargs):
        jobinfos = db.dumpjob.describe(brun, **kwargs)
        return jobinfos

    @classmethod
    def get_counts(cls, brun, **kwargs):
        """
        Get how many dump jobs match the given criteria.

        Args:
            `fabs.brun.BackupRun`: Backup run to search in.
            **kwargs: Search criteria. See `db.dumpjob.get_counts`.

        Returns:
            `QuotaCounts`: The counts for the matching jobs (contains a total
            count, per-server, and per-partition counts).
        """

        part_counts = db.dumpjob.get_counts(brun, **kwargs)

        srv_counts = {}
        total_count = 0

        for ((srv, _), count) in part_counts.items():
            srv_counts[srv] = srv_counts.get(srv, 0) + count
            total_count += srv_counts[srv]

        return QuotaCounts(total_count, srv_counts, part_counts)

    @classmethod
    def clear(cls, brun):
        """
        Delete all dumpjobs for the given brun.
        """
        db.dumpjob.clear(brun)

    @classmethod
    def create(cls, *args, **kwargs):
        db.dumpjob.create(*args, **kwargs)

    @classmethod
    def find_update(cls, cell, from_state, to_state, state_descr, **kwargs):
        """
        Transition any matching dump jobs in `from_state` to `to_state`, and
        set their status description to `state_descr`, andupdate any fields in
        **kwargs.
        """
        update = dict(state=to_state)
        if state_descr is not None:
            update['state_descr'] = state_descr
        update.update(kwargs)

        db.dumpjob.update_all(match=dict(cell=cell,
                                         state=from_state),
                              update=update)

        return cls.find(cell=cell, state=to_state)

    @classmethod
    def update_all(cls, brun, from_state, to_state, **kwargs):
        db.dumpjob.update_all(match=dict(br_id=brun.id,
                                         state=from_state),
                              update=dict(state=to_state,
                                          **kwargs))

    @classmethod
    def process_jobs(cls):
        cell = config.get('afs/cell')

        # Create our backup clone
        cls._process(cell, 'CLONE_READY', 'CLONE_START', 'CLONE_WORK',
                     cls.do_clone, "Starting volume clone")

        # Do our 'vos dump'
        cls._process(cell, 'DUMP_READY', 'DUMP_START', 'DUMP_WORK',
                     cls.do_dump, "Starting volume dump")

        # Don't use _process for this, since we do not want to find or do
        # anything with the resultant jobs in the DONE state.
        db.dumpjob.update_all(match=dict(cell=cell,
                                         state='DUMP_DONE'),
                              update=dict(state='DONE',
                                          state_descr="Done with volume",
                                          timeout=0))

        db.dumpjob.update_all(match=dict(cell=cell,
                                         state='DUMP_SKIPPED'),
                              update=dict(state='DONE',
                                          timeout=0))

        cls._check_stale()

        # Retry previous stages that failed with an error
        cls._process(cell, 'ERROR', 'DUMP_RECOVER_START', 'DUMP_RECOVER_WORK',
                     cls.do_recover, None)

    # todo: This is very similar to the brun._process method; could be
    # consolidated...
    @classmethod
    def _process(cls, cell, from_state, to_state, work_state, func, descr):
        jobs = cls.find_update(cell, from_state, to_state, descr, timeout=0)
        log.d("Found %d jobs from %s -> %s" % (len(jobs), from_state, to_state))

        if not jobs:
            # no matching jobs; nothing to do
            return

        procs = []

        with db.connect_bulk() as tx:
            for job in jobs:
                if work_state is not None:
                    job.update(state=work_state)
                    tx.entry_done()

                if func is not None:
                    procs.append(util.FabsProcess(target=job.process_child, args=(func,)))

        for proc in procs:
            proc.start()

    @classmethod
    def _check_stale(cls):
        jobs = cls.find(stale=True)
        if not jobs:
            return

        with db.connect_bulk() as tx:
            for job in jobs:
                log.error('job_stale', ("%s has taken too long to make " +
                          "progress. It may be stuck, or exited uncleanly. " +
                          "FABS will mark this as an error and attempt to " +
                          "retry it. (%s)") % (job, job.details()))
                job.error()
                tx.entry_done()

    def __init__(self, row):
        for col in row.keys():
            setattr(self, col, row[col])

    def __str__(self):
        return "dump job vl_id %s (vol %d, %s)" % (self.vl_id, self.rwid, \
                                                   self.name)

    def details(self):
        return "state %s mtime %d timeout %d descr %s" % (self.state,
                self.mtime, self.timeout, self.state_descr)

    def update(self, **kwargs):
        db.dumpjob.update(self, **kwargs)

    def process_child(self, func):
        try:
            func(self)
        except Exception:
            log.exception('child_err',
                          "Error when processing %s" % self)
            self.error()

    def error_nonfatal(self):
        db.dumpjob.error_nonfatal(self)

    def error(self):
        """
        Flag this dumpjob as encountering an error.
        """
        if self.state in ('ERROR', 'DUMP_RECOVER_START', 'DUMP_RECOVER_WORK'):
            log.error('dump_err_meta', ("%s encountered an error " +
                      "while trying to recover from a previous error. It " +
                      "will not be retried.") % self)
            self.update(state='FAILED', running=False, timeout=0)

        else:
            db.dumpjob.error(self)

    # If the volume should be skipped, return the skip reason (a string like
    # 'UNCHANGED'). If the volume should NOT be skipped, return None.
    @classmethod
    def should_skip(cls, cell, volname, rwid=None, dumpjob=None):
        """
        Determine if the given volume should be skipped.

        Args:
            cell (str): Cell for the volume.
            volname (str): Name of the volume.
            rwid (int, optional): RW volid of the volume, if known.
            dumpjob (`DumpJob`, optional): The associated dumpjob, if any, for
                logging purposes.

        Returns:
            str or None: If the volume should be skipped, return a skip reason
            (e.g. 'UNCHANGED'). If the volume should NOT be skipped, return
            None.
        """

        vos = util.vos_unauth(cell=cell)

        if rwid is None:
            res = vos.listvldb(name=volname, noresolv=True).run()
            for entry in res.entries:
                rwid = entry.rwid

        res = vos.examine(id=rwid, noresolv=True).run()
        cur_update_time = res.headers[0].update_time

        if res.headers[0].status is fabs.cmd.VolumeHeader.offline:
            dumpjob_str = ''
            if dumpjob is not None:
                dumpjob_str = ' (%s)' % dumpjob

            if_offline = config.get('dump/if_offline')
            if if_offline == 'skip':
                log.info('offline_skip', ("Skipping offline volume %d%s. To " +
                         "change this behavior, see option dump/if_offline.") % (rwid, dumpjob_str))
                return 'OFFLINE'

            else:
                assert if_offline == 'error'
                raise err.DumpOfflineError(("Volume %d is offline, and we cannot " +
                                            "backup offline volumes. See option dump/if_offline to " +
                                            "skip offline volumes instead.%s") % (rwid, dumpjob_str))


        last_voldump = voldump.find_latest(cell=cell, rwid=rwid)
        if last_voldump is None:
            log.d("No previous dump for %s, volume must be dumped" % volname)
            return None

        if last_voldump.hdr_update != cur_update_time:
            # The volume has clearly changed, so we must dump it
            log.d("Volume %s has obviously changed (%d != %d)" % (
                  volname, last_voldump.hdr_update, cur_update_time))
            return None

        # The time that the backup volume was created is recorded in both
        # the 'creation' and 'backup' volume header fields. Use the oldest one
        # of those, just in case they are different for any reason.
        backup_ran = min(last_voldump.hdr_backup, last_voldump.hdr_creation)

        if backup_ran < last_voldump.hdr_update:
            # According to these timestamps, the last backup was run before the
            # last update was made to the volume. That doesn't make any sense;
            # err on the side of backing up too much.

            log.warn('lastdump_time', ("Volume %s (voldump %d) claims its backup " +
                     "clone was created around timestamp %d (backup %d, " +
                     "creation %d), but was last updated around timestamp " +
                     "%d. That does not make much sense, so we are assuming " +
                     "this volume needs to be backed up.") % (
                     volname, last_voldump.id, backup_ran, last_voldump.hdr_backup,
                     last_voldump.hdr_creation, last_voldump.hdr_update))
            return None

        if backup_ran - last_voldump.hdr_update < cls.FUDGE_TIME:
            log.d(("volume %s appears to not have changed, but its " +
                   "hdr_update (%d) is close enough to the backup time " +
                   "(%d) that we're backing it up anyway") % (
                   volname, last_voldump.hdr_update, backup_ran))
            return None

        max_age = config.get('dump/max_unchanged_age')
        if max_age:
            curtime = int(time.time())
            age = curtime - backup_ran
            if backup_ran < curtime and age > max_age:
                # Force a new dump if the last dump for this vol is more than
                # max_unchanged_age seconds old
                log.d(("The last backup for volume %s is %d seconds old, " +
                       "which is greater than dump/max_unchanged_age (%d). " +
                       "forcing a new dump.") % (volname, age,
                       max_age))
                return None

        # The volume "Last Update" time has not changed since the last backup
        # run, and the "Last Update" time is far away from the last time a
        # backup was created. We can pretty confidently say that the volume
        # has not changed, so we do not need to back it up again.
        log.d("Volume %s appears to not have changed since last backup" % volname)
        return 'UNCHANGED'

    def do_clone(self):
        vlentry = db.vlentry.get(self.vl_id)

        vos = util.vos_unauth(cell=self.cell)

        self.update(state_descr="Checking if volume has changed since last backup")

        skip_reason = self.should_skip(self.cell, vlentry['name'], vlentry['rwid'])
        if skip_reason is None:
            self.update(state_descr="Cloning volume %s" % vlentry['name'])

            vos = util.vos_auth(cell=self.cell)
            cursor = vos.backup(id=vlentry['name'])

            log.d("Running vos backup for %s in pid %d" % (
                  vlentry['name'], cursor.get_proc().pid))

            cursor.run()

            log.d("Looking up new vldb info for volume")

            res = vos.listvldb(name=vlentry['name']).run()
            db.vlentry.update(self.vl_id, res.entries[0])

            self.update(state='CLONE_DONE',
                        state_descr="Done cloning volume",
                        running=False,
                        timeout=0)

        else:
            self.update(state='DUMP_SKIPPED',
                        state_descr="Skipping backup because volume is '%s'" % skip_reason,
                        running=False,
                        skip_reason=skip_reason,
                        timeout=0)

        fabs.server.made_progress()

    def _header_match(self, volname, vos_header, dump_path):
        """
        Returns whether the header in the given dump file matches the header of
        the live volume.
        """
        dumpscan = util.dumpscan()

        self.update(state_descr="Running dumpscan to examine vol header")

        file_header = dumpscan.scan_volheader(dump_path).run()

        # Compare these attributes between the 'vos' header, and the header
        # from the on-disk header that we just got from dumpscan.
        for attr in ('rwid', 'update_time'):
            vos_val = getattr(vos_header, attr)
            file_val = getattr(file_header, attr)

            if vos_val != file_val:
                log.warn('hdr_mismatch', ("'vos examine' said that the value " +
                         "for '%s' for volume %s is %r, but the value in the " +
                         "dump blob we just got was %r") % (attr, volname,
                         vos_val, file_val))
                return False

        return True

    def injected_blob(self):
        return bstore.blob_from_db(self.injected_blob_storid,
                                   self.injected_blob_spath,
                                   self.injected_blob_checksum)

    def do_dump(self):
        # Dummy class for a special exception case below
        class SkipDump(BaseException): pass

        vlentry = db.vlentry.get(self.vl_id)
        vos = util.vos_auth(cell=self.cell)

        self.update(state_descr="Examining volid %s" % vlentry['bkid'])

        res = vos.examine(id=vlentry['bkid']).run()
        header = res.headers[0]

        self.update(state_descr="Dumping volume %s" % vlentry['name'])

        proc = None
        dblob = None
        skipped = False

        try:
            injected_blob = self.injected_blob()
            if injected_blob:
                # We already have a blob to use
                log.d(("Skipping vos dump for vol %s, since we were given a " +
                       "dump to inject") % vlentry['name'])
                dblob = injected_blob
                dblob.set_tmp()

            else:
                # We don't have a blob; get one from 'vos dump'

                self.update(state_descr="Getting volume %s size" % vlentry['name'])

                dump_size = vos.size(id=vlentry['bkid'], dump=True).run()

                self.update(state_descr="About to alloc new blob for volume dump")
                dblob = bstore.BlobStore.alloc_blob(dump_size,
                                                    "vosdump_%d_%d" % (vlentry['rwid'], self.vl_id),
                                                    update=self.update)

                cursor = vos.dump(id=vlentry['bkid'])
                proc = cursor.get_proc()

                log.d("Running vos dump %d in pid %d" % (vlentry['bkid'], proc.pid))

                self._handle_vosdump(cursor, dump_size, dblob)

                proc = None

            # Check if the header.update_time et al match what's in the actual
            # dump blob we have
            if not self._header_match(vlentry['name'], header, dblob.abs_path()):
                if self.injected_blob():
                    # For injected dumps, a header mismatch is okay; we just
                    # ignore the dump
                    log.info('inject_stale', ("The injected dump for volume " +
                             "%s appears to not match the live volume. " +
                             "Ignoring the injected dump.") % vlentry['name'])
                    raise SkipDump()

                raise err.DumpRaceError(("Dump blob header does not match " +
                                         "'vos examine' info for volume %s (%s)") % (
                                         vlentry['bkid'], self))

            # Store symlinks in the db
            self._handle_links(dblob)

            # Calculate our checksum
            self.update(state_descr="Verifying checksum")
            dblob.verify_checksum()

            self.update(state_descr="Committing completed dump")

            # Get the real dump file size on disk
            dump_size = os.path.getsize(dblob.abs_path())

            # Move the dump blob to a permanent, non-tmp path
            dblob.commit(self, vlentry['rwid'])

            db.voldump.create(vl_id=self.vl_id,
                              hdr_size=header.size,
                              hdr_creation=header.create_time,
                              hdr_copy=header.copy_time,
                              hdr_backup=header.backup_time,
                              hdr_update=header.update_time,
                              dump_size=dump_size,
                              dump_storid=dblob.storid(),
                              dump_spath=dblob.rel_path(),
                              dump_checksum=dblob.checksum())

        except SkipDump:
            skipped = True

        except Exception as exc:
            try:
                if dblob:
                    dblob.cleanup()

                # Try to kill the underlying process, before we bail out
                if proc and proc.poll() != None:
                    # Ignore ESRCH errors; that'll just happen sometimes, if
                    # the process dies as we're trying to kill it.
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()

            except Exception:
                # Log any errors here to not ignore them, but still throw the
                # original 'exc' that got us here.
                log.exception('vosdump_cleanu_p_err',
                              "Error when trying to cleanup vos process after other error")
            finally:
                raise exc

        if skipped:
            # If we're skipping this volume, we're not going to be using the
            # dump blob that we have, so get rid of it.
            dblob.delete()

            self.update(state='DUMP_SKIPPED',
                        state_descr="Skipping dump of volume due to stale injected dump blob",
                        injected_blob_storid=None,
                        injected_blob_spath=None,
                        injected_blob_checksum=None,
                        running=False,
                        skip_reason='STALE_INJECT',
                        timeout=0)
        else:
            self.update(state='DUMP_DONE',
                        state_descr='Done dumping volume',
                        injected_blob_storid=None,
                        injected_blob_spath=None,
                        injected_blob_checksum=None,
                        running=False,
                        timeout=0)
        fabs.server.made_progress()

    def _handle_vosdump(self, cursor, dump_size, dblob):
        cursor.steal_stdout()

        ck_algo = config.get('dump/checksum')
        ck_m = hashlib.new(ck_algo)

        bufsize = config.get('bufsize')
        nbytes = 0
        pretty = util.PrettyBytes(dump_size)

        # Update status every 2 minutes
        update_int = 60*2

        # Fudge the initial last_update, so our first update happens in about
        # 10 seconds, but then 'update_int' seconds after that
        last_update = int(time.time()) - update_int + 10

        # 10 minute timeout
        read_timeout = 60 * 10

        with open(dblob.abs_path(), 'r+b') as dump_fh:
            while True:
                buf = cursor.read_stdout(bufsize, timeout=read_timeout)
                if buf is None:
                    # EOF
                    break

                ck_m.update(buf)
                dump_fh.write(buf)

                nbytes += len(buf)

                now = int(time.time())
                if now - last_update > update_int:
                    pretty.update_bytes(nbytes)
                    self.update(timeout=read_timeout*2,
                                state_descr="Running vos dump (%s / %s dumped, %s)" % (
                                             pretty.bytes, pretty.total, pretty.rate))
                    last_update = now

            dump_fh.flush()

            filesize = os.fstat(dump_fh.fileno()).st_size
            if filesize < nbytes:
                raise err.InternalError("Dumped file too small? %d < %d" % (
                                        filesize, nbytes))
            # Truncate the file back to size; it may be larger because of space
            # that was reserved pre-dump
            dump_fh.truncate(nbytes)

        dblob.set_checksum('%s:%s' % (ck_algo, ck_m.hexdigest()))
        self.update(state_descr="Done vos dumping volume")

    def _handle_links(self, dblob):
        self.update(state_descr="Running dumpscan to find symlinks")

        try:
            db.links.clear(self.vl_id)

            dumpscan = util.dumpscan()
            links = dumpscan.scan_symlinks(dblob.abs_path()).run()

            if links:
                with db.connect():
                    self.update(state_descr="Adding links to db")
                    db.links.add(self.vl_id, links)

        except Exception as exc:
            # Something went wrong when scanning for symlinks. What should we
            # do? Technically this is not required to backup the volume, but it
            # can confusingly break our path processing for restores. So, we
            # leave the decision to a config option.

            log.error('scan_err', "Error when scanning symlinks for %s" % self)

            if config.get('dump/scan_links/abort_on_error'):
                log.error('scan_err', ("The dump for this volume is being " +
                          "aborted. To change this behavior, see the option " +
                          "dump/scan_links/abort_on_error"))
                raise exc

            log.error('scan_err', ("Continuing to backup this volume, but " +
                      "path information for the volume will not be stored. " +
                      "To change this behavior, see the option " +
                      "dump/scan_links/abort_on_error."))
            log.exception('scan_err', "Error:")

            self.error_nonfatal()

        self.update(state_descr="Done scanning links")

    def do_recover(self):
        # e.g. If a state failed in CLONE_WORK, put it back in CLONE_START
        ret_table = dict(
            CLONE_WORK='CLONE_START',
            DUMP_WORK='DUMP_START',
        )

        failed = False

        if self.errors >= config.get('dump/error_limit'):
            log.error('dump_err_limit', ("%s has failed %d " +
                      "times, which exceeds our retry limit") % (
                      self, self.errors))
            failed = True

        elif self.state_last not in ret_table:
            log.error('dump_err_unknown', ("%s encountered " +
                      "an error in state %s, which we do not know how to " +
                      "retry (%s)") % (self, self.state_last, self.details()))
            failed = True

        if failed:
            self.update(state='FAILED', running=False, timeout=0)
        else:
            retry_state = ret_table[self.state_last]
            log.warn('dump_retry', "Retrying %s %s -> %s" % (
                     self, self.state_last, retry_state))
            self.update(state=retry_state, state_last=None, timeout=0)

        fabs.server.made_progress()

class QuotaCounts:
    """
    A QuotaCounts object contains the count of how many DumpJobs are running in
    total, for each server, and for each partition. A QuotaObject is used to
    calculate if any of these counts exceed their relevant quota (we may have a
    quota for the total number of dump jobs, as well as per-server and
    per-partition quotas).
    """
    def __init__(self, total_count, srv_counts, part_counts):
        self.total_count = total_count
        self.srv_counts = srv_counts
        self.part_counts = part_counts

    def quota_ok(self, job):
        """
        Would running the given job put us over any quota?

        Args:
            `DumpJob`: The job to check.

        Returns:
            bool: True if it's okay to run the given job. False if running the
            given job would put us over quota; we need to wait.
        """
        part_count = self.part_counts.get((job.server_id, job.partition), 0)
        if part_count >= config.get('dump/parallel/partition'):
            log.d("Partition quota maxed for %s server %d partition %s" % (
                  job, job.server_id, job.partition))
            return False

        srv_count = self.srv_counts.get(job.server_id, 0)
        if srv_count >= config.get('dump/parallel/server'):
            log.d("Server quota maxed for %s server %d partition %s" % (
                  job, job.server_id, job.partition))
            return False

        # Callers should check this on their own, to avoid checking this
        # per-job, but just in case...
        if not self.total_quota_ok():
            log.d("Total quota maxed for %s server %d partition %s" % (
                  job, job.server_id, job.partition))
            return False

        # Okay, we appear to be below all quotas. Increase our usage counts, to
        # indicate our resource usage for this job.
        self.srv_counts[job.server_id] = srv_count + 1
        self.part_counts[(job.server_id, job.partition)] = part_count + 1
        self.total_count = self.total_count + 1

        return True

    def total_quota_ok(self):
        """
        Are we over the total overall dumpjob quota?

        This is different than `quota_ok`, because if this returns True, we are
        over quota for _any_ job.

        Returns:
            bool: True if running any new job would put us over quota. False
            otherwise.
        """
        if self.total_count >= config.get('dump/parallel/total'):
            return False
        return True
