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

import calendar
import datetime
import locale
import os
import re
import select
import subprocess
import threading
import time

import fabs.log
log = fabs.log.getLogger(__name__)

_vos_parsers = {}
_fs_parsers = {}
_pts_parsers = {}

class ListVLDBResult:
    """
    Results from `vos.listvldb`.

    Attributes:
        entries (`VLDBEntry`): The returned VL entries.
        locked_entries (int): The number of locked entries.
        total_entries (int): The total number of entries.
    """
    def __init__(self):
        self.entries = []
        self.locked_entries = 0
        self.total_entries = 0

class ExamineResult:
    """
    Results from `vos.examine`.

    Attributes:
        vlentry (`VLDBEntry`): The vlentry for the volume.
        headers (list of `VolumeHeader`): Headers shown by 'vos examine'.
    """
    def __init__(self):
        self.vlentry = None
        self.headers = []

class ListAddrsResult:
    """
    Results from `vos.listaddrs`.

    Attributes:
        servers (list of `ServerAddrs`): Fileserver addresses.
    """
    def __init__(self):
        self.servers = []

class ServerAddrs:
    """
    Attributes:
        uuid (str): UUID of the fileserver.
        addrs (list of str): IP addresses.
    """
    def __init__(self):
        self.uuid = None
        self.addrs = []

class VLDBEntry:
    """
    Attributes:
        name (str): Volume name.
        rwid (int): RW volid.
        roid (int): RO volid.
        bkid (int): BK volid.
        clid (int): Clone volid.
        n_sites (int): Number of sites.
        sites (list of `VLDBSite`): Volume sites.
        weird_sites (bool): True if any sites have a type like "New release",
            "Old release", etc.
        locked (bool): Whether the entry is locked.
        lock_reason (str): Lock reason, if any.
    """
    def __init__(self):
        self.name = None

        self.rwid = None
        self.roid = None
        self.bkid = None
        self.clid = None

        self.n_sites = 0
        self.sites = []
        self.weird_sites = False

        self.locked = False
        self.lock_reason = None

    def __repr__(self):
        attrlist = []
        for attr in ('name', 'rwid', 'roid', 'bkid', 'clid', 'weird_sites',
                     'locked', 'lock_reason', 'n_sites', 'sites'):
            val = getattr(self, attr)
            if val is None:
                val = "<None>"
            attrlist.append("%s %s" % (attr, val))
        return "<VLDBEntry: " + ', '.join(attrlist) + ">"

class VLDBSite:
    """
    Attributes:
        server (str): Server IP.
        partition (str): Server partition.
        vtype (str): Volume type (e.g. 'RW', 'RO').
        release_type (class or str): Either a string describing the relevant
            line in the vos output, or one of the sentinel values:
            `normal_rel`, `new_rel`, `old_rel`, `not_rel`.
    """

    # Sentinel values for VLDBSite.release_type
    class normal_rel:
        pass
    class new_rel:
        pass
    class old_rel:
        pass
    class not_rel:
        pass

    def __init__(self):
        self.server = None
        self.partition = None
        self.vtype = None
        self.release_type = VLDBSite.normal_rel

    def __repr__(self):
        attrlist = []
        for attr in ('server', 'partition', 'vtype', 'release_type'):
            val = getattr(self, attr)
            if val is None:
                val = "<None>"
            attrlist.append("%s %s" % (attr, val))
        return "<VLDBSite: " + ', '.join(attrlist) + ">"

class VolumeHeader:
    """
    Attributes:
        volid (int): Volume ID.
        status (class): One of the sentinel values `busy`, `error`, `online`,
            `offline`.
        name (str): Volume name.
        vtype (str): Volume type (e.g. 'RW', 'RO').
        size (int): Volume size, in KB.
        server (str): Server IP.
        partition (str): Server partition (e.g. '/vicepa').
        rwid (int): Volume RW id.
        roid (int): Volume RO id.
        bkid (int): Volume BK id.
        maxquota (int): Volume quota, in KB.
        create_time (int): Volume creation time, in unix time.
        copy_time (int): Volume "copy" time, in unix time.
        backup_time (int): Volume backup time, in unix time.
        access_time (int): Volume access time, in unix time.
        update_time (int): Volume update time, in unix time.
        accesses (int): Volume accesses in the past day.
        needs_salvage (bool): Whether the volume is flagged as needing salvage.
    """

    # Sentinel values for VolumeHeader.status
    class busy:
        pass
    class error:
        pass
    class online:
        pass
    class offline:
        pass

    _attrs = ('volid', 'status', 'name', 'vtype', 'size', 'server',
              'partition', 'rwid', 'roid', 'bkid', 'maxquota', 'create_time',
              'copy_time', 'backup_time', 'access_time', 'update_time',
              'accesses', 'needs_salvage')
    volid = None
    status = None
    name = None
    vtype = None
    size = None
    server = None
    partition = None
    rwid = None
    roid = None
    bkid = None
    maxquota = None
    create_time = None
    copy_time = None
    backup_time = None
    access_time = None
    update_time = None
    accesses = None
    needs_salvage = None

    def __repr__(self):
        attrlist = []
        for attr in self._attrs:
            val = getattr(self, attr)
            if val is None:
                val = "<None>"
            attrlist.append("%s %s" % (attr, val))
        return "<VolumeHeader: " + ', '.join(attrlist) + ">"

class ACL:
    """
    Attributes:
        normal_rights (list of ACLEntry): Normal (positive) rights.
        negative_rights (list of ACLEntry): Negative rights.
    """
    def __init__(self):
        self.normal_rights = []
        self.negative_rights = []

class ACLEntry:
    """
    Attributes:
        name (str): User/group for the ACL entry.
        rights (str): Rights for the ACL entry (e.g. 'rlidwka').
    """
    def __init__(self, name, rights):
        self.name = name
        self.rights = rights

def _convert_str(byte_str):
    return byte_str.decode('utf-8')

