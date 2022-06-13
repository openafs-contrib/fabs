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

# This file contains some helper and utility functionality to run our tests.
# All tests should probably import this module and run tests through our
# FabsTest class (or subclasses) to get some convenient functionality.

import pytest
import sys
import shlex
import time
import contextlib
import tempfile
import os
import json
import multiprocessing
import copy
import yaml
import subprocess

from unittest import mock
import sqlalchemy as sa
from sqlalchemy.pool import NullPool

import fabs.scripts.fabsys
import fabs.db as db

yield_fixture = pytest.fixture
if int(pytest.__version__.split('.')[0]) < 3:
    # Before pytest 3.0, we need to use a separate @yield_fixture for fixtures
    # that yield. After 3.0, we can use the regular @fixture, and starting in
    # 6.2, using yield_fixture emits a deprecation warning.
    yield_fixture = pytest.yield_fixture

@contextlib.contextmanager
def nullcontext():
    yield

class FabsTest:
    curTest = None
    config = {
        'afs': {
            'cell': 'test.example.com',
        }
    }
    _capfd = None
    _tmp = None
    _out_strs = None
    _err_strs = None

    @yield_fixture(autouse=True)
    def fabs_cap(self, capfd):
        FabsTest.curTest = self
        self._capfd = capfd
        self._out_strs = []
        self._err_strs = []

        yield

        # After the rest has run, print out all of the captured and consumed
        # stdout/stderr data, so it doesn't disappear.
        if self._out_strs:
            sys.stdout.write("\n")
        for ostr in self._out_strs:
            sys.stdout.write(ostr)

        if self._err_strs:
            sys.stderr.write("\n")
        for estr in self._err_strs:
            sys.stderr.write(estr)

        self._out_strs = []
        self._err_strs = []
        self._capfd = None
        FabsTest.curTest = None

    @yield_fixture(autouse=True)
    def fabs_tmp(self, tmpdir):
        self._tmp = tmpdir
        yield
        self._tmp = None

    @yield_fixture
    def tzutc(self):
        prev_tz = os.environ.get('TZ', None)
        os.environ['TZ'] = 'UTC'
        time.tzset()

        yield

        if prev_tz is None:
            del os.environ['TZ']
        else:
            os.environ['TZ'] = prev_tz
        time.tzset()

    def comment(self, astr):
        # .disabled() doesn't exist in pytest 2.9. See:
        # https://github.com/pytest-dev/pytest/issues/1599#issuecomment-261964636
        # for a way to do this directly, if needed
        #with self._capfd.disabled():

        for line in astr.splitlines():
            print('# ' + line, file=sys.stderr)

    def mkftemp(self, prefix, adir=False):
        prefix = '%s.' % prefix
        kwargs = {}

        # Use line-buffered files
        kwargs['buffering'] = 1

        # Use our per-test tmp dir
        kwargs['dir'] = self._tmp

        assert self._tmp is not None

        if adir:
            return tempfile.mkdtemp(prefix=prefix, dir=self._tmp)
        else:
            return tempfile.NamedTemporaryFile(mode='w+', prefix=prefix, delete=False, **kwargs)

    def run_in_fork(self, func, fail=False, init_config=False):
        def target():
            with contextlib.ExitStack() as stack:
                stack.enter_context(db.ensure_reset())
                if init_config:
                    config_path = stack.enter_context(self.config_file())
                    fabs.config.load(cli_conf_file=config_path)
                ret = func()
                if ret is None:
                    ret = 0
            sys.exit(ret)

        child = multiprocessing.Process(target=target)
        child.start()
        child.join()
        assert child.exitcode is not None
        if fail:
            assert child.exitcode != 0
        else:
            assert child.exitcode == 0

    @contextlib.contextmanager
    def config_file(self, skip=False):
        """
        Write out our data in self.config to a tmp file, to use as a FABS
        config file.
        """
        if skip:
            yield None
            return

        config_fh = self.mkftemp('config')
        try:
            if self.config is not None:
                yaml.safe_dump(self.config, stream=config_fh)
                config_fh.flush()
            yield config_fh.name
        finally:
            config_fh.close()

    def _fabsys(self, argstr, fail=False):
        """
        Run the equivalent of 'fabsys <argstr>'.

        So, self._fabsys('foo --bar') is like running 'fabsys foo --bar'.

        Args:
            argstr (str): Arguments to 'fabsys'.
            fail (bool, optional): If True, assert that the command fails
                (exits with nonzero). Otherwise, assert that the command
                succeeds (exits with 0). Defaults to False.
        """

        argv = shlex.split(argstr)

        # Unless --config was explicitly specified in the args, provide a
        # config file to use, so we don't use the default /etc/fabs config file
        # if the machine we're running on has one.
        skip_config=False
        if '--config' in argv:
            skip_config=True

        with self.config_file(skip=skip_config) as config_path:
            if config_path is not None:
                argv.extend(['--config', config_path])

            self.run_in_fork(lambda: fabs.scripts.fabsys.main(argv), fail=fail)

    def _readout(self, stderr, show=False):
        """
        Args:
            stderr (bool): If True, return captured stderr data. Otherwise,
                return captured stdout data.
            show (bool, optional): If True, still print the captured data back
                out afterwards, in addition to returning it here.

        Returns:
            str: Captured data.
        """
        out, err = self._capfd.readouterr()

        if stderr:
            # Return stderr data
            retval = err
            if not show:
                # Don't save the captured stderr
                err = ''

        else:
            # Return stdout data
            retval = out
            if not show:
                # Don't save the captured stdout
                out = ''

        if out:
            self._out_strs.append(out)
        if err:
            self._err_strs.append(err)

        return retval

    def _fabsys_cap(self, argstr, stderr=False, fail=False):
        """
        Same as `fabsys`, but capture the stdout (or stderr) output from the
        fabsys call and return it.
        """
        self._readout(stderr=stderr, show=True)
        self._fabsys(argstr, fail=fail)
        return self._readout(stderr=stderr)

    def fabsys_stdout(self, argstr):
        """
        Run the given fabsys command, capture stdout from it, and return the
        captured stdout string
        """
        return self._fabsys_cap(argstr)

    def fabsys_stderr(self, argstr, fail=True):
        """
        Same as fabsys_stdout(), but capture stderr instead of stdout
        """
        return self._fabsys_cap(argstr, stderr=True, fail=fail)

    def fabsys_json(self, argstr):
        """
        Same as fabsys_stdout(), but run the command with json formatting, and
        return the parsed json object
        """
        return json.loads(self.fabsys_stdout(argstr + ' --format json'))

    def fabsys_comment(self, argstr):
        """
        Same as fabsys_stdout(), but output the captured stdout as a comment
        """
        self.comment(self.fabsys_stdout(argstr))

