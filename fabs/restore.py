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

import time
import os
import threading
import traceback

from fabs.voldump import VolDump
import fabs.server
import fabs.bstore as bstore
import fabs.util as util
import fabs.err as err
import fabs.db as db
import fabs.config as config
import fabs.log
log = fabs.log.getLogger(__name__)

class RestoreRequest:
    """
    A RestoreRequest represents a request to restore a particular volume dump.

    Do not instantiate a RestoreRequest directly; instead create one with
    `create`, or find an existing one with `find`.
    """

    @classmethod
    def create(cls, note, vdid, user, path_full):
        voldump = VolDump.find(id=vdid)[0]
        reqid = db.rreq.create(note, voldump, user, path_full)
        return cls.find(id=reqid)[0]

    @classmethod
    def find(cls, **kwargs):
        rows = db.rreq.find(**kwargs)

        # If we were given an id, that rreq should really exist.
        if 'id' in kwargs and not rows:
            raise err.InternalError("rreq id %d does not seem to exist" % kwargs['id'])

        return [RestoreRequest(row) for row in rows]

    @classmethod
    def process_jobs(cls):
        cell = config.get('afs/cell')

        # First, check that the requested user is allowed to restore this
        # volume.
        cls._process(cell, 'NEW', 'AUTHZ_START', 'AUTHZ_WORK',
                     cls.do_authz, "Checking user authorization")

        cls._process(cell, 'AUTHZ_DONE', 'VERIFY_START', 'VERIFY_WORK',
                     cls.do_verify, "Verifying dump blob")

        cls._process(cell, 'VERIFY_DONE', 'RESTORE_START', 'RESTORE_WORK',
                     cls.do_restore, "About to restore volume")

        cls._process(cell, 'RESTORE_DONE', 'REMOVE_START', 'REMOVE_WORK',
                     cls.do_remove, "Checking stage volume for removal")

        # REMOVE_DONE is the last stage; if we're in REMOVE_DONE, we don't have
        # any more work we need to do. So move the request into DONE; don't use
        # _process for this, since we don't want to find all DONE requests, we
        # don't need to spawn child processes to process the request, etc.
        cls._finish_reqs(cell, 'REMOVE_DONE')

        cls._process(cell, 'ERROR', 'RESTORE_RECOVER_START', 'RESTORE_RECOVER_WORK',
                     cls.do_recover, None)

    @classmethod
    def _finish_reqs(cls, cell, state):
        # Just transition requests into DONE; we don't need to do anything else
        # when a resetore request has finished
        for rreq in cls.find(cell=cell, state=state):
            rreq.update(state='DONE', state_descr="Done")
            log.info('req_done', "Restore request %d has finished all processing" % rreq.id)

    @classmethod
    def _process(cls, cell, from_state, to_state, start_state, func, descr):
        reqs = cls.find_update(cell, from_state, to_state, descr)
        log.d("Found %d reqs in state %s -> %s" % (len(reqs), from_state, to_state))

        if not reqs:
            # no matching reqs; nothing to do
            return

        procs = []

        with db.connect_bulk() as tx:
            for req in reqs:
                req.update(state=start_state)
                tx.entry_done()

                procs.append(util.FabsProcess(target=req.process_child, args=(func,)))

        for proc in procs:
            proc.start()

    @classmethod
    def find_update(cls, cell, from_state, to_state, descr):
        now = int(time.time())

        update = dict(state=to_state)
        if descr is not None:
            update['state_descr'] = descr

        db.rreq.update_all(match=dict(cell=cell, state=from_state, next_update_before=now),
                           update=update)

        return cls.find(cell=cell, state=to_state, next_update_before=now)

    @classmethod
    def status(cls, reqid=None, failed=False, admin=False, user=None):
        find_args = {}

        if reqid is not None:
            req_list = cls.find(id=reqid)
            find_args['id'] = reqid
        elif failed:
            find_args['state'] = 'FAILED'
        else:
            find_args['state_not'] = ['DONE', 'FAILED']

        if admin:
            find_args['username'] = None
        elif user:
            find_args['username'] = user

        req_list = cls.find(**find_args)

        infos = []
        for rreq in req_list:
            rreqinfo = {}

            for attr in rreq.attrs:
                rreqinfo[attr] = getattr(rreq, attr)

            rreqinfo['voldump'] = VolDump.find(id=rreq.vd_id)[0].as_dict()

            dump_path = None
            dblob = rreq.tmp_blob()
            if dblob:
                dump_path = dblob.abs_path()
            rreqinfo['tmp_dump_abspath'] = dump_path

            infos.append(rreqinfo)

        return {'rreq_status': infos}

    @classmethod
    def status_str(cls, info):
        info_list = info['rreq_status']

        ret = ''
        ret += "Found %d active restore request(s):\n" % len(info_list)
        for rreqinfo in info_list:
            ret += cls._rreqinfo_status(rreqinfo)
            ret += "\n"
        return ret

    @classmethod
    def _rreqinfo_status(cls, rreqinfo):
        ret = ''

        data = {}
        data.update(rreqinfo)

        if data['state_last'] is not None:
            data['state'] += "/%s" % data['state_last']

        for key in ('stage_time', 'next_update', 'mtime', 'ctime'):
            if data[key] is not None and int(data[key]) > 0:
                data[key] = time.ctime(int(data[key]))

        if data['username'] is None:
            data['username'] = "[admin]"

        for key in data.keys():
            if data[key] is None:
                data[key] = "[none]"

        for key, val in data['voldump'].items():
            data['voldump.%s' % key] = val

        # Indent our backend_req_info, in case it contains newlines
        info = data['backend_req_info']
        if info:
            lines = info.splitlines()
            info = '\n'.join([lines[0]] + ["    %s" % line for line in lines[1:]])
            data['backend_req_info'] = info

        ret += """
Restore request %(id)d [%(state)s]: %(note)s
  status.%(dv)d: %(state_descr)s
  cell: %(cell)s, volume: %(voldump.name)s (id %(voldump.rwid)d, voldump %(vd_id)d)
  errors: %(errors)d, user: %(username)s, requested path: %(user_path_full)s
  tmp dump blob: %(tmp_dump_abspath)s
  staging volume: %(stage_volume)s, mount: %(stage_path)s, time: %(stage_time)s
  next update: %(next_update)s, mtime: %(mtime)s, ctime: %(ctime)s
  backend request info: %(backend_req_info)s""" % data

        return ret

    def __init__(self, row):
        self.attrs = []
        for col in row.keys():
            self.attrs.append(col)
            setattr(self, col, row[col])

    def update(self, **kwargs):
        db.rreq.update(self, **kwargs)

    def as_dict(self):
        ret = {}
        # pylint: disable=not-an-iterable
        for col in db.model.restore_reqs.columns:
            ret[col.name] = getattr(self, col.name)
        return ret

    def process_child(self, func):
        try:
            func(self)
        except Exception:
            log.exception('rreq_child_err', "Error when processing restore request %d" % self.id)
            self.error()

    def error(self):
        if self.state in ('ERROR', 'RESTORE_RECOVER_START', 'RESTORE_RECOVER_WORK'):
            log.error('rreq_error_meta', ("Restore request %d encountered an " +
                      "error while processing another error.") % self.id)
            self.update(state='FAILED')
        else:
            db.rreq.error(self)

    def kill(self, note):
        db.rreq.kill(self, note)

    def retry(self):
        if self.state not in ('ERROR', 'FAILED'):
            raise err.RetryStateError(("Restore request %d is not in an error " +
                                       "state (it's in %s)") % (
                                       self.id, self.state))

        self.update(state='RESTORE_RECOVER_START', errors=0)

    def do_authz(self):
        voldump = VolDump.find(id=self.vd_id)[0]
        log.info('req_start', ("Starting to process restore request id " +
                 "%d, restoring volume dump %d (volume %s rwid %d)") % (self.id,
                 voldump.id, voldump.name, voldump.rwid))

        if self.username is not None:
            util.check_restore_authz(self.username, self.cell, voldump.rwid)

        self.update(state='AUTHZ_DONE', state_descr="Done checking authorization")

        fabs.server.made_progress()

    def _handle_backend_req(self, voldump):
        if self.backend_req_info is not None:
            log.d("backend_req_info is already set; don't need to run request command")
            return

        self.update(state_descr="Requesting volume blob restore")

        dblob = voldump.dump_blob()

        cmd = config.get('backend/request_cmd')
        if cmd is None:
            info = "Waiting for dump file %s to be restored" % dblob.abs_path()
        else:
            info = util.run_config_cmd(cmd, extra_env=dict(
                FABS_BACKEND_DUMP_PATH=dblob.abs_path(),
            ))

        log.info('req_missing', ("Dump blob %s for request %d is missing. " +
                 "Got the following info from the backend request command: %s") % (
                 dblob.abs_path(), self.id, info))

        self.update(backend_req_info=info)

    def do_verify(self):
        voldump = VolDump.find(id=self.vd_id)[0]

        tmp_blob = None
        if self.tmp_dump_spath:
            tmp_blob = self.tmp_blob()

        if tmp_blob and not tmp_blob.exists():
            log.warn('tmp_dump_gone',
                     ("Temporary volume blob %s mysteriously " +
                      "disappeared; trying to re-link it") % tmp_blob.abs_path())
            self.update(tmp_dump_storid=None, tmp_dump_spath=None)
            tmp_blob = None

        if not tmp_blob:
            voldump_blob = voldump.dump_blob()
            tmp_blob = voldump_blob.claim_restore(self)

            if not tmp_blob:
                log.d(("volume dump %s seems to not exist, requesting backend "+
                       "restore the file") % voldump_blob.abs_path())
                self._handle_backend_req(voldump)

                # Retry this operaton, in about backend/check_interval seconds
                self.update(state='VERIFY_START',
                            state_descr="Waiting for volume blob to be restored",
                            next_update=time.time() + config.get('backend/check_interval'))
                return

            else:
                with tmp_blob.maybe_delete():
                    self.update(tmp_dump_storid=tmp_blob.storid(),
                                tmp_dump_spath=tmp_blob.rel_path())
                    tmp_blob.no_delete()

        if not tmp_blob:
            raise err.InternalError("Still don't have a tmp_blob")

        self.update(state_descr="Verifying blob checksum")

        orig_blob = voldump.dump_blob()

        size = os.path.getsize(tmp_blob.abs_path())
        if size != voldump.dump_size:
            raise err.BadDumpError(("Volume dump blob %s (orig %s) has incorrect " +
                                    "size: %d != %d") % (
                                    tmp_blob, orig_blob,
                                    size, voldump.dump_size))

        tmp_blob.verify_checksum()

        self.update(state='VERIFY_DONE', state_descr="Done verifying volume blob")
        fabs.server.made_progress()

    def tmp_blob(self):
        return bstore.blob_from_db(self.tmp_dump_storid,
                                   self.tmp_dump_spath)

    def do_restore(self):
        if self.stage_volume is None:
            vos = util.vos_auth(cell=self.cell)

            volname = "%srreq.%d" % (config.get('stage/volume_prefix'),
                                      self.id)

            server = config.get('stage/server')
            partition = config.get('stage/partition')

            dblob = self.tmp_blob()

            try:
                cursor = vos.restore(server=server, partition=partition,
                                     name=volname, file=dblob.abs_path(),
                                     overwrite='full')
                proc = cursor.get_proc()

                log.d("Running vos restore for volume %s in pid %d" % (volname,
                                                                       proc.pid))

                self._handle_restore(cursor)

                self.update(state_descr="Done running vos restore", stage_volume=volname)

            except Exception:
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        log.warn('restore_cleanup_err', ("Cannot kill " +
                                 "running vos restore process for rreq %d: %s") % (
                                 self.id, traceback.format_exc()))

                try:
                    log.d("deleting volume %s if it exists" % volname)
                    if util.volume_exists(volname, self.cell):
                        vos.remove(id=volname).run()
                except Exception:
                    log.warn('restore_cleanup_err',
                             "Error cleaning up volume restore for rreq %d: %s" % (
                             self.id, traceback.format_exc()))

                raise

        if self.stage_volume is None:
            raise err.InternalError("Still don't have a stage_volume")

        if self.tmp_dump_spath:
            dblob = self.tmp_blob()
            try:
                dblob.delete()
            except Exception:
                log.warn('blob_cleanup_err', "Error deleting temporary dump blob %s: %s" % (
                         self.dump_dmp_path, traceback.format_exc()))

        self.update(state_descr="Running fs mkmount",
                    tmp_dump_storid=None,
                    tmp_dump_spath=None)

        stage_dir = config.get('stage/dir')
        if not os.path.isdir(stage_dir):
            raise err.StageDirError("Stage dir '%s' seems to not exist" % stage_dir)

        fs = util.fs_auth()

        stage_path = os.path.join(stage_dir, self.stage_volume)

        fs.mkmount(dir=stage_path,
                   vol=self.stage_volume,
                   cell=self.cell,
                   fast=True).run()

        now = int(time.time())

        self.update(state='RESTORE_DONE',
                    state_descr="Done mounting volume",
                    stage_path=stage_path,
                    stage_time=now)

        self._handle_notify()

        voldump = VolDump.find(id=self.vd_id)[0]
        log.info('req_staged', ("Restore request %d (volume %s) has been " +
                 "restored to %s") % (
                 self.id, voldump.name, self.stage_path))

        # Note that we don't made_progress() here, since the next state
        # transition will not happen immediately

    def _handle_restore(self, cursor):
        success = threading.Event()

        def _inthread():
            try:
                cursor.run()
                success.set()
            except Exception:
                log.exception('restore_err', ("Error when running vos " +
                              "restore for req %s") % self.id)
                raise

        th = threading.Thread(target=_inthread)
        th.start()

        while th.is_alive():
            self.update(state_descr="Running vos restore")
            # Wait 60 seconds
            th.join(60)

        if not success.is_set():
            raise err.VosRestoreError(("Some kind of error occurred while " +
                                       "running vos restore for restore req %d" % self.id))

    # Notify the requesting user that the restore has finished
    def _handle_notify(self):
        cmd = config.get('stage/notify_cmd')
        if cmd is None:
            log.d("No stage notify command configured")

        if self.user_path_partial:
            path = os.path.join(self.stage_path, self.user_path_partial)
        else:
            path = self.stage_path

        env = dict(
            FABS_STAGE_PATH=path,
            FABS_STAGE_VOLUME=self.stage_volume,
        )
        if self.username is not None:
            env['FABS_STAGE_USER'] = self.username
        if self.user_path_full is not None:
            env['FABS_REQUEST_PATH'] = self.user_path_full

        util.run_config_cmd(cmd, extra_env=env)

    def do_remove(self):
        now = int(time.time())
        remove_time = self.stage_time + config.get('stage/lifetime')

        if now < remove_time:
            interval = 3600
            lifetime = config.get('stage/lifetime')
            if lifetime < interval:
                interval = lifetime

            log.d("Waiting for about %d seconds for remove check" % interval)

            self.update(state='REMOVE_START',
                        state_descr="Waiting for stage volume to age before deleting",
                        next_update=now + interval)
            return

        if self.stage_volume and util.volume_exists(self.stage_volume, self.cell):
            # Remove the staging volume, if we have one

            vos = util.vos_auth(cell=self.cell)

            self.update(state_descr="Removing staging volume")
            try:
                vos.remove(id=self.stage_volume).run()
            except Exception:
                # If we can't remove the volume, still keep going, but log the
                # error
                raise err.VolCleanError(("Error trying to cleanup staging " +
                                         "volume %s: %s") % (
                                         self.stage_volume,
                                         traceback.format_exc())) from None

        if self.stage_path and util.path_exists(self.stage_path):
            # Remove the staging mount point, if we have one

            fs = util.fs_auth()

            self.update(state_descr="Removing staging mountpoint")
            try:
                fs.rmmount(dir=self.stage_path).run()
            except Exception:
                raise err.MtptCleanError(("Error trying to cleanup staging " +
                                          "mountpoint %s: %s") % (
                                          self.stage_path,
                                          traceback.format_exc())) from None

        update = {}
        if self.stage_volume and not util.volume_exists(self.stage_volume, self.cell):
            update['stage_volume'] = None
        if self.stage_path and not util.path_exists(self.stage_path):
            update['stage_path'] = None

        if update:
            self.update(state_descr="Removed staging volume components", **update)

        if self.stage_volume or self.stage_path:
            self.update(state='REMOVE_START',
                        state_descr="Retrying removing staging volume components")
            return

        voldump = VolDump.find(id=self.vd_id)[0]

        self.update(state='REMOVE_DONE',
                    state_descr="Done removing staging volume")

        log.info('req_removed', "Restore request %d (volume %s) staging data removed" % (
                 self.id, voldump.name))

    def do_recover(self):
        ret_table = dict(
            AUTHZ_WORK='AUTHZ_START',
            VERIFY_WORK='VERIFY_START',
            RESTORE_WORK='RESTORE_START',
            REMOVE_WORK='REMOVE_START',

            # These are states that can stay in the same state to recover. That
            # is, e.g. AUTHZ_START just transitions to AUTHZ_START to recover;
            # we don't need to go back to a different state.
            NEW=None,
            AUTHZ_START=None,
            AUTHZ_DONE=None,
            VERIFY_START=None,
            VERIFY_DONE=None,
            RESTORE_START=None,
            RESTORE_DONE=None,
            REMOVE_START=None,
            REMOVE_DONE=None,
        )

        failed = False
        if self.errors >= config.get('restore/error_limit'):
            log.error('err_limit', ("Restore req %d has failed %d times, " +
                      "which exceeds our retry limit") % (
                      self.id, self.errors))
            failed = True

        elif self.state_last not in ret_table:
            log.error('err_state', ("Restore request %d encountered an error " +
                      "in state %s, which we do not know how to retry") % (
                      self.id, self.state_last))
            failed = True

        if failed:
            self.update(state='FAILED')
        else:
            retry_state = ret_table[self.state_last]
            if retry_state is None:
                retry_state = self.state_last

            log.warn('retry', "Retrying restore req %d %s -> %s" % (
                     self.id, self.state_last, retry_state))
            self.update(state=retry_state, state_last=None)

            fabs.server.made_progress()

status = RestoreRequest.status
status_str = RestoreRequest.status_str

def create_rreq(note, vdid, user, path_full):
    return RestoreRequest.create(note, vdid, user, path_full)

def clear_voldump(voldump):
    rreqs = RestoreRequest.find(vd_id=voldump.id, state_not=['DONE','FAILED'])
    if rreqs:
        rreqinfo = []
        for r in rreqs:
            rreqinfo.append("rreq %d [%s]" % (r.id, r.state))
        rreqs_str = ', '.join(rreqinfo)

        raise err.VolDumpActiveError(
                ("Cannot delete voldump %d with active restore " +
                 "requests (kill or delete these to continue): %s") % (
                 voldump.id, rreqs_str))

    db.rreq.delete(vd_id=voldump.id, state=['DONE','FAILED'])

def kill(reqid, note):
    rreq = RestoreRequest.find(id=reqid)[0]
    rreq.kill(note)

def retry(reqid):
    rreq = RestoreRequest.find(id=reqid)[0]
    rreq.retry()
