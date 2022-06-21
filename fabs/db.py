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
import io
import time
import types
import platform
import os
import fcntl

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

import fabs.err as err
import fabs.config as config
import fabs.log
log = fabs.log.getLogger(__name__)

try:
    # pylint: disable=no-name-in-module,import-error,no-member
    import sqlalchemy.dialects.mysql.mariadb
    assert sqlalchemy.dialects.mysql.mariadb # silence pyflakes
except ImportError:
    _have_mariadb = False
else:
    _have_mariadb = True

try:
    # pylint: disable=import-error
    import alembic
except ImportError:
    _have_alembic = False
else:
    _have_alembic = True
    import alembic.migration
    import alembic.operations

# db version 3, effectively the oldest version we care about.
VERS_3 = 3
VERS_4 = 4
VERS_5 = 5

# The current version of our database schema.
VERSION = VERS_5

# List of all known versions
ALL_VERSIONS = list(range(VERS_3, VERSION+1))

# MySQL/MariaDB cannot index 'TEXT' columns, or varchar columns over a certain
# length. To work around this, we reduce the length of some columns, or reduce
# the size of some indices (so they are prefix indices), just for MySQL. The
# length we reduce to is _MYSQL_INDEX_MAXLEN.
_MYSQL_INDEX_MAXLEN = 766

# Disable the redefined-outer-name warning for the whole file. This warning
# happens because we have globals in this file like 'brun' (so other modules
# can refer to fabs.db.brun), but some functions also have args or local vars
# called 'brun' (and the same for 'rreq', 'vlentry', etc).
# pylint: disable=redefined-outer-name


# UTF8String and UTF8Text are light wrappers around the sqlalchemy 'Unicode'
# and 'UnicodeText' column types. These detect when raw bytes() are given to
# (or returned from) the database instead of proper unicode str() objects.
#
# Without these wrappers, if we give bytes() to such a column, SQLAlchemy will
# emit a warning, but will still insert the bytes() into the db. It is
# also possible get retrieve bytes() from the database (even though this is a
# unicode column!), if some bytes() managed to get into the db from an earlier
# FABS version (or due to bugs), or if someone interacted with the database
# manually.
#
# So, with these wrappers, if someone tries to store bytes() in a unicode
# column, we'll throw an error, so the bad data doesn't get in the database.
# And if we fetch data from the db that is (somehow) a bytes() object, we'll
# assume it is utf-8 and convert it into a str().
# pylint: disable=abstract-method
class UTF8Column(sa.TypeDecorator):
    def process_bind_param(self, value, dialect):
        if isinstance(value, bytes):
            raise err.InternalError("Column data must be unicode, but we got %r" % value)
        return value

    def process_result_value(self, value, dialect):
        # Even though the column is a unicode column, we can get back raw bytes
        # from the db. This can happen if someone previously inserted a raw b''
        # string (e.g. in a previous version of FABS, or manually in the db).
        # Assume the data is ascii or utf-8, so we can still work in at least
        # some situations. If the data isn't valid utf-8, we'll throw an
        # exception, but that's kind of unavoidable; the db will need some
        # manual inspection.
        if isinstance(value, bytes):
            return value.decode('utf-8')
        return value

class UTF8String(UTF8Column):
    cache_ok = True
    impl = sa.Unicode

# pylint: disable=abstract-method
class UTF8Text(UTF8Column):
    cache_ok = True
    impl = sa.UnicodeText

# Some special column types we use
class _Types:
    # For 'sqlite', a plain INTEGER is fine for a BigInteger (the size of the
    # column values are not actually restricted), so they are for the most part
    # identical. However, sqlite will not autoincrement BigInteger columns for
    # some reason; only Integer columns. So, for sqlite, use a plain integer
    # instead.
    Id = sa.BigInteger().with_variant(sqlite.INTEGER(), 'sqlite')

    # For columns representing a path (on disk or in an AFS volume).
    Path = UTF8Text()

    # For columns representing a timestamp, just use an integer representing the
    # UTC "seconds since epoch". We could use a DateTime column, but allegedly
    # some sqlalchemy engines have some issues in some cases...? Maybe in the
    # future we could use DateTime columns, but just go with ints for now.
    Timestamp = sa.BigInteger()

    # In sqlalchemy 1.4, Boolean was changed to default to
    # create_constraint=False (previously it was create_constraint=True). To
    # keep our schema the same as before, use create_constraint=True. If we add
    # new boolean columns, they could probably use create_constraint=False, but
    # we need to explicitly specify one or the other, to avoid
    # version-dependant behavior.
    CheckedBoolean = sa.Boolean(create_constraint=True)

# Simple wrapper around sa.Table. Add some default options for the table,
# versioning info, etc.
def _Table(table_vers, name, metadata, *cols):
    mariadb_opts = {
        'charset': 'utf8mb4',
        'engine': 'innodb',
        'row_format': 'dynamic',
    }

    kwargs = {}
    for key,val in mariadb_opts.items():
        kwargs['mysql_'+key] = val
        if _have_mariadb:
            kwargs['mariadb_'+key] = val

    db_vers = metadata.info['fabs_dbvers']
    if table_vers > db_vers:
        # Table is "newer" than the active db version, so the table shouldn't
        # exist. We don't actually handle this case yet, since we don't have
        # any tables that are added in newer db versions. So just throw an
        # error, as a sanity check.
        raise err.InternalError("table %s vers %s > db_vers %s" % (name,
                                table_vers, db_vers))

    return sa.Table(name, metadata, *cols, **kwargs)

# A simple wrapper around sa.Column. Just make 'nullable' default to False;
# everything else is the same
def _Column(*args, **kwargs):
    if 'nullable' not in kwargs:
        kwargs['nullable'] = False
    return sa.Column(*args, **kwargs)

# Use this instead of sa.Index to define an index involving a column whose
# length is too big for MySQL/MariaDB to use as an index. Specify which column
# name is too big as '_long_col', and that column will have an index prefix put
# on it.
def LongIndex(*args, _long_col=None, **kwargs):
    if _long_col is None:
        raise TypeError("LongIndex() missing arg 'long_col'")

    length_kwargs = {}
    length_kwargs['mysql_length'] = {_long_col: _MYSQL_INDEX_MAXLEN}
    if _have_mariadb:
        length_kwargs['mariadb_length'] = {_long_col: _MYSQL_INDEX_MAXLEN}

    return sa.Index(*args, **length_kwargs, **kwargs)

