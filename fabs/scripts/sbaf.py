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

import fabs.scripts.base as base

import fabs.db

_cmdlist = []

@base.append(_cmdlist)
class GenerateDataCmd(base.SubCommand):
    command = "generate-data"
    help_txt = "Generate a db full of fake data for testing"

    def parse(self, parser):
        parser.add_argument('workdir', help='dir to create data in')
        parser.add_argument('--url', help='db url')
        parser.add_argument('--bruns', type=int, default=1000,
                            help="Number of backup runs to generate")
        parser.add_argument('--servers', type=int, default=4,
                            help="Number of fileservers")
        parser.add_argument('--vlentries', type=int, default=1000,
                            help="Number of VL entries (and voldumps) per backup run")
        parser.add_argument('--sites', type=int, default=3,
                            help="Number of sites per VL entry")
        parser.add_argument('--links', type=int, default=50,
                            help="Number of symlinks per volume")
    def run_txt(self, args):
        if args.url is None:
            args.url = 'sqlite:///' + args.workdir + '/fabs.sqlite'

        print("Generating fake data in %s..." % args.workdir)
        fabs.db.db.gen_fake_data(args.workdir, args.url, args.bruns,
                                 args.servers, args.vlentries, args.sites,
                                 args.links)
        print("Done.")

class Sbaf(base.Command):
    description = "Testing/debug unstable sbaf commands"
    subcommands = _cmdlist

def main(argv=None):
    sbaf = Sbaf()
    return sbaf.main(argv)
