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

import json
import pytest
import select

import fabs.testutil as tu
import fabs.err

import fabs.util as util

# Tests for internal util functions.

class TestUtil(tu.FabsTest):
    def test_timeout(self):
        select_finished = False

        # Set a timeout of 1 second, and sleep (via select()) for 2 seconds.
        # The sleep should never finish, since the timeout should trigger well
        # before then.
        with util.syscall_timeout(1):
            select.select([], [], [], 2)
            select_finished = True

        assert not select_finished

    def test_timeout_nest(self):
        with pytest.raises(fabs.err.InternalError):
            with util.syscall_timeout(5):
                with util.syscall_timeout(5):
                    pass

    def test_time_str2unix(self):
        assert 0 == util.time_str2unix('@0')
        assert 1644102842 == util.time_str2unix('2022-02-05 23:14:02 UTC')
        assert 1644102842 == util.time_str2unix('@1644102842')
        assert 2117488442 == util.time_str2unix('2037-02-05 23:14:02 UTC')

    def test_time_unix2str(self, tzutc):
        assert 'Sat Feb 05 23:14:02 2022 UTC' == util.time_unix2str(1644102842)
        assert 'Thu Feb 05 23:14:02 2037 UTC' == util.time_unix2str(2117488442)

    @pytest.mark.parametrize('data', [
                                      [],
                                      [1],
                                      [1,2,3,4,5],
                                     ])
    def test_json_generator(self, data, tmpdir):
        def genfunc():
            for item in data:
                yield item

        obj = {'key': util.json_generator(genfunc())}
        res = json.dumps(obj)
        new_obj = json.loads(res)

        assert {'key': data} == new_obj

        obj = {'key': util.json_generator(genfunc())}
        with open(tmpdir + '/foo', 'w') as fh:
            json.dump(obj, fh)
        with open(tmpdir + '/foo') as fh:
            self.comment(fh.read())
        with open(tmpdir + '/foo') as fh:
            new_obj = json.load(fh)

        assert {'key': data} == new_obj

    @pytest.mark.parametrize('data,exp',
                             [
                              (['foo'],
                               'foo'),
                              (['foo','bar'],
                               'foo and bar'),
                              (['foo','bar','baz'],
                               'foo, bar, and baz'),
                              (['foo','bar','baz','quux'],
                               'foo, bar, baz, and quux'),
                             ])
    def test_list2str(self, data, exp):
        assert exp == util.list2str(data)
