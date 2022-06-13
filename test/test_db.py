#
# Copyright (c) 2021-2022, Sine Nomine Associates ("SNA")
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

import pytest
import re
import sqlalchemy as sa
import subprocess

import fabs.db
import fabs.testutil as tu

def _cleanup(sql):
    # Ignore "if not exists", which sqlite generates only sometimes for some reason
    sql = re.sub(r'CREATE TABLE IF NOT EXISTS', 'CREATE TABLE', sql)
    # Trim trailing whitespace
    sql = re.sub(r'\s+$', '', sql, flags=re.MULTILINE)
    # Make all commas be followed by a newline
    sql = re.sub(r',(?!\n)', ',\n', sql)
    # Remove double quotes
    sql = re.sub(r'"', '', sql)
    # Trim leading whitespace
    sql = re.sub(r'^\s+', '', sql, flags=re.MULTILINE)

    new_lines = []
    for line in sql.split('\n'):
        # Match lines like ")ROW_FORMAT=dynamic ENGINE=innodb CHARSET=utf8mb4"
        if line.startswith(')') and 'ROW_FORMAT' in line:
            # ...and change the FOO=bar specifiers into a consistent order
            line = ')' + ' '.join(sorted(line[1:].split()))
        new_lines.append(line)

    return '\n'.join(new_lines)

def _sortlines(val):
    lines = val.split('\n')
    lines.sort()
    return '\n'.join(lines)

# Assert that the two sql strings are the same. We do some cleanup on the
# strings to ignore differences we don't care about:
# - Ignore leading/trailing whitespace on each line
# - Ignore double-quote marks (")
# - Make all commas be followed by a newline
# - Ignore lines that are just reordered. This is ignored by sorting the lines,
#   but if there's an actual mismatch, we assert on the un-sorted text, so the
#   diff'd output from pytest is easier to read.
def _check_sql(expected, got):
    expected = _cleanup(expected)
    got = _cleanup(got)

    expected_s = _sortlines(expected)
    got_s = _sortlines(got)

    if expected_s == got_s:
        assert expected_s == got_s
    else:
        assert expected == got

class TestDb(tu.FabsMockEnvTest):
    @pytest.fixture
    def db_engine(self, db_url):
        engine = db_url.split(':')[0]
        return engine

    @pytest.fixture(params=fabs.db.ALL_VERSIONS)
    def db_vers(self, request):
        return request.param

    @pytest.fixture(params=["always", "never"])
    def use_builtin(self, request):
        use_builtin = request.param
        if use_builtin == "never":
            pytest.importorskip('alembic')
        return use_builtin

    def _run_sql(self, sql):
        def child():
            with fabs.db.connect(check=False, lock=False) as conn:
                # Run each statement separately, and keep going on
                # OperationalError exceptions. Otherwise, the 'DROP TABLE'
                # statements at the beginning will prevent the whole thing
                # from running.
                for stmt in sql.split(';'):
                    stmt = stmt + ';'
                    try:
                        conn.execute(sa.text(stmt))
                    except sa.exc.OperationalError as exc:
                        self.comment("warning: %s" % exc)
            return 0
        self.run_in_fork(child, init_config=True)

    # Check that the database we're using matches the given sql.
    def _check_db(self, exp):
        db_url = self.config['db']['url']

        sql = self.db_bulk_dump(db_url)

        _check_sql(exp, sql)

    @pytest.mark.parametrize("bad_vers", [0, 50,
                                         fabs.db.VERS_3-1,
                                         fabs.db.VERSION+1])
    def test_init_sql_fail(self, bad_vers):
        err = self.fabsys_stderr('db-init --sql --db-version %d' % bad_vers,
                                 fail=True)
        assert 'Invalid db version' in err

    def test_init_sql(self, db_engine, db_vers):
        args = 'db-init --sql --force'
        if db_vers != fabs.db.VERSION:
            args += ' --db-version %d' % db_vers
        sql = self.fabsys_stdout(args)

        # Check the --sql output.
        _check_sql(_init_sql[db_engine][db_vers], sql)

        # Now actually run the sql, and check that the resulting database is
        # what we expect.
        self._run_sql(sql)
        self._check_db(_dump_sql[db_engine][db_vers])

    def test_init_exec(self, db_engine, db_vers):
        args = 'db-init --exec --force'
        if db_vers != fabs.db.VERSION:
            args += ' --db-version %d' % db_vers
        self.fabsys_comment(args)

        self._check_db(_dump_sql[db_engine][db_vers])

    def test_upgrade_check(self, db_engine, db_vers):
        need_upgrade = True
        if db_vers == fabs.db.VERSION:
            need_upgrade = False

        exp = {
            'fabs_db_upgrade': {
                'upgrade_needed': need_upgrade,
                'upgrade_possible': need_upgrade,
                'db_version': db_vers,
                'latest_version': fabs.db.VERSION,
            }
        }

        self.fabsys_comment('db-init --exec --force --db-version %d' % db_vers)
        got = self.fabsys_json('db-upgrade')

        assert got == exp

    def test_upgrade_sql(self, db_engine, db_vers, use_builtin):
        self.fabsys_comment('db-init --exec --force --db-version %d' % db_vers)

        sql = self.fabsys_stdout('db-upgrade --sql --use-builtin=%s' % use_builtin)
        _check_sql(_upgrade_sql(db_engine, db_vers), sql)

        self._run_sql(sql)

        self._check_db(_dump_sql[db_engine][fabs.db.VERSION])

    def test_upgrade_sql_json(self, db_engine, db_vers, use_builtin):
        self.fabsys_comment('db-init --exec --force --db-version %d' % db_vers)

        need_upgrade = True
        if db_vers == fabs.db.VERSION:
            need_upgrade = False

        exp = {
            'fabs_db_upgrade': {
                'upgrade_needed': need_upgrade,
                'upgrade_possible': need_upgrade,
                'db_version': db_vers,
                'latest_version': fabs.db.VERSION,
            }
        }

        res = self.fabsys_json('db-upgrade --sql --use-builtin=%s' % use_builtin)

        if need_upgrade:
            sql = res['fabs_db_upgrade']['upgrade_sql']
            exp['fabs_db_upgrade']['upgrade_sql'] = sql

        assert exp == res

        if need_upgrade:
            _check_sql(_upgrade_sql(db_engine, db_vers), sql)
            self._run_sql(sql)

        self._check_db(_dump_sql[db_engine][fabs.db.VERSION])

    def test_upgrade(self, db_engine, db_vers, use_builtin):
        need_upgrade = True
        if db_vers == fabs.db.VERSION:
            need_upgrade = False

        self.fabsys_comment('db-init --exec --force --db-version %d' % db_vers)
        out = self.fabsys_stdout('db-upgrade --exec --use-builtin=%s' % use_builtin)

        if need_upgrade:
            exp = "Successfully upgraded FABS db from version %d -> %d" % (
                  db_vers, fabs.db.VERSION)
            assert exp in out
        else:
            assert "Cannot upgrade" in out

        self._check_db(_dump_sql[db_engine][fabs.db.VERSION])

    def test_upgrade_json(self, db_engine, db_vers, use_builtin):
        need_upgrade = True
        if db_vers == fabs.db.VERSION:
            need_upgrade = False

        exp = {
            'fabs_db_upgrade': {
                'upgrade_needed': need_upgrade,
                'upgrade_possible': need_upgrade,
                'db_version': db_vers,
                'latest_version': fabs.db.VERSION,
            }
        }

        self.fabsys_comment('db-init --exec --force --db-version %d' % db_vers)
        res = self.fabsys_json('db-upgrade --exec --use-builtin=%s' % use_builtin)

        if need_upgrade:
            exp['fabs_db_upgrade']['upgrade_success'] = True

        assert exp == res

        self._check_db(_dump_sql[db_engine][fabs.db.VERSION])

