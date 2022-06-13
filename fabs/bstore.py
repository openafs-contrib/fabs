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

import tempfile
import json
import os
import os.path
import contextlib
import fcntl
import hashlib
import uuid
import time
import traceback

import fabs.err as err
import fabs.config as config
import fabs.db as db
import fabs.util as util
import fabs.log
log = fabs.log.getLogger(__name__)

class BlobStore:
    """
    A BlobStore represents a storage location for volume dump blobs.

    You can instantiate a BlobStore for path '/foo/bar' by just passing the dir
    prefix like so:

        bstore = BlobStore('/foo/bar')

    Or get one by it's ID in the database:

        bstore = BlobStore.from_storid(5)

    Or just get all configured stores:

        for bstore in BlobStore.all_stores():
            pass

    An instantiated bstore may not actually exist on disk. Use
    `BlobStore.is_ok` to check if it exists on disk and is valid, or
    `BlobStore.create` to create it.
    """
    VERSION = '2'

    _all_checked = False
    store_by_uuid = {}
    store_by_storid = {}

    _bstores_locked = False

    @classmethod
    def all_stores(cls):
        """
        Generator for all bstores mentioned in our config.

        Yields:
            `BlobStore`: A BlobStore from our config.
        """
        for prefix in config.get('dump/storage_dirs'):
            yield BlobStore(prefix)

    @classmethod
    def all_bstores(cls):
        """
        Generator for all bstores that exist on disk.

        This is (confusingly) similar to `all_stores`, but this does check that
        all configured stores have been initialized on disk.

        Yields:
            `BlobStore`: An existing BlobStore.
        """
        cls.check_all()
        for bstore in cls.store_by_uuid.values():
            yield bstore

    @classmethod
    def check_all(cls):
        """
        Checks that all configured bstores have been initialized on disk.

        Raises:
            `fabs.err.StorageError`: If a bstore has not been initialized or is
            somehow not valid.
        """

        if cls._all_checked:
            return

        atleast_one = False

        for bstore in cls.all_stores():
            bstore.check()
            atleast_one = True

        if not atleast_one:
            raise err.StorageError("No storage dirs configured; we need at " +
                                   "least one (see config directive " +
                                   "dump/storage_dirs)")

        cls._all_checked = True

    @classmethod
    def storid_to_uuid(cls, storid):
        """
        Args:
            storid (int): A bstore ID from the database.

        Returns:
            str: The corresponding store's uuid.
        """
        row = db.bstore.lookup_storid(storid)
        if not row:
            raise err.InternalError("storid %d does not exist in db" % storid)

        return row['uuid']

    @classmethod
    def from_storid(cls, storid):
        """
        Args:
            storid (int): A bstore ID from the database.

        Returns:
            BlobStore: The corresponding bstore.
        """

        cls.check_all()

        bstore = cls.store_by_storid.get(storid, None)
        if bstore is not None:
            return bstore

        dir_uuid = cls.storid_to_uuid(storid)

        bstore = cls.store_by_uuid.get(dir_uuid, None)
        if bstore is None:
            raise err.StorageError(("Storage dir id %d refers to uuid %s, but " +
                                   "we do not know about that uuid") % (
                                   storid, dir_uuid))

        cls.store_by_storid[storid] = bstore
        return bstore

    def _post_check(self):
        if self._uuid is None:
            raise err.InternalError("bstore uuid not set (%s)" % self.prefix())

        dup_store = BlobStore.store_by_uuid.get(self._uuid, None)
        if dup_store is not None:
            raise err.ConfigError("Duplicate storage dir uuid '%s' (dirs %s and %s)" % (
                                  self._uuid, self.prefix(), dup_store.prefix()))

        BlobStore.store_by_uuid[self._uuid] = self

    @classmethod
    def bufsize(cls):
        return config.get('bufsize')

    @classmethod
    def round_up(cls, size):
        """
        Round the given size up to the next multiple of this bstore's
        `bufsize`.
        """
        multiple = int(size/cls.bufsize())
        return cls.bufsize() * (multiple + 1)

    @classmethod
    def _iter_dirs(cls, path):
        """
        Given foo/bar/baz, return ['foo', 'foo/bar', 'foo/bar/baz'].
        """
        dirs = []

        while len(path) > 0:
            dirs.append(path)
            path = os.path.dirname(path)

        dirs.reverse()
        return dirs

    @classmethod
    @contextlib.contextmanager
    def lock_bstores(cls, update=None):
        """
        Context manager for running some code while bstores are locked.

        Use like so:

            with BlobStore.lock_bstores(update=update):
                func()

        Our bstores will be locked, and then func() will return, and then our
        bstores will be unlocked. Nesting is not allowed; don't nest calls to
        this.

        Args:
            update (function, optional): If provided, we will call this
                function periodically while waiting for the bstore lock. It is
                called like so:

                    update(state_descr="some description", timeout=1234)

                Where 1234 is an upper bound of how long it will take us to get
                another update. This is used to update the status of the
                relevant job in the db to indicate that we're waiting for the
                bstore lock.
        """

        # Make sure we don't try to lock bstores while they're already locked;
        # that won't work correctly
        if cls._bstores_locked:
            raise err.InternalError("Nested lock_bstores?")

        with open(os.path.join(config.get('lockdir'),
                               'fabs-bstore.lock'), 'w+') as fh:
            if update:
                # If we have an update callback, try to get the lock non-blocking
                # first, to see if we need to wait at all. Ignore any errors
                # with obtaining the lock (._bstores_locked will just remain
                # false in that case).
                with contextlib.suppress(PermissionError, BlockingIOError):
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    cls._bstores_locked = True

            if not cls._bstores_locked:
                if update:
                    # If we have an 'update' callback, keep trying to lock the file
                    # for 'try_timeout' seconds at a time, re-calling update() on
                    # every try.
                    while not cls._bstores_locked:
                        try_timeout = 60*5 # 5 minutes

                        update(state_descr="Waiting for blob storage lock",
                               timeout=try_timeout*2)

                        with util.syscall_timeout(try_timeout):
                            fcntl.flock(fh, fcntl.LOCK_EX)
                            cls._bstores_locked = True

                else:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                    cls._bstores_locked = True

            try:
                yield
            finally:
                cls._bstores_locked = False

    @classmethod
    @contextlib.contextmanager
    def best_bstore(cls, size, bstore_l, update=None):
        """
        Find the best bstore for a new dump blob.

        The "best" bstore is considered to be the bstore with the most free
        space available.

        Args:
            size (int): The size of the new dump blob.
            bstore_l (list of `BlobStore`): List of bstores to consider.
            update (function, optional): Update callback while waiting for
                bstore lock; see `BlobStore.lock_bstores`.

        Returns:
            `BlobStore`: The best bstore to use.

        Raises:
            `fabs.err.StorageError`: If the best bstore doesn't have enough
            space to store the new dump blob.
        """

        cls.check_all()

        with cls.lock_bstores(update=update):
            best_bytes = -1
            best_bstore = None

            for bstore in bstore_l:
                bfree = bstore.bytes_free()

                if bfree >= best_bytes:
                    best_bytes = bfree
                    best_bstore = bstore

            if best_bstore is None:
                raise err.StorageError(("We ran out of storage dirs when " +
                                       "looking for a place to store a " +
                                       "dump of %d bytes") % size)
            if best_bytes < size:
                raise err.StorageError(("Cannot find storage dir to hold %d " +
                                       "bytes (best option is %s with %d " +
                                       "bytes free)") % (size, best_bstore.prefix(),
                                       best_bytes))

            yield best_bstore

    @classmethod
    def blob_from_user(cls, user_fname):
        """
        Create a new dump blob from a user-provided dump blob.

        Find the best bstore available that can store the given dump blob, and
        then copy the blob into that bstore.

        Returns:
            `DumpBlob`: The generated dump blob.
        """

        try:
            with open(user_fname, 'rb', buffering=0) as user_fh:
                size = os.fstat(user_fh.fileno()).st_size

                dblob = BlobStore.alloc_blob(size, "injectdump")

                bufsize = config.get('bufsize')

                ck_algo = config.get('dump/checksum')
                ck_m = hashlib.new(ck_algo)

                with open(dblob.abs_path(), 'wb', buffering=0) as blob_fh:
                    while True:
                        buf = user_fh.read(bufsize)
                        if not buf:
                            break

                        ck_m.update(buf)
                        blob_fh.write(buf)

            dblob.set_checksum("%s:%s" % (ck_algo, ck_m.hexdigest()))

        except Exception:
            if dblob:
                dblob.delete()
            raise
        return dblob

    @classmethod
    def alloc_blob(cls, size, name, update=None):
        """
        Allocate space for a new dump blob.

        This creates a new dump blob on disk, and allocates space for it (by
        writing placeholder data on disk).

        Args:
            size (int): The estimated size for the new blob.
            name (str): A string that's used when generating the temporary
                filename for the blob.
            update (function, optional): If provided, this function is called
                periodically while waiting for locks or i/o to complete. See
                `BlobStore.lock_bstores` for details.
        """
        cls.check_all()

        # "round up" the size we need for our dump, to try and make sure we
        # have enough space in advance (the given 'size' is just an estimate,
        # after all)
        size = cls.round_up(size)

        # Make a local copy of our bstores
        bstore_d = dict(cls.store_by_uuid)

        while True:
            with cls.best_bstore(size, list(bstore_d.values()), update=update) as bstore:
                dblob = bstore.create_dump(size, name, update)
                if dblob is None:
                    # Failed to create a dump in that bstore for whatever reason;
                    # retry while avoiding that bstore
                    del bstore_d[bstore.uuid()]
                    continue

                # Otherwise, we got a dump blob successfully, so just give it
                # to our caller
                return dblob

    @classmethod
    def latest_okay(cls, latest_vdump):
        """
        Check if the given volume dump is okay/valid.

        This is used when determining if volume dumps can be trimmed from disk.
        We must make sure the most recent voldump for a vol is valid, otherwise
        we cannot safely trim the other voldumps for that vol from disk.

        Args:
            latest_vdump (`fabs.voldump.VolDump`): The volume dump to check.

        Returns:
            bool: True if the dump file looks okay and valid, False otherwise.
        """
        dblob = latest_vdump.dump_blob()
        path = dblob.abs_path()

        if not dblob.exists():
            log.warn('trim_enoent', ("Dump blob %s does not exist on " +
                     "disk, but it should be the most recent dump for " +
                     "that volume. Skipping the relevant blob storage " +
                     "trimming.") % path)
            return False

        disk_size = os.path.getsize(path)

        if disk_size != latest_vdump.dump_size:
            log.error('trim_badsize', ("While trimming, found that %s has " +
                      "incorrect size (%d, but should be %d). Skipping " +
                      "trimming.") % (path, disk_size, latest_vdump.dump_size))
            return False

        # We _could_ maybe check the checksum here or something, but that seems
        # excessive; we'd have to read in the blob for every single volume
        # every time someone called 'storage-trim'
        return True

    @classmethod
    def periodic_cleaning(cls):
        for bstore in cls.all_bstores():
            bstore.clean_tmp()

    def all_disk_blobs(self, cell):
        """
        Get all dump blobs we can find on disk for this bstore.

        Args:
            cell (str): The cell to find dump blobs for.

        Yields:
            `DumpBlob`: A dump blob for every dump file found in the bstore.
        """
        top_dir = os.path.join(self.prefix(), 'cell-%s' % cell)

        if not os.path.isdir(top_dir):
            return

        def raise_exc(exc):
            raise exc

        for path, _, fnames in os.walk(top_dir, onerror=raise_exc):
            for fname in fnames:
                abs_path = os.path.join(path, fname)
                spath = os.path.relpath(abs_path, self.prefix())
                yield DumpBlob(self, spath)

    # Look at the dumps we have in our backend storage, and find the dumps we
    # have that are eligible for 'trimming'. A dump is eligible for trimming if
    # it's not the newest dump for the corresponding volume, and we have a
    # valid newest dump for that volume.
    #
    # For each dump blob that can be 'trimmed', this function yields the
    # VolDump object for that dump blob. The idea is that the administrator
    # will look at these dump files, see if they are backed up to long-term
    # tape storage, and delete them from disk if they are.
    @classmethod
    def trimmable_dumps(cls, cell):
        """
        Find volume dumps on disk that can be trimmed.

        A volume dump is eligible for trimming if it's not the newest dump for
        its volume, and the newest dump for that volume is valid. The idea is
        that an administrator can look at the list of trimmable dumps, and
        delete them from disk if they have been backed up to long-term storage.

        Args:
            cell (str): The cell to look for dumps for.

        Yields:
            `fabs.voldump.VolDump`: A voldump for each trimmable volume dump
            found.
        """

        import fabs.voldump as voldump
        # Remember which volumes we're skipping trimming, because there's some
        # issue with the newest dump for it
        skip_rwid = {}

        for bstore in cls.all_bstores():
            log.d("Trimming blobs in bstore %s" % bstore)

            for dblob in bstore.all_disk_blobs(cell):

                path = dblob.abs_path()
                log.d("Checking if file %s is eligible for trimming" % path)

                # What voldump corresponds to this file?
                vdump = voldump.find_byblob(cell, dblob)
                if not vdump:
                    log.warn('trim_extra', ("File %s is not associated with " +
                             "any volume backup") % path)
                    continue

                if skip_rwid.get(vdump.rwid, False):
                    # We're not trimming files for this volume; skip this
                    log.d("Skipping file for volume %d" % vdump.rwid)
                    continue

                # And who is the latest voldump for that volume?
                latest_vdump = voldump.find_latest(cell, rwid=vdump.rwid)

                if vdump.id == latest_vdump.id:
                    # If we _are_ the latest dump, we shouldn't be trimmed.
                    # Nothing to do.
                    log.d("File %s is newest dump for vol %d, not trimming" % (
                          path, vdump.rwid))
                    continue

                if not cls.latest_okay(latest_vdump):
                    # Don't try to trim any more dumps for this rwid, so we
                    # don't log more warnings repeatedly
                    skip_rwid[latest_vdump.rwid] = True
                    continue

                latest_path = latest_vdump.dump_blob().abs_path()
                latest_mtime = os.path.getmtime(latest_path)

                # If we've reached here, 'dblob' is the dump blob for 'vdump',
                # and vdump is a volume dump that is not the newest dump for
                # that volume, and we know that the newest dump (latest_vdump)
                # looks okay. Check that 'dblob' looks sane.
                if not os.path.isfile(path):
                    log.warn('trim_nonfile', ("Non-regular file %s found while " +
                             "trimming. Ignoring.") % path)
                    continue

                disk_mtime = os.path.getmtime(path)
                if disk_mtime >= latest_mtime:
                    log.warn('trim_newer', ("File %s appears to be at least as " +
                                            "new as %s, but the latter was " +
                                            "the newest dump for vol %d.") % (
                                            path, latest_path, vdump.rwid))
                    continue

                # dblob looks sane; send it to our caller for trimming
                log.d("Path %s can be trimmed (vdid %d)" % (path, vdump.id))
                yield vdump

            log.d("Done trimming blobs for bstore %s" % bstore)

    def __init__(self, prefix):
        """
        Args:
            prefix (str): The directory prefix for this bstore.
        """
        self._prefix = prefix
        self._uuid = None
        self._dbid = None
        self._checked = False
        self._storid = None

    def __str__(self):
        return self.prefix()

    def as_dict(self):
        return {
            'uuid': self.uuid(),
            'storid': self.storid(),
            'prefix': self.prefix(),
        }

    def storid(self):
        """
        Get the store ID for this bstore.

        If this bstore doesn't have an ID in the database, generate a new one
        and store it in the database.

        Returns:
            int: The store ID for this bstore.
        """
        self.check()
        if self._storid is None:
            self._storid = db.bstore.gen_storid(self._uuid)
            if self._storid is None:
                raise err.InternalError("Could not generate storid for uuid %s" % self._uuid)
        return self._storid

    def uuid(self):
        self.check()
        return self._uuid

    def check(self):
        """
        Check that this bstore exists on disk and is valid.

        Raises:
            `fabs.err.StorageError`: If this bstore doesn't exist on disk, or
            there is some problem with it.
        """
        if self._checked:
            return

        mdpath = self.metadata()

        try:
            with open(mdpath, 'r') as fh:
                info = json.load(fh)
        except Exception as e:
            raise err.StorageError(("Error loading blob storage metadata '%s'. " +
                                    "Maybe you need to run storage-init? " +
                                    "Error: %s") % (mdpath, str(e)))

        version = info.get('version', 'unknown')
        if version != self.VERSION:
            raise err.StorageError("Blob storage %s format version mismatch: %s != %s" %
                                   (mdpath, version, self.VERSION))

        dir_uuid = info.get('uuid', None)
        if dir_uuid is None:
            raise err.StorageError("Blob storage %s missing uuid" % mdpath)

        for path in [self.dump_tmp(), self.restore_tmp()]:
            if not os.path.isdir(path):
                raise err.StorageError("Required dir '%s' is missing" % path)

        self._uuid = dir_uuid
        self._checked = True

        self._post_check()

    def is_ok(self):
        """
        Does this bstore exist on disk, and is it valid?

        This is the same as `check`, but it returns False for invalid bstores,
        instead of throwing an exception.

        Returns:
            bool: True if the bstore exists on disk and looks valid, False
            otherwise.
        """
        try:
            self.check()
            check_ok = True
        except err.StorageError:
            check_ok = False
        return check_ok

    def create(self, dir_uuid=None, check=True):
        """
        Create this bstore on disk.

        Args:
            dir_uuid (str, optional): The uuid for the bstore. If not given,
                one is generated.
            check (bool, optional): If False, don't call `check`, in order to
                avoid adding this bstore to the global list of initialized
                bstores. (For testing.)
        """
        if self._checked:
            raise err.InternalError("Blob storage %s already opened?" % self.prefix())

        if not os.path.isdir(self.prefix()):
            raise err.StorageError("Dir '%s' does not exist" % self.prefix())

        # Is this dir already initialized just fine?
        if self.is_ok():
            raise err.StorageError("Dir '%s' has already been initialized" % self.prefix())

        for path in [self.dump_tmp(), self.restore_tmp()]:
            if not os.path.isdir(path):
                os.mkdir(path, 0o700)

        if dir_uuid is None:
            dir_uuid = str(uuid.uuid4())

        if not os.path.exists(self.metadata()):
            tmp_metadata = '%s.tmp' % self.metadata()
            with open(tmp_metadata, 'w') as fh:
                json.dump({
                    'version': self.VERSION,
                    'uuid': dir_uuid,
                }, fh)
            os.rename(tmp_metadata, self.metadata())

        if check:
            self.check()

    def clean_tmp(self):
        """
        Cleanup stale temporary files in this bstore.

        This should be called periodically on all bstores to remove old
        temporary files that haven't been cleaned up due to various edge cases.
        """

        now = int(time.time())
        max_age = config.get('storage/clean_age')

        # Go through each file in our dump_tmp() and restore_tmp() dirs. If any
        # file is older than 'storage/clean_age', then remove it. Skip any
        # non-regular files.

        for top_path in [self.dump_tmp(), self.restore_tmp()]:
            for rel_path in os.listdir(top_path):
                path = os.path.join(top_path, rel_path)

                if not os.path.isfile(path):
                    # We found a non-file in our tmp dir. That's kinda weird,
                    # but just ignore it. We could log a warning in this
                    # situation, but since we never remove the file, we'd keep
                    # logging this over and over, which isn't great. So just
                    # silently ignore it.
                    continue

                mtime = os.path.getmtime(path)

                if now > mtime and now - mtime > max_age:
                    log.warn('stale_tmp', ("Temp file %s looks stale; " +
                             "a previous backup run has possibly left it " +
                             "behind. Removing it (mtime %d, now %d, " +
                             "storage/clean_age %d)") % (path, mtime, now,
                             max_age))
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(path)

                else:
                    log.d(("Not cleaning tmp file %s (mtime %d, now %d, " +
                           "clean_age %d)") % (path, mtime, now, max_age))

    def prefix(self):
        return self._prefix

    def dump_tmp(self):
        """
        Returns:
            str: The path to our tmp dir for volume dumps.
        """
        return os.path.join(self.prefix(), 'tmp-dump')

    def restore_tmp(self):
        """
        Returns:
            str: The path to our tmp dir for volume restores.
        """
        return os.path.join(self.prefix(), 'tmp-restore')

    def metadata(self):
        """
        Returns:
            str: The path to our metadata file.
        """
        return os.path.join(self.prefix(), 'fabs-storage.json')

    def bytes_free(self):
        """
        Returns:
            int: How many bytes are free in this bstore.
        """
        stv = os.statvfs(self.prefix())
        return stv.f_frsize * stv.f_bavail

    def create_restore(self, name):
        """
        Returns:
            `DumpBlob`: A DumpBlob representing a path that can be used for
            claiming a dump blob for handling a restore request.
        """
        spath = os.path.join(self.restore_tmp(), name)
        return DumpBlob(self, spath)

    def create_dump(self, size, name, update=None):
        """
        Create a dump blob in this bstore for dumping a volume.

        Args:
            size (int): Estimated size of the new dump.
            name (str): A string that's used when generating the temporary
                filename for the blob.
            update (function, optional): Update callback to call while waiting
                for locks or i/o; see `BlobStore.lock_bstores`.

        Returns:
            `DumpBlob`: A new dump blob in this bstore that can be used to
            store the dump for a volume.
        """

        dblob = None
        fh = None
        success = False
        update_int = 60*5 # 5 minutes

        # Fudge the initial last_update, so our first update is in about 10
        # seconds, then 'update_int' seconds after that
        last_update = int(time.time()) - update_int + 10

        bfree = self.bytes_free()
        if bfree < size:
            raise err.StorageError(("Not enough space in %s to create new " +
                                   "dump blob; need %d but we only have " +
                                   "%d available") % (self.prefix(), size, bfree))

        try:
            fh = tempfile.NamedTemporaryFile(dir=self.dump_tmp(),
                                             prefix=name+'.',
                                             delete=False)
            spath = os.path.relpath(fh.name, self.prefix())
            dblob = DumpBlob(self, spath, dump_tmp=True)

            # Fill our created file with NULs, bufsize bytes at a time
            buf = b'\0' * self.bufsize()
            remaining = size
            pretty = util.PrettyBytes(remaining)

            while remaining > 0:

                if update:
                    now = int(time.time())
                    if now - last_update > update_int:
                        # If we haven't sent an update to the db in a while,
                        # refresh our status, to give progress and to prove
                        # that we're still alive
                        pretty.update_bytes(remaining)
                        update(timeout=update_int*3,
                               state_descr="Allocating space for dump in %s (%s / %s, %s)" % (
                                           self, pretty.bytes, pretty.total, pretty.rate))
                        last_update = now

                nbytes = min(remaining, len(buf))
                fh.write(buf[:nbytes])
                remaining -= nbytes
            fh.close()

            success = True

        except OSError as exc:
            log.warn('crdump_oserr', ("Error when trying to alloc new dump blob " +
                                      "in storage dir %s; skipping dir. Error: %s") % (
                                      self.prefix(), str(exc)))
            return None

        finally:
            if not success:
                if dblob:
                    dblob.delete()
                elif fh:
                    os.unlink(fh.name)

        return dblob

    def mkdirs(self, path):
        """
        Create the given dir path, as well as parent dirs.

        Args:
            path (str): Directory to create.
        """
        if not os.path.isdir(os.path.join(self.prefix(), path)):
            for _dir in self._iter_dirs(path):
                abs_path = os.path.join(self.prefix(), _dir)
                if not os.path.isdir(abs_path):
                    os.mkdir(abs_path, 0o700)