def curTest():
    """
    Get the object for the current-running test. We can't always easily pass in
    the test case object to some objects, callback functions, etc, so just
    store it globally for convenience.
    """
    assert FabsTest.curTest is not None
    return FabsTest.curTest

# Pick a path that's very unlikely to exist. We'll run all of our commands
# through this, to try to guarantee that an error occurs if something goes
# wrong and the relevant command is actually exec'd.
CMD_WRAPPER='/dev/nonexistent/path'
KEYTAB='/opt/fabs/etc/test.keytab'

class MockCommand:
    """
    This effectively mocks the subprocess.Popen class, using our mocked cell
    data to provide command results and output. We don't implement everything
    that Popen can do, just the functionality that gets used.

    We effectively pretend that the process has exited successfully as soon as
    this object is created, and stdout/stderr are stored in temporary files
    that wil be read and polled in the usual way.

    Just like with a normal subprocess.Popen, you can use these mock command
    handles like this:

        with MockWhatever() as ph:
            pass

    And on exiting the 'with' block, the relevant file handles will be closed.
    """

    def __init__(self, argv, env, data):
        self._argv = argv
        self._env = env
        self._mockdata = data

        test = curTest()

        self.stdin = test.mkftemp('stdin-%s' % argv[0])
        self.stdout = test.mkftemp('stdout-%s' % argv[0])
        self.stderr = test.mkftemp('stderr-%s' % argv[0])

        self.returncode = None

        # Some callers will grab the pid to log; just give an obviously fake
        # pid
        self.pid = 99999999

    def get_vol(self, name):
        return self._mockdata.vols.get(name, None)

    def start(self):
        self.returncode = self._populate_output(self.stdout, self.stderr)

        self.stdout.flush()
        self.stderr.flush()

        self.stdout.seek(0)
        self.stderr.seek(0)

    def _cleanup(self):
        self.stdin.close()
        self.stdout.close()
        self.stderr.close()

    def __enter__(self):
        return self
    def __exit__(self, *exc_info):
        self._cleanup()

    def _populate_output(self, stdout, stderr):
        raise NotImplementedError

    # pylint: disable=redefined-builtin
    def communicate(self, input=None):
        if self.returncode == 0:
            # Seek back to the end of our stdout/stderr files
            self.stdout.seek(0, 2)
            self.stderr.seek(0, 2)

            self.returncode = self._do_communicate(input, self.stdout, self.stderr)

            self.stdout.flush()
            self.stderr.flush()

            self.stdout.seek(0)
            self.stderr.seek(0)

        ret = (self.stdout.read().encode('utf-8'),
               self.stderr.read().encode('utf-8'))
        self._cleanup()
        return ret

    def poll(self):
        assert self.returncode is not None
        self._cleanup()
        return self.returncode

    def wait(self):
        return self.poll()

    def _do_communicate(self, stdin_bytes, stdout, stderr):
        return 0

    @classmethod
    def popen(cls, argv, env, data):
        return cls(argv, env, data)