# Our data 'model' (aka schema)
class _Model:
    def __init__(self, db_vers=VERSION):
        if db_vers < VERS_3 or db_vers > VERSION:
            raise ValueError("Invalid db version given: %d (must be between %d and %d)" % (
                             db_vers, VERS_3, VERSION))

        self.metadata = sa.MetaData(info={'fabs_dbvers': db_vers})
        metadata = self.metadata

        self.versions = _Table(VERS_3, 'versions', metadata,
            _Column('version', sa.Integer),
        )

        self.bstores = _Table(VERS_3, 'blob_stores', metadata,
            _Column('id', _Types.Id, primary_key=True, autoincrement=True),
            # Store uuids as strings with the intermediate dashes, for convenience
            _Column('uuid', sa.String(37), index=True),
        )

        # A table for storing backup runs. This is either an automatic scheduled
        # backup run (e.g. run every day at 2 am), or a manual run.
        self.backup_runs = _Table(VERS_3, 'backup_runs', metadata,
            _Column('id', _Types.Id, primary_key=True, autoincrement=True),
            _Column('cell', sa.String(256)),

            _Column('note', UTF8Text()),

            # If this backup run is for backing up a single volume, that volume
            # name is stored here. For 'normal' backup runs (which backup
            # everything that's been configured to be backed up), this will just be
            # null.
            _Column('volume', sa.String(256), nullable=True),

            # If 'volume' above is non-null, volume_blob_* specifies a volume blob
            # not generated by FABS, but instead given by the user to inject into
            # FABS. The 'storid' is a reference to which storage backend the blob
            # is in, and 'spath' is the relative path inside that storage backend
            # to the blob.
            _Column('injected_blob_storid', _Types.Id, sa.ForeignKey('blob_stores.id'), nullable=True),
            _Column('injected_blob_spath', _Types.Path, nullable=True),
            _Column('injected_blob_checksum', sa.String(1024), nullable=True),

            # When this backup run was created
            _Column('start', _Types.Timestamp),

            # When this backup run finished
            _Column('end', _Types.Timestamp),
        )
        self._add_state_cols(self.backup_runs)
        self._add_indexes(self.backup_runs, VERS_5,
            ['ix_backup_runs_state', self.backup_runs.c.state],
        )

        self.vl_fileservers = _Table(VERS_3, 'vl_fileservers', metadata,
            _Column('id', _Types.Id, primary_key=True, autoincrement=True),
            _Column('br_id', _Types.Id, sa.ForeignKey('backup_runs.id')),

            # Store uuids with the intermediate dashes, for convenience
            _Column('uuid', sa.String(37), nullable=True),
        )
        self._add_indexes(self.vl_fileservers, VERS_5,
            ['ix_vl_fileservers_br_id', self.vl_fileservers.c.br_id],
        )

        self.vl_addrs = _Table(VERS_3, 'vl_addrs', metadata,
            _Column('fs_id', _Types.Id, sa.ForeignKey('vl_fileservers.id')),
            _Column('addr', sa.String(256)),
        )
        self._add_indexes(self.vl_addrs, VERS_5,
            ['ix_vl_addrs_fs_id', self.vl_addrs.c.fs_id],
        )

        # Represents a volume vlentry, as it exists at the time of the backup run
        self.vl_entries = _Table(VERS_3, 'vl_entries', metadata,
            _Column('id', _Types.Id, primary_key=True, autoincrement=True),
            _Column('br_id', _Types.Id, sa.ForeignKey('backup_runs.id')),

            _Column('name', sa.String(256)),
            _Column('rwid', sa.Integer),
            _Column('roid', sa.Integer, nullable=True),
            _Column('bkid', sa.Integer, nullable=True),

            # A volume can only exist once per run
            sa.UniqueConstraint('br_id', 'name'),
        )
        self._add_indexes(self.vl_entries, VERS_5,
            ['ix_vl_entries_br_id', self.vl_entries.c.br_id],
            ['ix_vl_entries_rwid', self.vl_entries.c.rwid],
        )

        # Stores all sites for a volume (helpful for whole-server/whole-partition
        # restores, and the like)
        self.vl_sites = _Table(VERS_3, 'vl_sites', metadata,
            _Column('vl_id', _Types.Id, sa.ForeignKey('vl_entries.id')),

            _Column('type', sa.String(2)),
            _Column('server_id', _Types.Id, sa.ForeignKey('vl_fileservers.id')),
            _Column('partition', sa.String(2)),

            # A volume can only exist once per server per type
            sa.UniqueConstraint('vl_id', 'type', 'server_id'),
        )
        self._add_indexes(self.vl_sites, VERS_5,
            ['ix_vl_sites_server_id', self.vl_sites.c.server_id],
        )

        # Info about a dump for a particular volume
        self.volume_dumps = _Table(VERS_3, 'volume_dumps', metadata,
            _Column('id', _Types.Id, primary_key=True, autoincrement=True),
            _Column('vl_id', _Types.Id, sa.ForeignKey('vl_entries.id')),

            # The 'size' field from e.g. 'vos examine'
            _Column('hdr_size', sa.BigInteger),
            _Column('hdr_creation', _Types.Timestamp),
            _Column('hdr_copy', _Types.Timestamp),
            _Column('hdr_backup', _Types.Timestamp),
            _Column('hdr_update', _Types.Timestamp),

            # Store the -time value we gave to 'vos dump' when we dumped an
            # incremental volume. Currently we do not use incremental dumps; this
            # is just for future use. Currently this is thus always 0.
            _Column('incr_timestamp', _Types.Timestamp, default='0'),

            # The size of the actual dump blob (usually different from hdr_size)
            _Column('dump_size', sa.BigInteger),

            # Path to the dump blob on disk, relative to our root storage dir
            _Column('dump_storid', _Types.Id, sa.ForeignKey('blob_stores.id'), index=True),
            _Column('dump_spath', _Types.Path),
            LongIndex('idx_dump_spath', 'dump_spath', _long_col='dump_spath'),

            # Checksum of dump blob (prefixed by algo), e.g.:
            # md5:acebbec428dc7deb3c7c099365750da8
            # This probably does not need to be a cryptographically secure hash;
            # just use something that is fast.
            _Column('dump_checksum', sa.String(1024)),
        )
        self._add_indexes(self.volume_dumps, VERS_5,
            ['ix_volume_dumps_vl_id', self.volume_dumps.c.vl_id],
            ['ix_volume_dumps_hdr_creation', self.volume_dumps.c.hdr_creation],
        )

        # Stores information about symlinks, so we can "traverse" paths and get to
        # the target volume
        self.links = _Table(VERS_3, 'links', metadata,
            _Column('vl_id', _Types.Id, sa.ForeignKey('vl_entries.id'), nullable=True),
            _Column('path', _Types.Path, nullable=True),
            _Column('target', _Types.Path, nullable=True),

            # Don't allow multiple different targets for the same path
            LongIndex('idx_vlid_path', 'vl_id', 'path', unique=True, _long_col='path'),
        )

        self._alter_col(VERS_4, self.links.c.vl_id,  nullable=False)
        self._alter_col(VERS_4, self.links.c.path,   nullable=False)
        self._alter_col(VERS_4, self.links.c.target, nullable=False)

        # Table for storing status about dump 'jobs', while we're processing a
        # dump. These get deleted after a dump is completed.
        self.dump_jobs = _Table(VERS_3, 'dump_jobs', metadata,
            _Column('vl_id', _Types.Id, sa.ForeignKey('vl_entries.id')),

            # This is duplicated in the 'backup_runs' table, but having it directly
            # in this table makes some queries a lot more convenient...
            _Column('cell', sa.String(256)),

            # Say what server we're dumping from, in case we change what server we
            # use in the future (e.g. dumping from ROs, or some other source, etc),
            # or it differs from the rw server in vl_sites for any reason. Having
            # this directly in the dump_jobs table also makes quota calculations a
            # little simpler.
            _Column('server_id', _Types.Id, sa.ForeignKey('vl_fileservers.id')),
            _Column('partition', sa.String(2)),

            # Obsolete; use the 'skip_reason' column instead (with the value
            # 'UNCHANGED').
            _Column('unchanged', _Types.CheckedBoolean, default=False),

            # Store the -time value we will give to 'vos dump' when we dump an
            # incremental volume. Currently we do not use incremental dumps; this
            # is just for future use. Currently this is thus always 0.
            _Column('incr_timestamp', _Types.Timestamp, default='0'),

            _Column('ctime', _Types.Timestamp),
            _Column('mtime', _Types.Timestamp),
            _Column('timeout', sa.Integer),

            # If set, use this volume dump blob instead of actually dumping from
            # 'vos'. Used for dump injection.
            _Column('injected_blob_storid', _Types.Id, sa.ForeignKey('blob_stores.id'), nullable=True),
            _Column('injected_blob_spath', _Types.Path, nullable=True),
            _Column('injected_blob_checksum', sa.String(1024), nullable=True),

            # Indicates whether or not this job is considered "running", for the
            # purposes of counting this job against relevant per-server/etc quotas.
            # Note that jobs in 'ERROR' are still considered running, since they
            # will be retried and so still consume a quota slot for that
            # server/partition.
            _Column('running', _Types.CheckedBoolean, default=False),

            sa.UniqueConstraint('vl_id'),
        )
        self._add_state_cols(self.dump_jobs)
        self._add_cols(self.dump_jobs, VERS_4,
            # Why did we skip dumping the volume? Possible values include:
            # - NULL/None: volume dump was _not_ skipped
            # - 'UNCHANGED': volume was unchanged since last backup
            _Column('skip_reason', sa.String(256), nullable=True),

            # In what state should the RW vol be left in? This field is currently
            # not used, but may be used in the future to support features such as
            # temporarily onlining volumes for backup.
            _Column('rw_target_status', sa.String(256), nullable=True),
        )
        self._add_indexes(self.dump_jobs, VERS_5,
            ['ix_dump_jobs_cell_state', self.dump_jobs.c.cell, self.dump_jobs.c.state],
        )

        self.restore_reqs = _Table(VERS_3, 'restore_reqs', metadata,
            _Column('id', _Types.Id, primary_key=True, autoincrement=True),
            _Column('vd_id', _Types.Id, sa.ForeignKey('volume_dumps.id')),
            _Column('note', UTF8Text()),

            # This is duplicated in the 'backup_runs' table, but having it directly
            # in this table makes some queries a lot more convenient...
            _Column('cell', sa.String(256)),

            _Column('username', UTF8String(1024), nullable=True),
            LongIndex('idx_username', 'username', _long_col='username'),

            # User requests to restore path /afs/foo/bar/baz.txt, so this is that
            # full path.
            _Column('user_path_full', _Types.Path, nullable=True),

            # ...and volume was mounted at /afs/foo, so the path inside the volume
            # is bar/baz.txt. That path relative from the volume root is stored
            # here.
            _Column('user_path_partial', _Types.Path, nullable=True),

            # Our dump blob on disk; a temporary copy while the restore is ongoing.
            _Column('tmp_dump_storid', _Types.Id, sa.ForeignKey('blob_stores.id'), nullable=True),
            _Column('tmp_dump_spath', _Types.Path, nullable=True),

            # Arbitrary info we get from the configured script for requesting a
            # volume blob be restored from tape.
            _Column('backend_req_info', UTF8Text(), nullable=True),

            _Column('stage_volume', sa.String(256), nullable=True),
            _Column('stage_path', _Types.Path, nullable=True),
            _Column('stage_time', _Types.Timestamp, nullable=True),

            # For certain stages, we want to leave a restore request alone for a
            # while (e.g. waiting for a volume dump to be restored, or waiting for
            # the user to grab staged data before deleting it). This columns says
            # we should wait until timestamp `next_update` before processing this
            # rreq again.
            _Column('next_update', _Types.Timestamp, default=0),

            _Column('ctime', _Types.Timestamp),
            _Column('mtime', _Types.Timestamp),
        )
        self._add_state_cols(self.restore_reqs)
        self._add_indexes(self.restore_reqs, VERS_5,
            ['ix_restore_reqs_vd_id', self.restore_reqs.c.vd_id],
            ['ix_restore_reqs_state', self.restore_reqs.c.state],
        )

    @classmethod
    def _add_state_cols(cls, table):
        state_cols = [
            # Current state
            _Column('state', sa.String(256)),

            # Last non-error state, when we're in an error state
            _Column('state_last', sa.String(256), nullable=True),

            # dv, incremented when the job is changed in any way
            _Column('dv', sa.Integer),

            # Human-readable description of what's going on with this job
            _Column('state_descr', UTF8Text),

            # Human-readable description of who last touched this job (hostname and pid, usually)
            _Column('state_source', UTF8Text),

            # How many times this job has failed
            _Column('errors', sa.Integer, server_default='0'),
        ]
        for col in state_cols:
            table.append_column(col)

    @classmethod
    def _add_cols(cls, table, version, *cols):
        db_vers = table.metadata.info['fabs_dbvers']
        if version > db_vers:
            # These columns don't exist for the specified db version
            return

        for col in cols:
            col.info['fabs_dbvers'] = version
            table.append_column(col)

    @classmethod
    def _alter_col(cls, version, col, **kwargs):
        db_vers = col.table.metadata.info['fabs_dbvers']
        if version > db_vers:
            # These alterations don't exist for the specified db version
            return

        col.info.setdefault('fabs_dbalter', {})

        alargs = {
            'existing_type': col.type,
            'existing_server_default': col.server_default,
            'existing_nullable': col.nullable,
        }

        for key, val in kwargs.items():
            alargs[key] = val
            setattr(col, key, val)

        col.info['fabs_dbalter'][version] = alargs

    @classmethod
    def _add_indexes(cls, table, version, *idx_specs):
        db_vers = table.metadata.info['fabs_dbvers']
        if version > db_vers:
            # These indexes don't exist for the specified db version
            return

        for spec in idx_specs:
            idx = sa.Index(*spec)
            idx.info['fabs_dbvers'] = version
            table.append_constraint(idx)

