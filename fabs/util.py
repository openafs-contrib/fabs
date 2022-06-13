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

import dateutil.parser
import itertools
from multiprocessing import Process
import os
import os.path
import time
import hashlib
import subprocess
import traceback
import contextlib
import signal

import fabs.err as err
import fabs.db as db
import fabs.config as config
import fabs.log
log = fabs.log.getLogger(__name__)

class FabsProcess(Process):
    """
    Small wrapper around the real Process class.

    In addition to launching the child process with the specified function,
    this does the following:

    In the parent, resets our db connections (they're not supposed to be shared
    across forks). We must do this in the parent, not the child, contrary to
    what the SQLALchemy docs say. If this is done in the child and we're using
    MySQL/MariaDB, the child process will close down its connections on the
    sockets dup'd from the parent, making the parent connections unusable and
    causing "MySQL server has done away" errors to be thrown.

    We also reset our db connections in the child after the target function has
    been run, so they get destroyed cleanly. The normal code for destroying the
    handles usually doesn't get run, since the child process exits with
    `os._exit`, bypassing any 'finally' blocks in our callers or other cleanup
    code.
    """

    # @todo Consider storing all created processes in a weakref dict, and when the
    # server shuts down, it can wait for all spawned children to also exit. Just
    # store them as keys in a WeakKeyDictionary, and use popitem() to get them out.

    # pylint: disable=arguments-differ
    def start(self, *args, **kwargs):
        db.reset()
        super().start(*args, **kwargs)

    def run(self):
        with db.ensure_reset():
            super().run()

def _executable_file(path):
    return os.path.exists(path) and not os.path.isdir(path) and os.access(path, os.X_OK)

def _check_aklog(aklog):
    """
    Check if we have a usable AKLOG for use with k5start. If we don't check
    this and we don't have an aklog, k5start can give a somewhat confusing
    error message.
    """

    if _check_aklog.warned:
        # Only warn about this once
        return None

    abs_aklog = None
    if os.path.isabs(aklog):
        abs_aklog = aklog
    else:
        # Search for aklog in PATH
        for pdir in os.environ['PATH'].split(':'):
            abspath = os.path.join(pdir, aklog)
            if _executable_file(abspath):
                abs_aklog = abspath
                break

    if abs_aklog is None:
        log.warn('aklog_notfound', "Could not find aklog command '%s' in PATH" % aklog)
        _check_aklog.warned = True

    elif not _executable_file(abs_aklog):
        log.warn('aklog_noexec', "aklog command '%s' is not executable" % abs_aklog)
        _check_aklog.warned = True
_check_aklog.warned = False

def _auth_cmd(command, defaults):
    if config.get('afs/localauth'):
        # Run with -localauth if localauth is turned on
        defaults['localauth'] = True
    else:
        # Without localauth, we need to run through k5start
        k5start = list(config.get('k5start/command'))
        aklog = list(config.get('afs/aklog'))
        keytab = config.get('afs/keytab')
        princ = config.get('afs/keytab_princ')
        if princ is None:
            princ = '-U'

        os.environ['AKLOG'] = ' '.join(aklog)
        _check_aklog(aklog[0])

        # Prepend to 'command'
        command[:0] = k5start + ['-q', '-t', '-f', keytab, princ, '--']

def _vos(auth, defaults):
    command = list(config.get('afs/vos'))

    defaults['cell'] = config.get('afs/cell')

    if auth:
        _auth_cmd(command, defaults)
    else:
        defaults['noauth'] = True

    return fabs.cmd.Vos(vos=command, defaults=defaults)

def vos_auth(**defaults):
    return _vos(1, defaults)

def vos_unauth(**defaults):
    return _vos(0, defaults)

def pts_auth():
    command = config.get('afs/pts')
    defaults = {}
    _auth_cmd(command, defaults)
    return fabs.cmd.Pts(pts=command, defaults=defaults)

def fs_auth():
    command = config.get('afs/fs')
    defaults = {}
    _auth_cmd(command, defaults)
    return fabs.cmd.Fs(fs=command, defaults=defaults)

def dumpscan():
    return fabs.cmd.DumpScan(afsdump_scan=config.get('afs/dumpscan'))