class _NO_DEFAULT:
    pass

# pylint: disable=abstract-method
class MockAFSCommand(MockCommand):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._args = self.cli_to_args(self._argv)

    def vlnoent(self, stderr):
        stderr.write("VLDB: no such entry\n")
        return 255

    # Transforms e.g. '-foo bar -baz' into {'foo': 'bar', 'baz': True}
    @classmethod
    def cli_to_args(cls, argv):
        ret = {}
        key = None
        
        # Ignore the first two arguments (e.g. 'vos examine')
        for arg in argv[2:]:
            if arg.startswith('-'):
                key = arg[1:]
                ret[key] = True
            else:
                assert key is not None
                ret[key] = arg
                key = None
        return ret

    def arg(self, name, default=_NO_DEFAULT):
        if default is _NO_DEFAULT:
            assert name in self._args
        elif name not in self._args:
            return default
        return self._args[name]

class MockDumpFilter(MockCommand):
    def _populate_output(self, stdout, stderr):
        stdout.write("{}")
        return 0

class MockK5start:
    @classmethod
    def popen(cls, argv, env, data):
        argv = argv[:]
        # Remove the beginning of 'argv' until we hit the '--' arg. After the
        # '--' arg, our wrapped command follows.
        while argv.pop(0) != '--':
            pass
        return mock_popen(argv, env, data)

class MockVosListaddrs(MockAFSCommand):
    def _populate_output(self, stdout, stderr):
        assert self.arg('noresolv') is True
        assert self.arg('printuuid') is True

        for fs in self._mockdata.fs_list:
            stdout.write("UUID: %s\n" % fs.uuid)
            stdout.write("%s\n" % fs.ip)
            stdout.write("\n")
        return 0

class MockVosBackup(MockAFSCommand):
    def _populate_output(self, stdout, stderr):
        volid = self.arg('id')
        vol = self.get_vol(volid)
        if vol is None:
            return self.vlnoent(stderr)

        if not vol.online:
            stderr.write("\n")
            stderr.write("Could not re-clone backup volume %d\n" % vol.rwid)
            stderr.write("    Volume is off line\n")
            stderr.write("Volume is off line\n")
            stderr.write("Error in vos backup command.\n")
            stderr.write("Volume is off line\n")
            return 255

        stdout.write("Created backup volume for %s\n" % volid)
        return 0