def _convert_time(timestr):
    if timestr == b'Never':
        return 0

    # timestr is e.g. 'Fri Aug 28 11:01:13 2009'
    # Note that the time is in UTC, not the local time
    try:
        # convert from the string to a time_struct
        timestruct = time.strptime(timestr.decode('utf-8'), "%a %b %d %H:%M:%S %Y")

        # convert from the time_struct to the number of seconds since the epoch
        seconds = calendar.timegm(timestruct)
        return seconds

    except Exception as exc:
        raise ParseError("Error parsing time '%s': %r" % (timestr, exc)) from None

#
# How to use a cursor:
#
# cursor = vos.listvldb(noauth=True, ...)
#
# Then either:
#
# res = cursor.run()
# for vlentry in res.entries:
#     do_something(vlentry)
#
# (the following aren't implemented yet)
# or:
#
# for vlentry in cursor:
#     do_something(vlentry)
#
# or
#
# for vlentry in cursor.iter('vlentry'):
#     do_something(vlentry)
#
# or:
#
# def foo(entries):
#     for vlentry in entries:
#         do_something(vlentry)
# cursor.stream(vlentry=foo)

class _ProcessCursor:
    _nosig_delay = 5
    _sigterm_delay = 5
    _sigkill_delay = 5

    def __init__(self, parser, argv):
        self._parser = parser
        self._argv = argv
        self._stdout_closed = False
        self._stderr_closed = False

        self._stdout_stolen = False
        self._stdout_lastbuf = None

        self._proc = None
        self._proc_exit_thread = None

        # Default read buffer, 16 KiB
        self._bufsize = 16 * 1024

    def _popen_exc(self, exc, argv, env):
        if isinstance(exc, (FileNotFoundError, PermissionError)):
            log.error('runpath', "Cannot run '%s', is it in your PATH? (PATH: '%s', full command: '%s')" % \
                                 (argv[0], env.get('PATH', ''), ' '.join(argv)))
        else:
            log.error('popen', "Error running command '%s'" % ' '.join(argv))

    def _popen(self, argv, env):
        return subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                env=env)

    def start(self):
        if self._proc:
            return
        env = os.environ.copy()
        # Force the use of UTC timestamps, to avoid any races or other issues
        # with parsing output from the local time zone.
        env['TZ'] = 'UTC'
        # Make sure the subprocess is not using a different time locale than
        # us, since we need to parse the time strings.
        env['LC_TIME'] = locale.setlocale(locale.LC_TIME, None)
        try:
            self._proc = self._popen(self._argv, env)
        except Exception as exc:
            try:
                self._popen_exc(exc, self._argv, env)
            except Exception:
                pass
            # Re-raise the original exception
            raise exc

        self._proc.stdin.close()
        # maybe set stdout, stderr to be non-blocking?

    def get_proc(self):
        return self._proc

    def _read_step(self, bufsize=None, timeout=None):
        self._stdout_lastbuf = None
        if self._stdout_closed and self._stderr_closed:
            # stdout and stderr both closed? There's nothing to read from,
            # nothing to do
            return False

        if bufsize is None:
            bufsize = self._bufsize
        if timeout is None:
            timeout = self._parser.timeout

        poll = select.poll()
        if not self._stdout_closed:
            poll.register(self._proc.stdout, select.POLLIN)
        if not self._stderr_closed:
            poll.register(self._proc.stderr, select.POLLIN)

        # self._timeout is in seconds, but poll wants a timeout in ms
        ready = poll.poll(int(timeout * 1000))
        if not ready:
            raise ProcessError("Command timed out after we waited %d seconds for output" % timeout,
                               argv=self._argv)

        try:
            for fd, _ in ready:
                buf = os.read(fd, bufsize)
                if not self._stdout_closed and fd == self._proc.stdout.fileno():
                    if not buf:
                        self._stdout_closed = True
                    else:
                        if self._stdout_stolen:
                            self._stdout_lastbuf = buf
                        else:
                            self._parser.got_stdout(buf)
                else:
                    assert fd == self._proc.stderr.fileno()
                    if not buf:
                        self._stderr_closed = True
                    else:
                        self._parser.got_stderr(buf)

            if self._stdout_closed and self._stderr_closed:
                self._parser.done()
                return False
            return True

        except ParseError as exc:
            # If the underlying process exited with a failure, maybe that broke
            # our parsing. So throw the 'exit code' error instead of the 'parse
            # error' exception, since the failed command is more likely the
            # real error.

            # Give the process a chance to die
            time.sleep(1)
            self._check_retcode()

            self._throw_err("%s" % exc, tback=exc.__traceback__)

    def _graceful_exit(self, delay_seconds):
        def _inthread():
            self._proc.wait()
        if self._proc_exit_thread is None:
            self._proc_exit_thread = threading.Thread(target=_inthread)
            self._proc_exit_thread.daemon = True
            self._proc_exit_thread.start()

        self._proc_exit_thread.join(delay_seconds)
        if self._proc_exit_thread.is_alive():
            # Exiting thread is still alive; we have not exited gracefully
            return False
        else:
            return True

    def _cleanup_proc(self, graceful):
        with self._proc:
            self._proc.poll()
            if self._proc.returncode is not None:
                # Child has exited; we're okay
                return

            if graceful:
                # Process hasn't exited yet. First, wait for it to exit on
                # its own.
                if self._graceful_exit(self._nosig_delay):
                    return

                # Process didn't quit quickly enough; ask it to quit.
                self._proc.terminate()
                if self._graceful_exit(self._sigterm_delay):
                    return

            # Process is still taking too long; forcibly kill it.
            self._proc.kill()
            if not self._graceful_exit(self._sigkill_delay):
                # It just won't die; throw an exception since we can't
                # get the return code.
                raise ProcessError("Timed out waiting for process to die", argv=self._argv)

    def steal_stdout(self):
        self._stdout_stolen = True

    def read_stdout(self, bufsize, timeout):
        if not self._stdout_stolen:
            raise RuntimeError("Cursor read_stdout() called, but stdout is not stolen")

        graceful = False
        more = False
        try:
            more = self._read_step(bufsize=bufsize, timeout=timeout)
            while more and self._stdout_lastbuf is None:
                # If 'more' is True, but _stdout_lastbuf is None, we had better
                # try again to read some more data.
                more = self._read_step(bufsize=bufsize, timeout=timeout)
            graceful = True
        finally:
            if not more or not graceful:
                self._cleanup_proc(graceful)

        if more:
            # If we have more data to read, we had better not return None,
            # since None indicates EOF.
            buf = self._stdout_lastbuf
            assert buf is not None
            return buf

        else:
            # If we have no more data, _stdout_lastbuf had better not have any
            # data in it.
            assert self._stdout_lastbuf is None
            self._check_retcode()
            return None

    def _throw_err(self, message, tback=None):
        parser_err = self._parser.error_str()
        err_str = parser_err
        if err_str:
            err_str = "\n" + err_str
        exc = ProcessError("%s%s" % (message, err_str),
                           argv=self._argv,
                           stderr=parser_err)
        if tback is not None:
            exc = exc.with_traceback(tback)
        raise exc

    def _check_retcode(self):
        if self._proc:
            self._proc.poll()
            if self._proc.returncode is not None and self._proc.returncode != 0:
                self._throw_err("Process died with code %d" % self._proc.returncode)

    def run(self):
        if self._stdout_stolen:
            raise RuntimeError("Cursor run() called, but stdout is stolen")

        try:
            graceful = False
            try:
                while self._read_step():
                    pass
                graceful = True

            finally:
                self._cleanup_proc(graceful)

            self._check_retcode()
            return self._parser.get_result()

        except EnvironmentError as e:
            raise ProcessError("%s: %s" % (type(e).__name__, e), argv=self._argv) from e

