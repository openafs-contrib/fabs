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

import argparse
import sys
import json

import fabs.err as err
import fabs.config as config
import fabs.const as const
import fabs.util as util
import fabs.log
log = fabs.log.getLogger(__name__)

def append(classlist):
    """
    A utility decorator that is helpful for recording what subcommands we have
    defined. Doing this:

        mylist = []
        @append(mylist)
        class Foo:
            pass

    will record 'Foo' in 'mylist'
    """
    def f(klass):
        classlist.append(klass)
        return klass
    return f

class SubCommand:
    """
    Base class for defining subcommands.
    """

    command = None
    help_txt = None

    # By default, assume a subcommand is not a daemon
    daemon = False

    # By default, assume a subcommand needs the config loaded
    config = True

    def parse(self, parser):
        """
        Define extra argparse directives in the given parser. This doesn't
        actually parse anything, even though the method is called parse.
        """
        pass

    def post_parse(self, args):
        """
        Override this in subclasses to do something immediately after our
        command-line is parsed, but before we do anything. Some commands may
        use this to change whether they are considered a 'daemon' command or
        whether they need configuration depending on args passed on the command
        line.
        """
        pass

    def run(self, args):
        """
        Run the actual command.

        Most commands should override this to do their work. If a command only
        supports the 'txt' format (that is, it cannot return meaningful data
        via e.g. JSON) then instead define a `run_txt` method.

        Args:
            args (`argparse.Namespace`): The result from argparse's
                `parse_args`.

        Returns:
            (dict, str): The first item is used for formats like json; we just
            print the json serialization of the given dict. The second item is
            only used for the 'txt' format, and is printed as-is. If either
            item is None (or if None is returned directly), then nothing is
            printed.
        """
        if hasattr(self, 'run_txt'):
            if args.format == 'txt':
                return self.run_txt(args)
            raise err.ArgumentError("%s is only available with the 'txt' format" % (
                                    self.command))
        raise NotImplementedError

    def verbose(self, args, data):
        """
        Print the given string if we're being verbose.

        Args:
            args (`argparse.Namespace`): Our current parsed args.
            data (str): The message to print.
        """
        if args.verbose:
            print(data, file=sys.stderr)

class _DictAction(argparse.Action):
    """
    An argparse action to turn args like "--foo bar=baz --foo quux=pants" into
    a dict such that args.foo = dict(bar='baz', quux='pants')
    """
    def __call__(self, parser, namespace, values, option_string=None):
        parts = values.split('=')
        if len(parts) != 2:
            raise argparse.ArgumentTypeError("%r is not a value key/value pair" % values)

        if getattr(namespace, self.dest, None) is None:
            setattr(namespace, self.dest, {})

        # Copy existing value, so we don't update a provided default value
        newdict = getattr(namespace, self.dest).copy()
        newdict.update({parts[0]: parts[1]})
        setattr(namespace, self.dest, newdict)

class Command:
    """
    Base class for defining top-level commands
    """

    # A string passed to the ArgumentParser ctor
    description = None

    # A list of SubCommand classes
    subcommands = []

    def main(self, argv):
        """
        Parse the arguments in argv, and run the actual command
        """

        if argv is None:
            argv = sys.argv[1:]

        # "Global" options are understood by any subcommand, and even without a
        # subcommand. e.g. 'fabs --version' and 'fabs config --version' are
        # both valid. So the base parser understands these options, as well as
        # each subcommand parser.
        global_parser = argparse.ArgumentParser(add_help=False)
        global_opts = global_parser.add_argument_group(title="common options")
        global_opts.add_argument('-h', '--help', action='help',
                                 default=argparse.SUPPRESS)
        global_opts.add_argument('--version', action='version',
                                 version='fabs %s' % const.VERSION)

        # "Common" options are understood by all subcommands. e.g.
        # 'fabs config --dump --config /tmp/foo' works, but
        # 'fabs --config /tmp/foo config --dump' does not (because we can't
        # parse --config until we have a subcommand). So each subcommand parser
        # understands these options, but not the base parser. We don't let the
        # base parser understand these, because argparse works weirdly when the
        # same option exists in the base parser and our subcommand parsers,
        # when we need to actually extract values from the given options.
        common_parser = argparse.ArgumentParser(add_help=False)
        common_opts = common_parser.add_argument_group(title="common options")
        common_opts.add_argument('--config', default=None,
                                 help="alternate config file to use")
        common_opts.add_argument('-x', action=_DictAction, metavar="key=val", default={},
                                 help="override a specific config directive")
        common_opts.add_argument('--format', default='txt',
                                 choices=['txt', 'json'],
                                 help="output format")

        parser = argparse.ArgumentParser(description=self.description,
                                         add_help=False,
                                         parents=[global_parser])

        subparsers = parser.add_subparsers(metavar='subcommand')
        subparsers.required = True

        # For each class in 'subcommands', create an instance of that class,
        # get the parsing information from it, and add it to our argparse
        # object.
        for klass in self.subcommands:
            subcmd = klass()

            # Create the parser for our subcommand.
            subpar = subparsers.add_parser(subcmd.command,
                                           help=subcmd.help_txt, add_help=False,
                                           parents=[global_parser, common_parser])
            sub_opts = subpar.add_argument_group(title="subcommand arguments")
            subcmd.parse(sub_opts)

            # Store our subcommand object in a pseudo-option named
            # 'argparse_subcmd'. After our argv is parsed, we can access the
            # relevant subcommand in args.argparse_subcmd.
            subpar.set_defaults(argparse_subcmd=subcmd)

        args = parser.parse_args(argv)
        subcmd = args.argparse_subcmd

        subcmd.post_parse(args)

        if subcmd.daemon:
            config.set_daemon()
            sys.argv[0] = "%s_%s" % (sys.argv[0], sys.argv[1])

        fabs.log.init()

        if subcmd.config:
            config.load(args.x, args.config)
        else:
            config.load(args.x, args.config, no_disk_config=True)

        retcode = 0
        try:
            ret = subcmd.run(args)
            if ret is None:
                info = {}
                txt = None
            elif len(ret) == 2:
                (info, txt) = ret
            else:
                (info, txt, retcode) = ret

            # Output our given data
            if args.format == 'txt':
                if txt is not None:
                    print(txt)

            elif args.format == 'json':
                json.dump(info, sys.stdout)

            else:
                raise err.InternalError("Unknown format '%s'" % args.format)

        except Exception as exc:
            # Show our exception message and backtrace to stderr. The full_msg
            # string here is like the normal traceback.format_exc() string, but
            # we add an extra newline before printing the actual exception
            # info, to make it stand out more from the backtrace (and trim the
            # last trailing newline).

            exc_stack, exc_msg = util.format_exc(exc)
            print(exc_stack, file=sys.stderr)
            print(file=sys.stderr)
            print(exc_msg, file=sys.stderr)

            if args.format == 'json':
                json.dump({'error': exc_msg,
                           'error_trace': exc_stack}, sys.stdout)

            return 1
        return retcode
