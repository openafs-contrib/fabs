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

import os
import logging
import logging.config
import syslog
import warnings
import pkg_resources
import sys

def getLogger(name):
    assert name.startswith('fabs.') or name == 'fabs'
    return FabsLogger(name)

# Some logging-related init, but just run it once
def _init_once():
    if _init_once.called:
        return
    _init_once.called = True

    # The 'logging' module can log the filename/function/etc of the function
    # that logged a particular message. This will not work for us as-is, since
    # we wrap the logging functions. So, disable this, as it is useless (and
    # thus can provide confusing/wrong output, and is a minor performance hit).
    # If we want to enable this again, consider passing an extra= parameter to
    # override the filename/line and function name, to the actual caller of
    # _our_ logging functions.
    setattr(logging, '_srcfile', None)

    # Install our filter for warnings, so python warnings get routed through
    # our logging framework.
    _logWarning.orig = warnings.showwarning
    warnings.showwarning = _logWarning
_init_once.called = False

# Allow a user to force debugging on via the FABS_DEBUG environment
# variable, which lets us see debugging information very early, before
# our config or even command-line arguments are parsed.
_force_debug = False
if os.environ.get('FABS_DEBUG', False):
    _force_debug = True

def init(config_file=None, log_level=None, debug_fmt=None):
    """
    Init the logging subsystem, according to our loaded config, clearing our
    any previous logging info. This can be called more than once, and often is;
    we call this early while starting up before our config is parsed, and once
    again after our config is parsed.
    """

    _init_once()

    if _force_debug:
        log_level = 'debug'

    if config_file is None:
        config_file = pkg_resources.resource_filename('fabs', 'log_cli.conf')

    # Give fileConfig a file handle, not a filename. If we give it a filename,
    # and the filename is not readable, the filename is silently ignored. This
    # way, we actually get an error if the given file does not exist.
    with open(config_file) as fh:
        logging.config.fileConfig(fh, disable_existing_loggers=False)

    # Set the root logger to the level specified in log/level, if a level is
    # given. This just makes it easier to set the global logging level, without
    # needing to mess with the details of the logging config, etc.
    if log_level is not None:
        logging.getLogger().setLevel(logging.getLevelName(log_level.upper()))

        # If debugging is turned on and we have a debug_fmt, print debug
        # information to stderr
        if log_level == 'debug' and debug_fmt:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(debug_fmt))
            logging.getLogger().addHandler(handler)

    if log_level == 'debug' and not sys.warnoptions:
        # If debugging is turned on, show all warnings by default (normally,
        # python ignores some warnings by default, like deprecation warnings).
        # Don't change the warnings if sys.warnoptions is set, because that
        # means someone has already configured the warnings away from the
        # default (via -W or the PYTHONWARNINGS env var).
        warnings.simplefilter('default')
        log = getLogger(__name__)
        log.d('Enabling all warnings')

# Function callback for capturing python warnings
def _logWarning(message, category, filename, lineno, file_=None, line=None):
    if file_ is not None:
        if hasattr(_logWarning, 'orig') and _logWarning.orig is not None:
            _logWarning.orig(message, category, filename, lineno, file_, line)
    else:
        msg = warnings.formatwarning(message, category, filename, lineno, line)
        logger = getLogger('fabs.py.warnings')
        logger.warn('py_warning', msg)
_logWarning.orig = None

# Subclass the base 'logging' module logger clas, to force the 'mid' extra
# field to exist, if it was not specified. If we don't do this, non-fabs code
# that tries to log messages will not be able to do so, since our logging
# pattern will try to interpolate the 'mid' field.
_orig_loggerClass = logging.getLoggerClass()
class _RawFabsLogger(_orig_loggerClass):
    def makeRecord(self, *args, **kwargs):
        rv = _orig_loggerClass.makeRecord(self, *args, **kwargs)

        # Debug messages effectively have no mid.
        if rv.levelno == logging.DEBUG:
            rv.__dict__['mid'] = 'DEBUG'
        if 'mid' not in rv.__dict__:
            rv.__dict__['mid'] = '__NOMID__'
        return rv
logging.setLoggerClass(_RawFabsLogger)