def _Cursor(parser, argv):
    cursor = _ProcessCursor(parser, argv)
    cursor.start()
    return cursor

class DumpScan:
    def __init__(self, afsdump_scan=None):
        if afsdump_scan is None:
            afsdump_scan = ['afsdump_scan']
        self._afsdump_scan = afsdump_scan

    def scan_volheader(self, filename):
        """
        Run 'afsdump_scan -PV' against the given dump blob path.

        Returns:
            `_Cursor`: A cursor that returns a `VolumeHeader` from `run`.
        """
        parser = ScanVolheaderParser()
        argv = self._afsdump_scan + ['-PV', filename]
        return _Cursor(parser, argv)

    def scan_symlinks(self, filename):
        """
        Run 'afsdump_scan -Pvip' against the given dump blob path.

        Returns:
            `Cursor`: A cursor that returns a list of (path, target) pairs from
            `run` for each symlink in the given dump.
        """
        parser = ScanSymlinkParser()
        argv = self._afsdump_scan + ['-Pvip', filename]
        return _Cursor(parser, argv)

class _Base:
    def _args_to_cli(self, args):
        """
        Converts kwargs like foo(cell='bar', name='baz') to CLI args like
        ['-cell', 'bar', '-name', 'baz'].

        Also merges `_default_args` in with the provided args.

        Args:
            args (dict): A kwargs-style dict to convert.

        Returns:
            list of str: The CLI argument list.
        """
        all_args = self._default_args.copy()
        all_args.update(args)

        ret = []
        for (key, val) in all_args.items():
            if val is None:
                # A flag with a parameter, like -cell, but disabled
                pass

            elif val is False:
                # A flag like -noauth, but turned off
                pass

            elif val is True:
                # A flag like -noauth
                ret.append("-%s" % key)

            else:
                # A flag with a parameter, like -cell
                ret.append("-%s" % key)
                ret.append(str(val))
        return ret

    def get_parser(self, name):
        """
        Returns:
            `_BaseParser`: The parser used for parsing the output of our
            subcommand.
        """
        if name not in self._parsers:
            return None
        klass = self._parsers[name]
        return klass()

    def __getattr__(self, name):
        parser = self.get_parser(name)
        if parser is None:
            raise AttributeError("'%s' object has no attribute %r" %
                                 (type(self).__name__, name))

        # This is the function we return to the caller, so this is what
        # actually runs when e.g. vos.foo(bar=baz) is called. When that is
        # called, return a _Cursor
        def runcmd(**kwargs):
            # Maybe we should default to -nosort and -noresolve for commands
            # that accept them
            argv = self._cmd + [name] + self._args_to_cli(kwargs)
            parser.args = kwargs

            return _Cursor(parser, argv)

        return runcmd

class Pts(_Base):
    _parsers = _pts_parsers

    def __init__(self, pts=None, defaults=None):
        if pts is None:
            pts = ['pts']
        self._cmd = pts

        if defaults is None:
            defaults = {}
        self._default_args = defaults

class Fs(_Base):
    _parsers = _fs_parsers

    def __init__(self, fs=None, defaults=None):
        if fs is None:
            fs = ['fs']
        self._cmd = fs

        if defaults is None:
            defaults = {}
        self._default_args = defaults

class Vos(_Base):
    _parsers = _vos_parsers

    def __init__(self, vos=None, defaults=None):
        if vos is None:
            vos = ['vos']
        self._cmd = vos

        if defaults is None:
            defaults = {}
        self._default_args = defaults