class BulkTx:
    def __init__(self, conn):
        self._conn = conn
        self._tx = None
        self._n_entries = 0
        self._limit = config.get('db/batch_size')

    def __enter__(self):
        self._tx = self._conn.begin()

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type is not None:
            self._tx.rollback()
        else:
            self._tx.commit()
        self._tx = None

    def entry_done(self):
        self._n_entries += 1
        if self._n_entries > self._limit:
            log.d("Flushing db bulk tx (%d > %d)" % (
                  self._n_entries, self._limit))
            self.flush()

    def flush(self):
        self._tx.commit()
        self._tx = self._conn.begin()
        self._n_entries = 0

class Db:
    def __init__(self, db_vers=VERSION):
        self._setup_done = False
        self._checked = False
        self._engine = None
        self._sqlite_fk = True
        self._lockfh = None
        self._dbvers = db_vers
        self.model = _Model(db_vers)
        self._engine_driver = None

        self._cur_conn = None

    def set_dbvers(self, db_vers):
        # This had better not be called after actually using the db
        assert not self._checked
        self.model = _Model(db_vers)
        self._dbvers = db_vers

    # Reset some values that should not be remembered across a fork
    def reset(self):
        log.d("resetting db")
        if self._cur_conn is not None:
            self._cur_conn.close()
            self._cur_conn = None

        if self._engine is not None:
            self._engine.dispose()

        if self._lockfh:
            self._lockfh.close()
            self._lockfh = None

        self._setup_done = False
        self._checked = False
        self._engine = None

    @contextlib.contextmanager
    def lock(self, lock=True):
        if not lock or self._lockfh or self._engine_driver != 'sqlite':
            # Noop. Either our caller requested to bypass the lock ('lock' is
            # false), or the db is already locked (_lockfh), or we're
            # non-sqlite. Don't bother locking for non-sqlite, since the db can
            # probably handle concurrent connections just fine.
            yield
            return

        with open(os.path.join(config.get('lockdir'),
                               'fabs-db.lock'), 'w+b') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self._lockfh = fh
            try:
                yield
            finally:
                self._lockfh = None

    def _raw_connect(self):
        conn = self._engine.connect()
        if conn.engine.dialect.name == 'sqlite':
            if self._sqlite_fk:
                conn.execute(sa.text('PRAGMA foreign_keys = ON;'))
            else:
                log.d("Not enforcing foreign keys")
        return conn

    # Use this like so:
    # with db.connect() as conn:
    #     conn.dostuff()
    @contextlib.contextmanager
    def connect(self, check=True, lock=True):
        self._setup()
        if check:
            if not self._checked:
                log.d("Checking database structure")
                self._check()
                self._checked = True
        else:
            log.d("Skipping database check")

        # If someone nested a call to connect(), remember the current
        # connection, and just reuse it.
        if self._cur_conn is not None:
            try:
                yield self._cur_conn
            except Exception:
                if self._cur_conn:
                    self._cur_conn.close()
                    self._cur_conn = None
                raise
            return

        with self.lock(lock=lock):
            conn = self._raw_connect()
            try:
                self._cur_conn = conn
                yield conn
            finally:
                if self._cur_conn:
                    # pylint: disable=no-member
                    self._cur_conn.close()
                    self._cur_conn = None

    @contextlib.contextmanager
    def connect_bulk(self):
        with self.connect() as conn:
            tx = BulkTx(conn)
            with tx:
                yield tx

    def printable_url(self, url=None):
        if url is None:
            url = config.get('db/url')
        uobj = sa.engine.url.make_url(url)

        # Around sqlalchemy 1.4, __to_string__ was deprecated and replaced by
        # render_as_string.
        if hasattr(uobj, 'render_as_string'):
            # pylint: disable=no-member
            return uobj.render_as_string(hide_password=True)
        else:
            return uobj.__to_string__(hide_password=True)

    # Add some new items to the query string in the given db connection url. If
    # a key already exists in the url, don't change it. Example:
    #
    # _url_update_query('engine://user:pass@host', {'charset': 'utf8mb4'})
    #
    # returns: 'engine://user:pass@host?charset=utf8mb4'
    @staticmethod
    def _url_update_query(url, query_dict):
        url_obj = sa.engine.url.make_url(url)
        new_items = []

        for key, val in query_dict.items():
            if key not in url_obj.query:
                new_items.append("%s=%s" % (key,val))

        if new_items:
            if url_obj.query:
                url += '&'
            else:
                url += '?'
            url += '&'.join(new_items)
        return url

    # Setup our db stuff
    def _setup(self):
        if self._setup_done:
            return
        self._setup_done = True

        url = config.get('db/url')

        url_obj = sa.engine.url.make_url(url)
        self._engine_driver = url_obj.drivername

        kwargs = {}

        if url_obj.drivername == 'sqlite':
            timeout = 300
            # For sqlite, make busy_timeout be 300 seconds, so we wait 300
            # seconds on a busy db before throwing an error.
            log.d("specifying sqlite timeout of %d seconds" % timeout)
            kwargs['connect_args'] = {'timeout': timeout}

        if url_obj.drivername == 'mysql' or url_obj.drivername == 'mariadb':
            url = self._url_update_query(url, {'charset': 'utf8mb4'})

        log.d("Creating db engine for %s" % self.printable_url(url))
        self._engine = sa.create_engine(url, **kwargs)
        self._sqlite_fk = config.get('db/sqlite_foreign_keys')

    def _table_exists(self, conn, ins, table):
        # Inspector.has_table() is new in sqlalchemy 1.4
        if hasattr(ins, 'has_table'):
            return ins.has_table(table.name)
        else:
            return table.exists(bind=conn)

    def can_create(self):
        with self.connect(check=False) as conn:
            ins = sa.inspect(conn)
            for table in self.model.metadata.tables.values():
                if self._table_exists(conn, ins, table):
                    return False
        return True

    def _setver_exec(self, conn, vers):
        # pylint: disable=no-value-for-parameter
        res = conn.execute(self.model.versions.delete())
        res.close()

        # pylint: disable=no-value-for-parameter
        res = conn.execute(db.model.versions.insert(), dict(version=vers))
        res.close()

    def _setver_sql(self, vers):
        return "DELETE FROM versions;\nINSERT INTO versions (version) VALUES (%d);" % vers

    # Create our tables
    def create(self, force=False):
        with self.connect(check=False) as conn:
            if force:
                self.model.metadata.drop_all(bind=conn)

            self.model.metadata.create_all(bind=conn, checkfirst=False)
            #for tbl in self.model.metadata.sorted_tables:
            #    log.warn('create', "Creating table %r" % tbl.name)
            #    tbl.create(bind=conn, checkfirst=False)

            self._setver_exec(conn, self._dbvers)

    # Return the raw SQL code to create our tables as a string
    def create_sql(self, force=False):
        statements = []
        engine = None

        def dump(sql, *args, **kwargs):
            # pylint: disable=unused-argument
            statements.append(str(sql.compile(dialect=engine.dialect)))

        url = config.get('db/url')
        if hasattr(sa, 'create_mock_engine'):
            # .create_mock_engine() replaced .create_engine(stategy='mock') in
            # sqlalchemy 1.4
            # pylint: disable=no-member
            engine = sa.create_mock_engine(url, dump)
        else:
            engine = sa.create_engine(url, strategy='mock', executor=dump)

        if force:
            self.model.metadata.drop_all(engine)
        self.model.metadata.create_all(engine, checkfirst=False)

        statements.append(self._setver_sql(self._dbvers))

        return ';\n'.join(statements)

    def execute(self, q, *args, **kwargs):
        with self.connect() as conn:
            params = {}
            params.update(kwargs)
            params.update(q.compile().params)
            log.d("db executing (%r): %s, %s, %s" % (conn, q, args, params))
            return conn.execute(q, *args, **kwargs)

    def _exec_sql(self, conn, sql):
        if conn.engine.dialect.name == 'sqlite':
            # sqlite cannot execute multiple statements in a single execute(),
            # so use the sqlite-specific executescript() for sqlite.
            conn.connection.executescript(sql)
        else:
            conn.execute(sa.text(sql))

    # Check if our tables have been created, and our version of the db schema
    # is compatible with what's in the actual db
    def _check(self):
        with self.connect(check=False) as conn:
            ins = sa.inspect(conn)
            for table in self.model.metadata.tables.values():
                if not self._table_exists(conn, ins, table):
                    raise err.VersionError(("Table '%s' does not exist. Perhaps you " +
                                            "need to create the db?") % table.name)
            q = model.versions.select().where(model.versions.c.version == VERSION)

            res = conn.execute(q)
            row = res.fetchone()
            res.close()

            if row is None:
                raise err.VersionError("DB does not support DB version '%d'" % VERSION)

        log.d("Database check succeeded")

    def upgrade_needed(self):
        with self.connect(check=False) as conn:
            needed = True

            q = model.versions.select().where(model.versions.c.version == VERSION)

            res = conn.execute(q)
            row = res.fetchone()
            res.close()

            if row is not None:
                # We found a row for VERSION, indicating the db is
                # compatible with our current version
                needed = False
                log.d("Found row for db version %d" % VERSION)
            else:
                log.d("Found no rows for db version %d" % VERSION)

            # Get the max version in the versions table
            q = sa.select([sa.func.max(model.versions.c.version).label('max_version')])

            res = conn.execute(q)
            row = res.fetchone()
            res.close()

            if row is None:
                raise err.InternalError("DB does not report any version number")

            existing_ver = row['max_version']
            latest_ver = VERSION

            if needed and existing_ver > latest_ver:
                raise err.VersionError(("DB appears to be too new for this release " +
                                        "of FABS (db version %d, code version %d)") % (
                                        existing_ver, latest_ver))

            return (needed, existing_ver, latest_ver)

    def upgrade_sql(self, from_ver, to_ver, use_builtin):
        with io.StringIO() as sql_buf:

            sql_buf.write("BEGIN;\n")
            self._upgrade(from_ver, to_ver, sql_buf=sql_buf, use_builtin=use_builtin)
            sql_buf.write("COMMIT;\n")

            return sql_buf.getvalue()

    def upgrade_exec(self, from_ver, to_ver, use_builtin):
        self._upgrade(from_ver, to_ver, use_builtin=use_builtin)

    def _upgrade(self, from_ver, to_ver, sql_buf=None, use_builtin=None):
        assert from_ver < to_ver
        assert from_ver >= VERS_3 and from_ver <= VERSION
        assert to_ver >= VERS_3 and to_ver <= VERSION

        with self.connect(check=False) as conn:
            (_, db_ver, _) = self.upgrade_needed()

            if from_ver != db_ver:
                raise err.VersionError(("An upgrade from version %d was " +
                                        "requested, but the DB is version %d") % (
                                        from_ver, db_ver))

            if use_builtin is None:
                use_builtin = not _have_alembic
                if not _have_alembic:
                    log.warn('dbupgrade_builtin',
                             "Unable to import the Alembic library for db upgrades. " +
                             "Attempting to proceed with a builtin fallback; if " +
                             "this fails, please try installing Alembic.")

            if use_builtin:
                alop = None

            else:
                configure = alembic.migration.MigrationContext.configure
                if sql_buf is None:
                    alctx = configure(conn)
                else:
                    alctx = configure(dialect_name=conn.engine.dialect.name,
                                      opts={
                                        'as_sql': True,
                                        'output_buffer': sql_buf,
                                      })

                    # Try to make sure we don't accidentally exec anything on the conn
                    conn = None

                alop = alembic.operations.Operations(alctx)

            for prev_ver in range(from_ver, to_ver):
                self._upgrade_step(conn, prev_ver+1, sql_buf, alop, use_builtin)

    def _upgrade_step(self, conn, next_ver, sql_buf, alop, use_builtin):
        if use_builtin:
            self._upgrade_step_builtin(conn, next_ver, sql_buf)
        else:
            self._upgrade_step_alembic(conn, next_ver, sql_buf, alop)

        if sql_buf is not None:
            sql_buf.write(self._setver_sql(next_ver) + "\n")
        else:
            self._setver_exec(conn, next_ver)

    def _upgrade_step_builtin(self, conn, next_ver, sql_buf):
        dialect = conn.engine.dialect.name

        if sql_buf is not None:
            conn = None

        sql = builtin_upgrade_sql.get(dialect, {}).get(next_ver, None)
        if sql is None:
            raise err.DbUpgradeError(("No builtin upgrade found for %s, version %d. " +
                                      "Cannot perform this upgrade without Alembic " +
                                      "installed.") % (dialect, next_ver))

        if sql_buf is not None:
            sql_buf.write(sql + "\n")
        else:
            self._exec_sql(conn, sql)

    def _col_copy(self, col):
        # SQLAlchemy issue #5953 deprecates col.copy(). We don't need to copy
        # the column when Alembic issue #753 is fixed, but we can't easily tell
        # if that's fixed. So just copy the column always, using the private
        # col._copy() method to try to avoid deprecation warnings.
        if hasattr(col, '_copy'):
            # pylint: disable=protected-access
            return col._copy()
        return col.copy()

    def _upgrade_step_alembic(self, conn, next_ver, sql_buf, alop):
        old_model = _Model(next_ver-1)

        for table in self.model.metadata.tables.values():
            old_table = old_model.metadata.tables[table.name]

            table_vers = table.info.get('fabs_dbvers', None)
            if table_vers is not None and table_vers == next_ver:
                # The whole table is new in this version. We don't handle this
                # case yet; just make sure we don't do something wrong.
                raise err.InternalError("table %s vers %s upgrade" % (
                                        table.name, next_ver))

            with alop.batch_alter_table(table.name, copy_from=old_table) as batchop:
                for col in table.columns:
                    col_vers = col.info.get('fabs_dbvers', None)
                    if col_vers is not None and col_vers == next_ver:
                        batchop.add_column(self._col_copy(col))
                        continue

                    col_alter = col.info.get('fabs_dbalter', {}).get(next_ver, None)
                    if col_alter is not None:
                        batchop.alter_column(col.name, **col_alter)

                for idx in table.indexes:
                    idx_vers = idx.info.get('fabs_dbvers', None)
                    if idx_vers is not None and idx_vers == next_ver:
                        batchop.create_index(idx.name, [col.name for col in idx.columns])

    def gen_fake_data(self, workdir, dburl, n_bruns, n_servers, n_vlentries, n_sites,
                      n_links):
        assert not self._setup_done
        import fabs.bstore

        os.mkdir(workdir)
        blobdir = workdir + '/blob'
        lockdir = workdir + '/lock'
        os.mkdir(blobdir)
        os.mkdir(lockdir)

        conf = config.config.get_data()
        conf.setdefault('db', {})
        conf['db/url'] = dburl
        conf['lockdir'] = lockdir
        conf['dump/storage_dirs'] = [blobdir]

        bst = fabs.bstore.BlobStore(blobdir)
        bst.create()

        self.create()

        bruns_done = 0
        vlentries_done = 0
        voldumps_done = 0
        links_done = 0

        with self.connect_bulk() as tx:
            with self.connect() as conn:
                if conn.engine.dialect.name == 'sqlite':
                    conn.execute(sa.text('PRAGMA synchronous = OFF'))
                    conn.execute(sa.text('PRAGMA journal_mode = MEMORY'))

            storid = bst.storid()

            for br_i in range(n_bruns):
                now = 1649900000 + br_i

                br = types.SimpleNamespace()
                br.id = brun.create('fake.example.com',
                                    'fake backup run created by gen_fake_db', None)
                br.dv = 0
                br.state = 'NEW'
                brun.update(br, state='DONE')

                bruns_done += 1

                assert n_servers < 256
                servers = []
                for srv_i in range(n_servers):
                    srv = types.SimpleNamespace()
                    srv.uuid = 'd34db33f-d34d-b33f-d34d-%012x' % srv_i
                    srv.addrs = ['192.0.2.%d' % srv_i]

                    servers.append(srv)

                vladdr.create_many(br, servers)

                addrmap = vladdr.get_addrmap(br)

                assert n_sites <= n_servers

                for vle_i in range(n_vlentries):
                    vle = types.SimpleNamespace()
                    vle.name = 'vol.%x' % vle_i
                    vle.rwid = 0x20000000 + vle_i * 3
                    vle.roid = vle.rwid + 1
                    vle.bkid = vle.rwid + 2

                    vl_id = vlentry.create(br, vle)
                    vlentries_done += 1

                    sites = []
                    for site_i in range(n_sites):
                        vtype = 'RO'
                        addr = '192.0.2.%d' % (site_i-1)
                        if site_i == 0:
                            vtype = 'RW'
                            addr = '192.0.2.0'

                        fsid = addrmap[addr]

                        siteinfo = dict(
                            vl_id=vl_id,
                            type=vtype,
                            server_id=fsid,
                            partition='a',
                        )
                        sites.append(siteinfo)

                    vlentry.create_sites(vle, sites)

                    voldump.create(vl_id=vl_id,
                                   hdr_size=1024,
                                   hdr_creation=now,
                                   hdr_copy=now,
                                   hdr_backup=now,
                                   hdr_update=now,
                                   dump_size=1024,
                                   dump_storid=storid,
                                   dump_spath='fake/spath',
                                   dump_checksum='md5:d41d8cd98f00b204e9800998ecf8427e')
                    voldumps_done += 1

                    linkitems = []
                    for link_i in range(n_links):
                        path = 'some/slink.%d' % link_i
                        target = 'target/path.%d' % link_i
                        linkitems.append((path, target))
                        links_done += 1
                    links.add(vl_id, linkitems)

                    tx.entry_done()

                print("Inserted %d bruns, %d vlentries, %d voldumps, %d links" % (
                      bruns_done, vlentries_done, voldumps_done, links_done))

        print("Inserted %d bruns, %d vlentries, %d voldumps, %d links" % (
              bruns_done, vlentries_done, voldumps_done, links_done))

