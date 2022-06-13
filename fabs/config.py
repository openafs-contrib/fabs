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

import os.path
import collections.abc
import pkg_resources
import glob

import yaml

import fabs.err as err
import fabs.const as const
import fabs.log
from fabs.cmd import Fs

log = fabs.log.getLogger(__name__)

# This just contains a bunch of _Directive objects
class _DirectiveSet:
    def __init__(self, *dirs):
        self._dirs = dirs
        self._dirmap = {}
        for directive in dirs:
            self._dirmap[directive.full_key] = directive

    def find_dir(self, full_key):
        directive = self._dirmap.get(full_key, None)
        if directive is None:
            raise ValueError("Unknown configuration directive '%s'" % full_key)
        return directive

    def __iter__(self):
        for directive in self._dirs:
            yield directive

# An object that represents a single configuration directive
class _DirectiveBase:
    ok_types = None

    def __init__(self, full_key, default, example, choices=None):
        self.full_key = full_key
        self.default = default
        self.example = example
        self.choices = choices

        assert example is not None

    def _val_isok(self, val):
        if val is self.default:
            return True

        ok_types = self.ok_types
        if ok_types is None:
            ok_types = type(self.example)
        return isinstance(val, ok_types)

    def _convert_val(self, val):
        return val

    def check_val(self, val):
        if not self._val_isok(val):
            raise err.ConfigError("Config directive '%s' is type '%s', but should be type '%s' (for example: %r)" % (
                                  self.full_key, type(val).__name__,
                                  type(self.example).__name__, self.example))

        val = self._convert_val(val)

        if self.choices is not None and val not in self.choices:
            choices_str = ','.join([repr(item) for item in self.choices])
            raise err.ConfigError("Config directive '%s' set to invalid value %r (valid choices are: %s)" % (
                                  self.full_key, val, choices_str))

        return val

    def get_default(self, cfg):
        if isinstance(self.default, collections.abc.Callable):
            return self.default(cfg)
        return self.default

class _StrDirective(_DirectiveBase):
    ok_types = (str, bytes)

    def _convert_val(self, val):
        if isinstance(val, bytes):
            val = val.decode('utf-8')
        return val

class _ListDirective(_DirectiveBase):
    ok_types = (type(None), str, bytes, list, tuple)

    def _convert_val(self, val):
        if isinstance(val, bytes):
            val = val.decode('utf-8')
        if isinstance(val, str):
            val = [val]
        return val

class _BoolDirective(_DirectiveBase):
    def _conv_bool(self, val):
        if val == True or val == False:
            return val
        if isinstance(val, str):
            val = val.lower()
            if val == "true" or val == "1":
                return True
            if val == "false" or val == "0":
                return False
        return None

    def _val_isok(self, val):
        if self._conv_bool(val) is not None:
            return True
        return False

    def _convert_val(self, val):
        val = self._conv_bool(val)
        if val is None:
            raise err.InternalError("Directive %s not true or false?" % self.full_key)
        return val

class _IntDirective(_DirectiveBase):
    def _val_isok(self, val):
        try:
            int(val)
        except (ValueError, TypeError):
            return False
        return True

    def _convert_val(self, val):
        return int(val)

def _Directive(full_key, default, example=None, **kwargs):
    if example is None:
        example = default

    if isinstance(example, (str, bytes)):
        klass = _StrDirective
    elif isinstance(example, (list, tuple)):
        klass = _ListDirective
    elif isinstance(example, bool):
        klass = _BoolDirective
    elif isinstance(example, int):
        klass = _IntDirective
    else:
        raise err.InternalError("Unhandled config type for key %s (example %r)" % (
                                full_key, example))

    return klass(full_key, default, example, **kwargs)

def _default_cell(cfg):
    fs = Fs(cfg.get('afs/fs'))
    cell = fs.wscell().run()
    return cell

