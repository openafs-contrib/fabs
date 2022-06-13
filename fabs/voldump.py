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

import time

import fabs.path
import fabs.util as util
import fabs.err as err
import fabs.db as db
import fabs.bstore as bstore
import fabs.log
log = fabs.log.getLogger(__name__)

class VolDump:
    """
    A VolDump represents a dump from a volume during a backup run.

    Don't instantiate a VolDump directly. Instead, you can find existing ones
    with `find`, and related functions.
    """

    @classmethod
    def find_latest(cls, *args, **kwargs):
        """
        Returns:
            `VolDump`: The latest voldump matching the given search criteria.
        """
        row = db.voldump.find_latest(*args, **kwargs)
        if row is None:
            return None
        return VolDump(row)

    @classmethod
    def find_oldest(cls, *args, **kwargs):
        """
        Returns:
            `VolDump`: The oldest voldump matching the given search criteria.
        """
        row = db.voldump.find_oldest(*args, **kwargs)
        if row is None:
            return None
        return VolDump(row)

    @classmethod
    def find(cls, **kwargs):
        """
        Returns:
            list of `VolDump`: Volume dumps matching the given search criteria.
        """
        rows = db.voldump.find(**kwargs)

        # If we were given an id, the voldump should really exist
        if 'id' in kwargs and not rows:
            raise err.BadVoldumpIdError("Voldump %d does not seem to exist" % kwargs['id'])

        return [VolDump(row) for row in rows]

    @classmethod
    def find_byblob(cls, cell, dblob):
        """
        Find the volume dump for the given dump blob and cell.
        """
        vdumps = cls.find(cell=cell,
                          dump_storid=dblob.storid(),
                          dump_spath=dblob.rel_path())
        if len(vdumps) == 0:
            return None
        return vdumps[0]

    @classmethod
    def search(cls, cell=None, brid=None, rwid=None, before_ts=None,
               after_ts=None, redundant=None, n_max=0):
        """
        Search for volume dumps, for administrative/maintenance operations.
        """

        for row in db.voldump.finditer(cell=cell, brid=brid, rwid=rwid,
                                       before_ts=before_ts, after_ts=after_ts,
                                       redundant=redundant):
            yield VolDump(row)
            if n_max > 0:
                n_max -= 1
                if n_max == 0:
                    return

    @classmethod
    def search_user(cls, cell, user, path, vol_rwid, timestamp, max_dumps):
        """
        Similar to `search`, but intended more for looking for volume dumps to
        restore data from.
        """

        if path and vol_rwid:
            raise err.InternalError("VolDump.search_user cannot handle both path and volume")
        if not path and not vol_rwid:
            raise err.InternalError("VolDump.search_user must have either path or volume")

        if vol_rwid:
            if user:
                util.check_restore_authz(user, cell, vol_rwid)
            vd_list = cls._search_vol(cell, vol_rwid, timestamp, max_dumps)

        else:
            vd_list = cls._search_path(path, timestamp, max_dumps)
            if user:
                check_vols = {}
                for voldump in vd_list:
                    check_vols[voldump.rwid] = True

                for rwid in check_vols.keys():
                    util.check_restore_authz(user, cell, rwid)

        return vd_list

    @classmethod
    def _search_vol(cls, cell, rwid, timestamp, max_dumps):
        # First, find one volume dump to look at
        search_after = False

        # Hmm, should 'rwid' just be a numeric volume id, or should we maybe be
        # able to search by volume name? For now we just do numerical id
        # lookups, but we could possibly look volumes up by name in the db.
        # However, this possibly has security ramifications, in case volume
        # names changed or something (we only do the authz check once, and if
        # the volid changed, we would be checking a different volume...). Not
        # sure on the right behavior there, so just do numeric ids for now.

        if timestamp is None:
            timestamp = int(time.time())

        first_vd = cls.find_oldest(cell, rwid=rwid, after_time=timestamp)
        if first_vd is None:
            search_after = True
            first_vd = cls.find_latest(cell, rwid=rwid, before_time=timestamp)

        if first_vd is None:
            # We can't find any volume dump
            return []

        oldest_vd = first_vd
        latest_vd = first_vd
        vd_list = [first_vd]

        last_chance = False

        log.d("Looking for a max of %d voldumps" % max_dumps)

        # Now that we have one volume dump, look for others adjacent to it,
        # until we have max_dumps volume dumps
        while len(vd_list) < max_dumps:
            log.d("Got %d voldumps, looking for %d" % (len(vd_list), max_dumps))

            if search_after:
                if not last_chance:
                    # Alternate between directions, unless we ran out of
                    # matches in one direction (and thus 'last_chance' is set)
                    search_after = False
                next_vd = cls.find_oldest(cell, rwid=rwid, after_id=latest_vd.id)
                if next_vd is not None:
                    vd_list.append(next_vd)
                    latest_vd = next_vd

            else:
                if not last_chance:
                    search_after = True
                next_vd = cls.find_latest(cell, rwid=rwid, before_id=oldest_vd.id)
                if next_vd is not None:
                    # Prepend to the vd_list
                    vd_list[:0] = [next_vd]
                    oldest_vd = next_vd

            if next_vd is None:
                # We found no more volume dumps in this direction, so try the
                # other direction before giving up.
                if last_chance:
                    # ...but we already are trying the "other" direction. So,
                    # give up.
                    break

                log.d("Found no more voldumps, looking only %s from now on" % (
                      "forwards" if search_after else "backwards"))

                last_chance = True

        return vd_list

    @classmethod
    def _search_path(cls, path, timestamp, max_dumps):
        # First just use the translator to get a volume id from our path
        xlate = fabs.path.PathTranslator(path, timestamp)
        voldump = xlate.translate()

        # Then, just use the normal algorithm for going from a volume id to a
        # list of vol dumps
        return cls._search_vol(voldump.cell, voldump.rwid, timestamp, max_dumps)

    def __init__(self, row):
        self._cols = list(row.keys())
        for col in self._cols:
            setattr(self, col, row[col])

    def get_brun(self):
        return fabs.brun.find(id=self.br_id)[0]

    def delete(self, keep_blob):
        blob = None

        with db.connect():
            brun = self.get_brun()
            if brun.state not in ('DONE', 'FAILED'):
                raise err.VolDumpActiveError(
                        ("Cannot delete voldump %d from active backup run " +
                         "%d (state %s)") % (
                         self.id, brun.id, brun.state))

            if not keep_blob:
                blob = self.dump_blob()

            fabs.restore.clear_voldump(self)
            db.voldump.delete(self.id)
            db.links.clear(self.vl_id)
            db.vlentry.delete(self.vl_id)

        if blob is not None:
            blob.delete(suppress_error=FileNotFoundError)

    def as_dict(self):
        ret = {}
        for attr in self._cols:
            ret[attr] = getattr(self, attr)
        return ret

    def describe_dict(self):
        vdinfo = self.as_dict()

        vdinfo['br_id'] = db.vlentry.get(vdinfo['vl_id'])['br_id']
        vdinfo['dump_blob'] = self.dump_blob_dict()

        return vdinfo

    def describe(self):
        vdinfo = self.describe_dict()

        update_secs = vdinfo['hdr_update']
        if update_secs is None or update_secs == 0:
            update_ctime = 'Never'
        else:
            update_ctime = time.ctime(update_secs)
        vdinfo['update_ctime'] = update_ctime

        creation_secs = vdinfo['hdr_creation']
        if creation_secs is None or creation_secs == 0:
            creation_ctime = 'Never'
        else:
            creation_ctime = time.ctime(creation_secs)
        vdinfo['creation_ctime'] = creation_ctime

        dump_path = None
        if vdinfo['dump_blob'] is not None:
            dump_path = vdinfo['dump_blob']['abs_path']
        if dump_path is None:
            dump_path = '[none]'
        vdinfo['dump_path'] = dump_path

        return ("Volume dump id %(id)d, volume: %(rwid)d (%(name)s)\n" +
                "  cloned at:    %(creation_ctime)s\n" +
                "  last updated: %(update_ctime)s\n" +
                "  dump blob: %(dump_path)s") % vdinfo

    def dump_blob(self):
        return bstore.blob_from_db(self.dump_storid, self.dump_spath, checksum=self.dump_checksum)

    def dump_blob_dict(self):
        # Note: This is best-effort; failures to resolve the dump blob in the
        # db will just log a warning. This is just for CLI frontend stuff, not
        # for actual dump manipulation.
        return bstore.blobdict_from_db(self.dump_storid, self.dump_spath, checksum=self.dump_checksum)

find_latest = VolDump.find_latest
find_oldest = VolDump.find_oldest
find_byblob = VolDump.find_byblob
find = VolDump.find
search = VolDump.search
search_user = VolDump.search_user
