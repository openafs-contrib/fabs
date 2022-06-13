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

from collections import deque
import json
import sys
import yaml

import fabs.err as err
import fabs.util as util
import fabs.config as config
import fabs.const as const
import fabs.scripts.base as base
import fabs.db as db

import fabs.voldump
import fabs.restore
import fabs.bstore
import fabs.dumpjob
import fabs.server
import fabs.brun
import fabs.log
log = fabs.log.getLogger(__name__)

_cmdlist = []

@base.append(_cmdlist)
class VarsCmd(base.SubCommand):
    command = "vars"
    help_txt = "View fabs compile-time variables"
    # We don't need to load the config to run this command
    config = False

    def run(self, args):
        _vars = const.consts
        txt = ""
        for key in sorted(_vars):
            txt += "%s = %s\n" % (key, _vars[key])

        return ({'fabs_vars': _vars}, txt)

@base.append(_cmdlist)
class ConfigCmd(base.SubCommand):
    command = "config"
    help_txt = "Query information about the local config"

    def parse(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--check', action='store_true',
                           help="check if current config is valid")

        group.add_argument('--dump', action='store_true',
                           help="dump current config")

        group.add_argument('--dump-all', action='store_true',
                           help="dump current config, including default values")

        group.add_argument('key', nargs='?', metavar='KEY',
                           help="get the value of a specific config directive")

    def run(self, args):
        if args.check:
            config.check()
            return ({'fabs_config_check': {'ok': True}}, "Configuration is OK")

        elif args.dump:
            cfg = config.dump()
            return ({'fabs_config': cfg},
                    yaml.safe_dump(cfg, default_flow_style=False))

        elif args.dump_all:
            cfg = config.dump(include_defaults=True)
            return ({'fabs_config': cfg}, yaml.safe_dump(cfg, default_flow_style=False))

        else:
            assert args.key
            val_raw = config.get(args.key)
            val_str = str(val_raw)

            if not isinstance(val_raw, (bool, int, float, bytes, str)):
                # For 'complex' values (like lists and dicts), give back out
                # value as a JSON-encoded string for text output. Note that
                # this is also valid YAML (since JSON is a subset of YAML), and
                # so this string should be able to be used in our config file
                # directly. We just use the JSON encoder instead of the YAML
                # encoder, since it tends to produce nicer-looking, more
                # succinct results for small values like this.
                val_str = json.dumps(val_raw)

            return ({'fabs_config': {args.key: val_raw}},
                    val_str)

@base.append(_cmdlist)
class DbCleanCmd(base.SubCommand):
    command = "db-clean"
    help_text = "Cleanup dangling references in the database"

    def parse(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--brids', type=int, nargs='+', default=None,
                            help="clean just these backup runs")
        group.add_argument('--all', action='store_true',
                            help="clean everything in the db")
        group.add_argument('--recent', action='store_true',
                            help="clean recent backup runs in the db")

    def run(self, args):
        if args.brids:
            bruns = []
            for brid in args.brids:
                bruns.extend(fabs.brun.find(id=brid))
        elif args.all or args.recent:
            bruns = fabs.brun.cleanable_bruns(all_=args.all, recent=args.recent)
        else:
            raise ValueError("Internal error: no brids, but no --all")

        ret = []

        for brun in bruns:
            info = brun.clean_cruft()
            if args.format == 'txt':
                for key,val in info.items():
                    print("Cleaned up %d %s from backup run %d" % (
                          val, key, brun.id))
            else:
                ret.append({
                    'brid': brun.id,
                    'cleaned': info,
                })

        return ({'fabs_db_clean': ret}, None)

@base.append(_cmdlist)
class DbInitCmd(base.SubCommand):
    command = "db-init"
    help_txt = "Generate/execute the relevant SQL to initialize the database"

    def parse(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--exec', action='store_true', dest='do_exec',
                           help="execute the relevant SQL directly, instead of printing it")
        group.add_argument('--sql', action='store_true',
                           help="print the SQL statements needed to create the db")

        parser.add_argument('--force', action='store_true',
                            help="force creating the db, deleting existing tables as needed")

        parser.add_argument('--db-version', default=None, type=int,
                            help="db version to init (for testing)")

    def run_txt(self, args):
        if args.db_version is not None:
            db.set_dbvers(args.db_version)
        if args.do_exec:
            if not args.force:
                if not db.can_create():
                    raise err.ConfigError("Database already exists. Use " +
                                          "--force to overwrite the db.")

            print("Initializing db in %s..." % db.printable_url())
            db.create(force=args.force)
            print("Finished creating db")

        elif args.sql:
            print("%s" % db.create_sql(force=args.force))

        else:
            raise err.InternalError("No --exec and no --sql?")