def _state_source():
    return "%s pid:%d" % (platform.node(), os.getpid())

def _job_timeout(update):
    now = int(time.time())
    update['mtime'] = now

    # Set 'timeout' to 0 to disable timing out this job. Leave 'timeout'
    # unset to use the default timeout.
    if 'timeout' not in update:
        update['timeout'] = config.get('db/default_job_timeout')

class _BackupRunDb:
    def _clauses(self, **kwargs):
        clauses = []
        for attr, val in kwargs.items():
            if attr == 'state_not':
                if isinstance(val, list) or isinstance(val, tuple):
                    clauses.append(sa.not_(model.backup_runs.c.state.in_(val)))
                else:
                    clauses.append(model.backup_runs.c.state != val)
            elif attr == 'start_after':
                clauses.append(model.backup_runs.c.start > val)
            elif isinstance(val, list):
                clauses.append(model.backup_runs.c[attr].in_(val))
            else:
                clauses.append(model.backup_runs.c[attr] == val)
        return clauses

    def create(self, cell, note, volume,
               injected_blob_storid=None,
               injected_blob_spath=None,
               injected_blob_checksum=None):
        descr = "backup run created"

        # pylint: disable=no-value-for-parameter
        res = db.execute(model.backup_runs.insert(), dict(cell=cell,
                                                          state='NEW',
                                                          dv=0,
                                                          state_descr=descr,
                                                          state_source=_state_source(),
                                                          note=note,
                                                          volume=volume,
                                                          injected_blob_storid=injected_blob_storid,
                                                          injected_blob_spath=injected_blob_spath,
                                                          injected_blob_checksum=injected_blob_checksum,
                                                          start=int(time.time()),
                                                          end=0))
        brid = res.inserted_primary_key[0]
        res.close()

        return brid

    def find(self, **kwargs):
        clauses = self._clauses(**kwargs)

        q = model.backup_runs.select().where(sa.and_(*clauses))

        with db.connect():
            res = db.execute(q)
            rows = res.fetchall()
            res.close()

        return rows

    def error(self, brun):
        self.update(brun, state='ERROR', state_last=brun.state,
                    errors=brun.errors+1)

    def kill(self, brun, descr):
        kwargs = {}
        if brun.state_last is None:
            kwargs['state_last'] = brun.state

        self.update(brun, state='FAILED', state_descr=descr, **kwargs)

    def update(self, brun, **kwargs):
        if 'dv' in kwargs:
            raise err.InternalError("Illegal kwarg dv")

        # Always update dv
        kwargs['dv'] = brun.dv + 1
        kwargs['state_source'] = _state_source()

        # pylint: disable=no-value-for-parameter
        q = model.backup_runs.update().where(
            sa.and_(model.backup_runs.c.id == brun.id,
                    model.backup_runs.c.state == brun.state,
                    model.backup_runs.c.dv == brun.dv)
        ).values(**kwargs)

        res = db.execute(q)
        if res.rowcount == 0:
            raise err.DbNoUpdateError("Updated %d rows when trying to update brun id %d" % (
                                      res.rowcount, brun.id))
        if res.rowcount > 1:
            raise err.InternalError("Updated %d rows when trying to update brun id %d" % (
                                    res.rowcount, brun.id))

        # We've updated the db successfully, so update our local structure, too
        for attr, val in kwargs.items():
            setattr(brun, attr, val)

    def delete(self, brid):
        # Delete all vl_addrs entries for vl_fileservers entries that reference
        # this brid.
        #
        # Note that SQLite and maybe other databases cannot join on another
        # table in a DELETE statement. So we have to use a subquery to find the
        # 'vl_fileserver.id' values to match against vl_addrs.fs_id.
        sub_q = sa.select([model.vl_fileservers.c.id]).where(
                    model.vl_fileservers.c.br_id == brid
                )
        # pylint: disable=no-value-for-parameter
        q = model.vl_addrs.delete().where(
                model.vl_addrs.c.fs_id.in_(sub_q)
            )
        db.execute(q)

        # Delete all vl_sites entries for vl_fileservers entries that reference
        # this brid.
        sub_q = sa.select([model.vl_fileservers.c.id]).where(
                    model.vl_fileservers.c.br_id == brid
                )
        # pylint: disable=no-value-for-parameter
        q = model.vl_sites.delete().where(
                model.vl_sites.c.server_id.in_(sub_q)
            )
        db.execute(q)

        # Delete all vl_fileservers entries that reference this brid.
        # pylint: disable=no-value-for-parameter
        q = model.vl_fileservers.delete().where(model.vl_fileservers.c.br_id == brid)
        db.execute(q)

        # Delete all 'links' entries that reference vl_entries in this brid.
        sub_q = sa.select([model.vl_entries.c.id]).where(
                    model.vl_entries.c.br_id == brid
                )
        # pylint: disable=no-value-for-parameter
        q = model.links.delete().where(model.links.c.vl_id.in_(sub_q))
        db.execute(q)

        # Delete all vl_entries that reference this brid.
        # pylint: disable=no-value-for-parameter
        q = model.vl_entries.delete().where(model.vl_entries.c.br_id == brid)
        db.execute(q)

        # Now delete the brid itself.
        # pylint: disable=no-value-for-parameter
        q = model.backup_runs.delete().where(model.backup_runs.c.id == brid)
        db.execute(q)

