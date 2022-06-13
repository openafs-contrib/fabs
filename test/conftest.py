#
# Copyright (c) 2022, Sine Nomine Associates ("SNA")
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
import sys

# Monkeypatch the 'fabs.const' module, which is generated at build time. This
# lets us run our tests on the source tree directly, without needing to run the
# build.
def _patch_fabs_const():
    fabs_const = type(sys)('fabs.const')
    fabs_const.consts = {
        'VERSION': '0.0.test',
        'PREFIX':        '/opt/fabstest',
        'SYSCONFDIR':    '/opt/fabstest/etc',
        'CONF_DIR':      '/opt/fabstest/etc/fabs',
        'LOCALSTATEDIR': '/opt/fabstest/var',
        'LOCKDIR':       '/opt/fabstest/var/lock',
        'STORAGE_DIR':   '/opt/fabstest/var/lib/fabs',
    }
    for key, val in fabs_const.consts.items():
        setattr(fabs_const, key, val)
    sys.modules['fabs.const'] = fabs_const
    import fabs
    fabs.const = fabs_const
_patch_fabs_const()

# Add the --db-url option to the pytest command line.
def pytest_addoption(parser):
    parser.addoption("--db-url", action="append",
                     help="Run db tests against the given url (e.g. mysql://scott:tiger@localhost/foo). WARNING: this will delete all data in the given database!")

# If a test requests a "raw_db_url" fixture, run that test with "raw_db_url"
# set to None, and then each --db-url given on the command line by the user.
def pytest_generate_tests(metafunc):
    if "raw_db_url" in metafunc.fixturenames:
        db_urls = [None]
        cli_urls = metafunc.config.getoption("--db-url")
        if cli_urls:
            db_urls.extend(cli_urls)

        # For each db url, generate a test id string for it that looks like
        # "sqlite.0", "mysql.1", etc. If we don't do this, then the generated
        # test ID would have a db url in it, which is annoyingly long, and may
        # contain passwords.
        ids = []
        for idx, url in enumerate(db_urls):
            if url is None:
                eng = "sqlite"
            else:
                eng = url.split(':')[0]
            ids.append("%s.%d" % (eng, idx))

        metafunc.parametrize("raw_db_url", db_urls, ids=ids)