@base.append(_cmdlist)
class DbUpgradeCmd(base.SubCommand):
    command = "db-upgrade"
    help_txt = "Generate/execute the relevant SQL to upgrade the db from an older version"

    def parse(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--exec', action='store_true', dest='do_exec',
                           help="Execute the relevant SQL directly, instead of printing it")
        group.add_argument('--sql', action='store_true',
                           help="Print the SQL statements needed to upgrade the db")

        parser.add_argument('--use-builtin', default=None, choices=["auto", "always", "never"],
                            help="When to use builtin logic")

    def run(self, args):
        (needed, db_ver, code_ver) = db.upgrade_needed()

        possible = False
        if db_ver < code_ver:
            possible = True

        data = {
            'upgrade_needed': needed,
            'upgrade_possible': possible,
            'db_version': db_ver,
            'latest_version': code_ver
        }

        if args.do_exec or args.sql:
            use_builtin = {
                'auto': None,
                'always': True,
                'never': False,
            }.get(args.use_builtin)

            if not possible:
                txt = ''
                if args.sql:
                    txt += '-- '
                txt += 'Cannot upgrade, db is already at latest version %d' % code_ver

            elif args.do_exec:
                db.upgrade_exec(db_ver, code_ver, use_builtin=use_builtin)
                data['upgrade_success'] = True
                txt = 'Successfully upgraded FABS db from version %d -> %d'  % (db_ver, code_ver)

            else:
                assert args.sql
                txt = '-- FABS db upgrade from version %d -> %d\n' % (db_ver, code_ver)
                txt += db.upgrade_sql(db_ver, code_ver, use_builtin=use_builtin)
                data['upgrade_sql'] = txt

        else:
            if args.use_builtin is not None:
                raise ValueError("--use-builtin can only be used with --exec or --sql")

            if needed:
                txt = '\nDatabase MUST be upgraded (version %d -> %d)\n' % (db_ver, code_ver)
            elif possible:
                txt = '\nDatabase can be upgraded (version %d -> %d)\n' % (db_ver, code_ver)
            else:
                txt = '\nDatabase already at latest version %d\n' % code_ver

            if needed or possible:
                txt += "\n"
                txt += "See the documentation for the --exec and --sql options to perform the upgrade.\n"
                txt += "Please make a backup copy of the database before upgrading!\n"

        return ({'fabs_db_upgrade': data}, txt)

@base.append(_cmdlist)
class StorageInitCmd(base.SubCommand):
    command = "storage-init"
    help_txt = "Initialize our local storage for volume blobs"

    def parse(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--all', action='store_true',
                           help="initialize all configured storage dirs")
        group.add_argument('dir', default=None, nargs='?', metavar='DIR',
                           help="initialize only the given storage dir")

    def run(self, args):
        if args.all:
            stores = fabs.bstore.all_stores()
        else:
            stores = [fabs.bstore.BlobStore(args.dir)]

        storinfo = []
        for bstore in stores:
            if bstore.is_ok():
                # The given dir has already been initialized
                skipped = True
                if args.format == 'txt':
                    print("Storage dir %s is already initialized (uuid %s), skipping" % (
                          bstore, bstore.uuid()))
            else:
                bstore.create()
                skipped = False
                if args.format == 'txt':
                    print("Created storage dir in %s (uuid %s)" % (
                          bstore, bstore.uuid()))

            storinfo.append({'path': bstore.prefix(),
                             'uuid': bstore.uuid(),
                             'skipped': skipped})

        return ({'fabs_storage_init': storinfo}, None)