def _scalar_subq(query):
    # In sqlalchemy 1.4, .as_scalar() was replaced by .scalar_subquery().
    if hasattr(query, 'scalar_subquery'):
        return query.scalar_subquery()
    else:
        return query.as_scalar()

class _RestoreReqDb:
    def create(self, note, voldump, user, path_full):
        now = int(time.time())
        descr = "restore request created"

        # pylint: disable=no-value-for-parameter
        insert_q = model.restore_reqs.insert().values(
            state='NEW',
            dv=0,
            cell=voldump.cell,
            state_descr=descr,
            state_source=_state_source(),
            ctime=now,
            mtime=now,
            vd_id=voldump.id,
            note=note,
            username=user,
            user_path_full=path_full,
        )

        res = db.execute(insert_q)
        reqid = res.inserted_primary_key[0]

        return reqid

    def _clauses(self, **kwargs):
        clauses = []
        for attr, val in kwargs.items():
            if attr == 'state_not':
                if isinstance(val, list) or isinstance(val, tuple):
                    clauses.append(sa.not_(model.restore_reqs.c.state.in_(val)))
                else:
                    clauses.append(model.restore_reqs.c.state != val)
            elif attr == 'next_update_before':
                clauses.append(model.restore_reqs.c.next_update <= val)
            elif isinstance(val, list):
                clauses.append(model.restore_reqs.c[attr].in_(val))
            else:
                clauses.append(model.restore_reqs.c[attr] == val)
        return clauses

    def find(self, **kwargs):
        clauses = self._clauses(**kwargs)

        q = model.restore_reqs.select().where(sa.and_(*clauses))

        with db.connect():
            res = db.execute(q)
            rows = res.fetchall()
            res.close()

        return rows

    def update_all(self, match, update):
        if 'dv' in update:
            raise err.InternalError("Illegal kwarg dv")

        clauses = self._clauses(**match)

        update['dv'] = model.restore_reqs.c.dv + 1
        update['state_source'] = _state_source()
        update['mtime'] = int(time.time())

        if 'next_update' not in update:
            update['next_update'] = 0

        # pylint: disable=no-value-for-parameter
        q = model.restore_reqs.update().where(
            sa.and_(*clauses)
        ).values(**update)

        db.execute(q)

    def update(self, rreq, **kwargs):
        if 'dv' in kwargs:
            raise err.InternalError("Illegal kwarg dv")

        kwargs['dv'] = rreq.dv + 1
        kwargs['state_source'] = _state_source()
        kwargs['mtime'] = int(time.time())

        if 'next_update' not in kwargs:
            kwargs['next_update'] = 0

        # pylint: disable=no-value-for-parameter
        q = model.restore_reqs.update().where(
            sa.and_(model.restore_reqs.c.id == rreq.id,
                    model.restore_reqs.c.state == rreq.state,
                    model.restore_reqs.c.dv == rreq.dv)
        ).values(**kwargs)

        res = db.execute(q)
        if res.rowcount == 0:
            raise err.DbNoUpdateError("Updated %d rows when trying to update rreq id %d" % (
                                      res.rowcount, rreq.id))
        if res.rowcount > 1:
            raise err.InternalError("Updated %d rows when trying to update rreq id %d" % (
                                    res.rowcount, rreq.id))

        for attr, val in kwargs.items():
            setattr(rreq, attr, val)

    def error(self, rreq):
        self.update(rreq, state='ERROR', state_last=rreq.state,
                    errors=rreq.errors+1)

    def kill(self, rreq, descr):
        kwargs = {}
        if rreq.state_last is None:
            kwargs['state_last'] = rreq.state

        self.update(rreq, state='FAILED', state_descr=descr, **kwargs)

    def delete(self, **kwargs):
        clauses = self._clauses(**kwargs)

        # pylint: disable=no-value-for-parameter
        q = model.restore_reqs.delete().where(sa.and_(*clauses))
        db.execute(q)

class _VLAddrDb:
    def create_nonuuid(self, brun, addr):
        # pylint: disable=no-value-for-parameter
        fs_q = model.vl_fileservers.insert()
        # pylint: disable=no-value-for-parameter
        addr_q = model.vl_addrs.insert()

        with db.connect():
            res = db.execute(fs_q, dict(br_id=brun.id))
            server_id = res.inserted_primary_key[0]
            res.close()

            db.execute(addr_q, dict(fs_id=server_id, addr=addr))

    def create_many(self, brun, servers):
        if not servers:
            raise err.InternalError("Need at least one fileserver address")

        # pylint: disable=no-value-for-parameter
        fs_q = model.vl_fileservers.insert()
        # pylint: disable=no-value-for-parameter
        addr_q = model.vl_addrs.insert()

        with db.connect():
            server_ids = []

            # Insert our servers one at a time, so we can get an id back
            for server in servers:
                res = db.execute(fs_q, dict(br_id=brun.id, uuid=server.uuid))
                server_ids.append(res.inserted_primary_key[0])
                res.close()

            # Now insert our addresses for those servers
            db.execute(addr_q, [dict(fs_id=fsid,
                                     addr=addr)
                                for server, fsid in zip(servers, server_ids)
                                for addr in server.addrs])

    def get_addrmap(self, brun):
        q = model.vl_addrs.select().where(
            sa.and_(model.vl_fileservers.c.br_id == brun.id,
                    model.vl_addrs.c.fs_id == model.vl_fileservers.c.id)
        )

        addrmap = {}

        with db.connect():
            res = db.execute(q)

            for row in res:
                addrmap[row['addr']] = row['fs_id']

        return addrmap

    def get_uuidmap(self, brun):
        q = model.vl_fileservers.select().where(model.vl_fileservers.c.br_id == brun.id)

        uuidmap = {}

        with db.connect():
            res = db.execute(q)

            for row in res:
                uuidmap[row['uuid']] = row['id']

        return uuidmap

    def get_idmap(self, brun):
        q = sa.select([
            model.vl_fileservers.c.id,
            model.vl_fileservers.c.uuid,

            model.vl_addrs.c.addr,
        ]).where(
            sa.and_(
                model.vl_fileservers.c.br_id == brun.id,
                model.vl_fileservers.c.id == model.vl_addrs.c.fs_id,
            )
        )

        ret = {}

        with db.connect():
            res = db.execute(q)
            for row in res:
                if row.id not in ret:
                    ret[row.id] = {'uuid': row.uuid, 'addrs': []}
                ret[row.id]['addrs'].append(row.addr)

        return ret