class _Config:
    """
    Represents all of our configuration directives.

    Primarily you should use `get` to get the values of configured items.
    Configuration can be loaded from disk via `load`, and all items can be
    retrieved at once with `dump`.
    """

    _dirs = _DirectiveSet(
        _Directive('afs/cell', default=_default_cell, example='example.com'),
        _Directive('afs/dumpscan', default=['afsdump_scan']),
        _Directive('afs/vos', default=['vos']),
        _Directive('afs/fs', default=['fs']),
        _Directive('afs/pts', default=['pts']),
        _Directive('afs/aklog', default=['aklog']),
        _Directive('afs/localauth', default=False),
        _Directive('afs/keytab',
                   default=os.path.join(const.CONF_DIR, 'afsadmin.keytab')),
        _Directive('afs/keytab_princ', default=None,
                   example='user@EXAMPLE.COM'),

        _Directive('k5start/command', default=['k5start']),

        _Directive('log/level', default=None, example='info'),
        _Directive('log/config_file',
                   default=pkg_resources.resource_filename('fabs', 'log_daemon.conf')),
        _Directive('log/debug_format',
                   default='%(name)s.%(mid)s %(levelname)s: %(message)s'),

        _Directive('db/url',
                   default='sqlite:///%s' % os.path.join(const.STORAGE_DIR,
                                                         'fabs.sqlite')),
        _Directive('db/batch_size', default=1024),
        _Directive('db/default_job_timeout', default=60 * 10), # 10 minutes
        _Directive('db/sqlite_foreign_keys', default=True),

        _Directive('storage/clean_age',
                   default=60*60*24*7), # 1 week

        _Directive('dump/storage_dirs',
                   default=[os.path.join(const.STORAGE_DIR, 'fabs-dumps')]),
        _Directive('dump/parallel/total', default=100),
        _Directive('dump/parallel/server', default=10),
        _Directive('dump/parallel/partition', default=4),

        _Directive('dump/if_offline', default='error', choices=['error', 'skip']),
        _Directive('dump/include/volumes', default=[]),
        _Directive('dump/include/fileservers', default=[]),
        _Directive('dump/exclude/volumes', default=['fabs.*']),
        _Directive('dump/exclude/fileservers', default=[]),
        _Directive('dump/filter_cmd/json',
                   default=[os.path.join(const.CONF_DIR, 'hooks', 'dump-filter')]),

        _Directive('dump/checksum', default='md5'),
        _Directive('dump/error_limit', default=3),
        _Directive('dump/max_unchanged_age', default=0),
        _Directive('dump/scan_links/abort_on_error', default=True),

        _Directive('restore/error_limit', default=3),

        _Directive('stage/server', default='pick-a-real-server.example.com'),
        _Directive('stage/partition', default='pick-a-partition'),
        _Directive('stage/volume_prefix', default='fabs.'),
        _Directive('stage/dir', default='/pick-a-staging-dir-in-/afs'),
        _Directive('stage/lifetime', default=60*60*24*7),
        _Directive('stage/notify_cmd',
                   default=[os.path.join(const.CONF_DIR, 'hooks', 'stage-notify')]),

        _Directive('backend/check_interval', default=60),
        _Directive('backend/request_cmd',
                   default=[os.path.join(const.CONF_DIR, 'hooks', 'backend-request')]),

        _Directive('report/only_on_error', default=False),
        _Directive('report/txt/command',
                   default=[os.path.join(const.CONF_DIR, 'hooks', 'dump-report')]),
        _Directive('report/json/command', default=None,
                   example=['/path/to/report-json.sh']),

        _Directive('lockdir', default=const.LOCKDIR),
        _Directive('bufsize', default=1024*1024),
    )

    _conf_file = os.path.join(const.CONF_DIR, 'fabs.yaml.d', '*.yaml')
    _daemon = False

    def __init__(self):
        self._data = {}
        self._overrides = {}

    # Given a list of keys (e.g. 'foo', 'bar', 'baz'), construct a full key
    # string (e.g. 'foo/bar/baz')
    @staticmethod
    def _key_str(*keys):
        """
        Convert a list of key components into a full key string.

        Args:
            *keys: A list of key components (e.g. 'foo', 'bar', 'baz').

        Returns:
            str: A full key string (e.g. 'foo/bar/baz').
        """
        return '/'.join(keys)

    # Given a full key (e.g. 'foo/bar/baz'), return a list of the individual
    # key components (e.g. ['foo', 'bar', 'baz'])
    @staticmethod
    def _key_list(full_key):
        """
        Convert a full key string into key components.

        Args:
            full_key (str): A full key string (e.g. 'foo/bar/baz').

        Returns:
            list of str: A list of key components (e.g. ['foo', 'bar', 'baz']).
        """
        return full_key.split('/')

    def set_val(self, full_key, val):
        """
        Set a config directive to the given value.

        Args:
            full_key (str): Config directive to set.
            val: The value to set the config directive to.
        """
        self._data[full_key] = val

    def check_internal(self, filename=None):
        """
        Check if this config is valid.
        """
        try:
            for full_key in self._data.keys():
                directive = self._dirs.find_dir(full_key)
                val = directive.check_val(self.get(full_key))
                self.set_val(full_key, val)
        except Exception as e:
            if filename:
                raise err.ConfigError("Error in configuration file '%s': %s" % (
                                      filename, e)) from None
            else:
                raise err.ConfigError("Error in configuration: %s" % e) from None

    # Get a list of all full (key,val) pairs for the specified nested dict
    @classmethod
    def _iteritems(cls, root):
        """
        Get a list of all (full_key, val) pairs for the given dict.

        Args:
            root (dict): A nested dictionary of config keys and values.

        Yields:
            (full_key, val) for each config directive.
        """
        for (key, val) in root.items():
            if isinstance(val, collections.abc.Mapping):
                for (subkey, subval) in cls._iteritems(val):
                    yield (cls._key_str(key, subkey), subval)
            else:
                yield (key, val)

    @classmethod
    def _merge_dict(cls, old_root, new_root):
        for (key, new_val) in new_root.items():
            if key not in old_root:
                old_root[key] = new_val

            elif isinstance(old_root[key], collections.abc.Mapping) and \
                 isinstance(new_val, collections.abc.Mapping):

                cls._merge_dict(old_root[key], new_val)

            else:
                old_root[key] = new_val

    # Merge the data in the 'data' dict into our current config
    def merge_data(self, data):
        for (key, val) in self._iteritems(data):
            self._data[key] = val

    def _get_default(self, full_key):
        directive = self._dirs.find_dir(full_key)
        return directive.get_default(self)

    def get(self, full_key, printable=False):
        """
        Get a directive from the loaded config.

        Args:
            full_key (str): The config directive to get (e.g. 'foo/bar/baz').

        Returns:
            The config value.
        """
        if full_key == '_daemon':
            val = self._daemon
        elif full_key in self._data:
            val = self._data[full_key]
        else:
            val = self._get_default(full_key)

        return val

    def _to_dict(self, full_key, val):
        """
        Convert a full key and value to a dict.

        Args:
            full_key (str): A directive key (e.g. 'foo/bar/baz').
            val: The directive's value (e.g. 'val').

        Returns:
            dict: A dict representing the directive (e.g.
            {'foo':{'bar':{'baz':'val'}}} .
        """
        ret = val
        for key in reversed(self._key_list(full_key)):
            ret = {key: ret}
        return ret

    def dump(self, include_defaults=False):
        """
        Get all of the config directives.

        Args:
            include_defaults (bool, optional): If True, include default values.
                Otherwise, only include values actually set in the config.
                Defaults to False.

        Returns:
            dict: A dict of all config directives (e.g.
            {'foo':{'bar':{'baz':'val'}}}).
        """

        ret = {}

        if include_defaults:
            # Go through all known directives, so we get default values
            for directive in self._dirs:
                self._merge_dict(ret, self._to_dict(directive.full_key,
                                                    self.get(directive.full_key)))

        for full_key in self._data:
            self._merge_dict(ret, self._to_dict(full_key,
                                                self.get(full_key)))
        return ret

    def load(self, cli_overrides=None, cli_conf_file=None,
             no_disk_config=False):
        """
        Load config data from disk.

        Args:
            cli_overrides (dict, optional): Config directive overrides
                specified by the command line (-x), if any.
            cli_conf_file (str, optional): Config file specified by the command
                line, if any.
            no_disk_config (bool, optional): If True, don't load the config
                data from disk (just initialize things and set defaults).
        """
        if cli_overrides:
            self._overrides.update(cli_overrides)

        if cli_conf_file:
            self._conf_file = cli_conf_file

        if no_disk_config:
            self._conf_file = None

        log.d("Loading config with overrides %r, config file %r" %
              (self._overrides, self._conf_file))
        new_cfg = _Config()

        if self._conf_file is not None:
            # Load in values from all 'conf_file' files into 'new_cfg'
            for filename in glob.glob(self._conf_file):
                log.d("Loading config file %s" % filename)

                # Skip files we cannot open
                try:
                    fh = open(filename, 'r')
                except Exception as e:
                    log.d("Error opening config file %s (%s):, skipping" % (filename, e))
                    continue

                with fh:
                    try:
                        for data in yaml.safe_load_all(fh):
                            new_cfg.merge_data(data)
                    except Exception as e:
                        raise err.ConfigError("Error while processing '%s': %s" % (filename, e))

                new_cfg.check_internal(filename)

        # Override any found directives with the values in _overrides
        for (full_key, val) in self._overrides.items():
            new_cfg.set_val(full_key, val)

        # Check the config for validity, once everything is set
        new_cfg.check_internal()

        # Make the new data our data
        self._data = new_cfg.get_data()

        # Our logging configuration may have changed, so reinitialize the
        # logging stuff.
        log_level = config.get('log/level')
        if config.get('_daemon'):
            log_config = config.get('log/config_file')
            debug_fmt = config.get('log/debug_format')
            fabs.log.init(config_file=log_config,
                           log_level=log_level,
                           debug_fmt=debug_fmt)
        else:
            fabs.log.init(log_level=log_level)

    def set_daemon(self, daemon=True):
        self._daemon = daemon

    def get_data(self):
        return self._data

    def check(self):
        errors = []

        # Just try to get a vlue for every directive we know about, and see if
        # any exceptions are thrown.
        for directive in self._dirs:
            try:
                self.get(directive.full_key)
            except Exception as e:
                errors.append("  %s: %s" % (directive.full_key, e))

        if errors:
            raise err.ConfigError("The following configuration errors were found:\n%s" %
                                  '\n'.join(errors))

config = _Config()

# Set some shortcuts for method available to other callers. This makes it so
# calling e.g. fabs.config.get() is the same as fabs.config.config.get().
get        = config.get
load       = config.load
set_daemon = config.set_daemon
dump       = config.dump
check      = config.check