class MockVosExamine(MockAFSCommand):
    def _examine(self, vol, stdout):
        data = {
            'name': vol.name,
            'volid': vol.rwid,
            'typestr': 'RW',
            'size': vol.size/1024,
            'online': 'On-line',
            'need_salvage': '',
            'fs': vol.fs.ip,
            'part': vol.part,
            'rwid': vol.rwid,
            'roid': vol.roid,
            'bkid': vol.bkid,
            'clid': vol.clid,
            'maxquota': vol.maxquota,
            'crdate': vol.crdate,
            'cpdate': vol.cpdate,
            'bkdate': vol.bkdate,
            'ladate': vol.ladate,
            'ludate': vol.ludate,
            'n_access': vol.n_access,
        }

        if not vol.online:
            data['online'] = 'Off-line'

        stdout.write("""%(name)-32s %(volid)10lu %(typestr)s %(size)d K  %(online)s%(need_salvage)s
    %(fs)s %(part)s 
    RWrite %(rwid)10lu ROnly %(roid)10lu Backup %(bkid)10lu 
    MaxQuota %(maxquota)10d K 
    Creation    %(crdate)s
    Copy        %(cpdate)s
    Backup      %(bkdate)s
    Last Access %(ladate)s
    Last Update %(ludate)s
    %(n_access)d accesses in the past day (i.e., vnode references)
""" % data)

        return self._vldata(vol, stdout)

    def _listvldb_one(self, vol, stdout):
        stdout.write("\n%s\n" % vol.name)
        return self._vldata(vol, stdout)

    def _listvldb_all(self, stdout):
        stdout.write("VLDB entries for all servers\n")
        vol_list = self._mockdata.vol_list
        for vol in vol_list:
            stdout.write("\n%s\n" % vol.name)
            code = self._vldata(vol, stdout)
            if code != 0:
                return code
        stdout.write("\nTotal entries: %d\n" % len(vol_list))
        return 0

    def _vldata(self, vol, stdout):
        data = {
            'rwid': vol.rwid,
            'roid': vol.roid,
            'bkid': vol.bkid,
            'clid': vol.clid,
            'fs': vol.fs.ip,
            'part': vol.part,
            'site_type': 'RW',
            'flag_str': '',
            'n_sites': 1,
        }

        stdout.write("""    RWrite: %(rwid)-10u    ROnly: %(roid)-10u    Backup: %(bkid)-10u    RClone: %(clid)-10lu
    number of sites -> %(n_sites)lu
""" % data)

        stdout.write("        server %(fs)s partition %(part)s %(site_type)s Site %(flag_str)s\n" % data)
        return 0

    def _populate_output(self, stdout, stderr):
        cmd = self._argv[1]
        if cmd == 'listvldb':
            volid = self.arg('name', default=None)
            if volid is None:
                return self._listvldb_all(stdout)
        else:
            assert cmd == 'examine'
            volid = self.arg('id')

        vol = self.get_vol(volid)
        if vol is None:
            return self.vlnoent(stderr)
        elif cmd == 'listvldb':
            return self._listvldb_one(vol, stdout)
        else:
            return self._examine(vol, stdout)

class MockVosSize(MockAFSCommand):
    def _populate_output(self, stdout, stderr):
        assert self.arg('dump') is True

        volid = self.arg('id')
        vol = self.get_vol(volid)

        if vol is None:
            return self.vlnoent(stderr)

        stdout.write("Volume: %s\ndump_size: %d\n" % (volid, vol.size))
        return 0

class MockVosDump(MockAFSCommand):
    def _populate_output(self, stdout, stderr):
        volid = self.arg('id')
        vol = self.get_vol(volid)
        if vol is None:
            return self.vlnoent(stderr)

        stdout.write("mock vos dump data:%s" % vol.rwid)
        return 0

class MockDumpReport(MockCommand):
    def _populate_output(self, stdout, stderr):
        return 0

    def _do_communicate(self, stdin_bytes, stdout, stderr):
        self._mockdata.report(stdin_bytes.decode('utf-8'))
        return 0