class _TextLines:
    lines_limit = 100
    _newline = b'\n'
    _newline_re = re.compile(b'(%s)' % _newline)

    def __init__(self, cb=None, strip_lines=True):
        self.lines = []

        self._lastline = b''
        self._cb = cb
        self._strip_lines = strip_lines

    @classmethod
    def splitnl(cls, text):
        """
        Split the given lines of text.

        This is basically `splitlines(True)`, but we only split on the '\n'
        character (retaining '\r', if it's present), and we retain the trailing
        '\n' on each line that has one.

        Args:
            text (bytes): Text to split.

        Returns:
            list of bytes: The split lines.
        """

        # On "foo\nbar\nbaz" this gives ["foo", "\n", "bar", "\n", "baz"]
        raw_lines = cls._newline_re.split(text)

        # Each entry in raw_lines alternates between an actual line and a
        # newline char
        on_newline = False
        lines = []

        for cur_line in raw_lines:
            if on_newline:
                assert cur_line == cls._newline
                lines[-1] = lines[-1] + cur_line
                on_newline = False

            else:
                lines.append(cur_line)
                on_newline = True

        if lines[-1] == b'':
            # If the very last line is blank, that means the string ended with
            # '\n\n' or something. To behave more similarly to splitlines(),
            # don't return the last empty line. The trailing newline characters
            # are covered by the penultimate line, which would contain just
            # '\n' in this example.
            del lines[-1]

        return lines

    def _got_lines(self, lines):
        if self._cb:
            for line in lines:
                self._cb(line)
        else:
            self.lines.extend(lines)
            # If we have more than N lines, just remember the last N lines
            if len(self.lines) > self.lines_limit:
                self.lines = self.lines[-self.lines_limit:]

    def new_text(self, text, prefix=b''):
        text = self._lastline + text
        lines = self.splitnl(text)

        if not lines:
            self._lastline = b''
            return

        for index, line in enumerate(lines):
            if index > 0 or not self._lastline:
                # self._lastline already has the timestamp added to it, so if
                # we added self._lastline to the first line, don't add the
                # timestamp yet again
                line = prefix + line
                lines[index] = line

        # This just tests if lines[-1] ends with a newline, without doing any
        # newline-related stuff here (we just see if splitnl() splits it).
        if len(self.splitnl(lines[-1] + b"foo")) == 1:
            # The last line in 'lines' is not a complete line (it doesn't end
            # with a newline). So save it in self._lastline and give it to the
            # caller when the line is complete.
            self._lastline = lines[-1]
            lines = lines[:-1]
        else:
            self._lastline = b''

        # If _strip_lines is set, strip trailing whitespace on each line.
        # Otherwise, just strip the trailing newline.
        strip_chars = None
        if not self._strip_lines:
            strip_chars = b'\n'

        for index, line in enumerate(lines):
            lines[index] = line.rstrip(strip_chars)

        self._got_lines(lines)

    def tail(self, nlines):
        if self._lastline:
            nlines -= 1

        ret = self.lines[-nlines:]
        if self._lastline:
            ret += [self._lastline]
        return ret

    def done(self):
        if self._lastline:
            self._got_lines([self._lastline])

class _BaseParser:
    # By default, timeout after 10 minutes of inactivity
    timeout = 60 * 10 # 10 minutes

    _patterns = None

    def __init__(self, strip_lines=True, args=None):
        self.args = args

        # We just save copies of all stdout and stderr for diagnostics, in
        # case we encounter an error
        self._stdout_lines = _TextLines(strip_lines=False)
        self._stderr_lines = _TextLines(strip_lines=False)

        self._data_lines = _TextLines(cb=self._parse_line, strip_lines=strip_lines)

    def error_str(self):
        ret = ''

        ret += "stdout:\n"
        for line in self._stdout_lines.tail(24):
            ret += "%r\n" % line

        ret += "stderr:\n"
        for line in self._stderr_lines.tail(24):
            ret += "%r\n" % line

        return ret

    def parse_line_data(self, data):
        raise NotImplementedError

    def _get_timestamp(self):
        # e.g. b'2014-06-23 09:32:52.781723 -0500: '
        return (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f') +
                time.strftime(' %z: ')).encode('utf-8')

    # Got data on stdout
    def got_stdout(self, data):
        for line in self._stdout_lines.splitnl(data):
            self._stdout_lines.new_text(line,
                                        prefix=self._get_timestamp())
            self._data_lines.new_text(line)

    # Got data on stderr
    def got_stderr(self, data):
        self._stderr_lines.new_text(data,
                                    prefix=self._get_timestamp())

    def _parse_line(self, line):
        data = self._patterns.parse(line)
        if data is _PatternSkip:
            return
        if data is _PatternNotMatched:
            raise ParseError("Unmatched cmd output line: '%s'" % line)

        self.parse_line_data(data)
        if data:
            raise ValueError("Unknown data keys unprocessed: %s" % ','.join(key for key in data))

    def done(self):
        for textlines in (self._stdout_lines, self._stderr_lines, self._data_lines):
            textlines.done()
        self.parse_line_data(None)

class _Match:
    def match(self, rematch, regroup, data):
        raise NotImplementedError

class _MatchVal(_Match):
    def __init__(self, key, converter):
        self._key = key
        self._converter = converter

    def match(self, rematch, regroup, data):
        raw_val = rematch.group(regroup)

        data[self._key] = self._converter(raw_val)
        return True

class _MatchOptional(_MatchVal):
    def match(self, rematch, regroup, data):
        raw_val = rematch.group(regroup)
        if raw_val is not None:
            data[self._key] = self._converter(raw_val)
        return True

class _MatchConst(_Match):
    def __init__(self, key, val):
        self._key = key
        self._val = val

    def match(self, rematch, regroup, data):
        data[self._key] = self._val
        return False

# Sentinel return value for _Pattern.parse()
class _PatternSkip:
    pass

class _PatternNotMatched:
    pass

# Currently works like:
# _Pattern(rb'^\s+Foo is (\d+)$', _MatchVal('foo', int))
#
# but instead of self.parse_line_data, should probably do something like:
# _Pattern(rb'^\s+Foo is (?<foo>\d+)$', self.match_thing)
#
# or maybe could do a decorator like...
# @match(rb'^\s+Foo is (?<foo>\d+)$')
# def match_thing(self, foo):
#
# or
# _Pattern(rb'^\s+Foo is (?<foo>\d+)$', __class__.match_thing)
#
class _Pattern:
    def __init__(self, patstr, *matches, **kwargs):
        # Create a list copy to let us change items in 'matches'
        matches = [match for match in matches]

        if isinstance(patstr, str):
            raise TypeError("cmd _Patterns must be byte-strings")

        self._re = re.compile(patstr)
        self._patstr = patstr
        self._matches = matches

        self._init_kwargs(**kwargs)

        assert self._matches or self._skip
        for index, match in enumerate(matches):
            if isinstance(match, str):
                match = _MatchVal(match, _convert_str)
                self._matches[index] = match

            assert isinstance(match, _Match)

    def _init_kwargs(self, skip=False):
        self._skip = skip

    def parse(self, line):
        rematch = self._re.search(line)
        if not rematch:
            return _PatternNotMatched
        if self._skip:
            return _PatternSkip

        ret = {}

        # Pass each regex capture group to the appropriate _Match object
        regroup = 1
        for match in self._matches:
            swallowed = match.match(rematch, regroup, ret)
            # Only move on to the next regex capture group if the _Match
            # object used up the
            if swallowed:
                regroup += 1

        # We must have used up all of the match groups, or something is wrong
        assert regroup == len(rematch.groups())+1

        return ret

