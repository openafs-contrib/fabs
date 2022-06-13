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

import contextlib
import hashlib
import sys
import os
import json

import fabs.db as db
import fabs.testutil as tu
import fabs.util

import fabs.const

class TestBackup(tu.FabsMockEnvTest):
    def test_backup_single(self):
        self.do_backup_init()
        res = self.do_backup_success('root.cell')

        # Check that we didn't encounter any recoverable errors
        vols = 0
        for srv, srv_data in res['success']['jobs'].items():
            for part, data in srv_data.items():
                for vol in data:
                    vols += 1
                    assert vol['errors'] == 0

        assert vols == 1

        # Check that we only backed up one volume, and it backed up
        # successfully
        assert res['success']['nvols_done'] == 1
        assert res['success']['nvols_skipped'] == 0
        assert res['success']['nvols_total'] == 1
        assert res['success']['nvols_err'] == 0

        status = self.fabsys_json('backup-status %d' % res['success']['id'])
        stdata = status['fabs_backup_status']['status'][0]
        exp = {
            "fabs_backup_status": {
                "status": [{
                    "id": 1,
                    "cell": "test.example.com",
                    "note": "Manually-issued backup run from command line",
                    "volume": 'root.cell',
                    "injected_blob_storid": None,
                    "injected_blob_spath": None,
                    "injected_blob_checksum": None,
                    "start": stdata['start'],
                    "end": stdata['end'],
                    "state": "DONE",
                    "state_last": None,
                    "dv": stdata['dv'],
                    "state_descr": "Done",
                    "state_source": stdata['state_source'],
                    "errors": 0,
                    "jobs": [],
                }]
            }
        }

        assert status == exp

    def test_backup_all(self):
        self.do_backup_init()
        res = self.do_backup_success()

        # Check that we didn't encounter any recoverable errors
        vols = 0
        for srv, srv_data in res['success']['jobs'].items():
            for part, data in srv_data.items():
                for vol in data:
                    vols += 1
                    assert vol['errors'] == 0

        assert vols == 11

        # Check that we backed up 11 volumes successfully.
        assert res['success']['nvols_done'] == 11
        assert res['success']['nvols_skipped'] == 0
        assert res['success']['nvols_total'] == 11
        assert res['success']['nvols_err'] == 0

        status = self.fabsys_json('backup-status %d' % res['success']['id'])
        stdata = status['fabs_backup_status']['status'][0]
        exp = {
            "fabs_backup_status": {
                "status": [{
                    "id": 1,
                    "cell": "test.example.com",
                    "note": "Manually-issued backup run from command line",
                    "volume": None,
                    "injected_blob_storid": None,
                    "injected_blob_spath": None,
                    "injected_blob_checksum": None,
                    "start": stdata['start'],
                    "end": stdata['end'],
                    "state": "DONE",
                    "state_last": None,
                    "dv": stdata['dv'],
                    "state_descr": "Done",
                    "state_source": stdata['state_source'],
                    "errors": 0,
                    "jobs": [],
                }]
            }
        }

        assert status == exp


        res = self.fabsys_json('dump-list --volume root.cell')
        dumpdata = res['fabs_dump_list']['dumps'][0]
        store_uuid = dumpdata['dump_blob']['bstore']['uuid']
        dumpid = dumpdata['id']

        sdir = self.config['dump']['storage_dirs'][0]
        exp = {
            'fabs_dump_list': {
                'dumps': [
                    {
                        'cell': 'test.example.com',
                        'br_id': 1,
                        'dump_blob': {
                            'abs_path': sdir+'/cell-test.example.com/20/00/00/536870912/536870912.1.dump',
                            'bstore': {
                                'prefix': sdir,
                                'storid': 1,
                                'uuid': store_uuid
                            },
                            'rel_path': 'cell-test.example.com/20/00/00/536870912/536870912.1.dump'
                        },
                        'dump_checksum': 'md5:a9f0ff640040db65f019046f72591469',
                        'dump_size': 28,
                        'dump_spath': 'cell-test.example.com/20/00/00/536870912/536870912.1.dump',
                        'dump_storid': 1,
                        'hdr_backup': 1436209991,
                        'hdr_copy': 1436209991,
                        'hdr_creation': 1436209991,
                        'hdr_size': 4,
                        'hdr_update': 1436209991,
                        'id': dumpid,
                        'incr_timestamp': 0,
                        'name': 'root.cell',
                        'rwid': 536870912,
                        'roid': 536870913,
                        'bkid': 536870914,
                        'vl_id': 1,
                    }
                ]
            }
        }
        assert exp == res

        blob_path = dumpdata['dump_blob']['abs_path']
        with open(blob_path, 'rb', buffering=0) as fh:
            dump_blob = fh.read(1024)
        exp = b"mock vos dump data:536870912"
        assert exp == dump_blob

        exp = 'a9f0ff640040db65f019046f72591469'
        cksum = hashlib.md5(dump_blob).hexdigest()
        assert exp == cksum

        def check_post():
            with fabs.db.connect(lock=False):
                n_vlentries = db.vlentry.find_count(1)
                assert n_vlentries == 11
        self.run_in_fork(check_post, init_config=True)

    def test_offline_fail(self):
        self.mockdata.vols['root.cell'].set_online(False)

        self.do_backup_init()
        res = self.do_backup_success()

        # Check that we didn't encounter any recoverable errors
        vols = 0
        vols_error = 0
        for srv, srv_data in res['success']['jobs'].items():
            for part, data in srv_data.items():
                for vol in data:
                    vols += 1
                    if vol['errors'] > 0:
                        vols_error += 1

        assert vols, 11
        assert vols_error, 1

        # We should have backed up 10 volumes, but failed on 1
        assert res['success']['nvols_done'], 10
        assert res['success']['nvols_skipped'] == 0
        assert res['success']['nvols_total'], 11
        assert res['success']['nvols_err'], 1

        status = self.fabsys_json('backup-status %d' % res['success']['id'])
        stdata = status['fabs_backup_status']['status'][0]
        exp = {
            "fabs_backup_status": {
                "status": [{
                    "id": 1,
                    "cell": "test.example.com",
                    "note": "Manually-issued backup run from command line",
                    "volume": None,
                    "injected_blob_storid": None,
                    "injected_blob_spath": None,
                    "injected_blob_checksum": None,
                    "start": stdata['start'],
                    "end": stdata['end'],
                    "state": "DONE",
                    "state_last": None,
                    "dv": stdata['dv'],
                    "state_descr": "Done",
                    "state_source": stdata['state_source'],
                    "errors": 0,
                    "jobs": [],
                }]
            }
        }

        assert status == exp

        res = self.fabsys_json('dump-list --volume root.cell')

        sdir = self.config['dump']['storage_dirs'][0]
        exp = {
            'fabs_dump_list': {
                'dumps': []
            }
        }

        assert exp == res

    def test_offline_skip(self):
        self.mockdata.vols['root.cell'].set_online(False)
        self.config['dump']['if_offline'] = 'skip'

        self.do_backup_init()
        res = self.do_backup_success()

        # Check that we didn't encounter any recoverable errors
        vols = 0
        for srv, srv_data in res['success']['jobs'].items():
            for part, data in srv_data.items():
                for vol in data:
                    vols += 1
                    assert vol['errors'] == 0

        assert vols == 11

        # Check that we backed up 10 volumes, but skipped 1
        assert res['success']['nvols_done'] == 10
        assert res['success']['nvols_skipped'] == 1
        assert res['success']['nvols_total'] == 11
        assert res['success']['nvols_err'] == 0

        status = self.fabsys_json('backup-status %d' % res['success']['id'])
        stdata = status['fabs_backup_status']['status'][0]
        exp = {
            "fabs_backup_status": {
                "status": [{
                    "id": 1,
                    "cell": "test.example.com",
                    "note": "Manually-issued backup run from command line",
                    "volume": None,
                    "injected_blob_storid": None,
                    "injected_blob_spath": None,
                    "injected_blob_checksum": None,
                    "start": stdata['start'],
                    "end": stdata['end'],
                    "state": "DONE",
                    "state_last": None,
                    "dv": stdata['dv'],
                    "state_descr": "Done",
                    "state_source": stdata['state_source'],
                    "errors": 0,
                    "jobs": [],
                }]
            }
        }

        assert status == exp

        res = self.fabsys_json('dump-list --volume root.cell')

        sdir = self.config['dump']['storage_dirs'][0]
        exp = {
            'fabs_dump_list': {
                'dumps': []
            }
        }

        assert exp == res

        def check_post():
            with fabs.db.connect(lock=False):
                n_vlentries = db.vlentry.find_count(1)
                assert n_vlentries == 10
        self.run_in_fork(check_post, init_config=True)