class MockDumpscan(MockCommand):
    def _populate_output(self, stdout, stderr):
        flags = self._argv[1]

        if flags == '-Pvip':
            # Print out vnode info; just output nothing for now
            return 0

        assert self._argv[1] == "-PV"
        # -PV means just print out some volume metadata

        fname = self._argv[2]
        with open(fname, 'r') as fh:
            volname = fh.read().split(':')[1]

        vol = self.get_vol(volname)
        assert vol is not None

        data = {
            'volid': vol.rwid,
            'volname': volname,
            'rwid': vol.rwid,
            'clid': vol.clid,
            'maxquota': vol.maxquota,
            'size': vol.size,
            'crdate': vol.crdate,
            'ladate': vol.ladate,
            'ludate': vol.ludate,
            'bkdate': vol.bkdate,
            'n_access': vol.n_access,
        }

        stdout.write("""* VOLUME HEADER [36 = 0x0000000000000024]
 Volume ID:   %(volid)d
 Version:     1
 Volume name: %(volname)s
 In service?  true
 Blessed?     true
 Uniquifier:  5
 Type:        0
 Parent ID:   %(rwid)d
 Clone ID:    %(clid)d
 Max quota:   %(maxquota)d
 Min quota:   0
 Disk used:   %(size)d
 File count:  5
 Account:     0
 Owner:       0
 Created:     %(crdate)s
 Accessed:    %(ladate)s
 Updated:     %(ludate)s
 Expires:     Wed Dec 31 18:00:00 1969
 Backed up:   %(bkdate)s
 Offine Msg:  volume has been soft detached
 MOTD:        
 Weekuse:              0          0          0          0
 Weekuse:              0          0          0
 Dayuse Date: Thu Aug 10 00:00:00 2017
 Daily usage: %(n_access)d
""" % data)

        return 0

_mocked_commands = {
    ('k5start',): MockK5start,
    ('vos', 'backup'): MockVosBackup,
    ('vos', 'dump'): MockVosDump,
    ('vos', 'examine'): MockVosExamine,
    ('vos', 'listvldb'): MockVosExamine,
    ('vos', 'listaddrs'): MockVosListaddrs,
    ('vos', 'size'): MockVosSize,
    ('dump-filter',): MockDumpFilter,
    ('dumpscan',): MockDumpscan,
    ('dump-report',): MockDumpReport,
}

def mock_popen(argv, env, data, **kwargs):
    wrapper = argv[0]
    assert wrapper == CMD_WRAPPER

    argv = argv[1:]
    cmd = argv[:]
    while cmd:
        t_cmd = tuple(cmd)
        if t_cmd in _mocked_commands:
            proc = _mocked_commands[t_cmd].popen(argv, env, data)
            proc.start()
            return proc
        cmd.pop()

    raise Exception("cmd %r not found in _mocked_commands" % argv)

class MockVol:
    def __init__(self, mockdata, fs, part, name=None):
        self.mockdata = mockdata

        if name is None:
            name = self.get_name()
        self.name = name

        self.fs = fs
        self.part = part

        self.size = 4096
        self.online = True

        self.rwid = self.get_id()
        self.roid = self.get_id()
        self.bkid = self.get_id()
        self.clid = self.get_id()

        self.crdate = 'Mon Jul  6 19:13:11 2015'
        self.cpdate = 'Mon Jul  6 19:13:11 2015'
        self.bkdate = 'Mon Jul  6 19:13:11 2015'
        self.ladate = 'Mon Jul  6 19:13:11 2015'
        self.ludate = 'Mon Jul  6 19:13:11 2015'

        self.n_access = 5

        self.maxquota = 5000

    def ids(self):
        return [self.rwid, self.roid, self.bkid, self.clid]

    def set_online(self, online=True):
        self.online = online

    def get_id(self):
        ret = self.mockdata.volid_counter
        self.mockdata.volid_counter += 1
        return ret

    def get_name(self):
        ret = 'vol.mock.%d' % self.mockdata.volname_counter
        self.mockdata.volname_counter += 1
        return ret