class FabsLogger:
    """
    The class for fabs logging.

    This is slightly different from native python logging, in that all of the
    logging methods have an extra required parameter, which is the msg id
    ("mid"). This is a machine-readable string that uniquely identifies a log
    message, and can be used by log processing stuff. Typically it's a
    snake-case name like a variable, like 'dump_err_meta'.
    """

    def __init__(self, name):
        self._logger = logging.getLogger(name)

    def isEnabledFor(self, *args, **kwargs):
        return self._logger.isEnabledFor(*args, **kwargs)

    # Filter 'kwargs' so we add {'mid': mid} to the 'extra' dict, so %(mid)s
    # can be used in logging formatters
    def _log(self, level, mid, msg, args, kwargs):

        # Many errors generated in fabs code include an mid in the exception
        # itself; use that mid.
        if kwargs.get('exc_info', False):
            exc = sys.exc_info()[1]
            if hasattr(exc, 'fabs_mid'):
                mid = exc.fabs_mid

        kwargs = kwargs.copy()
        if 'extra' in kwargs:
            kwargs['extra']['mid'] = mid
        else:
            kwargs['extra'] = {'mid': mid}

        self._logger.log(level, msg, *args, **kwargs)

    def d(self, msg, *args, **kwargs):
        self._log(logging.DEBUG, 'debug', msg, args, kwargs)

    def info(self, mid, msg, *args, **kwargs):
        self._log(logging.INFO, mid, msg, args, kwargs)

    def warn(self, mid, msg, *args, **kwargs):
        self._log(logging.WARNING, mid, msg, args, kwargs)

    def error(self, mid, msg, *args, **kwargs):
        self._log(logging.ERROR, mid, msg, args, kwargs)

    def critical(self, mid, msg, *args, **kwargs):
        self._log(logging.CRITICAL, mid, msg, args, kwargs)

    def exception(self, mid, msg, *args, **kwargs):
        kwargs['exc_info'] = 1
        self.error(mid, msg, *args, **kwargs)

class LocalSyslogHandler(logging.Handler):
    """
    A logging Handler that just logs to the local syslog system. That is, it
    logs via openlog()/syslog() etc.
    """

    # Map syslog facility names to the syslog.* facility constants
    _fac_map = {
        'KERN':   syslog.LOG_KERN,
        'USER':   syslog.LOG_USER,
        'MAIL':   syslog.LOG_MAIL,
        'DAEMON': syslog.LOG_DAEMON,
        'AUTH':   syslog.LOG_AUTH,
        'LPR':    syslog.LOG_LPR,
        'NEWS':   syslog.LOG_NEWS,
        'UUCP':   syslog.LOG_UUCP,
        'CRON':   syslog.LOG_CRON,
        'SYSLOG': syslog.LOG_SYSLOG,
        'LOCAL0': syslog.LOG_LOCAL0,
        'LOCAL1': syslog.LOG_LOCAL1,
        'LOCAL2': syslog.LOG_LOCAL2,
        'LOCAL3': syslog.LOG_LOCAL3,
        'LOCAL4': syslog.LOG_LOCAL4,
        'LOCAL5': syslog.LOG_LOCAL5,
        'LOCAL6': syslog.LOG_LOCAL6,
        'LOCAL7': syslog.LOG_LOCAL7,
    }

    # Map logging levels to syslog priorities
    _prio_map = {
        logging.DEBUG:    syslog.LOG_DEBUG,
        logging.INFO:     syslog.LOG_INFO,
        logging.WARNING:  syslog.LOG_WARNING,
        logging.ERROR:    syslog.LOG_ERR,
        logging.CRITICAL: syslog.LOG_CRIT,
    }

    def __init__(self, facility, ident=None):
        logging.Handler.__init__(self)

        if facility.upper() not in self._fac_map:
            raise ValueError("'%s' is not a valid syslog facility" % facility)

        facility = self._fac_map[facility.upper()]

        # We should be able to avoid specifying 'ident', and rely on the
        # default. But due to bug <https://bugs.python.org/issue38361> (fixed
        # in 3.9.0), the default ident doesn't get rid of the last slash in
        # argv[0], which leads to annoying syslog messages that look like
        # "/fabsys_server[1234]: mid INFO: Message". So construct our own
        # default.
        if ident is None:
            ident = sys.argv[0].split('/')[-1]
        syslog.openlog(ident, syslog.LOG_PID, facility)

    def close(self):
        logging.Handler.close(self)
        syslog.closelog()

    def emit(self, record):
        msg = self.format(record)

        if record.levelno not in self._prio_map:
            raise ValueError("Unable to interpret priority of log level %d (%s)" % \
                             (record.levelno, record.levelname))
        prio = self._prio_map[record.levelno]

        for line in msg.splitlines():
            syslog.syslog(prio, line)