_init_sql = {
    'sqlite': {
        fabs.db.VERS_3: """
DROP TABLE restore_reqs;

DROP TABLE dump_jobs;

DROP TABLE links;

DROP TABLE volume_dumps;

DROP TABLE vl_sites;

DROP TABLE vl_addrs;

DROP TABLE vl_entries;

DROP TABLE vl_fileservers;

DROP TABLE backup_runs;

DROP TABLE blob_stores;

DROP TABLE versions;

CREATE TABLE versions (
	version INTEGER NOT NULL
)

;

CREATE TABLE blob_stores (
	id INTEGER NOT NULL,
	uuid VARCHAR(37) NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_blob_stores_uuid ON blob_stores (uuid);

CREATE TABLE backup_runs (
	id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	note TEXT NOT NULL,
	volume VARCHAR(256),
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	start BIGINT NOT NULL,
	end BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id)
)

;

CREATE TABLE vl_fileservers (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	uuid VARCHAR(37),
	PRIMARY KEY (id),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)

;

CREATE TABLE vl_entries (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	name VARCHAR(256) NOT NULL,
	rwid INTEGER NOT NULL,
	roid INTEGER,
	bkid INTEGER,
	PRIMARY KEY (id),
	UNIQUE (br_id, name),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)

;

CREATE TABLE vl_addrs (
	fs_id INTEGER NOT NULL,
	addr VARCHAR(256) NOT NULL,
	FOREIGN KEY(fs_id) REFERENCES vl_fileservers (id)
)

;

CREATE TABLE vl_sites (
	vl_id INTEGER NOT NULL,
	type VARCHAR(2) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	UNIQUE (vl_id, type, server_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id)
)

;

CREATE TABLE volume_dumps (
	id INTEGER NOT NULL,
	vl_id INTEGER NOT NULL,
	hdr_size BIGINT NOT NULL,
	hdr_creation BIGINT NOT NULL,
	hdr_copy BIGINT NOT NULL,
	hdr_backup BIGINT NOT NULL,
	hdr_update BIGINT NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	dump_size BIGINT NOT NULL,
	dump_storid INTEGER NOT NULL,
	dump_spath TEXT NOT NULL,
	dump_checksum VARCHAR(1024) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(dump_storid) REFERENCES blob_stores (id)
)

;
CREATE INDEX ix_volume_dumps_dump_storid ON volume_dumps (dump_storid);
CREATE INDEX idx_dump_spath ON volume_dumps (dump_spath);

CREATE TABLE links (
	vl_id INTEGER,
	path TEXT,
	target TEXT,
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id)
)

;
CREATE UNIQUE INDEX idx_vlid_path ON links (vl_id, path);

CREATE TABLE dump_jobs (
	vl_id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	unchanged BOOLEAN NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	timeout INTEGER NOT NULL,
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	running BOOLEAN NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	UNIQUE (vl_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id),
	CHECK (unchanged IN (0, 1)),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id),
	CHECK (running IN (0, 1))
)

;

CREATE TABLE restore_reqs (
	id INTEGER NOT NULL,
	vd_id INTEGER NOT NULL,
	note TEXT NOT NULL,
	cell VARCHAR(256) NOT NULL,
	username VARCHAR(1024),
	user_path_full TEXT,
	user_path_partial TEXT,
	tmp_dump_storid INTEGER,
	tmp_dump_spath TEXT,
	backend_req_info TEXT,
	stage_volume VARCHAR(256),
	stage_path TEXT,
	stage_time BIGINT,
	next_update BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vd_id) REFERENCES volume_dumps (id),
	FOREIGN KEY(tmp_dump_storid) REFERENCES blob_stores (id)
)

;
CREATE INDEX idx_username ON restore_reqs (username);
DELETE FROM versions;
INSERT INTO versions (version) VALUES (3);
""",
    fabs.db.VERS_4: """
DROP TABLE restore_reqs;

DROP TABLE dump_jobs;

DROP TABLE links;

DROP TABLE volume_dumps;

DROP TABLE vl_sites;

DROP TABLE vl_addrs;

DROP TABLE vl_entries;

DROP TABLE vl_fileservers;

DROP TABLE backup_runs;

DROP TABLE blob_stores;

DROP TABLE versions;

CREATE TABLE versions (
	version INTEGER NOT NULL
)

;

CREATE TABLE blob_stores (
	id INTEGER NOT NULL,
	uuid VARCHAR(37) NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_blob_stores_uuid ON blob_stores (uuid);

CREATE TABLE backup_runs (
	id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	note TEXT NOT NULL,
	volume VARCHAR(256),
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	start BIGINT NOT NULL,
	end BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id)
)

;

CREATE TABLE vl_fileservers (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	uuid VARCHAR(37),
	PRIMARY KEY (id),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)

;

CREATE TABLE vl_entries (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	name VARCHAR(256) NOT NULL,
	rwid INTEGER NOT NULL,
	roid INTEGER,
	bkid INTEGER,
	PRIMARY KEY (id),
	UNIQUE (br_id, name),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)

;

CREATE TABLE vl_addrs (
	fs_id INTEGER NOT NULL,
	addr VARCHAR(256) NOT NULL,
	FOREIGN KEY(fs_id) REFERENCES vl_fileservers (id)
)

;

CREATE TABLE vl_sites (
	vl_id INTEGER NOT NULL,
	type VARCHAR(2) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	UNIQUE (vl_id, type, server_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id)
)

;

CREATE TABLE volume_dumps (
	id INTEGER NOT NULL,
	vl_id INTEGER NOT NULL,
	hdr_size BIGINT NOT NULL,
	hdr_creation BIGINT NOT NULL,
	hdr_copy BIGINT NOT NULL,
	hdr_backup BIGINT NOT NULL,
	hdr_update BIGINT NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	dump_size BIGINT NOT NULL,
	dump_storid INTEGER NOT NULL,
	dump_spath TEXT NOT NULL,
	dump_checksum VARCHAR(1024) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(dump_storid) REFERENCES blob_stores (id)
)

;
CREATE INDEX ix_volume_dumps_dump_storid ON volume_dumps (dump_storid);
CREATE INDEX idx_dump_spath ON volume_dumps (dump_spath);

CREATE TABLE links (
	vl_id INTEGER NOT NULL,
	path TEXT NOT NULL,
	target TEXT NOT NULL,
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id)
)

;
CREATE UNIQUE INDEX idx_vlid_path ON links (vl_id, path);

CREATE TABLE dump_jobs (
	vl_id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	unchanged BOOLEAN NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	timeout INTEGER NOT NULL,
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	running BOOLEAN NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	skip_reason VARCHAR(256),
	rw_target_status VARCHAR(256),
	UNIQUE (vl_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id),
	CHECK (unchanged IN (0, 1)),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id),
	CHECK (running IN (0, 1))
)

;

CREATE TABLE restore_reqs (
	id INTEGER NOT NULL,
	vd_id INTEGER NOT NULL,
	note TEXT NOT NULL,
	cell VARCHAR(256) NOT NULL,
	username VARCHAR(1024),
	user_path_full TEXT,
	user_path_partial TEXT,
	tmp_dump_storid INTEGER,
	tmp_dump_spath TEXT,
	backend_req_info TEXT,
	stage_volume VARCHAR(256),
	stage_path TEXT,
	stage_time BIGINT,
	next_update BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vd_id) REFERENCES volume_dumps (id),
	FOREIGN KEY(tmp_dump_storid) REFERENCES blob_stores (id)
)

;
CREATE INDEX idx_username ON restore_reqs (username);
DELETE FROM versions;
INSERT INTO versions (version) VALUES (4);
""",
    fabs.db.VERS_5: """
DROP TABLE restore_reqs;

DROP TABLE dump_jobs;

DROP TABLE links;

DROP TABLE volume_dumps;

DROP TABLE vl_sites;

DROP TABLE vl_addrs;

DROP TABLE vl_entries;

DROP TABLE vl_fileservers;

DROP TABLE backup_runs;

DROP TABLE blob_stores;

DROP TABLE versions;

CREATE TABLE versions (
	version INTEGER NOT NULL
)

;

CREATE TABLE blob_stores (
	id INTEGER NOT NULL,
	uuid VARCHAR(37) NOT NULL,
	PRIMARY KEY (id)
)

;
CREATE INDEX ix_blob_stores_uuid ON blob_stores (uuid);

CREATE TABLE backup_runs (
	id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	note TEXT NOT NULL,
	volume VARCHAR(256),
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	start BIGINT NOT NULL,
	end BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id)
)

;
CREATE INDEX ix_backup_runs_state ON backup_runs (state);

CREATE TABLE vl_fileservers (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	uuid VARCHAR(37),
	PRIMARY KEY (id),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)

;

CREATE INDEX ix_vl_fileservers_br_id ON vl_fileservers (br_id);

CREATE TABLE vl_entries (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	name VARCHAR(256) NOT NULL,
	rwid INTEGER NOT NULL,
	roid INTEGER,
	bkid INTEGER,
	PRIMARY KEY (id),
	UNIQUE (br_id, name),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)

;

CREATE INDEX ix_vl_entries_br_id ON vl_entries (br_id);
CREATE INDEX ix_vl_entries_rwid ON vl_entries (rwid);

CREATE TABLE vl_addrs (
	fs_id INTEGER NOT NULL,
	addr VARCHAR(256) NOT NULL,
	FOREIGN KEY(fs_id) REFERENCES vl_fileservers (id)
)

;

CREATE INDEX ix_vl_addrs_fs_id ON vl_addrs (fs_id);

CREATE TABLE vl_sites (
	vl_id INTEGER NOT NULL,
	type VARCHAR(2) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	UNIQUE (vl_id, type, server_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id)
)

;

CREATE INDEX ix_vl_sites_server_id ON vl_sites (server_id);

CREATE TABLE volume_dumps (
	id INTEGER NOT NULL,
	vl_id INTEGER NOT NULL,
	hdr_size BIGINT NOT NULL,
	hdr_creation BIGINT NOT NULL,
	hdr_copy BIGINT NOT NULL,
	hdr_backup BIGINT NOT NULL,
	hdr_update BIGINT NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	dump_size BIGINT NOT NULL,
	dump_storid INTEGER NOT NULL,
	dump_spath TEXT NOT NULL,
	dump_checksum VARCHAR(1024) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(dump_storid) REFERENCES blob_stores (id)
)

;
CREATE INDEX ix_volume_dumps_hdr_creation ON volume_dumps (hdr_creation);
CREATE INDEX idx_dump_spath ON volume_dumps (dump_spath);
CREATE INDEX ix_volume_dumps_dump_storid ON volume_dumps (dump_storid);
CREATE INDEX ix_volume_dumps_vl_id ON volume_dumps (vl_id);
CREATE TABLE links (
	vl_id INTEGER NOT NULL,
	path TEXT NOT NULL,
	target TEXT NOT NULL,
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id)
)

;
CREATE UNIQUE INDEX idx_vlid_path ON links (vl_id, path);

CREATE TABLE dump_jobs (
	vl_id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	unchanged BOOLEAN NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	timeout INTEGER NOT NULL,
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	running BOOLEAN NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	skip_reason VARCHAR(256),
	rw_target_status VARCHAR(256),
	UNIQUE (vl_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id),
	CHECK (unchanged IN (0, 1)),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id),
	CHECK (running IN (0, 1))
)

;
CREATE INDEX ix_dump_jobs_cell_state ON dump_jobs (cell, state);

CREATE TABLE restore_reqs (
	id INTEGER NOT NULL,
	vd_id INTEGER NOT NULL,
	note TEXT NOT NULL,
	cell VARCHAR(256) NOT NULL,
	username VARCHAR(1024),
	user_path_full TEXT,
	user_path_partial TEXT,
	tmp_dump_storid INTEGER,
	tmp_dump_spath TEXT,
	backend_req_info TEXT,
	stage_volume VARCHAR(256),
	stage_path TEXT,
	stage_time BIGINT,
	next_update BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vd_id) REFERENCES volume_dumps (id),
	FOREIGN KEY(tmp_dump_storid) REFERENCES blob_stores (id)
)

;
CREATE INDEX ix_restore_reqs_vd_id ON restore_reqs (vd_id);
CREATE INDEX idx_username ON restore_reqs (username);
CREATE INDEX ix_restore_reqs_state ON restore_reqs (state);
DELETE FROM versions;
INSERT INTO versions (version) VALUES (5);
""",
    },
    'mysql': {
        fabs.db.VERS_3: """
DROP TABLE restore_reqs;
DROP TABLE dump_jobs;
DROP TABLE links;
DROP TABLE volume_dumps;
DROP TABLE vl_sites;
DROP TABLE vl_addrs;
DROP TABLE vl_entries;
DROP TABLE vl_fileservers;
DROP TABLE backup_runs;
DROP TABLE blob_stores;
DROP TABLE versions;
CREATE TABLE versions (
version INTEGER NOT NULL
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE TABLE blob_stores (
id BIGINT NOT NULL AUTO_INCREMENT,
uuid VARCHAR(37) NOT NULL,
PRIMARY KEY (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE INDEX ix_blob_stores_uuid ON blob_stores (uuid);
CREATE TABLE backup_runs (
id BIGINT NOT NULL AUTO_INCREMENT,
cell VARCHAR(256) NOT NULL,
note TEXT NOT NULL,
volume VARCHAR(256),
injected_blob_storid BIGINT,
injected_blob_spath TEXT,
injected_blob_checksum VARCHAR(1024),
start BIGINT NOT NULL,
end BIGINT NOT NULL,
state VARCHAR(256) NOT NULL,
state_last VARCHAR(256),
dv INTEGER NOT NULL,
state_descr TEXT NOT NULL,
state_source TEXT NOT NULL,
errors INTEGER NOT NULL DEFAULT '0',
PRIMARY KEY (id),
FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE TABLE vl_fileservers (
id BIGINT NOT NULL AUTO_INCREMENT,
br_id BIGINT NOT NULL,
uuid VARCHAR(37),
PRIMARY KEY (id),
FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE TABLE vl_entries (
id BIGINT NOT NULL AUTO_INCREMENT,
br_id BIGINT NOT NULL,
name VARCHAR(256) NOT NULL,
rwid INTEGER NOT NULL,
roid INTEGER,
bkid INTEGER,
PRIMARY KEY (id),
UNIQUE (br_id,
name),
FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE TABLE vl_addrs (
fs_id BIGINT NOT NULL,
addr VARCHAR(256) NOT NULL,
FOREIGN KEY(fs_id) REFERENCES vl_fileservers (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE TABLE vl_sites (
vl_id BIGINT NOT NULL,
type VARCHAR(2) NOT NULL,
server_id BIGINT NOT NULL,
`partition` VARCHAR(2) NOT NULL,
UNIQUE (vl_id,
type,
server_id),
FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
FOREIGN KEY(server_id) REFERENCES vl_fileservers (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE TABLE volume_dumps (
id BIGINT NOT NULL AUTO_INCREMENT,
vl_id BIGINT NOT NULL,
hdr_size BIGINT NOT NULL,
hdr_creation BIGINT NOT NULL,
hdr_copy BIGINT NOT NULL,
hdr_backup BIGINT NOT NULL,
hdr_update BIGINT NOT NULL,
incr_timestamp BIGINT NOT NULL,
dump_size BIGINT NOT NULL,
dump_storid BIGINT NOT NULL,
dump_spath TEXT NOT NULL,
dump_checksum VARCHAR(1024) NOT NULL,
PRIMARY KEY (id),
FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
FOREIGN KEY(dump_storid) REFERENCES blob_stores (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE INDEX idx_dump_spath ON volume_dumps (dump_spath(766));
CREATE INDEX ix_volume_dumps_dump_storid ON volume_dumps (dump_storid);
CREATE TABLE links (
vl_id BIGINT,
path TEXT,
target TEXT,
FOREIGN KEY(vl_id) REFERENCES vl_entries (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE UNIQUE INDEX idx_vlid_path ON links (vl_id,
path(766));
CREATE TABLE dump_jobs (
vl_id BIGINT NOT NULL,
cell VARCHAR(256) NOT NULL,
server_id BIGINT NOT NULL,
`partition` VARCHAR(2) NOT NULL,
unchanged BOOL NOT NULL,
incr_timestamp BIGINT NOT NULL,
ctime BIGINT NOT NULL,
mtime BIGINT NOT NULL,
timeout INTEGER NOT NULL,
injected_blob_storid BIGINT,
injected_blob_spath TEXT,
injected_blob_checksum VARCHAR(1024),
running BOOL NOT NULL,
state VARCHAR(256) NOT NULL,
state_last VARCHAR(256),
dv INTEGER NOT NULL,
state_descr TEXT NOT NULL,
state_source TEXT NOT NULL,
errors INTEGER NOT NULL DEFAULT '0',
UNIQUE (vl_id),
FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
FOREIGN KEY(server_id) REFERENCES vl_fileservers (id),
CHECK (unchanged IN (0,
1)),
FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id),
CHECK (running IN (0,
1))
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE TABLE restore_reqs (
id BIGINT NOT NULL AUTO_INCREMENT,
vd_id BIGINT NOT NULL,
note TEXT NOT NULL,
cell VARCHAR(256) NOT NULL,
username VARCHAR(1024),
user_path_full TEXT,
user_path_partial TEXT,
tmp_dump_storid BIGINT,
tmp_dump_spath TEXT,
backend_req_info TEXT,
stage_volume VARCHAR(256),
stage_path TEXT,
stage_time BIGINT,
next_update BIGINT NOT NULL,
ctime BIGINT NOT NULL,
mtime BIGINT NOT NULL,
state VARCHAR(256) NOT NULL,
state_last VARCHAR(256),
dv INTEGER NOT NULL,
state_descr TEXT NOT NULL,
state_source TEXT NOT NULL,
errors INTEGER NOT NULL DEFAULT '0',
PRIMARY KEY (id),
FOREIGN KEY(vd_id) REFERENCES volume_dumps (id),
FOREIGN KEY(tmp_dump_storid) REFERENCES blob_stores (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE INDEX idx_username ON restore_reqs (username(766));
DELETE FROM versions;
INSERT INTO versions (version) VALUES (3);
""",
        fabs.db.VERS_4: """
DROP TABLE restore_reqs;
DROP TABLE dump_jobs;
DROP TABLE links;
DROP TABLE volume_dumps;
DROP TABLE vl_sites;
DROP TABLE vl_addrs;
DROP TABLE vl_entries;
DROP TABLE vl_fileservers;
DROP TABLE backup_runs;
DROP TABLE blob_stores;
DROP TABLE versions;
CREATE TABLE versions (
version INTEGER NOT NULL
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE TABLE blob_stores (
id BIGINT NOT NULL AUTO_INCREMENT,
uuid VARCHAR(37) NOT NULL,
PRIMARY KEY (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE INDEX ix_blob_stores_uuid ON blob_stores (uuid);
CREATE TABLE backup_runs (
id BIGINT NOT NULL AUTO_INCREMENT,
cell VARCHAR(256) NOT NULL,
note TEXT NOT NULL,
volume VARCHAR(256),
injected_blob_storid BIGINT,
injected_blob_spath TEXT,
injected_blob_checksum VARCHAR(1024),
start BIGINT NOT NULL,
end BIGINT NOT NULL,
state VARCHAR(256) NOT NULL,
state_last VARCHAR(256),
dv INTEGER NOT NULL,
state_descr TEXT NOT NULL,
state_source TEXT NOT NULL,
errors INTEGER NOT NULL DEFAULT '0',
PRIMARY KEY (id),
FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE TABLE vl_fileservers (
id BIGINT NOT NULL AUTO_INCREMENT,
br_id BIGINT NOT NULL,
uuid VARCHAR(37),
PRIMARY KEY (id),
FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE TABLE vl_entries (
id BIGINT NOT NULL AUTO_INCREMENT,
br_id BIGINT NOT NULL,
name VARCHAR(256) NOT NULL,
rwid INTEGER NOT NULL,
roid INTEGER,
bkid INTEGER,
PRIMARY KEY (id),
UNIQUE (br_id,
name),
FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE TABLE vl_addrs (
fs_id BIGINT NOT NULL,
addr VARCHAR(256) NOT NULL,
FOREIGN KEY(fs_id) REFERENCES vl_fileservers (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE TABLE vl_sites (
vl_id BIGINT NOT NULL,
type VARCHAR(2) NOT NULL,
server_id BIGINT NOT NULL,
`partition` VARCHAR(2) NOT NULL,
UNIQUE (vl_id,
type,
server_id),
FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
FOREIGN KEY(server_id) REFERENCES vl_fileservers (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE TABLE volume_dumps (
id BIGINT NOT NULL AUTO_INCREMENT,
vl_id BIGINT NOT NULL,
hdr_size BIGINT NOT NULL,
hdr_creation BIGINT NOT NULL,
hdr_copy BIGINT NOT NULL,
hdr_backup BIGINT NOT NULL,
hdr_update BIGINT NOT NULL,
incr_timestamp BIGINT NOT NULL,
dump_size BIGINT NOT NULL,
dump_storid BIGINT NOT NULL,
dump_spath TEXT NOT NULL,
dump_checksum VARCHAR(1024) NOT NULL,
PRIMARY KEY (id),
FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
FOREIGN KEY(dump_storid) REFERENCES blob_stores (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE INDEX ix_volume_dumps_dump_storid ON volume_dumps (dump_storid);
CREATE INDEX idx_dump_spath ON volume_dumps (dump_spath(766));
CREATE TABLE links (
vl_id BIGINT NOT NULL,
path TEXT NOT NULL,
target TEXT NOT NULL,
FOREIGN KEY(vl_id) REFERENCES vl_entries (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE UNIQUE INDEX idx_vlid_path ON links (vl_id,
path(766));
CREATE TABLE dump_jobs (
vl_id BIGINT NOT NULL,
cell VARCHAR(256) NOT NULL,
server_id BIGINT NOT NULL,
`partition` VARCHAR(2) NOT NULL,
unchanged BOOL NOT NULL,
incr_timestamp BIGINT NOT NULL,
ctime BIGINT NOT NULL,
mtime BIGINT NOT NULL,
timeout INTEGER NOT NULL,
injected_blob_storid BIGINT,
injected_blob_spath TEXT,
injected_blob_checksum VARCHAR(1024),
running BOOL NOT NULL,
state VARCHAR(256) NOT NULL,
state_last VARCHAR(256),
dv INTEGER NOT NULL,
state_descr TEXT NOT NULL,
state_source TEXT NOT NULL,
errors INTEGER NOT NULL DEFAULT '0',
skip_reason VARCHAR(256),
rw_target_status VARCHAR(256),
UNIQUE (vl_id),
FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
FOREIGN KEY(server_id) REFERENCES vl_fileservers (id),
CHECK (unchanged IN (0,
1)),
FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id),
CHECK (running IN (0,
1))
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE TABLE restore_reqs (
id BIGINT NOT NULL AUTO_INCREMENT,
vd_id BIGINT NOT NULL,
note TEXT NOT NULL,
cell VARCHAR(256) NOT NULL,
username VARCHAR(1024),
user_path_full TEXT,
user_path_partial TEXT,
tmp_dump_storid BIGINT,
tmp_dump_spath TEXT,
backend_req_info TEXT,
stage_volume VARCHAR(256),
stage_path TEXT,
stage_time BIGINT,
next_update BIGINT NOT NULL,
ctime BIGINT NOT NULL,
mtime BIGINT NOT NULL,
state VARCHAR(256) NOT NULL,
state_last VARCHAR(256),
dv INTEGER NOT NULL,
state_descr TEXT NOT NULL,
state_source TEXT NOT NULL,
errors INTEGER NOT NULL DEFAULT '0',
PRIMARY KEY (id),
FOREIGN KEY(vd_id) REFERENCES volume_dumps (id),
FOREIGN KEY(tmp_dump_storid) REFERENCES blob_stores (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE INDEX idx_username ON restore_reqs (username(766));
DELETE FROM versions;
INSERT INTO versions (version) VALUES (4);
""",
        fabs.db.VERS_5: """
DROP TABLE restore_reqs;
DROP TABLE dump_jobs;
DROP TABLE links;
DROP TABLE volume_dumps;
DROP TABLE vl_sites;
DROP TABLE vl_addrs;
DROP TABLE vl_entries;
DROP TABLE vl_fileservers;
DROP TABLE backup_runs;
DROP TABLE blob_stores;
DROP TABLE versions;
CREATE TABLE versions (
version INTEGER NOT NULL
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE TABLE blob_stores (
id BIGINT NOT NULL AUTO_INCREMENT,
uuid VARCHAR(37) NOT NULL,
PRIMARY KEY (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE INDEX ix_blob_stores_uuid ON blob_stores (uuid);
CREATE TABLE backup_runs (
id BIGINT NOT NULL AUTO_INCREMENT,
cell VARCHAR(256) NOT NULL,
note TEXT NOT NULL,
volume VARCHAR(256),
injected_blob_storid BIGINT,
injected_blob_spath TEXT,
injected_blob_checksum VARCHAR(1024),
start BIGINT NOT NULL,
end BIGINT NOT NULL,
state VARCHAR(256) NOT NULL,
state_last VARCHAR(256),
dv INTEGER NOT NULL,
state_descr TEXT NOT NULL,
state_source TEXT NOT NULL,
errors INTEGER NOT NULL DEFAULT '0',
PRIMARY KEY (id),
FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE INDEX ix_backup_runs_state ON backup_runs (state);
CREATE TABLE vl_fileservers (
id BIGINT NOT NULL AUTO_INCREMENT,
br_id BIGINT NOT NULL,
uuid VARCHAR(37),
PRIMARY KEY (id),
FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE INDEX ix_vl_fileservers_br_id ON vl_fileservers (br_id);
CREATE TABLE vl_entries (
id BIGINT NOT NULL AUTO_INCREMENT,
br_id BIGINT NOT NULL,
name VARCHAR(256) NOT NULL,
rwid INTEGER NOT NULL,
roid INTEGER,
bkid INTEGER,
PRIMARY KEY (id),
UNIQUE (br_id,
name),
FOREIGN KEY(br_id) REFERENCES backup_runs (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE INDEX ix_vl_entries_br_id ON vl_entries (br_id);
CREATE INDEX ix_vl_entries_rwid ON vl_entries (rwid);
CREATE TABLE vl_addrs (
fs_id BIGINT NOT NULL,
addr VARCHAR(256) NOT NULL,
FOREIGN KEY(fs_id) REFERENCES vl_fileservers (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE INDEX ix_vl_addrs_fs_id ON vl_addrs (fs_id);
CREATE TABLE vl_sites (
vl_id BIGINT NOT NULL,
type VARCHAR(2) NOT NULL,
server_id BIGINT NOT NULL,
`partition` VARCHAR(2) NOT NULL,
UNIQUE (vl_id,
type,
server_id),
FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
FOREIGN KEY(server_id) REFERENCES vl_fileservers (id)
)ENGINE=innodb CHARSET=utf8mb4 ROW_FORMAT=dynamic
;
CREATE INDEX ix_vl_sites_server_id ON vl_sites (server_id);
CREATE TABLE volume_dumps (
id BIGINT NOT NULL AUTO_INCREMENT,
vl_id BIGINT NOT NULL,
hdr_size BIGINT NOT NULL,
hdr_creation BIGINT NOT NULL,
hdr_copy BIGINT NOT NULL,
hdr_backup BIGINT NOT NULL,
hdr_update BIGINT NOT NULL,
incr_timestamp BIGINT NOT NULL,
dump_size BIGINT NOT NULL,
dump_storid BIGINT NOT NULL,
dump_spath TEXT NOT NULL,
dump_checksum VARCHAR(1024) NOT NULL,
PRIMARY KEY (id),
FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
FOREIGN KEY(dump_storid) REFERENCES blob_stores (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE INDEX ix_volume_dumps_hdr_creation ON volume_dumps (hdr_creation);
CREATE INDEX idx_dump_spath ON volume_dumps (dump_spath(766));
CREATE INDEX ix_volume_dumps_vl_id ON volume_dumps (vl_id);
CREATE INDEX ix_volume_dumps_dump_storid ON volume_dumps (dump_storid);
CREATE TABLE links (
vl_id BIGINT NOT NULL,
path TEXT NOT NULL,
target TEXT NOT NULL,
FOREIGN KEY(vl_id) REFERENCES vl_entries (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE UNIQUE INDEX idx_vlid_path ON links (vl_id,
path(766));
CREATE TABLE dump_jobs (
vl_id BIGINT NOT NULL,
cell VARCHAR(256) NOT NULL,
server_id BIGINT NOT NULL,
`partition` VARCHAR(2) NOT NULL,
unchanged BOOL NOT NULL,
incr_timestamp BIGINT NOT NULL,
ctime BIGINT NOT NULL,
mtime BIGINT NOT NULL,
timeout INTEGER NOT NULL,
injected_blob_storid BIGINT,
injected_blob_spath TEXT,
injected_blob_checksum VARCHAR(1024),
running BOOL NOT NULL,
state VARCHAR(256) NOT NULL,
state_last VARCHAR(256),
dv INTEGER NOT NULL,
state_descr TEXT NOT NULL,
state_source TEXT NOT NULL,
errors INTEGER NOT NULL DEFAULT '0',
skip_reason VARCHAR(256),
rw_target_status VARCHAR(256),
UNIQUE (vl_id),
FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
FOREIGN KEY(server_id) REFERENCES vl_fileservers (id),
CHECK (unchanged IN (0,
1)),
FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id),
CHECK (running IN (0,
1))
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE INDEX ix_dump_jobs_cell_state ON dump_jobs (cell, state);
CREATE TABLE restore_reqs (
id BIGINT NOT NULL AUTO_INCREMENT,
vd_id BIGINT NOT NULL,
note TEXT NOT NULL,
cell VARCHAR(256) NOT NULL,
username VARCHAR(1024),
user_path_full TEXT,
user_path_partial TEXT,
tmp_dump_storid BIGINT,
tmp_dump_spath TEXT,
backend_req_info TEXT,
stage_volume VARCHAR(256),
stage_path TEXT,
stage_time BIGINT,
next_update BIGINT NOT NULL,
ctime BIGINT NOT NULL,
mtime BIGINT NOT NULL,
state VARCHAR(256) NOT NULL,
state_last VARCHAR(256),
dv INTEGER NOT NULL,
state_descr TEXT NOT NULL,
state_source TEXT NOT NULL,
errors INTEGER NOT NULL DEFAULT '0',
PRIMARY KEY (id),
FOREIGN KEY(vd_id) REFERENCES volume_dumps (id),
FOREIGN KEY(tmp_dump_storid) REFERENCES blob_stores (id)
)CHARSET=utf8mb4 ENGINE=innodb ROW_FORMAT=dynamic
;
CREATE INDEX ix_restore_reqs_vd_id ON restore_reqs (vd_id);
CREATE INDEX idx_username ON restore_reqs (username(766));
CREATE INDEX ix_restore_reqs_state ON restore_reqs (state);
DELETE FROM versions;
INSERT INTO versions (version) VALUES (5);
""",
    },
}