class MockFS:
    def __init__(self, mockdata):
        self.mockdata = mockdata
        self.ip = self.get_ip()
        self.uuid = self.get_uuid()

    def get_ip(self):
        ret = '127.0.0.%d' % self.mockdata.fsip_counter
        self.mockdata.fsip_counter += 1
        return ret

    def get_uuid(self):
        ret = '%08x-%04x-%04x-%02x-%02x-%012x' % (
              1, 2, 3, 4, 5, self.mockdata.fsuuid_counter)
        self.mockdata.fsuuid_counter += 1
        return ret

class MockCellData:
    def __init__(self, report_fh):
        self.volname_counter = 1
        # 0x20000000, the number that the vlserver starts counting volids from
        self.volid_counter = 536870912

        self.fsip_counter = 1
        self.fsuuid_counter = 1

        self._report_fh = report_fh

        fs = MockFS(self)
        self.fs_list = [fs]
        self.vols = {}

        vol_list = [MockVol(self, fs, '/vicepa', 'root.cell')]
        for _ in range(10):
            vol_list.append(MockVol(self, fs, '/vicepa'))

        self.vol_list = vol_list
        for vol in vol_list:
            self.vols[vol.name] = vol
            for volid in vol.ids():
                self.vols[str(volid)] = vol

    def report(self, data):
        self._report_fh.write(data)
        self._report_fh.flush()

    def report_data(self):
        self._report_fh.seek(0)
        return self._report_fh.read()

