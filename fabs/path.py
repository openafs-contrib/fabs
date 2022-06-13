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

import fabs.voldump
import fabs.err as err
import fabs.db as db
import fabs.log
log = fabs.log.getLogger(__name__)

class PathTranslator:
    """
    A PathTranslator is used to translate a plain string path (and timestamp)
    into a specific fabs volume dump. Example usage:

        xlator = PathTranslator('/afs/foo/bar/baz', int(time.time()))
        voldump = xlator.translate()
    """

    # A very high limit, just to prevent endless loops.
    # This is way higher than most symlink limits in actual OS VFSes, so we
    # shouldn't encounter any false positives with this.
    MAX_SYMLINKS = 1000

    def __init__(self, path, timestamp):
        """
        Args:
            path (str): Path to translate.
            timestamp (int): When we should go back in time to translate the
                given path, as a unix timestamp.
        """
        if timestamp is None:
            timestamp = int(time.time())

        self._timestamp = timestamp
        self._orig_path = path
        self._n_symlinks = 0

        if not path.startswith('/afs/'):
            raise err.PathError(("The provided path '%s' does not " +
                                 "start with /afs/") % path)
        self._path = self._split_path(path)
        self._path_pos = 0
        self._voldump = None

        log.d("Starting path translation for path '%s' around timestamp %d" % (
              path, timestamp))

    @classmethod
    def _split_path(cls, path):
        # Given a path like foo/bar/baz, split into [foo, bar, baz]
        return [comp for comp in path.split('/') if len(comp) > 0]

    def translate(self):
        """
        Returns:
            `fabs.voldump.VolDump`: The closest volume dump we can find
            corresponding to the given path.
        """
        while not self._done():
            self._traverse_one()
        return self._voldump

    def _done(self):
        # If we cannot pull off any more elements from _path, we have nothing
        # more to do, so we're done
        if self._path_pos >= len(self._path):
            return True
        return False

    def path_str(self):
        return '/'.join([''] + self._path)

    def _pop_path(self):
        if self._done():
            raise err.PathError(("Expected another path element at index %d " +
                                 "for path '%s'") % (
                                 self._path_pos, self.path_str()))

        ret = self._path[self._path_pos]
        self._path_pos += 1
        return ret

    def _traverse_one(self):
        if self._path_pos == 0:
            # We're starting from the root
            self._voldump = None
            self._flatten_dots()

            first = self._pop_path()
            if first != 'afs':
                raise err.PathError("Unknown root dir '%s' in path '%s'" % (
                                    first, self.path_str()))

            cellname = self._pop_path()
            if cellname.startswith('.'):
                # Trim off leading '.', if this is just an RW mount
                cellname = cellname[1:]

            self._set_vol(cellname, 'root.cell')
            return

        # Keep track of our path relative to the volume root
        cur_path = ''

        while not self._done():
            dentry = self._pop_path()

            cur_path += '/'
            cur_path += dentry

            links = db.links.search(vl_id=self._voldump.vl_id,
                                    path=cur_path)
            if links:
                # pylint: disable=unused-variable
                (path, target) = links[0]
                self._handle_link(target)
                return

    def _flatten_dots(self):
        index = self._path_pos
        while index < len(self._path):
            if self._path[index] == '.':
                # Entries for '.' we can just ignore
                del self._path[index]
                continue

            if self._path[index] == '..':
                if index == 0:
                    raise err.PathError(("Tried to '..' above the root dir " +
                                         "in path '%s'") % self._orig_path)

                # For '..' entries, delete the current component _and_ the
                # previous one
                del self._path[index]
                del self._path[index-1]

                # Go back one step
                index -= 1
                continue

            index += 1

    def _set_vol(self, cell, volname):
        if volname.endswith('.readonly'):
            volname = volname[0:-len('.readonly')]
        elif volname.endswith('.backup'):
            volname = volname[0:-len('.backup')]

        if cell is None:
            cell = self._voldump.cell

        self._voldump = fabs.voldump.find_oldest(cell=cell, rwname=volname,
                                                 after_time=self._timestamp-1)

        if self._voldump is None:
            self._voldump = fabs.voldump.find_latest(cell=cell, rwname=volname,
                                                     before_time=self._timestamp+1)

        if self._voldump is None:
            raise err.PathError(("Path '%s' element %d refers to volume %s, " +
                                 "which appears to not be backed up") % (
                                 self.path_str(), self._path_pos, volname))

    def _handle_link(self, target):
        if (target.startswith('#') or target.startswith('#')) and target.endswith('.'):
            self._handle_mntpt(target)
            return

        self._n_symlinks += 1
        if self._n_symlinks > self.MAX_SYMLINKS:
            raise err.PathError("Path '%s' contains too many (%d) symlinks" % (
                                self._orig_path, self._n_symlinks))

        new_path = self._split_path(target)

        if target.startswith('/'):
            # Replace all path components up to the current one with the new
            # path
            self._path[0:self._path_pos] = new_path

            # Start processing from the root when we continue processing our path
            self._path_pos = 0
            return

        # Replace our current path component with the new target path
        self._path[self._path_pos-1:self._path_pos] = new_path

        # Continue processing our path starting from the root. It's maybe
        # possible to just continue from our current location in the path, but
        # it requires more work (we need to re-evaluate our relative path
        # inside the volume root, etc)
        self._path_pos = 0

    def _handle_mntpt(self, target):
        # Trim off leading '%'/'#', and trailing '.'
        target = target[1:-1]
        parts = target.split(':', 1)

        if len(parts) == 1:
            cell = None
            volname = parts[0]
        else:
            assert len(parts) == 2
            cell = parts[0]
            volname = parts[1]

        self._set_vol(cell, volname)