@base.append(_cmdlist)
class BackupStartCmd(base.SubCommand):
    command = "backup-start"
    help_txt = "Manually start a new backup run"

    def parse(self, parser):
        parser.add_argument('--note', default=None,
                            help="add a note or description for the created backup run")

        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--volume', default=None,
                           help="backup specific volume")
        group.add_argument('--all', action='store_true',
                           help="backup all configured volumes")

    def run(self, args):
        note = args.note
        if note is None:
            note = "Manually-issued backup run from command line"

        run = fabs.brun.backup_now(note, args.volume)

        if args.volume:
            txt = "Created new backup run %d for volume %s" % (run.id, args.volume)
        else:
            txt = "Created new backup run %d for all volumes" % run.id
        return ({'fabs_backup_start': {'brun_id': run.id}}, txt)

@base.append(_cmdlist)
class ServerCmd(base.SubCommand):
    command = "server"
    help_txt = "Run the fabs backup server"
    daemon = True

    def parse(self, parser):
        parser.add_argument('--once', action='store_true',
                            help="just run one iteration of the server, and exit")

    def post_parse(self, args):
        if args.once:
            self.daemon = False

    def run_txt(self, args):
        fabs.server.run(once=args.once)

@base.append(_cmdlist)
class BackupStatusCmd(base.SubCommand):
    command = "backup-status"
    help_txt = "View current backup status"

    def parse(self, parser):
        parser.add_argument('brid', type=int, nargs='?', default=None,
                            help="backup run id number")
        parser.add_argument('--failed', action='store_true',
                            help="show failed backup runs")

    def run(self, args):
        info = fabs.brun.status(brid=args.brid, failed=args.failed)
        txt = fabs.brun.status_str(info)

        return ({'fabs_backup_status': info}, txt)

@base.append(_cmdlist)
class RestoreStatusCmd(base.SubCommand):
    command = "restore-status"
    help_txt = "View status of active restore requests"

    def parse(self, parser):
        parser.add_argument('reqid', type=int, nargs='?', default=None,
                            help="restore request id number")
        parser.add_argument('--failed', action='store_true',
                            help="show failed restore requests")

        group = parser.add_mutually_exclusive_group()
        group.add_argument('--admin', action='store_true',
                           help="show restore requests from admins")
        group.add_argument('--user',
                           help="show restore requests from this user")

    def run(self, args):
        info = fabs.restore.status(reqid=args.reqid, failed=args.failed,
                                    admin=args.admin, user=args.user)
        txt = fabs.restore.status_str(info)

        return ({'fabs_restore_status': info}, txt)

@base.append(_cmdlist)
class RestoreStartCmd(base.SubCommand):
    command = "restore-start"
    help_txt = "Restore a volume from backup"

    def parse(self, parser):
        parser.add_argument('--dump-id', required=True, type=int,
                            help="volume dump id to restore from")

        parser.add_argument('--path',
                            default=None,
                            help="path associated with restore request")

        parser.add_argument('--note',
                            default=None,
                            help="arbitrary note to attach to restore request")

        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--admin', action='store_true',
                           help="bypass authorization checks")
        group.add_argument('--user',
                           help="check authz using this username")

    def run(self, args):
        if args.admin:
            user = None
        else:
            if args.user is None:
                raise err.InternalError("Got None for non-admin user")
            user = args.user

        note = args.note
        if note is None:
            note = "Manually-issued restore request from command line"

        rreq = fabs.restore.create_rreq(note=note, vdid=args.dump_id, user=user,
                                         path_full=args.path)

        return ({'fabs_restore_start': {'rreq': rreq.as_dict()}},
                "Created new restore request id %d" % rreq.id)