# We already have some match results in parent_match, and we got a new match
# result in 'match'. This function returns what we should give to the caller;
# usually we just merge in the values from 'match' into 'parent_match'.
def _MergeMatch(parent_match, match):
    if (parent_match is _PatternSkip) or (match is _PatternSkip):
        return _PatternSkip
    if (parent_match is _PatternNotMatched) and (match is _PatternNotMatched):
        return _PatternNotMatched
    ret = {}
    if parent_match is not _PatternNotMatched:
        ret.update(parent_match)
    if match is not _PatternNotMatched:
        ret.update(match)
    return ret

class _PatternSingle:
    def __init__(self, *patterns, **kwargs):
        self._patterns = patterns
        self._init_kwargs(**kwargs)

    def _init_kwargs(self, onlyif=None):
        self._onlyif = onlyif

    def parse(self, line):
        onlyif_match = _PatternNotMatched

        if self._onlyif is not None:
            onlyif_match = self._onlyif.parse(line)

            # If the 'onlyif' pattern did not match, bail out
            if onlyif_match is _PatternNotMatched:
                return onlyif_match

            # We want to merge this 'match' into the sub-pattern match, but we
            # can't really merge a 'skip' match, so... just say 'skip'
            if onlyif_match is _PatternSkip:
                return onlyif_match

        return self._parse_subpats(line, onlyif_match)

    def _parse_subpats(self, line, parent_match):
        for pattern in self._patterns:
            match = pattern.parse(line)
            if match is not _PatternNotMatched:
                return _MergeMatch(parent_match, match)
        return parent_match

class _PatternAll(_PatternSingle):
    def _parse_subpats(self, line, parent_match):
        ret = parent_match

        for pattern in self._patterns:
            match = pattern.parse(line)

            # Merge the sub-pattern match into our own.
            ret = _MergeMatch(ret, match)

            # If the current match says to 'skip', any future matches aren't
            # going to change that. So just skip right now.
            if ret is _PatternSkip:
                return ret

        return ret

class ProcessError(Exception):
    def __init__(self, message, argv=None, stderr=None):
        if argv:
            message = "While running '%s': %s" % (' '.join(argv), message)
        self.stderr = stderr
        super().__init__(message)

class ParseError(ProcessError):
    pass

class ListVLDBParser(_BaseParser):

    # REs for parsing vos output
    # The first element in each 'row' is the RE to match against. The next
    # elements are what names to use to capture the groups in the RE. If there
    # is no captured group for that element, None goes in there instead. We
    # return the captured groups as a dict to the caller.
    #
    # _PatternSingle means we stop on the first match. _PatternAll means we
    # keep going even after we've found a match.

    _patterns = _PatternSingle(
        _Pattern(rb'^\s*$', skip=True),
        _Pattern(rb'^VLDB entries for ', skip=True),

        _Pattern(rb'^Total entries:\s+(\d+)', _MatchVal('total_entries', int)),
        _Pattern(rb'^(\S.*)$', 'volname'),

        _PatternAll(
            _Pattern(rb'\s+RWrite:\s*(\d+)', _MatchVal('rwid', int)),
            _Pattern(rb'\s+ROnly:\s*(\d+)', _MatchVal('roid', int)),
            _Pattern(rb'\s+Backup:\s*(\d+)', _MatchVal('bkid', int)),
            _Pattern(rb'\s+RClone:\s*(\d+)', _MatchVal('clid', int)),
        ),

        _Pattern(rb'^\s+number of sites ->\s+(\d+)', _MatchVal('n_sites', int)),

        _PatternSingle(
            _Pattern(rb' -- New release$', _MatchConst('release_type', VLDBSite.new_rel)),
            _Pattern(rb' -- Old release$', _MatchConst('release_type', VLDBSite.old_rel)),
            _Pattern(rb' -- Not released$', _MatchConst('release_type', VLDBSite.not_rel)),
            _Pattern(rb' -- (.*)$', 'release_type'),

            onlyif=_Pattern(rb'^\s+server\s+(\S+)\s+partition\s+(/vicep\w+)\s+([A-Z]{2})\s+Site(?:\s|$)',
                            'server', 'partition', 'vtype'),
        ),

        _Pattern(rb'^\s+Volume is currently LOCKED$', _MatchConst('locked', True)),
        _Pattern(rb'^\s+Volume is locked for (.+)$', 'lock_reason'),
    )

    def __init__(self):
        super().__init__()
        self._result = ListVLDBResult()
        self._cur_vlentry = None

    def _got_vlentry(self):
        if self._cur_vlentry is not None:
            if self._cur_vlentry.locked:
                self._result.locked_entries += 1

            self._result.entries.append(self._cur_vlentry)
            self._cur_vlentry = None

    def get_result(self):
        return self._result

    # 'data' is a dict, with the keys as in the above patterns
    # or 'data' is None, if we're done parsing stuff
    def parse_line_data(self, data):
        if data is None:
            self._got_vlentry()
            return

        if 'total_entries' in data:
            self._result.total_entries = int(data['total_entries'])
            del data['total_entries']

        if 'volname' in data:
            self._got_vlentry()
            self._cur_vlentry = VLDBEntry()
            self._cur_vlentry.name = data['volname']
            del data['volname']

        for key in ('rwid', 'roid', 'bkid', 'clid', 'n_sites', 'locked', 'lock_reason'):
            if key in data:
                if self._cur_vlentry is None:
                    raise ParseError("Saw '%s' before volume name" % key)
                setattr(self._cur_vlentry, key, data[key])
                del data[key]

        if 'server' in data:
            if self._cur_vlentry is None:
                raise ParseError("Saw site info before volume name")
            if self._cur_vlentry.n_sites is None:
                raise ParseError("Saw site info before n_sites")

            cursite = VLDBSite()

            for key in ('server', 'partition', 'vtype', 'release_type'):
                if key in data:
                    setattr(cursite, key, data[key])
                    del data[key]

            self._cur_vlentry.sites.append(cursite)
            if cursite.release_type is not VLDBSite.normal_rel:
                self._cur_vlentry.weird_sites = True