class PrettyBytes:
    def __init__(self, total):
        self._last_bytes = 0
        self._last_time = time.time()

        self.bytes = 'unknown bytes'
        self.rate = 'unknown bytes/s'
        self.total = self._pretty(total)

    def _pretty(self, raw):
        for suffix in ('bytes', 'kB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB'):
            if raw < 1024:
                return "%.2f %s" % (raw, suffix)
            raw = raw / 1024
        # Getting here is a bit weird, but it's not worth throwing an error for
        return "unknown bytes"

    def update_path(self, path):
        self.update_bytes(os.path.getsize(path))

    def update_bytes(self, cur_bytes):
        cur_time = time.time()

        diff_bytes = cur_bytes - self._last_bytes
        diff_time = cur_time - self._last_time
        raw_rate = diff_bytes / diff_time

        self.bytes = self._pretty(cur_bytes)
        self.rate = "%s/s" % self._pretty(raw_rate)

        self._last_bytes = cur_bytes
        self._last_time = cur_time

def checksum(algo, path):
    """
    Get the checksum of the given file, using the given algorithm.
    """
    m = hashlib.new(algo)

    with open(path, 'rb', buffering=0) as fh:
        while True:
            buf = fh.read(config.get('bufsize'))
            if not buf:
                break
            m.update(buf)

    return m.hexdigest()

def run_config_cmd(cmd, stdin=None, extra_env=None):
    try:
        if isinstance(stdin, str):
            # We need to give our subprocess raw bytes, not a unicode string.
            # Encode the data we give to the subprocess in utf-8.
            stdin = stdin.encode('utf-8')

        env = None
        if extra_env is not None:
            env = os.environ.copy()
            env.update(extra_env)

        with subprocess.Popen(cmd, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env) as proc:
            log.d("Running configured command %r in pid %d" % (cmd, proc.pid))

            (stdout, stderr) = proc.communicate(stdin)
            if proc.returncode != 0:
                raise err.ConfigCmdError(("Configured command '%s' exited with status " +
                                          "%d. stdout: %s stderr: %s") % (
                                          cmd, proc.returncode, stdout, stderr))

            log.d("cmd '%s' stdout: %s, stderr: %s" % (cmd, stdout, stderr))
            return stdout.decode('utf-8')

    except err.ConfigCmdError:
        raise

    except Exception:
        raise err.ConfigCmdError("Error running command '%s': %s" % (
                                 cmd, traceback.format_exc())) from None

def get_rights(username, cellname, volid):
    # First, get the 1st-level membership for username
    pts = pts_auth()
    user_groups = pts.membership(nameorid=username).run()

    # Then, get the ACL for the root of the relevant volume
    fs = fs_auth()
    path = '/afs/.:mount/%s:%d' % (cellname, volid)
    acl = fs.listacl(path=path).run()

    cps = [username] + user_groups
    rights = {}

    # Now, go through all of the CPS for the user, and see what rights we gain
    for entry in acl.normal_rights:
        if entry.name in cps:
            for right in entry.rights:
                rights[right] = True

        elif entry.name in ('system:authuser', 'system:anyuser'):
            # s:authuser and s:anyuser will not show up via 'pts mem'. Let the
            # user inherit 'r' and 'l' rights here, in case they don't have
            # them otherwise, but do not let them inherit write-level access
            # from these.
            if 'r' in entry.rights:
                rights['r'] = True
            if 'l' in entry.rights:
                rights['l'] = True

    # Remove all rights applicable from negative acl entries.
    for entry in acl.negative_rights:
        if entry.name in cps or entry.name in ('system:authuser', 'system:anyuser'):
            for right in entry.rights:
                if right in rights:
                    del rights[right]

    return list(rights.keys())

def check_restore_authz(username, cellname, volid):
    log.d("Checking rights for user %s cell %s volid %d" % (
          username, cellname, volid))

    rights = get_rights(username, cellname, volid)

    log.d("User %s has rights %s on volid %d" % (username, rights, volid))

    for req_right in 'rlidw':
        if req_right not in rights:
            log.warn('no_access_info', "User %s does not have access right '%s' for volume id %d" % (
                                       username, req_right, volid))
            raise err.AccessError("Access denied for user %s" % username)