class _DumpLister(base.SubCommand):
    can_list_all = False

    def _search_args(self, parser):
        parser.add_argument('--verbose', action='store_true')

        parser.add_argument('id', type=int, nargs='*', default=None,
                            help='Dump ids')

        if self.can_list_all:
            parser.add_argument('--all', action='store_true',
                                help='List all dumps')

        parser.add_argument('--brid', type=int,
                            help="Dumps must be from this backup run ID")

        vol_g = parser.add_mutually_exclusive_group()
        vol_g.add_argument('--volid', type=int,
                           help="Dumps must be for this RW volume id")
        vol_g.add_argument('--volume',
                           help="Dumps must be for this volume (resolved to an RW volid)")

        parser.add_argument('--before', type=util.time_str2unix,
                            help="Dumps must be from before this time")
        parser.add_argument('--after', type=util.time_str2unix,
                            help="Dumps must be from after this time")

        parser.add_argument('--redundant', nargs='?', type=int,
                            const=1, metavar='N',
                            help="Dumps must be redundant, volume must have at least N (default 1) newer dumps")

        parser.add_argument('-n', type=int, default=0,
                            help="Only include up to N dumps (or 0 for no limit)")

    def _search_dumps(self, args):
        rwid = args.volid
        if rwid is None:
            rwid = self._get_rwid(args.volume)

        crits = []
        if args.brid is not None:
            crits.append("for backup run %d" % args.brid)
        if rwid is not None:
            crits.append("for volume %d" % rwid)
        if args.before is not None:
            crits.append("before time %d" % args.before)
        if args.after is not None:
            crits.append("after time %d" % args.after)
        if args.redundant is not None:
            crits.append("that are n=%d redundant" % args.redundant)

        crit_given = False
        if crits or args.n:
            crit_given = True
        if self.can_list_all and args.all:
            crit_given = True

        if not args.id and not crit_given:
            if self.can_list_all:
                raise err.ArgumentError("You must specify a dump id, search criteria, or --all")
            raise err.ArgumentError("You must specify a dump id or search criteria")
        if args.id and crit_given:
            raise err.ArgumentError("Cannot specify dump ids and search criteria")

        if args.id:
            for vdid in args.id:
                yield from fabs.voldump.find(id=vdid)
            return

        max_str = ""
        if args.n:
            max_str = " (max %d)" % args.n

        crit_str = ''
        if crits:
            crit_str = ' ' + util.list2str(crits)

        self.verbose(args, "Searching for volume dumps%s%s" % (
                     crit_str, max_str))

        yield from fabs.voldump.search(cell=config.get('afs/cell'),
                                       brid=args.brid, rwid=rwid,
                                       before_ts=args.before,
                                       after_ts=args.after,
                                       redundant=args.redundant, n_max=args.n)

    def _get_rwid(self, volstr):
        if volstr is None:
            return None

        vos = util.vos_unauth(cell=config.get('afs/cell'))
        try:
            res = vos.listvldb(name=volstr).run()
            return res.entries[0].rwid
        except fabs.cmd.ProcessError as exc:
            if volstr.isdecimal():
                log.warn('dumpfind_novol', ("Error when looking up volume %s; " +
                         "assuming it is a numeric RW volume id. Lookup error: %s") % (
                         volstr, exc))
                return int(volstr)
            raise exc

    def _vdinfo_list(self, voldumps):
        for vd in voldumps:
            yield vd.describe_dict()

    def _show_voldumps(self, jsonkey, args, voldumps):
        if args.format == 'txt':
            found = False
            for vd in voldumps:
                print("%s" % vd.describe())
                found = True
            if not found:
                print("Found 0 matching volume dumps")
            return (None, None)

        else:
            vdlist = util.json_generator(self._vdinfo_list(voldumps))
            return ({jsonkey: {'dumps': vdlist}}, None)

@base.append(_cmdlist)
class DumpFindCmd(_DumpLister):
    command = "dump-find"
    help_text = "List and search for dumps of volumes"

    def parse(self, parser):
        def convert_time(timestr):
            if timestr.isdecimal():
                # As a special case for dump-find for backwards compatibility,
                # assume a string of just numbers is a unix timestamp.
                return int(timestr)
            return util.time_str2unix(timestr)

        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--path',
                           help="list backups containing this /afs path")
        group.add_argument('--volume',
                           help="list backups containing this volume")

        parser.add_argument('--near', type=convert_time,
                            help="list backups near this time")
        parser.add_argument('-n', type=int, metavar='N', default=3,
                            help="list N dumps")

        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--admin', action='store_true',
                           help="bypass authorization checks")
        group.add_argument('--user',
                           help="check authz using this username")

    def run(self, args):
        if args.admin:
            user = None
        else:
            if args.user is None:
                raise err.InternalError("Got None for non-admin user")
            user = args.user

        rwid = self._get_rwid(args.volume)

        # Lookup the given volume, if we have one
        voldumps = fabs.voldump.search_user(cell=config.get('afs/cell'),
                                            user=user,
                                            path=args.path,
                                            vol_rwid=rwid,
                                            timestamp=args.near,
                                            max_dumps=args.n)

        return self._show_voldumps('fabs_dump_find', args, voldumps)