_vos_parsers['listvldb'] = ListVLDBParser



class SizeParser(_BaseParser):
    _patterns = _PatternSingle(
        _Pattern(rb'^Volume: ', skip=True),
        _Pattern(rb'^dump_size: (\d+)$', 'dump_size'),
    )

    def __init__(self):
        super().__init__()
        self._dump_size = None

    def get_result(self):
        return self._dump_size

    def parse_line_data(self, data):
        if data is None:
            return

        if 'dump_size' in data:
            self._dump_size = int(data['dump_size'])
            del data['dump_size']

_vos_parsers['size'] = SizeParser

class ListAddrsParser(_BaseParser):
    _patterns = _PatternSingle(
        _Pattern(rb'^UUID: ([0-9a-fA-F-]+)$', 'uuid'),
        _Pattern(rb'^(.+)$', 'addr'),
        _Pattern(rb'^$', skip=True),
    )

    def __init__(self):
        super().__init__()
        self._cur_server = None
        self._result = ListAddrsResult()

    def get_result(self):
        return self._result

    def _got_server(self):
        if self._cur_server is not None:
            self._result.servers.append(self._cur_server)
        self._cur_server = None

    def parse_line_data(self, data):
        if data is None:
            # Done parsing
            self._got_server()
            return
        if 'uuid' in data:
            self._got_server()
            self._cur_server = ServerAddrs()
            self._cur_server.uuid = data['uuid'].lower()
            del data['uuid']

        if 'addr' in data:
            if self._cur_server is None:
                self._cur_server = ServerAddrs()
            self._cur_server.addrs.append(data['addr'])

            if self._cur_server.uuid is None and 'uuid' not in self.args:
                # We're getting individual addresses, not associated addresses
                # with uuids. So each line we get is a new "server".
                self._got_server()
            del data['addr']

_vos_parsers['listaddrs'] = ListAddrsParser

class ExamineParser(_BaseParser):
    _patterns = _PatternSingle(
        _Pattern(rb'^$', skip=True),
        _Pattern(rb'^Fetching VLDB entry for ', skip=True),
        _Pattern(rb'^Getting volume listing from ', skip=True),

        _Pattern(rb'^\*\*\*\* Volume (\d+) is busy ', _MatchVal('volid', int),
                                                     _MatchConst('status', VolumeHeader.busy)),

        _Pattern(rb'^\*\*\*\* Could not attach volume (\d+) ', _MatchVal('volid', int),
                                                              _MatchConst('status', VolumeHeader.error)),

        _Pattern(rb'^(\S+)\s+(\d+)\s+(RW|RO|BK)\s+(\d+)\s+K\s+([\w-]+)(\*\*needs salvage\*\*)?$',
                 'name', _MatchVal('volid', int), 'vtype', _MatchVal('size', int),
                 'status_str', _MatchOptional('salvage_str', _convert_str)),

        _Pattern(rb'^\s+(\S+)\s+(/vicep\w+)\s*$', 'server', 'partition'),

        _Pattern(rb'^\s+RWrite\s+(\d+)\s+ROnly\s+(\d+)\s+Backup\s+(\d+)\s*$',
                 _MatchVal('rwid', int), _MatchVal('roid', int), _MatchVal('bkid', int)),

        _Pattern(rb'^\s+MaxQuota\s+(\d+)\s+K\s*', _MatchVal('maxquota', int)),

        _Pattern(rb'^\s+Creation\s+(.*)\s*$', _MatchVal('create_time', _convert_time)),
        _Pattern(rb'^\s+Copy\s+(.*)\s*$', _MatchVal('copy_time', _convert_time)),
        _Pattern(rb'^\s+Backup\s+(.*)\s*$', _MatchVal('backup_time', _convert_time)),
        _Pattern(rb'^\s+Last Access\s+(.*)\s*$', _MatchVal('access_time', _convert_time)),
        _Pattern(rb'^\s+Last Update\s+(.*)\s*$', _MatchVal('update_time', _convert_time)),

        _Pattern(rb'^\s+(\d+) accesses in the past day', _MatchVal('accesses', int)),

        # The rest of these are for matching VLDB data, so the pattern match
        # lines are the same as for VLDB info

        _PatternAll(
            _Pattern(rb'\s+RWrite:\s*(\d+)', _MatchVal('vl_rwid', int)),
            _Pattern(rb'\s+ROnly:\s*(\d+)', _MatchVal('vl_roid', int)),
            _Pattern(rb'\s+Backup:\s*(\d+)', _MatchVal('vl_bkid', int)),
            _Pattern(rb'\s+RClone:\s*(\d+)', _MatchVal('vl_clid', int)),
        ),

        _Pattern(rb'^\s+number of sites ->\s+(\d+)', _MatchVal('n_sites', int)),

        _PatternSingle(
            _Pattern(rb' -- New release$', _MatchConst('release_type', VLDBSite.new_rel)),
            _Pattern(rb' -- Old release$', _MatchConst('release_type', VLDBSite.old_rel)),
            _Pattern(rb' -- Not released$', _MatchConst('release_type', VLDBSite.not_rel)),
            _Pattern(rb' -- (.*)$', 'release_type'),

            onlyif=_Pattern(rb'^\s+server\s+(\S+)\s+partition\s+(/vicep\w+)\s+([A-Z]{2})\s+Site(?:\s|$)',
                            'vl_server', 'vl_partition', 'vl_vtype'),
        ),

        _Pattern(rb'^\s+Volume is currently LOCKED$', _MatchConst('locked', True)),
        _Pattern(rb'^\s+Volume is locked for (.+)$', 'lock_reason'),

        # If we can't reach the target server, vos will output some stuff on
        # stderr, and will show some vldb-only info, including the volume name
        # on a line by itself. We don't print that out in any other situation,
        # so this is a special error-only case; if we don't catch the data
        # here, it will result in a parsing error.
        _Pattern(rb'^(\S+)$', 'err_volname'),
    )

    def __init__(self):
        super().__init__()
        self._result = ExamineResult()
        self._cur_volheader = None
        self._err = False

    def get_result(self):
        return self._result

    def _got_volheader(self):
        if self._cur_volheader is not None:
            self._result.headers.append(self._cur_volheader)
            self._cur_volheader = None

    def parse_line_data(self, data):
        if data is None:
            # Done parsing
            self._got_volheader()
            return

        # First, detect if we're looking at error output
        if 'err_volname' in data:
            self._err = True
            del data['err_volname']

        if self._err:
            # These keys we can see when 'vos' prints out the VLDB-only
            # information after an error
            for key in ('vl_rwid', 'vl_roid', 'vl_bkid', 'vl_clid', 'n_sites',
                        'vl_server', 'vl_partition', 'vl_vtype', 'release_type',
                        'locked', 'lock_reason'):
                if key in data:
                    del data[key]

            # Don't do any normal processing, since there's an error.
            return

        # Otherwise first, handle Volume Header stuff

        if 'volid' in data:
            self._got_volheader()
            self._cur_volheader = VolumeHeader()

        # Keys where we can just set the appropriate attr in self._cur_volheader
        # directly.
        for key in ('volid', 'status', 'name', 'vtype', 'size', 'server', 'partition',
                    'rwid', 'roid', 'bkid', 'maxquota', 'create_time', 'copy_time',
                    'backup_time', 'access_time', 'update_time', 'accesses'):
            if key in data:
                if self._cur_volheader is None:
                    raise ParseError("Saw '%s' before volid" % key)
                setattr(self._cur_volheader, key, data[key])
                del data[key]

        if 'status_str' in data:
            if self._cur_volheader is None:
                raise ParseError("Saw 'status_str' before volid")
            status_str = data['status_str']
            if status_str == 'Off-line':
                self._cur_volheader.status = VolumeHeader.offline
            elif status_str == 'On-line':
                self._cur_volheader.status = VolumeHeader.online
            else:
                raise ParseError("Unknown volume status '%s'" % status_str)
            del data['status_str']

        if 'salvage_str' in data:
            if self._cur_volheader is None:
                raise ParseError("Saw 'salvage_str' before volid")
            self._cur_volheader.needs_salvage = True
            del data['salvage_str']

        # Now handle VLDB stuff

        for key in ('vl_rwid', 'vl_roid', 'vl_bkid', 'vl_clid'):
            if key in data:
                if self._result.vlentry is None:
                    self._result.vlentry = VLDBEntry()
                break

        for key in ('vl_rwid', 'vl_roid', 'vl_bkid', 'vl_clid', 'n_sites', 'locked', 'lock_reason'):
            if key in data:
                if self._result.vlentry is None:
                    raise ParseError("Saw '%s' before volume VLDB ids" % key)
                orig_key = key
                if key.startswith('vl_'):
                    key = key[3:]
                setattr(self._result.vlentry, key, data[orig_key])
                del data[orig_key]

        if 'vl_server' in data:
            if self._result.vlentry is None:
                raise ParseError("Saw site info before VLDB ids")
            if self._result.vlentry.n_sites is None:
                raise ParseError("Saw site info before n_sites")

            cursite = VLDBSite()

            for key in ('vl_server', 'vl_partition', 'vl_vtype', 'release_type'):
                if key in data:
                    orig_key = key
                    if key.startswith('vl_'):
                        key = key[3:]
                    setattr(cursite, key, data[orig_key])
                    del data[orig_key]

            self._result.vlentry.sites.append(cursite)
            if cursite.release_type is not VLDBSite.normal_rel:
                self._result.vlentry.weird_sites = True