class _VLEntryDb:
    # Given a query to get a single vlentry row, return a dict of the row's
    # contents, and also populate the per-site information for the vlentry.
    def _vlentries_from_query(self, vlentry_q):
        site_q = model.vl_sites.select().where(model.vl_sites.c.vl_id == sa.bindparam('vl_id'))

        rows = []
        with db.connect():
            res = db.execute(vlentry_q)
            for row in res:
                row = dict((key, row[key]) for key in row.keys())

                subres = db.execute(site_q, vl_id=row['id'])
                row['sites'] = []
                for siterow in subres:
                    row['sites'].append(dict((key, siterow[key]) for key in siterow.keys()))
                subres.close()

                rows.append(row)
            res.close()

        return rows

    def get(self, id_):
        vlentry_q = model.vl_entries.select().where(model.vl_entries.c.id == id_)

        rows = self._vlentries_from_query(vlentry_q)
        if len(rows) == 0:
            raise err.BadVlidError("Could not find vlentry id %d in db" % id_)
        if len(rows) != 1:
            raise err.InternalError("Got %d rows when fetching vlentry id %d" % (len(rows), id_))

        return rows[0]

    def _find_next_batch(self, brun, prev_row=None):
        batch_size = config.get('db/batch_size')

        clauses = []
        if prev_row is not None:
            # If we have a previous vlentry, our next vlentry must have a
            # higher id.
            clauses.append(model.vl_entries.c.id > prev_row['id'])

        # Get the first vlentries we can find; get the smallest id first.
        vlentry_q = model.vl_entries.select().where(
            sa.and_(
                model.vl_entries.c.br_id == brun.id,
                *clauses
            )
        ).order_by(model.vl_entries.c.id).limit(batch_size)

        return self._vlentries_from_query(vlentry_q)

    def finditer(self, brun):
        # Find all vlentries for the given brun. We don't fetch all of our
        # vlentries in a single query, because our caller may be committing and
        # recreating the relevant db transaction. If we kept a db cursor open
        # during that, the results may get screwed up, so instead we fetch our
        # vlentries in batches, using separate connections for each batch
        # (_find_next_batch).

        prev_row = None
        while True:
            batch = self._find_next_batch(brun, prev_row)
            if len(batch) == 0:
                break # EOF

            for row in batch:
                yield row
                prev_row = row

    def find_count(self, brid):
        q = sa.select([sa.func.count()]).where(model.vl_entries.c.br_id == brid)
        res = db.execute(q)
        row = res.fetchone()
        res.close()
        return row[0]

    def create(self, brun, vlentry):
        # pylint: disable=no-value-for-parameter
        q = model.vl_entries.insert()

        res = db.execute(q, dict(br_id=brun.id,
                                 name=vlentry.name,
                                 rwid=vlentry.rwid,
                                 roid=vlentry.roid,
                                 bkid=vlentry.bkid))
        vlid = res.inserted_primary_key[0]

        return vlid

    def update(self, vl_id, vlentry):
        # Assume name and rwid don't change, for simplicity. But let other
        # fields change.
        # pylint: disable=no-value-for-parameter
        q = model.vl_entries.update().where(
            sa.and_(
                model.vl_entries.c.id == vl_id,
                model.vl_entries.c.name == vlentry.name,
                model.vl_entries.c.rwid == vlentry.rwid
            )
        ).values(
            roid=vlentry.roid,
            bkid=vlentry.bkid,
        )

        res = db.execute(q)
        if res.rowcount != 1:
            raise err.InternalError("Updated %d rows when trying to update vlentry id %d" % vl_id)

    def create_sites(self, vlentry, vlsites):
        if not vlsites:
            raise err.InternalError("Need at least one vlsite")

        # Do some sanity checking on the input data, just in case...
        for site in vlsites:
            if site['type'] not in ('RW', 'RO', 'BK'):
                raise err.InternalError("Unrecognized site type %s for vlentry %s" % (
                                        site['type'], vlentry.name))

            part = site['partition']
            bad_part = False
            if 1 <= len(part) <= 2:
                for char in part:
                    if 'a' <= char <= 'z':
                        pass
                    else:
                        bad_part = True
                        break
            else:
                bad_part = True
            if bad_part:
                raise err.InternalError("Bad partition '%s' for vlentry %s" % (
                                        part, vlentry.name))

            # pylint: disable=not-an-iterable
            cols = sorted(c.name for c in model.vl_sites.columns)
            if sorted(site.keys()) != cols:
                raise err.InternalError("Bad vlsite data: %r != %r" %
                                        (sorted(site.keys()), cols))

        # pylint: disable=no-value-for-parameter
        q = model.vl_sites.insert()
        db.execute(q, vlsites)

    def delete(self, vlid):
        # pylint: disable=no-value-for-parameter
        q = model.vl_sites.delete().where(model.vl_sites.c.vl_id == vlid)
        db.execute(q)

        # pylint: disable=no-value-for-parameter
        q = model.vl_entries.delete().where(model.vl_entries.c.id == vlid)
        db.execute(q)

    def delete_cruft(self, brid):
        sub_q = sa.select([model.vl_entries.c.id]).where(
                    sa.and_(model.vl_entries.c.br_id == brid,
                            sa.not_(model.vl_entries.c.id.in_(sa.select([model.volume_dumps.c.vl_id]))),
                    )
                )

        # pylint: disable=no-value-for-parameter
        q = model.links.delete().where(model.links.c.vl_id.in_(sub_q))
        res = db.execute(q)
        res.close()

        # pylint: disable=no-value-for-parameter
        q = model.vl_sites.delete().where(model.vl_sites.c.vl_id.in_(sub_q))
        res = db.execute(q)
        res.close()

        # pylint: disable=no-value-for-parameter
        q = model.vl_entries.delete().where(model.vl_entries.c.id.in_(sub_q))
        res = db.execute(q)
        count = res.rowcount
        res.close()
        return count