@base.append(_cmdlist)
class DumpList(_DumpLister):
    command = "dump-list"
    help_text = "List volume dumps"
    can_list_all = True

    def parse(self, parser):
        self._search_args(parser)

    def run(self, args):
        voldumps = self._search_dumps(args)

        return self._show_voldumps('fabs_dump_list', args, voldumps)

@base.append(_cmdlist)
class DumpDelete(_DumpLister):
    command = "dump-delete"
    help_text = "Delete volume dumps"

    def parse(self, parser):
        self._search_args(parser)
        parser.add_argument('--keep-blob', action='store_true', default=False,
                            help='do not delete dump blob files on disk, just delete db records')

    def _cleanup_brids(self, args, brids):
        for brid in brids:
            brun = fabs.brun.find(id=brid)[0]
            if brun.is_empty():
                brinfo = brun.describe_dict()
                if args.format == 'txt':
                    print("Deleting %s" % brun.describe(from_dict=brinfo))
                yield {'backup_run': brinfo}
                brun.delete()

    def _del_dumps(self, args, voldumps):
        brids = set()

        # Force our dumps into a list (well, a deque), to avoid iterating
        # over db results while we're deleting things from the db. Use a deque
        # instead of a list so we can pop from the 'left' side of the list, so
        # we can reduce the number of items we need to keep in memory as we go
        # along.
        dumplist = deque(voldumps)

        while len(dumplist) > 0:
            vd = dumplist.popleft()

            val = {}
            val['dump'] = vd.describe_dict()

            exc = None
            try:
                if args.format == 'txt':
                    print("Deleting %s" % vd.describe())
                brids.add(vd.br_id)
                vd.delete(keep_blob=args.keep_blob)
            except Exception as e:
                exc = e

            if exc is not None:
                val['error_trace'], val['error'] = util.format_exc(exc)
                print("Error deleting dump %d:" % vd.id, file=sys.stderr)
                self.verbose(args, val['error_trace'] + '\n')
                print(val['error'], file=sys.stderr)
            yield val

            if len(brids) > config.get('db/batch_size'):
                yield from self._cleanup_brids(args, brids)
                brids.clear()
        yield from self._cleanup_brids(args, brids)

    def run(self, args):
        n_success = 0
        n_error = 0

        voldumps = self._search_dumps(args)

        if args.format == 'txt':
            for delinfo in self._del_dumps(args, voldumps):
                if 'dump' not in delinfo:
                    continue
                if 'error' in delinfo:
                    n_error += 1
                else:
                    n_success += 1

            if n_success == 1:
                dumpstr = "1 dump"
            else:
                dumpstr = "%d dumps" % n_success
            print("Successfully deleted %s" % dumpstr)

            if n_error == 0:
                retcode = 0
            else:
                retcode = 1
            return (None, None, retcode)

        else:
            del_infos = util.json_generator(self._del_dumps(args, voldumps))
            return ({'fabs_dump_delete': del_infos}, None)

@base.append(_cmdlist)
class BackupKillCmd(base.SubCommand):
    command = "backup-kill"
    help_txt = "Forcibly stop a backup run from running"

    def parse(self, parser):
        parser.add_argument('brid', type=int,
                            help="backup run id number")
        parser.add_argument('--note',
                            default="Forcibly killed via command line",
                            help="give a note to describe why the backup run must die")

    def run(self, args):
        fabs.brun.kill(args.brid, args.note)
        return ({'fabs_backup_kill': {'brid_killed': args.brid}},
                "Backup run %d killed" % args.brid)

@base.append(_cmdlist)
class RestoreKillCmd(base.SubCommand):
    command = "restore-kill"
    help_txt = "Forcibly kill a restore request"

    def parse(self, parser):
        parser.add_argument('reqid', type=int,
                            help="restore request id number")
        parser.add_argument('--note',
                            default="Forcibly killed via command line",
                            help="give a note to describe why the restore request must die")

    def run(self, args):
        fabs.restore.kill(args.reqid, args.note)
        return ({'fabs_restore_kill': {'reqid_killed': args.reqid}},
                "Restore request %d killed" % args.reqid)

@base.append(_cmdlist)
class BackupRetryCmd(base.SubCommand):
    command = "backup-retry"
    help_txt = "Retry a failed backup run"

    def parse(self, parser):
        parser.add_argument('brid', type=int,
                            help="backup run id number")

    def run(self, args):
        fabs.brun.retry(args.brid)
        return ({'fabs_backup_retry': {'brid_retried': args.brid}},
                "Backup run %d has been scheduled to be retried" % args.brid)