_vos_parsers['examine'] = ExamineParser

class WscellParser(_BaseParser):
    _patterns = _Pattern(rb"^This workstation belongs to cell '(.*)'\s*$", 'cell')

    def __init__(self):
        super().__init__()
        self._result = None

    def get_result(self):
        return self._result

    def parse_line_data(self, data):
        if data is None:
            return

        if 'cell' in data:
            if self._result is not None:
                raise ParseError("Saw more than one cell? '%s' and '%s'" % \
                                 (self._result, data['cell']))
            self._result = data['cell']
            del data['cell']

_fs_parsers['wscell'] = WscellParser

class ListaclParser(_BaseParser):
    _patterns = _PatternSingle(
        _Pattern(rb'^Access list for .*', skip=True),

        _Pattern(rb'^Normal rights:\s*', _MatchConst('normal_rights', True)),
        _Pattern(rb'^Negative rights:\s*', _MatchConst('negative_rights', True)),

        _Pattern(rb'^  (\S+) ([rlidwkaA-H]+)\s*', 'name', 'rights'),
    )

    do_normal = False
    do_negative = False

    def __init__(self):
        super().__init__()
        self.result = ACL()

    def get_result(self):
        return self.result

    def parse_line_data(self, data):
        if data is None:
            return

        if 'normal_rights' in data:
            self.do_normal = True
            del data['normal_rights']

        if 'negative_rights' in data:
            self.do_normal = False
            self.do_negative = True
            del data['negative_rights']

        if 'name' in data:
            if self.do_normal:
                acl_list = self.result.normal_rights
            elif self.do_negative:
                acl_list = self.result.negative_rights
            else:
                raise ParseError("Saw acl entry without normal/negative header? %s" %
                                 data['name'])

            assert 'rights' in data

            acl_list.append(ACLEntry(data['name'], data['rights']))

            del data['name']
            del data['rights']

_fs_parsers['listacl'] = ListaclParser

class MembershipParser(_BaseParser):
    _patterns = _PatternSingle(
        _Pattern(rb'^Groups .* is a member of:\s*', skip=True),
        _Pattern(rb'^  (\S+)\s*$', 'group'),
    )

    def __init__(self):
        super().__init__()
        self.groups = []

    def get_result(self):
        return self.groups

    def parse_line_data(self, data):
        if data is None:
            return

        if 'group' in data:
            self.groups.append(data['group'])
            del data['group']

_pts_parsers['membership'] = MembershipParser

# Parser class for commands that don't generate any output we actually need
class _NopParser(_BaseParser):
    _patterns = _Pattern(rb'^', skip=True)

    def get_result(self):
        return None

    # Should never get called, except if 'data' is None, indicating that we're
    # done parsing data.
    def parse_line_data(self, data):
        assert data is None