class _DumpJobsDb:
    def _clauses(self, _qinfo=None, **kwargs):
        clauses = []

        if 'br_id' in kwargs:
            if _qinfo is not None:
                _qinfo['other_tables'] = True
            clauses.append(model.vl_entries.c.br_id == kwargs['br_id'])
            clauses.append(model.dump_jobs.c.vl_id == model.vl_entries.c.id)
            del kwargs['br_id']

        if 'stale' in kwargs:
            # A dump job is 'stale' if the timeout field is set, and at least
            # 'timeout' seconds have passed since the job was last touched
            now = int(time.time())
            stale_clause = sa.and_(
                model.dump_jobs.c.timeout != 0,
                model.dump_jobs.c.mtime + model.dump_jobs.c.timeout < now
            )
            if kwargs['stale']:
                clauses.append(stale_clause)
            else:
                clauses.append(sa.not_(stale_clause))
            del kwargs['stale']

        for attr, val in kwargs.items():
            if attr == 'state_not':
                clauses.append(model.dump_jobs.c.state != val)
            elif isinstance(val, list):
                clauses.append(model.dump_jobs.c[attr].in_(val))
            else:
                clauses.append(model.dump_jobs.c[attr] == val)
        return clauses

    # Delete all dump jobs for the given brun
    def clear(self, brun):
        # pylint: disable=no-value-for-parameter
        q = model.dump_jobs.delete().where(
            model.dump_jobs.c.vl_id.in_(
                sa.select([model.vl_entries.c.id]).where(
                    model.vl_entries.c.br_id == brun.id
                )
            )
        )
        db.execute(q)

    def create(self, cell, vl_id, server_id, partition, state, state_descr,
               injected_blob_storid=None, injected_blob_spath=None,
               injected_blob_checksum=None):

        # pylint: disable=no-value-for-parameter
        q = model.dump_jobs.insert()

        now = int(time.time())
        db.execute(q, dict(cell=cell,
                           vl_id=vl_id,
                           server_id=server_id,
                           partition=partition,
                           state=state,
                           state_source=_state_source(),
                           dv=0,
                           state_descr=state_descr,
                           ctime=now,
                           mtime=now,
                           timeout=0,
                           injected_blob_storid=injected_blob_storid,
                           injected_blob_spath=injected_blob_spath,
                           injected_blob_checksum=injected_blob_checksum))

    def find(self, **kwargs):
        clauses = self._clauses(**kwargs)

        q = model.dump_jobs.select().where(sa.and_(*clauses))
        q = sa.select(list(model.dump_jobs.columns) + [
            model.vl_entries.c.name,
            model.vl_entries.c.rwid,
        ]).where(
            sa.and_(
                model.dump_jobs.c.vl_id == model.vl_entries.c.id,
                *clauses
            )
        )

        with db.connect():
            res = db.execute(q)
            rows = res.fetchall()
            res.close()

        return rows

    def finditer(self, brun, **kwargs):
        clauses = self._clauses(**kwargs)

        q = sa.select(list(model.dump_jobs.columns) + [
            model.vl_entries.c.name,
            model.vl_entries.c.rwid,
        ]).where(
            sa.and_(
                model.dump_jobs.c.vl_id == model.vl_entries.c.id,
                model.vl_entries.c.br_id == brun.id,
                *clauses
            )
        )

        with db.connect():
            res = db.execute(q)
            for row in res:
                yield row

    def describe(self, brun, **kwargs):
        clauses = self._clauses(**kwargs)

        q = sa.select([
            model.dump_jobs.c.vl_id,
            model.dump_jobs.c.cell,
            model.dump_jobs.c.server_id,
            model.dump_jobs.c.partition,
            model.dump_jobs.c.state,
            model.dump_jobs.c.state_last,
            model.dump_jobs.c.dv,
            model.dump_jobs.c.state_descr,
            model.dump_jobs.c.state_source,
            model.dump_jobs.c.ctime,
            model.dump_jobs.c.mtime,
            model.dump_jobs.c.timeout,
            model.dump_jobs.c.errors,
            model.dump_jobs.c.incr_timestamp,
            model.dump_jobs.c.skip_reason,

            model.vl_entries.c.br_id,
            model.vl_entries.c.name,
            model.vl_entries.c.rwid,
            model.vl_entries.c.roid,
            model.vl_entries.c.bkid,

            model.vl_fileservers.c.uuid,

            _scalar_subq(
                sa.select([
                    model.vl_addrs.c.addr
                ]).where(
                    model.vl_fileservers.c.id == model.vl_addrs.c.fs_id
                ).limit(1)
            ).label('server_addr')

        ]).where(
            sa.and_(
                model.dump_jobs.c.vl_id == model.vl_entries.c.id,
                model.vl_entries.c.br_id == brun.id,
                model.dump_jobs.c.server_id == model.vl_fileservers.c.id,
                *clauses
            )
        )

        info = []

        with db.connect():
            res = db.execute(q)
            for row in res:
                info.append(dict((key, row[key]) for key in row.keys()))
            res.close()

        return info

    # Count the number of dump jobs that match the given criteria
    def get_counts(self, brun, **kwargs):
        clauses = self._clauses(**kwargs)

        q = sa.select([
            model.dump_jobs.c.server_id,
            model.dump_jobs.c.partition,
            sa.func.count().label('count')
        ]).where(
            sa.and_(
                model.vl_entries.c.br_id == brun.id,
                model.dump_jobs.c.vl_id == model.vl_entries.c.id,
                *clauses
            )
        ).group_by(model.dump_jobs.c.server_id,
                   model.dump_jobs.c.partition)

        part_counts = {}

        with db.connect():
            res = db.execute(q)
            for row in res:
                part_counts[(row['server_id'], row['partition'])] = row['count']

        return part_counts

    def count(self, brun, **kwargs):
        clauses = self._clauses(**kwargs)

        q = sa.select([sa.func.count().label('count')]).where(
            sa.and_(
                model.vl_entries.c.br_id == brun.id,
                model.dump_jobs.c.vl_id == model.vl_entries.c.id,
                *clauses
            )
        )

        with db.connect():
            res = db.execute(q)
            row = res.fetchone()
            res.close()
        return row[0]

    def update_all(self, match, update):
        if 'dv' in update:
            raise err.InternalError("Illegal kwarg dv")

        qinfo = {'other_tables': False}
        clauses = self._clauses(qinfo, **match)

        update['dv'] = model.dump_jobs.c.dv + 1
        update['state_source'] = _state_source()
        _job_timeout(update)

        if qinfo['other_tables']:
            # If our clauses reference other tables, use a sub-query to find
            # what dumpjobs to update. (If we just use the clauses directly
            # without a subquery, sqlalchemy will use an UPDATE FROM statement,
            # which sqlite doesn't support before 3.33.)
            sub_q = _scalar_subq(
                sa.select([model.dump_jobs.c.vl_id]).where(
                    sa.and_(*clauses)
                )
            )
            where_clause = model.dump_jobs.c.vl_id.in_(sub_q)

        else:
            # If our clauses don't reference other tables, don't use a
            # sub-query, and instead just determine which dumpjobs to update by
            # specifying our clauses as WHERE clauses in the UPDATE statement.
            # If we try to use a subquery in this case, MySQL/MariaDB will
            # complain "You can't specify target table 'dump_jobs' for update
            # in FROM clause".
            where_clause = sa.and_(*clauses)

        # pylint: disable=no-value-for-parameter
        q = model.dump_jobs.update().where(
            where_clause
        ).values(**update)

        db.execute(q)

    def error(self, dumpjob):
        self.update(dumpjob, state='ERROR', state_last=dumpjob.state,
                    errors=dumpjob.errors+1, timeout=0)

    def error_nonfatal(self, dumpjob):
        self.update(dumpjob, errors=dumpjob.errors+1, timeout=dumpjob.timeout)

    # @todo: this method is almost identical to brun.update, they should be
    # refactored and consolidated somehow
    def update(self, dumpjob, **kwargs):
        if 'dv' in kwargs:
            raise err.InternalError("Illegal kwarg dv")

        # Always update dv
        kwargs['dv'] = dumpjob.dv + 1
        kwargs['state_source'] = _state_source()
        _job_timeout(kwargs)

        # pylint: disable=no-value-for-parameter
        q = model.dump_jobs.update().where(
            sa.and_(model.dump_jobs.c.vl_id == dumpjob.vl_id,
                    model.dump_jobs.c.state == dumpjob.state,
                    model.dump_jobs.c.dv == dumpjob.dv)
        ).values(**kwargs)

        res = db.execute(q)
        if res.rowcount == 0:
            raise err.DbNoUpdateError("Updated %d rows when trying to update dumpjob vl_id %d" % (
                                      res.rowcount, dumpjob.vl_id))
        if res.rowcount > 1:
            raise err.InternalError("Updated %d rows when trying to update dumpjob vl_id %d" % (
                                    res.rowcount, dumpjob.vl_id))

        # We've updated the db successfully, so update our local structure, too
        for attr, val in kwargs.items():
            setattr(dumpjob, attr, val)

    # Delete all dumpjobs for a given backup run
    def clear_jobs(self, brun):
        # pylint: disable=no-value-for-parameter
        q = model.dump_jobs.delete().where(
            model.dump_jobs.c.vl_id.in_(
                sa.select([model.vl_entries.c.id]).where(
                    model.vl_entries.c.br_id == brun.id
                )
            )
        )

        res = db.execute(q)
        count = res.rowcount
        res.close()
        return count