@base.append(_cmdlist)
class RestoreRetryCmd(base.SubCommand):
    command = "restore-retry"
    help_txt = "Retry a failed restore request"

    def parse(self, parser):
        parser.add_argument('reqid', type=int,
                            help="restore request id number")

    def run(self, args):
        fabs.restore.retry(args.reqid)
        return ({'fabs_restore_retry': {'reqid_retried': args.reqid}},
                "Restore request %d has been scheduled to be retried" % args.reqid)

@base.append(_cmdlist)
class BackupNeeded(base.SubCommand):
    command = "backup-needed"
    help_text = ("Check if a volume would be dumped (and not skipped) during " +
                 "a backup run")

    def parse(self, parser):
        parser.add_argument('volume',
                            help="the volume name to check")

    def run(self, args):
        cell = config.get('afs/cell')
        skip_reason = fabs.dumpjob.DumpJob.should_skip(cell, args.volume)

        skip = True
        if skip_reason is None:
            skip = False
            txt = ("Our backups of volume %s are out of date, and a new " +
                   "backup is needed") % args.volume

        elif skip_reason == 'UNCHANGED':
            txt = "Our backups of %s are up to date" % args.volume

        elif skip_reason == 'OFFLINE':
            txt = ("Volume %s is offline, and fabs is configured to skip " +
                   "offline volumes. (See config directive dump/if_offline " +
                   "to change this behavior)") % args.volume

        else:
            txt = ("Backup of volume %s would be skipped (unknown skip " +
                   "reason '%s')") % (args.volume, skip_reason)

        return ({'fabs_backup_needed': {'volume': args.volume,
                                        'skip': skip,
                                        'skip_reason': skip_reason}},
                txt)

@base.append(_cmdlist)
class BackupInject(base.SubCommand):
    command = "backup-inject"
    help_text = "Inject a backup dump blob into the backup database"

    def parse(self, parser):
        parser.add_argument('volume',
                            help="name of the volume we're injecting a dump for")
        parser.add_argument('filename',
                            help="path to the dump blob for the given volume")
        parser.add_argument('--note', default=None,
                            help="add a note or description for the created backup run")

    def run(self, args):
        note = args.note
        if note is None:
            note = "backup-inject backup run from command line"

        # Copy the given filename into a temporary file in bstore
        if args.format == 'txt':
            print("Copying %s to staging area..." % (args.filename))
        dblob = fabs.bstore.blob_from_user(args.filename)

        try:
            if args.format == 'txt':
                print("Scheduling single-volume backup run...")

            run = fabs.brun.backup_now(note, args.volume, volume_blob=dblob)

        except Exception:
            dblob.delete()
            raise

        return ({'fabs_backup_inject': {'brun_id': run.id,
                                         'volume': args.volume,
                                         'orig_filename': args.filename,
                                         'tmp_filename': dblob.abs_path()}},
                "Created backup run %d to inject dump %s for volume %s" % (
                    run.id, args.filename, args.volume,
                ))

@base.append(_cmdlist)
class StorageTrim(base.SubCommand):
    command = "storage-trim"
    help_text = "List trimmable files in our storage backend"

    def parse(self, parser):
        parser.add_argument('-0', dest='sep', default='\n',
                            action='store_const', const='\x00',
                            help=("filenames are terminated by a NUL " +
                                  "character instead of a newline"))

    def _get_trimmables(self, args):
        voldumps = fabs.bstore.trimmable_dumps(config.get('afs/cell'))
        for vd in voldumps:
            path = vd.dump_blob().abs_path()

            if args.format == 'txt':
                # Don't need the vdinfo dict for 'txt' output; just skip
                # calculating it.
                yield {'path': path}
            else:
                yield {'path': path,
                       'dump': vd.describe_dict()}

    def run(self, args):
        trim_items = self._get_trimmables(args)

        if args.format == 'txt':
            for triminfo in trim_items:
                print(triminfo['path'], end=args.sep)
            return None

        return ({'fabs_storage_trim': util.json_generator(trim_items)},
                None)

class Fabsys(base.Command):
    description = "Main FABS command suite"
    subcommands = _cmdlist

def main(argv=None):
    fabsys = Fabsys()
    return fabsys.main(argv)