class DumpBlob:
    """
    A DumpBlob represents a file on disk for a volume dump.

    The underlying file may not actually exist for a DumpBlob; all you need to
    create a DumpBlob object is at least a BlobStore and a path.
    """

    @classmethod
    def blob_from_db(cls, storid, spath, checksum=None):
        """
        Create a DumpBlob from database-derived data.

        Args:
            storid (int): The ID for the containing BlobStore.
            spath (str): Relative path to the dump blob file in the store.
            checksum (str, optional): The checksum string for the blob, if
                known.

        Returns:
            `DumpBlob`: The dump blob specified.
        """
        if spath is None:
            return None
        bstore = BlobStore.from_storid(storid)
        return DumpBlob(bstore, spath, checksum=checksum)

    @classmethod
    def blobdict_from_db(cls, storid, spath, checksum=None):
        """
        Get a dict describing a DumpBlob from database-derived data.

        Normally this is the same as calling `.as_dict` on the result from
        `blob_from_db`, but if we cannot construct a full DumpBlob for any
        reason (e.g. an invalid store ID), we still return as much info in the
        returned dict as possible.

        Args:
            storid (int): The ID for the containing BlobStore.
            spath (str): The relative path to the dump blob file in the store.
            checksum (str, optional): The checksum string for the blob, if
                known.

        Returns:
            dict: A dict of info describing the dump blob.
        """
        try:
            return cls.blob_from_db(storid, spath, checksum).as_dict()
        except Exception as exc:
            log.warn('dumpfind_noblob', ("Cannot get full dump blob " +
                     "info for storid %d; enable debugging to see " +
                     "error details (error: %s)") % (storid, exc))
            log.d(traceback.format_exc())

        dir_uuid = None
        try:
            dir_uuid = BlobStore.storid_to_uuid(storid)
        except Exception:
            log.d("Error getting storid %d" % storid)
            log.d(traceback.format_exc())

        return {
            'bstore': {
                'uuid': dir_uuid,
                'storid': storid,
                'prefix': None,
            },
            'rel_path': spath,
            'abs_path': None,
        }

    def __init__(self, bstore, spath, checksum=None, dump_tmp=False):
        """
        Args:
            bstore (`BlobStore`): The containing blob store.
            spath (str): Relative path to the dump blob file.
            checksum (str, optional): The checksum string for the blob, if
                known.
            dump_tmp (bool, optional): If True, this indicates the dump blob is
                temporary; it will be automatically deleted unless it is moved
                to a permanent path (e.g. with `commit`). Defaults to False.
        """
        self._storid = bstore.storid()
        self._prefix = bstore.prefix()
        self._spath = spath
        self._checksum = checksum
        self._dump_tmp = dump_tmp
        self._delete = False
        if dump_tmp:
            self._delete = True

    def __str__(self):
        return self.abs_path()

    def as_dict(self):
        return {
            'bstore': self.bstore().as_dict(),
            'rel_path': self.rel_path(),
            'abs_path': self.abs_path(),
        }

    def abs_path(self):
        return os.path.join(self._prefix, self._spath)
    def rel_path(self):
        return self._spath
    def storid(self):
        return self._storid
    def bstore(self):
        return BlobStore.from_storid(self.storid())

    def exists(self):
        """
        Returns:
            bool: True if the underlying file for this DumpBlob exists on disk.
            False otherwise.
        """
        return os.path.exists(self.abs_path())

    def set_tmp(self):
        self._dump_tmp = True

    def set_checksum(self, cksum):
        self._checksum = cksum
    def checksum(self):
        if self._checksum is None:
            raise err.InternalError("checksum requested for non-checksum blob %s" % self)
        return self._checksum

    def delete(self, suppress_error=OSError):
        path = self.abs_path()
        with contextlib.suppress(suppress_error):
            os.unlink(path)
        self._delete = False

    def cleanup(self):
        """
        Cleanup the dump blob.

        If this blob should be automatically deleted (e.g. it was flagged as a
        temporary blob when created), it will be deleted at this point.
        """
        if self._delete:
            self.delete()

    @contextlib.contextmanager
    def maybe_delete(self):
        """
        Automatically delete this dump blob at the end of the given context,
        unless the context marks the blob as not needing deletion.

        This is useful to run like so:

            with dblob.maybe_delete():
                do_something()
                dblob.no_delete()

        If an exception is thrown, the blob will automatically be deleted at
        the end of the context. But if we successfully run `dblob.no_delete()`,
        then the blob will no longer be marked as needing deletion, and it
        won't be deleted. Thus, the blob will be automatically deleted if any
        unexpected errors occur during the context.
        """
        self._delete = True
        try:
            yield
        finally:
            self.cleanup()

    def no_delete(self):
        """
        Indicate that this blob should not be deleted automatically.
        """
        self._delete = False

    def _calc_dump_spath(self, dumpjob, rwid):
        return os.path.join(
            'cell-%s' % dumpjob.cell,
            '%02x' % ((rwid & 0xFF000000) >> 24),
            '%02x' % ((rwid & 0x00FF0000) >> 16),
            '%02x' % ((rwid & 0x0000FF00) >>  8),
            '%d' % rwid,
            '%d.%d.dump' % (rwid, dumpjob.vl_id),
        )

    def commit(self, dumpjob, rwid):
        """
        Commit a temporary blob into its permanent storage location.

        After this is run, the underlying file blob is moved to a new,
        non-temporary path, and it will no longer be automatically deleted.

        Args:
            dumpjob (`fabs.dumpjob.DumpJob`): The dump job that this dump blob
                is for.
            rwid (int): The RW volid for the underlying volume.
        """
        if not self._dump_tmp:
            raise err.InternalError("store() called on non-dump-tmp blob %s" % str(self))

        spath_rel = self._calc_dump_spath(dumpjob, rwid)
        spath_abs = os.path.join(self._prefix, spath_rel)

        # Create intermediate dirs if they don't exist
        self.bstore().mkdirs(os.path.dirname(spath_rel))

        os.rename(self.abs_path(), spath_abs)

        self._spath = spath_rel
        self._delete = False
        self._dump_tmp = False

    def _doclaim(self, rreq, attempt=0):
        if not os.path.exists(self.abs_path()):
            return None

        new_blob = self.bstore().create_restore('rreq.%d.dump' % rreq.id)

        if os.path.exists(new_blob.abs_path()):
            os.unlink(new_blob.abs_path())

        try:
            os.link(self.abs_path(), new_blob.abs_path())
        except FileNotFoundError as exc:
            # If we got an ENOENT, it's possible 'self' was unlinked before
            # we could link it; we can't tell if that happened, or if
            # tmp_path was not reachable for some reason. So retry this
            # once; under normal conditions, we'll notice that 'self'
            # doesn't exist and we'll return None early. If we need to
            # retry more than once, this is unusual enough that we just
            # bail out with an error.
            if attempt > 0:
                raise err.InternalError("Unable to link %s -> %s" % (
                                        self.abs_path(), new_blob.abs_path())) from exc
            return self._doclaim(rreq, attempt+1)

        new_blob.set_checksum(self._checksum)
        return new_blob

    def claim_restore(self, rreq):
        return self._doclaim(rreq)

    def verify_checksum(self):
        """
        Verify that the underlying file for this dump blob matches the
        checksum.

        Raises:
            `fabs.err.BadDumpError`: When the underlying file does not match
            the checksum.
        """
        old_cksum = self._checksum
        algo, _ = old_cksum.split(':')

        new_cksum = '%s:%s' % (algo, util.checksum(algo, self.abs_path()))

        if old_cksum != new_cksum:
            raise err.BadDumpError(("Cannot verify checksum for dump blob %s; " +
                                   "orig checksum %s != new checksum %s") % (
                                   self, old_cksum, new_cksum))

blob_from_user = BlobStore.blob_from_user
all_stores = BlobStore.all_stores
trimmable_dumps = BlobStore.trimmable_dumps
periodic_cleaning = BlobStore.periodic_cleaning

blob_from_db = DumpBlob.blob_from_db
blobdict_from_db = DumpBlob.blobdict_from_db
