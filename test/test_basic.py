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

import json
import os
import pytest

import fabs.testutil as tu

import fabs.const

# Some very basic tests that don't need any db, any AFS environment, etc. Just
# run some basic commands and see that they work.

class TestBasic(tu.FabsTest):
    def test_vars(self):
        vars_ = self.fabsys_stdout('vars')

        # Just check that we got something of reasonable size in stdout
        self.comment('vars size: %d' % len(vars_))
        assert len(vars_) > 50
        assert "VERSION = " in vars_
        assert "CONF_DIR = " in vars_

    def test_db_init_sql(self):
        sql = self.fabsys_stdout('db-init --sql')
        self.comment("sql size: %d" % len(sql))
        assert len(sql) > 1000

    def test_config_check(self):
        res = self.fabsys_json('config --check')
        assert res['fabs_config_check']['ok'] is True

        # Just make sure the text version returns success; don't bother
        # checking what it sends to stdout
        self.fabsys_stdout('config --check')

    def test_config_dump_cli(self):
        cellname = 'mockcell.example.com'

        # Try setting a simple config directive via the command-line
        res = self.fabsys_json('config --dump -x afs/cell=%s' % cellname)

        assert res['fabs_config']['afs']['cell'] == cellname

    def test_config_dump_cli_xmerge(self):
        # Give several -x options, and check that they are merged properly.
        res = self.fabsys_json(('config --dump ' +
                                '-x afs/cell=example.com ' +
                                '-x afs/fs=/foo/fs ' +
                                '-x afs/aklog=/foo/aklog ' +
                                '-x afs/cell=example.net'))

        assert res['fabs_config']['afs']['cell'] == 'example.net'
        assert res['fabs_config']['afs']['fs'] == ['/foo/fs']
        assert res['fabs_config']['afs']['aklog'] == ['/foo/aklog']

    def test_config_get_fail(self):
        err = self.fabsys_stderr('config foo/bar', fail=True)
        assert "Unknown configuration directive" in err

    def test_config_get_cli(self):
        cellname = 'mockcell.example.com'
        expected = {'fabs_config': {'afs/cell': cellname}}

        # Try getting afs/cell from the command line, while overriding afs/cell
        res = self.fabsys_json('config afs/cell -x afs/cell=%s' % cellname)

        assert res == expected

    def test_config_get_text(self):
        cellname = 'mockcell.example.com'
        expected = cellname + "\n"

        res = self.fabsys_stdout('config afs/cell -x afs/cell=%s' % cellname)

        assert res == expected

    def test_config_get_complex_text(self):
        expected = '["vos"]\n'
        res = self.fabsys_stdout('config afs/vos')
        assert res == expected

    def test_config_get_complex_json(self):
        res = self.fabsys_json('config afs/vos')
        vos = res['fabs_config']['afs/vos']
        assert isinstance(vos, list)
        assert vos == ['vos']

    def test_config_get_format(self):
        cellname = 'mockcell.example.com'
        expected = {'fabs_config': {'afs/cell': cellname}}

        # fabsys_json will append a final '--format json', which should
        # override our '--format txt' here.
        res = self.fabsys_json('config afs/cell -x afs/cell=%s --format txt' % cellname)
        assert res == expected

    def test_config_dump_file(self):
        cellname = 'mockcell.example.com'

        # Try setting the config via a config file
        with self.mkftemp('config') as config_fh:
            self.comment('config file: %s' % config_fh.name)
            config_fh.write("afs:\n  cell: '%s'\n" % cellname)

            res = self.fabsys_json('config --dump --config %s' % config_fh.name)

        assert res['fabs_config']['afs']['cell'] == cellname

    def test_config_meta(self):
        bufsize = 1234

        res = self.fabsys_json('config --dump -x bufsize=%s' % bufsize)
        assert res['fabs_config']['bufsize'] == bufsize

        # Make sure that config stuff isn't persisting between fabsys runs
        res = self.fabsys_json('config --dump')
        assert 'bufsize' not in res['fabs_config']

    def test_version(self):
        out = self.fabsys_stdout('--version').rstrip()
        assert out == ('fabs %s' % fabs.const.VERSION)