class FabsMockEnvTest(FabsTest):
    mockdata = None
    db_data = None

    config_base = {
## Uncomment to show a ton of debug info while test backups are running
#        'log': {
#            'level': 'debug',
#        },
        'afs': {
            'cell': 'test.example.com',
            'dumpscan': [CMD_WRAPPER, 'dumpscan'],
            'vos': [CMD_WRAPPER, 'vos'],
            'fs': [CMD_WRAPPER, 'fs'],
            'pts': [CMD_WRAPPER, 'pts'],
            'aklog': [CMD_WRAPPER, 'aklog'],
            'keytab': KEYTAB,
        },
        'k5start': {
            'command': [CMD_WRAPPER, 'k5start'],
        },
        'db': {
            'url': None,
            'batch_size': 3,
        },
        'dump': {
            'include': {
                'volumes': ['*'],
            },
            'storage_dirs': None,
            'filter_cmd': {
                'json': [CMD_WRAPPER, 'dump-filter'],
            },
        },
        'stage': {
            'notify_cmd': [CMD_WRAPPER, 'stage-notify'],
        },
        'backend': {
            'request_cmd': [CMD_WRAPPER, 'backend-request'],
        },
        'report': {
            'json': {
                'command': [CMD_WRAPPER, 'dump-report'],
            },
            'txt': {
                'command': None,
            },
        },
        'lockdir': None,
    }

    @yield_fixture(autouse=True)
    def fabs_mock_data(self, fabs_tmp, fabs_cap):
        self.config = copy.deepcopy(self.config_base)

        self.config['dump']['storage_dirs'] = [self.mkftemp('storage', adir=True)]
        self.config['lockdir'] = self.mkftemp('lockdir', adir=True)
        with self.mkftemp('db') as db_fh:
            self.config['db']['url'] = 'sqlite:///%s' % db_fh.name

        with self.mkftemp('report') as report_fh:
            mockdata = MockCellData(report_fh)
            with self._patch_popen(mockdata):
                self.mockdata = mockdata
                yield mockdata
                self.mockdata = None

    @pytest.fixture(autouse=True)
    def db_url(self, raw_db_url, fabs_mock_data):
        if raw_db_url is not None:
            # Drop all tables in given db
            def reset_db():
                engine = sa.create_engine(raw_db_url, poolclass=NullPool)
                with engine.connect() as conn:
                    meta = sa.MetaData(engine)
                    meta.reflect(bind=conn)
                    meta.drop_all()
                return 0
            self.run_in_fork(reset_db)

            # Point our FABS config to the given db
            self.config['db']['url'] = raw_db_url

        return self.config['db']['url']

    @contextlib.contextmanager
    def _patch_popen(self, data):
        _orig_popen = subprocess.Popen

        def new_popen(argv, env=None, **kwargs):
            if isinstance(argv, list) and argv[0] == CMD_WRAPPER:
                return mock_popen(argv, env, data)
            return _orig_popen(argv, env=env, **kwargs)

        with mock.patch('subprocess.Popen', new=new_popen):
            # Silence warnings about our aklog command not being runnable
            # (since we mock actual commands, our aklog points to a bogus path)
            with mock.patch('fabs.util._check_aklog.warned', new=True):
                yield

    def _db_connargs(self, url):
        if url.drivername == 'sqlite':
            return [url.database]

        if url.drivername == 'mysql':
            args = []
            if url.username:
                args.append('--user=%s' % url.username)
            if url.password:
                args.append('--password=%s' % url.password)
            if url.host:
                args.append('--host=%s' % url.host)
            if url.port:
                args.append('--port=%s' % url.port)

            assert url.database
            args.append(url.database)

            return args

        raise ValueError("Unhandled db dialect %r" % url.drivername)

    def db_bulk_dump(self, db_url):
        url = sa.engine.url.make_url(db_url)

        if url.drivername == 'sqlite':
            args = ['sqlite3'] + self._db_connargs(url) + ['.dump']
        elif url.drivername == 'mysql':
            args = ['mysqldump', '--compact'] + self._db_connargs(url)
        else:
            raise ValueError("Unhandled db dialect %r" % url.drivername)

        res = subprocess.run(args, check=True, stdout=subprocess.PIPE)
        return res.stdout.decode('ascii')

    def db_bulk_load(self, a_url, a_sql):
        url = sa.engine.url.make_url(a_url)

        sql = ''.join([
            'BEGIN;',
            a_sql,
            'COMMIT;'
        ]).encode('utf-8')

        if url.drivername == 'sqlite':
            args = ['sqlite3'] + self._db_connargs(url)

        elif url.drivername == 'mysql':
            args = ['mysql'] + self._db_connargs(url)
        else:
            raise ValueError("Unhandled db dialect %r" % url.drivername)

        subprocess.run(args, input=sql, check=True)

    def do_backup_init(self):
        self.fabsys_comment('db-init --exec')
        if self.db_data is not None:
            self.db_bulk_load(self.config['db']['url'], self.db_data)
        self.fabsys_comment('storage-init --all')

    def do_backup_run(self, volname=None):
        self.comment("Starting backup")
        if volname is None:
            res = self.fabsys_json('backup-start --all')
        else:
            res = self.fabsys_json('backup-start --volume %s' % volname)

        assert res['fabs_backup_start']['brun_id'] is not None
        brid = res['fabs_backup_start']['brun_id']

        self.comment("Running backup")
        n_steps = 30
        for _ in range(n_steps):
            self.fabsys_comment('server --once')

        return brid

    def do_backup_success(self, volname=None):
        brid = self.do_backup_run(volname)

        # Check that the backup run finished
        res = self.fabsys_json('backup-status %d' % brid)

        brinfo = res['fabs_backup_status']['status'][0]
        assert brinfo['id'] == brid
        assert brinfo['state'] == 'DONE'

        res = json.loads(self.mockdata.report_data())

        assert res['success']['id'] == brid

        return res

# Kinda hack-y, but this turns off some consistency options in sqlite. We don't
# care about on-disk consistency just for the tests, and this makes the tests
# run a bit faster.
@sa.event.listens_for(sa.engine.Engine, 'connect')
def db_pragma(dbapi_conn, conn_record):
    if type(dbapi_conn).__module__ == 'sqlite3':
        # Only do this for sqlite3 conns. This check is a bit iffy, but there
        # doesn't seem to be a better way to check for the conn's dialect.
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA synchronous = OFF")
        cursor.execute("PRAGMA journal_mode = MEMORY")
        cursor.close()