_dump_sql = {
    'sqlite': {
        fabs.db.VERS_3: """PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE versions (
	version INTEGER NOT NULL
);
INSERT INTO versions VALUES(3);
CREATE TABLE blob_stores (
	id INTEGER NOT NULL,
	uuid VARCHAR(37) NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE backup_runs (
	id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	note TEXT NOT NULL,
	volume VARCHAR(256),
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	start BIGINT NOT NULL,
	end BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id)
);
CREATE TABLE vl_fileservers (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	uuid VARCHAR(37),
	PRIMARY KEY (id),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
);
CREATE TABLE vl_entries (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	name VARCHAR(256) NOT NULL,
	rwid INTEGER NOT NULL,
	roid INTEGER,
	bkid INTEGER,
	PRIMARY KEY (id),
	UNIQUE (br_id, name),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
);
CREATE TABLE vl_addrs (
	fs_id INTEGER NOT NULL,
	addr VARCHAR(256) NOT NULL,
	FOREIGN KEY(fs_id) REFERENCES vl_fileservers (id)
);
CREATE TABLE vl_sites (
	vl_id INTEGER NOT NULL,
	type VARCHAR(2) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	UNIQUE (vl_id, type, server_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id)
);
CREATE TABLE volume_dumps (
	id INTEGER NOT NULL,
	vl_id INTEGER NOT NULL,
	hdr_size BIGINT NOT NULL,
	hdr_creation BIGINT NOT NULL,
	hdr_copy BIGINT NOT NULL,
	hdr_backup BIGINT NOT NULL,
	hdr_update BIGINT NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	dump_size BIGINT NOT NULL,
	dump_storid INTEGER NOT NULL,
	dump_spath TEXT NOT NULL,
	dump_checksum VARCHAR(1024) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(dump_storid) REFERENCES blob_stores (id)
);
CREATE TABLE links (
	vl_id INTEGER,
	path TEXT,
	target TEXT,
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id)
);
CREATE TABLE dump_jobs (
	vl_id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	unchanged BOOLEAN NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	timeout INTEGER NOT NULL,
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	running BOOLEAN NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	UNIQUE (vl_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id),
	CHECK (unchanged IN (0, 1)),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id),
	CHECK (running IN (0, 1))
);
CREATE TABLE restore_reqs (
	id INTEGER NOT NULL,
	vd_id INTEGER NOT NULL,
	note TEXT NOT NULL,
	cell VARCHAR(256) NOT NULL,
	username VARCHAR(1024),
	user_path_full TEXT,
	user_path_partial TEXT,
	tmp_dump_storid INTEGER,
	tmp_dump_spath TEXT,
	backend_req_info TEXT,
	stage_volume VARCHAR(256),
	stage_path TEXT,
	stage_time BIGINT,
	next_update BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vd_id) REFERENCES volume_dumps (id),
	FOREIGN KEY(tmp_dump_storid) REFERENCES blob_stores (id)
);
CREATE INDEX ix_blob_stores_uuid ON blob_stores (uuid);
CREATE INDEX ix_volume_dumps_dump_storid ON volume_dumps (dump_storid);
CREATE INDEX idx_dump_spath ON volume_dumps (dump_spath);
CREATE UNIQUE INDEX idx_vlid_path ON links (vl_id, path);
CREATE INDEX idx_username ON restore_reqs (username);
COMMIT;
""",
    fabs.db.VERS_4: """PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE versions (
	version INTEGER NOT NULL
);
INSERT INTO versions VALUES(4);
CREATE TABLE blob_stores (
	id INTEGER NOT NULL,
	uuid VARCHAR(37) NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE backup_runs (
	id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	note TEXT NOT NULL,
	volume VARCHAR(256),
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	start BIGINT NOT NULL,
	end BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id)
);
CREATE TABLE vl_fileservers (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	uuid VARCHAR(37),
	PRIMARY KEY (id),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
);
CREATE TABLE vl_entries (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	name VARCHAR(256) NOT NULL,
	rwid INTEGER NOT NULL,
	roid INTEGER,
	bkid INTEGER,
	PRIMARY KEY (id),
	UNIQUE (br_id, name),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
);
CREATE TABLE vl_addrs (
	fs_id INTEGER NOT NULL,
	addr VARCHAR(256) NOT NULL,
	FOREIGN KEY(fs_id) REFERENCES vl_fileservers (id)
);
CREATE TABLE vl_sites (
	vl_id INTEGER NOT NULL,
	type VARCHAR(2) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	UNIQUE (vl_id, type, server_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id)
);
CREATE TABLE volume_dumps (
	id INTEGER NOT NULL,
	vl_id INTEGER NOT NULL,
	hdr_size BIGINT NOT NULL,
	hdr_creation BIGINT NOT NULL,
	hdr_copy BIGINT NOT NULL,
	hdr_backup BIGINT NOT NULL,
	hdr_update BIGINT NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	dump_size BIGINT NOT NULL,
	dump_storid INTEGER NOT NULL,
	dump_spath TEXT NOT NULL,
	dump_checksum VARCHAR(1024) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(dump_storid) REFERENCES blob_stores (id)
);
CREATE TABLE links (
	vl_id INTEGER NOT NULL,
	path TEXT NOT NULL,
	target TEXT NOT NULL,
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id)
);
CREATE TABLE dump_jobs (
	vl_id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	unchanged BOOLEAN NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	timeout INTEGER NOT NULL,
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	running BOOLEAN NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	skip_reason VARCHAR(256),
	rw_target_status VARCHAR(256),
	UNIQUE (vl_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id),
	CHECK (unchanged IN (0, 1)),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id),
	CHECK (running IN (0, 1))
);
CREATE TABLE restore_reqs (
	id INTEGER NOT NULL,
	vd_id INTEGER NOT NULL,
	note TEXT NOT NULL,
	cell VARCHAR(256) NOT NULL,
	username VARCHAR(1024),
	user_path_full TEXT,
	user_path_partial TEXT,
	tmp_dump_storid INTEGER,
	tmp_dump_spath TEXT,
	backend_req_info TEXT,
	stage_volume VARCHAR(256),
	stage_path TEXT,
	stage_time BIGINT,
	next_update BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vd_id) REFERENCES volume_dumps (id),
	FOREIGN KEY(tmp_dump_storid) REFERENCES blob_stores (id)
);
CREATE INDEX ix_blob_stores_uuid ON blob_stores (uuid);
CREATE INDEX ix_volume_dumps_dump_storid ON volume_dumps (dump_storid);
CREATE INDEX idx_dump_spath ON volume_dumps (dump_spath);
CREATE UNIQUE INDEX idx_vlid_path ON links (vl_id, path);
CREATE INDEX idx_username ON restore_reqs (username);
COMMIT;
""",
    fabs.db.VERS_5: """PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE versions (
	version INTEGER NOT NULL
);
INSERT INTO versions VALUES(5);
CREATE TABLE blob_stores (
	id INTEGER NOT NULL,
	uuid VARCHAR(37) NOT NULL,
	PRIMARY KEY (id)
);
CREATE TABLE backup_runs (
	id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	note TEXT NOT NULL,
	volume VARCHAR(256),
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	start BIGINT NOT NULL,
	end BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id)
);
CREATE TABLE vl_fileservers (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	uuid VARCHAR(37),
	PRIMARY KEY (id),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
);
CREATE TABLE vl_entries (
	id INTEGER NOT NULL,
	br_id INTEGER NOT NULL,
	name VARCHAR(256) NOT NULL,
	rwid INTEGER NOT NULL,
	roid INTEGER,
	bkid INTEGER,
	PRIMARY KEY (id),
	UNIQUE (br_id, name),
	FOREIGN KEY(br_id) REFERENCES backup_runs (id)
);
CREATE TABLE vl_addrs (
	fs_id INTEGER NOT NULL,
	addr VARCHAR(256) NOT NULL,
	FOREIGN KEY(fs_id) REFERENCES vl_fileservers (id)
);
CREATE TABLE vl_sites (
	vl_id INTEGER NOT NULL,
	type VARCHAR(2) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	UNIQUE (vl_id, type, server_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id)
);
CREATE TABLE volume_dumps (
	id INTEGER NOT NULL,
	vl_id INTEGER NOT NULL,
	hdr_size BIGINT NOT NULL,
	hdr_creation BIGINT NOT NULL,
	hdr_copy BIGINT NOT NULL,
	hdr_backup BIGINT NOT NULL,
	hdr_update BIGINT NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	dump_size BIGINT NOT NULL,
	dump_storid INTEGER NOT NULL,
	dump_spath TEXT NOT NULL,
	dump_checksum VARCHAR(1024) NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(dump_storid) REFERENCES blob_stores (id)
);
CREATE TABLE links (
	vl_id INTEGER NOT NULL,
	path TEXT NOT NULL,
	target TEXT NOT NULL,
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id)
);
CREATE TABLE dump_jobs (
	vl_id INTEGER NOT NULL,
	cell VARCHAR(256) NOT NULL,
	server_id INTEGER NOT NULL,
	partition VARCHAR(2) NOT NULL,
	unchanged BOOLEAN NOT NULL,
	incr_timestamp BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	timeout INTEGER NOT NULL,
	injected_blob_storid INTEGER,
	injected_blob_spath TEXT,
	injected_blob_checksum VARCHAR(1024),
	running BOOLEAN NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	skip_reason VARCHAR(256),
	rw_target_status VARCHAR(256),
	UNIQUE (vl_id),
	FOREIGN KEY(vl_id) REFERENCES vl_entries (id),
	FOREIGN KEY(server_id) REFERENCES vl_fileservers (id),
	CHECK (unchanged IN (0, 1)),
	FOREIGN KEY(injected_blob_storid) REFERENCES blob_stores (id),
	CHECK (running IN (0, 1))
);
CREATE TABLE restore_reqs (
	id INTEGER NOT NULL,
	vd_id INTEGER NOT NULL,
	note TEXT NOT NULL,
	cell VARCHAR(256) NOT NULL,
	username VARCHAR(1024),
	user_path_full TEXT,
	user_path_partial TEXT,
	tmp_dump_storid INTEGER,
	tmp_dump_spath TEXT,
	backend_req_info TEXT,
	stage_volume VARCHAR(256),
	stage_path TEXT,
	stage_time BIGINT,
	next_update BIGINT NOT NULL,
	ctime BIGINT NOT NULL,
	mtime BIGINT NOT NULL,
	state VARCHAR(256) NOT NULL,
	state_last VARCHAR(256),
	dv INTEGER NOT NULL,
	state_descr TEXT NOT NULL,
	state_source TEXT NOT NULL,
	errors INTEGER DEFAULT '0' NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(vd_id) REFERENCES volume_dumps (id),
	FOREIGN KEY(tmp_dump_storid) REFERENCES blob_stores (id)
);
CREATE INDEX ix_blob_stores_uuid ON blob_stores (uuid);
CREATE INDEX ix_backup_runs_state ON backup_runs (state);
CREATE INDEX ix_vl_fileservers_br_id ON vl_fileservers (br_id);
CREATE INDEX ix_vl_entries_br_id ON vl_entries (br_id);
CREATE INDEX ix_vl_entries_rwid ON vl_entries (rwid);
CREATE INDEX ix_vl_addrs_fs_id ON vl_addrs (fs_id);
CREATE INDEX ix_vl_sites_server_id ON vl_sites (server_id);
CREATE INDEX ix_volume_dumps_hdr_creation ON volume_dumps (hdr_creation);
CREATE INDEX ix_volume_dumps_vl_id ON volume_dumps (vl_id);
CREATE INDEX ix_volume_dumps_dump_storid ON volume_dumps (dump_storid);
CREATE INDEX idx_dump_spath ON volume_dumps (dump_spath);
CREATE UNIQUE INDEX idx_vlid_path ON links (vl_id, path);
CREATE INDEX ix_dump_jobs_cell_state ON dump_jobs (cell, state);
CREATE INDEX ix_restore_reqs_vd_id ON restore_reqs (vd_id);
CREATE INDEX idx_username ON restore_reqs (username);
CREATE INDEX ix_restore_reqs_state ON restore_reqs (state);
COMMIT;
""",
    },
    'mysql': {
        fabs.db.VERS_3: """
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `backup_runs` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`cell` varchar(256) NOT NULL,
`note` text NOT NULL,
`volume` varchar(256) DEFAULT NULL,
`injected_blob_storid` bigint(20) DEFAULT NULL,
`injected_blob_spath` text DEFAULT NULL,
`injected_blob_checksum` varchar(1024) DEFAULT NULL,
`start` bigint(20) NOT NULL,
`end` bigint(20) NOT NULL,
`state` varchar(256) NOT NULL,
`state_last` varchar(256) DEFAULT NULL,
`dv` int(11) NOT NULL,
`state_descr` text NOT NULL,
`state_source` text NOT NULL,
`errors` int(11) NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `injected_blob_storid` (`injected_blob_storid`),
CONSTRAINT `backup_runs_ibfk_1` FOREIGN KEY (`injected_blob_storid`) REFERENCES `blob_stores` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `blob_stores` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`uuid` varchar(37) NOT NULL,
PRIMARY KEY (`id`),
KEY `ix_blob_stores_uuid` (`uuid`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `dump_jobs` (
`vl_id` bigint(20) NOT NULL,
`cell` varchar(256) NOT NULL,
`server_id` bigint(20) NOT NULL,
`partition` varchar(2) NOT NULL,
`unchanged` tinyint(1) NOT NULL,
`incr_timestamp` bigint(20) NOT NULL,
`ctime` bigint(20) NOT NULL,
`mtime` bigint(20) NOT NULL,
`timeout` int(11) NOT NULL,
`injected_blob_storid` bigint(20) DEFAULT NULL,
`injected_blob_spath` text DEFAULT NULL,
`injected_blob_checksum` varchar(1024) DEFAULT NULL,
`running` tinyint(1) NOT NULL,
`state` varchar(256) NOT NULL,
`state_last` varchar(256) DEFAULT NULL,
`dv` int(11) NOT NULL,
`state_descr` text NOT NULL,
`state_source` text NOT NULL,
`errors` int(11) NOT NULL DEFAULT 0,
UNIQUE KEY `vl_id` (`vl_id`),
KEY `server_id` (`server_id`),
KEY `injected_blob_storid` (`injected_blob_storid`),
CONSTRAINT `dump_jobs_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`),
CONSTRAINT `dump_jobs_ibfk_2` FOREIGN KEY (`server_id`) REFERENCES `vl_fileservers` (`id`),
CONSTRAINT `dump_jobs_ibfk_3` FOREIGN KEY (`injected_blob_storid`) REFERENCES `blob_stores` (`id`),
CONSTRAINT `CONSTRAINT_1` CHECK (`unchanged` in (0,
1)),
CONSTRAINT `CONSTRAINT_2` CHECK (`running` in (0,
1))
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `links` (
`vl_id` bigint(20) DEFAULT NULL,
`path` text DEFAULT NULL,
`target` text DEFAULT NULL,
UNIQUE KEY `idx_vlid_path` (`vl_id`,
`path`(766)),
CONSTRAINT `links_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `restore_reqs` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`vd_id` bigint(20) NOT NULL,
`note` text NOT NULL,
`cell` varchar(256) NOT NULL,
`username` varchar(1024) DEFAULT NULL,
`user_path_full` text DEFAULT NULL,
`user_path_partial` text DEFAULT NULL,
`tmp_dump_storid` bigint(20) DEFAULT NULL,
`tmp_dump_spath` text DEFAULT NULL,
`backend_req_info` text DEFAULT NULL,
`stage_volume` varchar(256) DEFAULT NULL,
`stage_path` text DEFAULT NULL,
`stage_time` bigint(20) DEFAULT NULL,
`next_update` bigint(20) NOT NULL,
`ctime` bigint(20) NOT NULL,
`mtime` bigint(20) NOT NULL,
`state` varchar(256) NOT NULL,
`state_last` varchar(256) DEFAULT NULL,
`dv` int(11) NOT NULL,
`state_descr` text NOT NULL,
`state_source` text NOT NULL,
`errors` int(11) NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `vd_id` (`vd_id`),
KEY `tmp_dump_storid` (`tmp_dump_storid`),
KEY `idx_username` (`username`(766)),
CONSTRAINT `restore_reqs_ibfk_1` FOREIGN KEY (`vd_id`) REFERENCES `volume_dumps` (`id`),
CONSTRAINT `restore_reqs_ibfk_2` FOREIGN KEY (`tmp_dump_storid`) REFERENCES `blob_stores` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `versions` (
`version` int(11) NOT NULL
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
INSERT INTO `versions` VALUES (3);
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_addrs` (
`fs_id` bigint(20) NOT NULL,
`addr` varchar(256) NOT NULL,
KEY `fs_id` (`fs_id`),
CONSTRAINT `vl_addrs_ibfk_1` FOREIGN KEY (`fs_id`) REFERENCES `vl_fileservers` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_entries` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`br_id` bigint(20) NOT NULL,
`name` varchar(256) NOT NULL,
`rwid` int(11) NOT NULL,
`roid` int(11) DEFAULT NULL,
`bkid` int(11) DEFAULT NULL,
PRIMARY KEY (`id`),
UNIQUE KEY `br_id` (`br_id`,
`name`),
CONSTRAINT `vl_entries_ibfk_1` FOREIGN KEY (`br_id`) REFERENCES `backup_runs` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_fileservers` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`br_id` bigint(20) NOT NULL,
`uuid` varchar(37) DEFAULT NULL,
PRIMARY KEY (`id`),
KEY `br_id` (`br_id`),
CONSTRAINT `vl_fileservers_ibfk_1` FOREIGN KEY (`br_id`) REFERENCES `backup_runs` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_sites` (
`vl_id` bigint(20) NOT NULL,
`type` varchar(2) NOT NULL,
`server_id` bigint(20) NOT NULL,
`partition` varchar(2) NOT NULL,
UNIQUE KEY `vl_id` (`vl_id`,
`type`,
`server_id`),
KEY `server_id` (`server_id`),
CONSTRAINT `vl_sites_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`),
CONSTRAINT `vl_sites_ibfk_2` FOREIGN KEY (`server_id`) REFERENCES `vl_fileservers` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `volume_dumps` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`vl_id` bigint(20) NOT NULL,
`hdr_size` bigint(20) NOT NULL,
`hdr_creation` bigint(20) NOT NULL,
`hdr_copy` bigint(20) NOT NULL,
`hdr_backup` bigint(20) NOT NULL,
`hdr_update` bigint(20) NOT NULL,
`incr_timestamp` bigint(20) NOT NULL,
`dump_size` bigint(20) NOT NULL,
`dump_storid` bigint(20) NOT NULL,
`dump_spath` text NOT NULL,
`dump_checksum` varchar(1024) NOT NULL,
PRIMARY KEY (`id`),
KEY `vl_id` (`vl_id`),
KEY `ix_volume_dumps_dump_storid` (`dump_storid`),
KEY `idx_dump_spath` (`dump_spath`(766)),
CONSTRAINT `volume_dumps_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`),
CONSTRAINT `volume_dumps_ibfk_2` FOREIGN KEY (`dump_storid`) REFERENCES `blob_stores` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
""",
        fabs.db.VERS_4: """
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `backup_runs` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`cell` varchar(256) NOT NULL,
`note` text NOT NULL,
`volume` varchar(256) DEFAULT NULL,
`injected_blob_storid` bigint(20) DEFAULT NULL,
`injected_blob_spath` text DEFAULT NULL,
`injected_blob_checksum` varchar(1024) DEFAULT NULL,
`start` bigint(20) NOT NULL,
`end` bigint(20) NOT NULL,
`state` varchar(256) NOT NULL,
`state_last` varchar(256) DEFAULT NULL,
`dv` int(11) NOT NULL,
`state_descr` text NOT NULL,
`state_source` text NOT NULL,
`errors` int(11) NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `injected_blob_storid` (`injected_blob_storid`),
CONSTRAINT `backup_runs_ibfk_1` FOREIGN KEY (`injected_blob_storid`) REFERENCES `blob_stores` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `blob_stores` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`uuid` varchar(37) NOT NULL,
PRIMARY KEY (`id`),
KEY `ix_blob_stores_uuid` (`uuid`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `dump_jobs` (
`vl_id` bigint(20) NOT NULL,
`cell` varchar(256) NOT NULL,
`server_id` bigint(20) NOT NULL,
`partition` varchar(2) NOT NULL,
`unchanged` tinyint(1) NOT NULL,
`incr_timestamp` bigint(20) NOT NULL,
`ctime` bigint(20) NOT NULL,
`mtime` bigint(20) NOT NULL,
`timeout` int(11) NOT NULL,
`injected_blob_storid` bigint(20) DEFAULT NULL,
`injected_blob_spath` text DEFAULT NULL,
`injected_blob_checksum` varchar(1024) DEFAULT NULL,
`running` tinyint(1) NOT NULL,
`state` varchar(256) NOT NULL,
`state_last` varchar(256) DEFAULT NULL,
`dv` int(11) NOT NULL,
`state_descr` text NOT NULL,
`state_source` text NOT NULL,
`errors` int(11) NOT NULL DEFAULT 0,
`skip_reason` varchar(256) DEFAULT NULL,
`rw_target_status` varchar(256) DEFAULT NULL,
UNIQUE KEY `vl_id` (`vl_id`),
KEY `server_id` (`server_id`),
KEY `injected_blob_storid` (`injected_blob_storid`),
CONSTRAINT `dump_jobs_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`),
CONSTRAINT `dump_jobs_ibfk_2` FOREIGN KEY (`server_id`) REFERENCES `vl_fileservers` (`id`),
CONSTRAINT `dump_jobs_ibfk_3` FOREIGN KEY (`injected_blob_storid`) REFERENCES `blob_stores` (`id`),
CONSTRAINT `CONSTRAINT_1` CHECK (`unchanged` in (0,
1)),
CONSTRAINT `CONSTRAINT_2` CHECK (`running` in (0,
1))
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `links` (
`vl_id` bigint(20) NOT NULL,
`path` text NOT NULL,
`target` text NOT NULL,
UNIQUE KEY `idx_vlid_path` (`vl_id`,
`path`(766)),
CONSTRAINT `links_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `restore_reqs` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`vd_id` bigint(20) NOT NULL,
`note` text NOT NULL,
`cell` varchar(256) NOT NULL,
`username` varchar(1024) DEFAULT NULL,
`user_path_full` text DEFAULT NULL,
`user_path_partial` text DEFAULT NULL,
`tmp_dump_storid` bigint(20) DEFAULT NULL,
`tmp_dump_spath` text DEFAULT NULL,
`backend_req_info` text DEFAULT NULL,
`stage_volume` varchar(256) DEFAULT NULL,
`stage_path` text DEFAULT NULL,
`stage_time` bigint(20) DEFAULT NULL,
`next_update` bigint(20) NOT NULL,
`ctime` bigint(20) NOT NULL,
`mtime` bigint(20) NOT NULL,
`state` varchar(256) NOT NULL,
`state_last` varchar(256) DEFAULT NULL,
`dv` int(11) NOT NULL,
`state_descr` text NOT NULL,
`state_source` text NOT NULL,
`errors` int(11) NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `vd_id` (`vd_id`),
KEY `tmp_dump_storid` (`tmp_dump_storid`),
KEY `idx_username` (`username`(766)),
CONSTRAINT `restore_reqs_ibfk_1` FOREIGN KEY (`vd_id`) REFERENCES `volume_dumps` (`id`),
CONSTRAINT `restore_reqs_ibfk_2` FOREIGN KEY (`tmp_dump_storid`) REFERENCES `blob_stores` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `versions` (
`version` int(11) NOT NULL
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
INSERT INTO `versions` VALUES (4);
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_addrs` (
`fs_id` bigint(20) NOT NULL,
`addr` varchar(256) NOT NULL,
KEY `fs_id` (`fs_id`),
CONSTRAINT `vl_addrs_ibfk_1` FOREIGN KEY (`fs_id`) REFERENCES `vl_fileservers` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_entries` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`br_id` bigint(20) NOT NULL,
`name` varchar(256) NOT NULL,
`rwid` int(11) NOT NULL,
`roid` int(11) DEFAULT NULL,
`bkid` int(11) DEFAULT NULL,
PRIMARY KEY (`id`),
UNIQUE KEY `br_id` (`br_id`,
`name`),
CONSTRAINT `vl_entries_ibfk_1` FOREIGN KEY (`br_id`) REFERENCES `backup_runs` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_fileservers` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`br_id` bigint(20) NOT NULL,
`uuid` varchar(37) DEFAULT NULL,
PRIMARY KEY (`id`),
KEY `br_id` (`br_id`),
CONSTRAINT `vl_fileservers_ibfk_1` FOREIGN KEY (`br_id`) REFERENCES `backup_runs` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_sites` (
`vl_id` bigint(20) NOT NULL,
`type` varchar(2) NOT NULL,
`server_id` bigint(20) NOT NULL,
`partition` varchar(2) NOT NULL,
UNIQUE KEY `vl_id` (`vl_id`,
`type`,
`server_id`),
KEY `server_id` (`server_id`),
CONSTRAINT `vl_sites_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`),
CONSTRAINT `vl_sites_ibfk_2` FOREIGN KEY (`server_id`) REFERENCES `vl_fileservers` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `volume_dumps` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`vl_id` bigint(20) NOT NULL,
`hdr_size` bigint(20) NOT NULL,
`hdr_creation` bigint(20) NOT NULL,
`hdr_copy` bigint(20) NOT NULL,
`hdr_backup` bigint(20) NOT NULL,
`hdr_update` bigint(20) NOT NULL,
`incr_timestamp` bigint(20) NOT NULL,
`dump_size` bigint(20) NOT NULL,
`dump_storid` bigint(20) NOT NULL,
`dump_spath` text NOT NULL,
`dump_checksum` varchar(1024) NOT NULL,
PRIMARY KEY (`id`),
KEY `vl_id` (`vl_id`),
KEY `ix_volume_dumps_dump_storid` (`dump_storid`),
KEY `idx_dump_spath` (`dump_spath`(766)),
CONSTRAINT `volume_dumps_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`),
CONSTRAINT `volume_dumps_ibfk_2` FOREIGN KEY (`dump_storid`) REFERENCES `blob_stores` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
""",
        fabs.db.VERS_5: """
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `backup_runs` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`cell` varchar(256) NOT NULL,
`note` text NOT NULL,
`volume` varchar(256) DEFAULT NULL,
`injected_blob_storid` bigint(20) DEFAULT NULL,
`injected_blob_spath` text DEFAULT NULL,
`injected_blob_checksum` varchar(1024) DEFAULT NULL,
`start` bigint(20) NOT NULL,
`end` bigint(20) NOT NULL,
`state` varchar(256) NOT NULL,
`state_last` varchar(256) DEFAULT NULL,
`dv` int(11) NOT NULL,
`state_descr` text NOT NULL,
`state_source` text NOT NULL,
`errors` int(11) NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `injected_blob_storid` (`injected_blob_storid`),
KEY `ix_backup_runs_state` (`state`),
CONSTRAINT `backup_runs_ibfk_1` FOREIGN KEY (`injected_blob_storid`) REFERENCES `blob_stores` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `blob_stores` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`uuid` varchar(37) NOT NULL,
PRIMARY KEY (`id`),
KEY `ix_blob_stores_uuid` (`uuid`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `dump_jobs` (
`vl_id` bigint(20) NOT NULL,
`cell` varchar(256) NOT NULL,
`server_id` bigint(20) NOT NULL,
`partition` varchar(2) NOT NULL,
`unchanged` tinyint(1) NOT NULL,
`incr_timestamp` bigint(20) NOT NULL,
`ctime` bigint(20) NOT NULL,
`mtime` bigint(20) NOT NULL,
`timeout` int(11) NOT NULL,
`injected_blob_storid` bigint(20) DEFAULT NULL,
`injected_blob_spath` text DEFAULT NULL,
`injected_blob_checksum` varchar(1024) DEFAULT NULL,
`running` tinyint(1) NOT NULL,
`state` varchar(256) NOT NULL,
`state_last` varchar(256) DEFAULT NULL,
`dv` int(11) NOT NULL,
`state_descr` text NOT NULL,
`state_source` text NOT NULL,
`errors` int(11) NOT NULL DEFAULT 0,
`skip_reason` varchar(256) DEFAULT NULL,
`rw_target_status` varchar(256) DEFAULT NULL,
UNIQUE KEY `vl_id` (`vl_id`),
KEY `server_id` (`server_id`),
KEY `injected_blob_storid` (`injected_blob_storid`),
KEY `ix_dump_jobs_cell_state` (`cell`, `state`),
CONSTRAINT `dump_jobs_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`),
CONSTRAINT `dump_jobs_ibfk_2` FOREIGN KEY (`server_id`) REFERENCES `vl_fileservers` (`id`),
CONSTRAINT `dump_jobs_ibfk_3` FOREIGN KEY (`injected_blob_storid`) REFERENCES `blob_stores` (`id`),
CONSTRAINT `CONSTRAINT_1` CHECK (`unchanged` in (0,
1)),
CONSTRAINT `CONSTRAINT_2` CHECK (`running` in (0,
1))
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `links` (
`vl_id` bigint(20) NOT NULL,
`path` text NOT NULL,
`target` text NOT NULL,
UNIQUE KEY `idx_vlid_path` (`vl_id`,
`path`(766)),
CONSTRAINT `links_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `restore_reqs` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`vd_id` bigint(20) NOT NULL,
`note` text NOT NULL,
`cell` varchar(256) NOT NULL,
`username` varchar(1024) DEFAULT NULL,
`user_path_full` text DEFAULT NULL,
`user_path_partial` text DEFAULT NULL,
`tmp_dump_storid` bigint(20) DEFAULT NULL,
`tmp_dump_spath` text DEFAULT NULL,
`backend_req_info` text DEFAULT NULL,
`stage_volume` varchar(256) DEFAULT NULL,
`stage_path` text DEFAULT NULL,
`stage_time` bigint(20) DEFAULT NULL,
`next_update` bigint(20) NOT NULL,
`ctime` bigint(20) NOT NULL,
`mtime` bigint(20) NOT NULL,
`state` varchar(256) NOT NULL,
`state_last` varchar(256) DEFAULT NULL,
`dv` int(11) NOT NULL,
`state_descr` text NOT NULL,
`state_source` text NOT NULL,
`errors` int(11) NOT NULL DEFAULT 0,
PRIMARY KEY (`id`),
KEY `tmp_dump_storid` (`tmp_dump_storid`),
KEY `ix_restore_reqs_vd_id` (`vd_id`),
KEY `idx_username` (`username`(766)),
KEY `ix_restore_reqs_state` (`state`),
CONSTRAINT `restore_reqs_ibfk_1` FOREIGN KEY (`vd_id`) REFERENCES `volume_dumps` (`id`),
CONSTRAINT `restore_reqs_ibfk_2` FOREIGN KEY (`tmp_dump_storid`) REFERENCES `blob_stores` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `versions` (
`version` int(11) NOT NULL
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
INSERT INTO `versions` VALUES (5);
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_addrs` (
`fs_id` bigint(20) NOT NULL,
`addr` varchar(256) NOT NULL,
KEY `ix_vl_addrs_fs_id` (`fs_id`),
CONSTRAINT `vl_addrs_ibfk_1` FOREIGN KEY (`fs_id`) REFERENCES `vl_fileservers` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_entries` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`br_id` bigint(20) NOT NULL,
`name` varchar(256) NOT NULL,
`rwid` int(11) NOT NULL,
`roid` int(11) DEFAULT NULL,
`bkid` int(11) DEFAULT NULL,
PRIMARY KEY (`id`),
UNIQUE KEY `br_id` (`br_id`,
`name`),
KEY `ix_vl_entries_br_id` (`br_id`),
KEY `ix_vl_entries_rwid` (`rwid`),
CONSTRAINT `vl_entries_ibfk_1` FOREIGN KEY (`br_id`) REFERENCES `backup_runs` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_fileservers` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`br_id` bigint(20) NOT NULL,
`uuid` varchar(37) DEFAULT NULL,
PRIMARY KEY (`id`),
KEY `ix_vl_fileservers_br_id` (`br_id`),
CONSTRAINT `vl_fileservers_ibfk_1` FOREIGN KEY (`br_id`) REFERENCES `backup_runs` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `vl_sites` (
`vl_id` bigint(20) NOT NULL,
`type` varchar(2) NOT NULL,
`server_id` bigint(20) NOT NULL,
`partition` varchar(2) NOT NULL,
UNIQUE KEY `vl_id` (`vl_id`,
`type`,
`server_id`),
KEY `ix_vl_sites_server_id` (`server_id`),
CONSTRAINT `vl_sites_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`),
CONSTRAINT `vl_sites_ibfk_2` FOREIGN KEY (`server_id`) REFERENCES `vl_fileservers` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `volume_dumps` (
`id` bigint(20) NOT NULL AUTO_INCREMENT,
`vl_id` bigint(20) NOT NULL,
`hdr_size` bigint(20) NOT NULL,
`hdr_creation` bigint(20) NOT NULL,
`hdr_copy` bigint(20) NOT NULL,
`hdr_backup` bigint(20) NOT NULL,
`hdr_update` bigint(20) NOT NULL,
`incr_timestamp` bigint(20) NOT NULL,
`dump_size` bigint(20) NOT NULL,
`dump_storid` bigint(20) NOT NULL,
`dump_spath` text NOT NULL,
`dump_checksum` varchar(1024) NOT NULL,
PRIMARY KEY (`id`),
KEY `ix_volume_dumps_hdr_creation` (`hdr_creation`),
KEY `ix_volume_dumps_vl_id` (`vl_id`),
KEY `ix_volume_dumps_dump_storid` (`dump_storid`),
KEY `idx_dump_spath` (`dump_spath`(766)),
CONSTRAINT `volume_dumps_ibfk_1` FOREIGN KEY (`vl_id`) REFERENCES `vl_entries` (`id`),
CONSTRAINT `volume_dumps_ibfk_2` FOREIGN KEY (`dump_storid`) REFERENCES `blob_stores` (`id`)
)CHARSET=utf8mb4 DEFAULT ENGINE=InnoDB ROW_FORMAT=DYNAMIC;
/*!40101 SET character_set_client = @saved_cs_client */;
""",
    },
}

def _upgrade_sql(engine, dbvers):
    if dbvers == fabs.db.VERSION:
        return "-- Cannot upgrade, db is already at latest version %d" % (fabs.db.VERSION)

    if fabs.db.VERS_3 <= dbvers < fabs.db.VERSION:
        to_vers = dbvers + 1

        sql = ''
        for vers in range(dbvers, fabs.db.VERSION):
            sql += fabs.db.builtin_upgrade_sql[engine][vers+1]
            sql += "DELETE FROM versions;\n"
            sql += "INSERT INTO versions (version) VALUES (%d);\n" % (vers+1)

        return """-- FABS db upgrade from version %d -> %d
BEGIN;
%s COMMIT;
    """ % (dbvers, fabs.db.VERSION, sql)

    raise ValueError("Unhandled dbvers %r" % dbvers)