def volume_exists(volname, cell):
    vos = vos_unauth(cell=cell)
    cursor = vos.listvldb(name=volname)

    try:
        cursor.run()
    except fabs.cmd.ProcessError as exc:
        # If we got an error from listvldb, verify that the error that we got was
        # just that the volume didn't exist. Otherwise, raise the error we got.
        stderr = exc.stderr
        if stderr is not None and "VLDB: no such entry" in stderr:
            return False

        raise

    # No error from listvldb, so volume must exist
    return True

def path_exists(path):
    """
    Determine if `path` exists, without using stat()/lstat(). Instead we look
    at the directory listing for the parent dir, and see if an entry matches
    the given path. This is helpful for situations where stat'ing the actual
    path may hang, or error out with ENODEV, etc.
    """
    (head, tail) = os.path.split(path)
    if not head or not tail:
        raise err.InternalError("path_exists given '%s', which splits into (%s, %s)" % (
                                path, head, tail))

    entries = os.listdir(head)
    if tail in entries:
        return True

    return False

_alrm_set = False
@contextlib.contextmanager
def syscall_timeout(timeout):
    """
    Wrap a syscall in this to effectively get a timeout on the call. Example:

        with syscall_timeout(5):
            fcntl.flock(fh, fcntl.LOCK_EX)
            lock_success = True
        if not lock_success:
            timed_out()

    Since this is implemented with SIGALRM, you can only have one running at
    once, so you can't nest these. But the idea is just to wrap a single
    syscall, so the with: block for this should be very small anyway.
    """

    global _alrm_set
    if _alrm_set:
        raise err.InternalError("nested syscall_timeout call")

    class AlarmTriggered(Exception): pass
    def handler(signum, frame):
        raise AlarmTriggered

    orig_alrm = signal.signal(signal.SIGALRM, handler)

    try:
        _alrm_set = True
        signal.alarm(timeout)
        yield
    except AlarmTriggered:
        return
    finally:
        _alrm_set = False
        signal.alarm(0)
        signal.signal(signal.SIGALRM, orig_alrm)

def time_str2unix(timestr):
    """
    Parse a given datetime string, and return a unix timestamp.
    """
    if timestr[0] == '@':
        return int(timestr[1:])

    dt = dateutil.parser.parse(timestr)
    return int(dt.timestamp())

def time_unix2str(seconds):
    """
    Convert a unix timestamp into a human-readable datetime string.
    """
    return time.strftime("%a %b %d %H:%M:%S %Y %Z", time.localtime(seconds))

def chomp(msg, sep='\n'):
    """
    Trim off the trailing newline (or trailing sep') of 'msg', if it has one.
    """
    if msg[-1] == sep:
        return msg[:-1]
    return msg

def format_exc(exc):
    # Format the given exception into two strings, showing the backtrace
    # information, and the exception message.
    #
    # This is a bit cumbersome, because the traceback module does not give
    # convenient methods for formatting the full stack (along with chained
    # exceptions, etc) separately from the format_exception_only string. So, we
    # calculate the format_exception_only string, and just subtract those lines
    # from the full format_exception string to get the backtrace portion.
    msg_lines = list(traceback.format_exception_only(type(exc), exc))
    full_lines = list(traceback.format_exception(type(exc), exc,
                                                 exc.__traceback__))
    stack_lines = full_lines[:-len(msg_lines)]

    stack_str = chomp(''.join(stack_lines))
    msg_str = chomp(''.join(msg_lines))

    return (stack_str, msg_str)

class json_generator(list):
    """
    Wrap a generator in this 'list' subclass, in order to let json.dump()
    encode the generator like a normal list. An alternative to this is to use
    simplejson.dump(iterable_as_array=True), but using this lets us use the
    standard json module.

    This effectively just iterates over the given generator, but also appends
    something to the internal list if the generator is non-empty. This is
    important, because json needs __len__ to return nonzero in order to render
    the list at all, but __len__ must return 0 for an empty list.
    """

    # pylint: disable=super-init-not-called
    def __init__(self, gen):
        it = iter(gen)
        try:
            self._first = iter([next(it)])
            self.append(it)
        except StopIteration:
            self._first = []

    def __iter__(self):
        return itertools.chain(self._first, *self[:1])

def list2str(items):
    """
    Stringify a list like ['foo', 'bar', 'baz'] into a human-readable string
    like 'foo, bar, and baz'.
    """
    if len(items) > 1:
        items = items[:-1] + ['and ' + items[-1]]
    if len(items) > 2:
        return ', '.join(items)
    else:
        return ' '.join(items)