class _VolumeDumpsDb:
    def create(self, **kwargs):
        # pylint: disable=no-value-for-parameter
        q = model.volume_dumps.insert()
        db.execute(q, **kwargs)

    # Find the latest volume dump for a volume. If 'before_time' is given,
    # limit ourselves to volume dumps done before the given timestamp. If
    # 'before_id' is given, limit ourselves to volume dumps with ids lower than
    # the given id.
    def find_latest(self, cell, rwid=None, rwname=None, before_time=None,
                    before_id=None):

        conditions = []

        if rwid is not None:
            conditions.append(model.vl_entries.c.rwid == rwid)
        if rwname is not None:
            # When searching by volume name, search against the actual name and
            # all volume IDs. This is just so we find the volume even if
            # someone refers to it by e.g. RO id
            conditions.append(sa.or_(
                model.vl_entries.c.name == rwname,
                model.vl_entries.c.rwid == rwname,
                model.vl_entries.c.roid == rwname,
                model.vl_entries.c.bkid == rwname,
            ))

        conditions.extend([
            model.volume_dumps.c.vl_id == model.vl_entries.c.id,
            model.vl_entries.c.br_id == model.backup_runs.c.id,
            model.backup_runs.c.cell == cell,
        ])

        if before_time is not None:
            conditions.append(model.volume_dumps.c.hdr_creation <= before_time)
        if before_id is not None:
            conditions.append(model.volume_dumps.c.id < before_id)

        q = sa.select(list(model.volume_dumps.columns) + [

            model.vl_entries.c.br_id,
            model.vl_entries.c.name,
            model.vl_entries.c.rwid,
            model.vl_entries.c.roid,
            model.vl_entries.c.bkid,

            model.backup_runs.c.cell,
        ]).where(
            sa.and_(*conditions)
        ).order_by(model.volume_dumps.c.id.desc()).limit(1)

        with db.connect():
            res = db.execute(q)
            return res.fetchone()

    # Find the oldest volume dump for a volume. If 'after_time' is given, limit
    # ourselves to volume dumps done after the given timestamp. If 'after_id'
    # is given, limit ourselves to volume dumps with ids greater tha nthe given
    # id.
    def find_oldest(self, cell, rwid=None, rwname=None, after_time=None,
                    after_id=None):

        conditions = []

        if rwid is not None:
            conditions.append(model.vl_entries.c.rwid == rwid)
        if rwname is not None:
            # When searching by volume name, search against the actual name and
            # all volume IDs. This is just so we find the volume even if
            # someone refers to it by e.g. RO id
            conditions.append(sa.or_(
                model.vl_entries.c.name == rwname,
                model.vl_entries.c.rwid == rwname,
                model.vl_entries.c.roid == rwname,
                model.vl_entries.c.bkid == rwname,
            ))

        conditions.extend([
            model.volume_dumps.c.vl_id == model.vl_entries.c.id,
            model.vl_entries.c.br_id == model.backup_runs.c.id,
            model.backup_runs.c.cell == cell,
        ])

        if after_time is not None:
            conditions.append(model.volume_dumps.c.hdr_creation >= after_time)
        if after_id is not None:
            conditions.append(model.volume_dumps.c.id > after_id)

        q = sa.select(list(model.volume_dumps.columns) + [

            model.vl_entries.c.br_id,
            model.vl_entries.c.name,
            model.vl_entries.c.rwid,
            model.vl_entries.c.roid,
            model.vl_entries.c.bkid,

            model.backup_runs.c.cell,
        ]).where(
            sa.and_(*conditions)
        ).order_by(model.volume_dumps.c.id.asc()).limit(1)

        with db.connect():
            res = db.execute(q)
            return res.fetchone()

    def finditer(self, cell=None, brid=None, rwid=None, before_ts=None,
                 after_ts=None, redundant=None, **kwargs):
        voldumps = model.volume_dumps.alias()
        vlentries = model.vl_entries.alias()
        bruns = model.backup_runs.alias()

        clauses = [
            voldumps.c.vl_id == vlentries.c.id,
            vlentries.c.br_id == bruns.c.id,
        ]

        if brid is not None:
            clauses.append(vlentries.c.br_id == brid)
        else:
            if cell is None:
                raise ValueError("Must specify a cell or a brid")
            clauses.append(bruns.c.cell == cell)

        if rwid is not None:
            clauses.append(vlentries.c.rwid == rwid)
        if before_ts is not None:
            clauses.append(voldumps.c.hdr_creation <= before_ts)
        if after_ts is not None:
            clauses.append(voldumps.c.hdr_creation >= after_ts)

        for key, val in kwargs.items():
            if val is not None:
                clauses.append(voldumps.c[key] == val)

        if redundant is not None:
            sub_voldumps = model.volume_dumps.alias()
            sub_vlentries = model.vl_entries.alias()
            sub_bruns = model.backup_runs.alias()

            br_subq = sa.select([sub_bruns.c.id]).where(sub_bruns.c.cell == cell)

            # Count how many voldumps exist for this same rwid, but are newer
            sub_q = sa.select([sa.func.count()]).where(sa.and_(
                # join the voldump to its vl_entry
                sub_voldumps.c.vl_id == sub_vlentries.c.id,

                # the vl_entry must be for the same RW volid as our parent query
                sub_vlentries.c.rwid == vlentries.c.rwid,

                # the counted voldump must be newer than our current voldump
                sub_voldumps.c.hdr_creation > voldumps.c.hdr_creation,

                # the vl_entry must be for this cell
                sub_vlentries.c.br_id.in_(br_subq),

            )).limit(redundant)

            # We must have at least 'redundant' newer voldumps for this same
            # volume
            clauses.append(_scalar_subq(sub_q) >= redundant)

        # pylint: disable=not-an-iterable
        q = sa.select(
            [col for col in voldumps.columns] +

            [vlentries.c.br_id,
             vlentries.c.name,
             vlentries.c.rwid,
             vlentries.c.roid,
             vlentries.c.bkid] +

            [bruns.c.cell]
        ).where(
            sa.and_(*clauses)
        )

        with db.connect():
            res = db.execute(q)
            for row in res:
                yield row
            res.close()

    def find(self, cell=None, **kwargs):
        clauses = [
            model.volume_dumps.c.vl_id == model.vl_entries.c.id,
            model.vl_entries.c.br_id == model.backup_runs.c.id,
        ]

        for attr, val in kwargs.items():
            clauses.append(model.volume_dumps.c[attr] == val)

        if cell is not None:
            clauses.append(model.backup_runs.c.cell == cell)

        # pylint: disable=not-an-iterable
        q = sa.select(
            [col for col in model.volume_dumps.columns] +

            [model.vl_entries.c.br_id,
             model.vl_entries.c.name,
             model.vl_entries.c.rwid,
             model.vl_entries.c.roid,
             model.vl_entries.c.bkid] +

            [model.backup_runs.c.cell]
        ).where(
            sa.and_(*clauses)
        )

        with db.connect():
            res = db.execute(q)
            rows = res.fetchall()
            res.close()

        return rows

    def delete(self, vdid):
        # pylint: disable=no-value-for-parameter
        q = model.volume_dumps.delete().where(model.volume_dumps.c.id == vdid)
        db.execute(q)

class _LinksDb:
    def add(self, vl_id, links):
        # pylint: disable=no-value-for-parameter
        q = model.links.insert()
        db.execute(q, [dict(vl_id=vl_id,
                            path=path,
                            target=target)
                       for (path, target) in links])

    def clear(self, vl_id):
        # pylint: disable=no-value-for-parameter
        q = model.links.delete().where(model.links.c.vl_id == vl_id)
        db.execute(q)

    def search(self, vl_id, path):
        q = model.links.select().where(sa.and_(
            model.links.c.vl_id == vl_id,
            model.links.c.path == path,
        ))

        with db.connect():
            res = db.execute(q)
            rows = res.fetchall()
            res.close()

        return [(row['path'], row['target']) for row in rows]

class _BlobStoreDb:
    def lookup_storid(self, storid):
        q = model.bstores.select().where(
            model.bstores.c.id == storid,
        ).limit(1)

        with db.connect():
            res = db.execute(q)
            return res.fetchone()

    def gen_storid(self, uuid):
        q = model.bstores.select().where(
            model.bstores.c.uuid == uuid,
        ).limit(1)

        with db.connect():
            res = db.execute(q)
            row = res.fetchone()
            if row is not None:
                return row['id']
            res.close()

            # pylint: disable=no-value-for-parameter
            res = db.execute(model.bstores.insert(),
                             dict(uuid=uuid))
            storid = res.inserted_primary_key[0]
            res.close()

            return storid

builtin_upgrade_sql = {
    'sqlite': {
        VERS_4: """
CREATE TABLE _alembic_tmp_links (
    vl_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    target TEXT NOT NULL,
    FOREIGN KEY(vl_id) REFERENCES vl_entries (id)
);
INSERT INTO _alembic_tmp_links (vl_id, path, target) SELECT links.vl_id, links.path, links.target
FROM links;
DROP TABLE links;
ALTER TABLE _alembic_tmp_links RENAME TO links;
CREATE UNIQUE INDEX idx_vlid_path ON links (vl_id, path);
ALTER TABLE dump_jobs ADD COLUMN skip_reason VARCHAR(256);
ALTER TABLE dump_jobs ADD COLUMN rw_target_status VARCHAR(256);
""",
        VERS_5: """
CREATE INDEX ix_backup_runs_state ON backup_runs (state);
CREATE INDEX ix_vl_fileservers_br_id ON vl_fileservers (br_id);
CREATE INDEX ix_vl_entries_br_id ON vl_entries (br_id);
CREATE INDEX ix_vl_entries_rwid ON vl_entries (rwid);
CREATE INDEX ix_vl_addrs_fs_id ON vl_addrs (fs_id);
CREATE INDEX ix_vl_sites_server_id ON vl_sites (server_id);
CREATE INDEX ix_volume_dumps_vl_id ON volume_dumps (vl_id);
CREATE INDEX ix_volume_dumps_hdr_creation ON volume_dumps (hdr_creation);
CREATE INDEX ix_dump_jobs_cell_state ON dump_jobs (cell, state);
CREATE INDEX ix_restore_reqs_vd_id ON restore_reqs (vd_id);
CREATE INDEX ix_restore_reqs_state ON restore_reqs (state);
""",
    },
    'mysql': {
        VERS_4: """
ALTER TABLE links MODIFY vl_id BIGINT NOT NULL;
ALTER TABLE links MODIFY path TEXT NOT NULL;
ALTER TABLE links MODIFY target TEXT NOT NULL;
ALTER TABLE dump_jobs ADD COLUMN skip_reason VARCHAR(256);
ALTER TABLE dump_jobs ADD COLUMN rw_target_status VARCHAR(256);
""",
        VERS_5: """
CREATE INDEX ix_backup_runs_state ON backup_runs (state);
CREATE INDEX ix_vl_fileservers_br_id ON vl_fileservers (br_id);
CREATE INDEX ix_vl_entries_br_id ON vl_entries (br_id);
CREATE INDEX ix_vl_entries_rwid ON vl_entries (rwid);
CREATE INDEX ix_vl_addrs_fs_id ON vl_addrs (fs_id);
CREATE INDEX ix_vl_sites_server_id ON vl_sites (server_id);
CREATE INDEX ix_volume_dumps_vl_id ON volume_dumps (vl_id);
CREATE INDEX ix_volume_dumps_hdr_creation ON volume_dumps (hdr_creation);
CREATE INDEX ix_dump_jobs_cell_state ON dump_jobs (cell, state);
CREATE INDEX ix_restore_reqs_vd_id ON restore_reqs (vd_id);
CREATE INDEX ix_restore_reqs_state ON restore_reqs (state);
""",
    },
}

db = Db()
model = db.model
brun = _BackupRunDb()
rreq = _RestoreReqDb()
vladdr = _VLAddrDb()
vlentry = _VLEntryDb()
dumpjob = _DumpJobsDb()
voldump = _VolumeDumpsDb()
links = _LinksDb()
bstore = _BlobStoreDb()

# Create some convenience shortcuts for users from other modules
connect = db.connect
connect_bulk = db.connect_bulk
can_create = db.can_create
create = db.create
create_sql = db.create_sql
printable_url = db.printable_url
reset = db.reset
set_dbvers = db.set_dbvers
upgrade_needed = db.upgrade_needed
upgrade_sql = db.upgrade_sql
upgrade_exec = db.upgrade_exec

# Use like this:
# with db.ensure_reset():
#     foo()
# And we'll make sure db.reset() gets called after foo() is done.
@contextlib.contextmanager
def ensure_reset():
    try:
        yield
    finally:
        try:
            # Try to make sure db.reset() gets called even if an exception was
            # thrown. But if something in here throws an exception, ignore it;
            # this is just best-effort.
            db.reset()
        except Exception:
            pass