_vos_parsers['backup'] = _NopParser
_vos_parsers['remove'] = _NopParser

class DumpParser(_NopParser):
    # Set an extremely long timeout for 'vos dump's, since running a dump can
    # take a very long time
    timeout = 60 * 60 * 24 # 24 hours
_vos_parsers['dump'] = DumpParser

class RestoreParser(_NopParser):
    # Set an extremely long timeout for 'vos restore's, since running a restore
    # can take a very long time
    timeout = 60 * 60 * 24 # 24 hours
_vos_parsers['restore'] = RestoreParser

_fs_parsers['mkmount'] = _NopParser
_fs_parsers['rmmount'] = _NopParser

class ScanVolheaderParser(_BaseParser):
    _patterns = _PatternSingle(
        _Pattern(rb'^[*] VOLUME HEADER', skip=True),

        _Pattern(rb'^ Volume ID:\s+([0-9]+)$', _MatchVal('volid', int)),

        _Pattern(rb'^ Version:\s+[0-9]+', skip=True),

        _Pattern(rb'^ Volume name:\s+(\S+)$', 'name'),

        _Pattern(rb'^ In service[?]\s+(?:true|false)$', skip=True),
        _Pattern(rb'^ Blessed[?]\s+(?:true|false)$', skip=True),
        _Pattern(rb'^ Uniquifier:\s+\d+$', skip=True),

        _Pattern(rb'^ Type:\s+0$', _MatchConst('vtype', 'RW')),
        _Pattern(rb'^ Type:\s+1$', _MatchConst('vtype', 'RO')),
        _Pattern(rb'^ Type:\s+2$', _MatchConst('vtype', 'BK')),

        _Pattern(rb'^ Parent ID:\s+(\d+)$', _MatchVal('rwid', int)),
        _Pattern(rb'^ Clone ID:\s+\d+$', skip=True),

        _Pattern(rb'^ Max quota:\s+(\d+)$', _MatchVal('maxquota', int)),

        _Pattern(rb'^ Min quota:\s+\d+$', skip=True),

        _Pattern(rb'^ Disk used:\s+(\d+)$', _MatchVal('size', int)),

        _Pattern(rb'^ File count:\s+\d+$', skip=True),
        _Pattern(rb'^ Account:\s+\d+$', skip=True),
        _Pattern(rb'^ Owner:\s+\d+$', skip=True),

        _Pattern(rb'^ Created:\s+(.*)$', _MatchVal('create_time', _convert_time)),
        _Pattern(rb'^ Accessed:\s+(.*)$', _MatchVal('access_time', _convert_time)),
        _Pattern(rb'^ Updated:\s+(.*)$', _MatchVal('update_time', _convert_time)),
        _Pattern(rb'^ Expires:\s+.*$', skip=True),
        _Pattern(rb'^ Backed up:\s+(.*)$', _MatchVal('backup_time', _convert_time)),

        _Pattern(rb'^ Offl?ine Msg:.*$', skip=True),
        _Pattern(rb'^ MOTD:.*$', skip=True),
        _Pattern(rb'^ Weekuse:.*$', skip=True),
        _Pattern(rb'^ Dayuse Date:.*$', skip=True),
        _Pattern(rb'^ Daily usage:\s+(\d+)$', _MatchVal('accesses', int)),
    )

    def __init__(self):
        super().__init__()
        self._header = VolumeHeader()

    def get_result(self):
        return self._header

    def parse_line_data(self, data):
        if data is None:
            return
        for key in ('volid', 'rwid', 'status', 'name', 'vtype', 'size',
                    'maxquota', 'create_time', 'backup_time', 'access_time',
                    'update_time', 'accesses'):
            if key in data:
                setattr(self._header, key, data[key])
                del data[key]

class ScanSymlinkParser(_BaseParser):
    _patterns = _PatternSingle(
        _Pattern(rb'^ VNode type:\s+Symbolic Link .3.',
                 _MatchConst('symlink', True)),
        _Pattern(rb'^ VNode type:', _MatchConst('symlink', False)),

        _Pattern(rb'^ ?Target:\s+(.+)$', 'target'),

        _Pattern(rb'^ Path: (/.*)$', 'path'),

        # Skip any other recognized fields; don't just skip everything, in
        # case there's some weird or otherwise unhandled output. We want to
        # get an error if something weird is going on.
        _Pattern(b'^(?:[*] VNODE | Link count:| Version:| Client Date:)',
                 skip=True),
        _Pattern(b'^(?: Author:| Owner:| Group:| UNIX mode:| Parent:)',
                 skip=True),
        _Pattern(b'^(?: Server Date:| Contents:|[*] DUMP END)',
                 skip=True),

        # ... unless we're skipping a non-symlink vnode. Then we don't care so
        # much about strict parsing. So catch other unmatched data here, and
        # let parse_line_data decide if we're okay with letting it through or
        # not.
        _Pattern(rb'^(.*)$', 'unmatched'),
    )

    def __init__(self):
        # Don't rstrip lines; we need whitespace preserved for paths etc
        super().__init__(strip_lines=False)
        self._results = []
        self._symlink = None
        self._target = None

    def get_result(self):
        return self._results

    def parse_line_data(self, data):
        if data is None:
            return

        if 'symlink' in data:
            self._symlink = data['symlink']
            self._target = None
            del data['symlink']

        if self._symlink is False:
            # We only care about symlinks
            data.clear()
            return

        if self._symlink is None:
            raise ParseError("Found data (%r) before finding vnode type" % (data,))

        if 'unmatched' in data:
            raise ParseError("Unmatched data '%s' in symlink info" % data['unmatched'])

        if 'target' in data:
            if not self._symlink:
                raise ParseError("Got symlink target '%s' in non-symlink?" % data['target'])
            self._target = data['target']
            del data['target']

        if 'path' in data:
            if self._target:
                self._results.append((data['path'], self._target))
            else:
                raise ParseError("Got symlink path '%s', but did not find target?" % data['path'])

            self._symlink = None
            self._target = None
            del data['path']
