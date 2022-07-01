
This is FABS, the "Flexible AFS Backup System", a set of tools and daemons to
help backup AFS cells. This file serves as a kind of "quick start" for setting
up fabs, as well as a general overview of what it is. It does not document all
of the functionality in fabs; see our manpages for more detailed info.

Install (RPM/Debian)
====================

A few prebuilt packages are available for each release on GitHub:
<https://github.com/openafs-contrib/fabs/releases/>

For RHEL/CentOS and Fedora, you can also use the openafs-contrib yum repo
provided by Sine Nomine Associates:

    $ yum install https://download.sinenomine.net/openafs/contrib/sna-openafs-contrib-release-latest.noarch.rpm

See <https://download.sinenomine.net/openafs/contrib/> for info about the repo.

Note that the RPM packaging does not declare dependencies for the following:

 - OpenAFS itself
 - dumpscan

This just makes it easier to install fabs when those are not installed via RPM,
which is pretty common. You must ensure yourself that OpenAFS and dumpscan are
built and installed somewhere. You can get dumpscan from the OpenAFS source
tree, or you can build a standalone dumpscan from here:
<https://github.com/openafs-contrib/cmu-dumpscan>.

Install (pip)
=============

    $ pip3 install --upgrade fabs

Install from source
===================

To manually install from a git checkout, run something like the following:

    $ python3 setup.py build
    $ python3 setup.py install --skip-build

However, by default, that will use paths in `/opt/fabs`. For more traditional
paths, you can specify a few path variables like so:

    $ export PREFIX=/usr
    $ export LOCALSTATEDIR=/var
    $ export SYSCONFDIR=/etc
    $ python3 setup.py build && python3 setup.py install --skip-build

This will only install the fabs libraries and commands, though. There are some
additional man pages and other documentation in the `doc` dir. Ideally, just
use the RPM or other packaging to install fabs.

Building Packages
=================

Example Debian packaging exists in the `debian` dir. Run the normal `debuild`
command or similar to build a Debian package.

RPM packaging is in the `rpm` dir. Run:

    $ ./rpm/rules help

To see a list of targets to build.

Setup
=====

After installing fabs, you must initialize a couple of things (the SQL db, and
our dump blob storage). But before you can initialize those, you should check
the configuration to see if the db and dump blob storage are configured for
where you want them to go.

To change the configuration, edit or add new files in /etc/fabs/fabs.yaml.d/.
To see what the built-in defaults are, run:

    $ fabsys config --dump-all

Which can give you an idea for the configuration file format, and what options
are available. For actual documentation on the configuration directives, see
the manpage for `fabsys_config(1)`.

DB Setup
========

To check the db connection url, run this:

    $ fabsys config db/url

That will give you the currently-configured url fabs will use to connect to
the database. The built-in default is a sqlite database, which is fine unless
you want to use some other external database. (See the section about the
`db/url` directive in `fabsys_config(1)` for more information about other
database types.)

To get the relevant SQL commands to initialize the db (create tables and such),
run this:

    $ fabsys db-init --sql

Of course, if you want to save that SQL to a file, just redirect the output:

    $ fabsys db-init --sql > /tmp/fabs-db.sql

Or, if you want fabs to run that SQL itself, and cause the tables to be
created, you can do this:

    $ fabsys db-init --exec

Of course, in order to run that, you must have rights (via the `db/url`
connection url) to access the database and create tables, etc.

See the manpage for `fabsys_config(1)` for more information (look for the
section on `db/url`).

Storage Setup
=============

To check what directories fabs will use for storing volume blobs, run this:

    $ fabsys config dump/storage_dirs

That will tell you what fabs will use as a directory to store volume blobs.
Before fabs will be willing to use that directory, though, you must initialize
it, by running this command:

    $ fabsys storage-init --all

That will create a few things in that directory to indicate that it is valid
for use as a fabs blob storage directory.

AFS/krb5 Setup
==============

In order for fabs to be able to dump volumes and do other privileged operations
with AFS, it needs some credentials to do. This can be provided by a krb5
keytab.

You can also use -localauth for authenticated AFS commands, but this is not
recommended for production use, since not all operations support -localauth. To
use this (perhaps for initial testing or debugging), enable the configuration
option `afs/localauth`.

For non-localauth mode, you need a krb5 keytab to authenticate to AFS. By
default, fabs looks for this keytab in /etc/fabs/afsadmin.keytab, but you can
specify a different path with the config option `afs/keytab`.

You must be able to authenticate to AFS using this keytab. Here is an example
of how you can do so manually:

    $ k5start -t -f /etc/fabs/afsadmin.keytab -U -- vos examine root.cell

That command will examine the root.cell volume in your cell after
authenticating to AFS using the afsadmin.keytab file via k5start. If you see
any errors or warnings, something is probably wrong.

FABS also makes use of some AFS `fs` commands, so make sure you have an AFS
client running and operational.

Server Daemon
=============

fabs has a single server daemon that is used for orchestrating backup runs, as
well as checking for errors, sending reports, etc. You can run it manually like so:

    $ fabsys server

And it will run in the foreground until it receives a SIGINT or SIGTERM,
logging to the local syslog `daemon` facility.

To run it with debugging turned on, you can run it like so:

    $ fabsys server -x log/level=debug

But be warned that that generates a _LOT_ of data when backup runs are actually
running.

We do not provide an init script for `fabsys server`. Instead, it is intended
that you just run it under bosserver. See OpenAFS documentation for how to run
a command under bosserver.

Note that the server process currently has no way to re-read configuration
while running. You must restart the server process for it to notice any
configuration changes.

Running Backups
===============

To run a backup, run this:

    $ fabsys backup-start --all

However, by default, fabs will not backup any volumes. You have to give it a
pattern of volumes to match in the `dump/include/volumes` configuration option
(to backup by volume name), or via `dump/include/fileservers` (to backup by
fileserver). For example, specify a pattern of `app.*` to backup all volumes
that begin with `app.`.

To backup a specific volume, run this:

    $ fabsys backup-start --volume app.foo

Which will only backup the volume `app.foo`.

It is also useful to be able to give a "note" to a backup run, like so:

    $ fabsys backup-start --all --note "Daily backup for Fri Jun 19"

Or something like that. That note will be associated with the backup run in the
database, and will be printed by status reporting tools and the like.

Also note that `fabsys backup-start` just schedules/starts the backup. The process
that actually runs the backup is the `fabsys server` process. The `fabsys server`
process also only scans for scheduled backups ever minute or so, so you will
need to wait up to a minute for the dumps to actually start running.

There are also more options for including/excluding volumes and fileservers
from backups. See the documentation for the `dump/include/*`, `dump/exclude/*`,
and `dump/filter_cmd/*` options in `fabsys_config(1)`.

Note that, at least currently, there is no mechanism for scheduling backups at
certain times. To run a backup every day or every week, etc, just run `fabs
backup-start` via cron.

Backup Status
=============

To view the status of a backup run while it is running, you can run this:

    $ fabsys backup-status

Which shows some information about a backup run that is currently running. It
also shows information about the individual dumps that are spawned by the
backup run, but depending on the state, each dump is either just included in a
count (e.g. `56 job(s) in state CLONE_PENDING`), or details about the job are
actually shown (such as, when the `vos dump` command is actually running).

By default this just shows some human-readable plain text. This information is
also available as machine-readable JSON by running this:

    $ fabsys backup-status --format json

Which can be used to format the status output differently, or can be used for
more automated monitoring/alerts, or something similar.

Reporting
=========

fabs can generate reports when a backup run finishes, providing some
information about failures, which volumes were unchanged, and other
information. By default no report is generated, but if you specify a command in
the config options `report/txt/command` or `report/json/command`, that command
will be run with the report data on stdin when a backup run finishes.

The `report/txt/command` command is given a plain text human-readable report.
The `report/json/command` command is given JSON-formatted data instead, so you
can format your own reports. See the `fabsys_config(1)` manpage on those
directives for more information.

Volume Dump Blob Storage
========================

The format of the volume dump blob storage directory is fairly simple. There
are a few levels of directories just to reduce the number of directory entries
per directory, but at the lowest level, there is just one dump blob per volume.
Right now, the filenames are all formatted as such:

    <volid>.<fabs_internal_id>.dump

So, for example:

    536870915.324.dump

So, whenever a volume has changed and is dumped again, it gets a different (but
very similar) filename.

Currently, the general scheme in mind for limiting space in the blob storage
directory is to keep only the most recent copy of a volume around, and delete
all other copies from disk (but keeping them around on tape). The way that you
can achieve this is to periodically run `fabsys storage-trim`, which will give
you a list of path names that are redundant (that is, we have another dump file
that is the most recent for that volume).

The idea is that you run `fabsys storage-trim`, and examine each printed
filename to see if it has been backed up to tape or other long-term storage. If
it has, then you can just delete the file from disk. If not, just skip the
file, or you can back it up to tape immediately.

Disaster Recovery
=================

In the event of losing everything on the fabs backup server, it's still
possible to restore volume data relatively intuitively. We store volume blobs
as described in the previous section, so manually looking for dump blobs for a
specific volume id is straightforward, and those blobs can be passed to `vos
restore` like any volume dump blob.

You may want to backup all fabs-related data, though, so in the event of data
loss on the fabs backup server, you can easily restore all of the
fabs-related data so all path data and other information is just as it was
before. You can of course ensure that you've backed up everything needed for
running fabs by backing up everything on the machine, but you may want to know
what specific files are important to fabs.

All of the fabs-related data that is currently worth backing up is as follows:

 - The fabs configuration, typically in /etc/fabs.
 - The fabs database. Where this is located can be found by running `fabs
   config db/url`. By default, our database is in a sqlite database in
   /var/lib/fabs/fabs.sqlite.
 - The volume dump blob storage. The locations for this can be found by running
   `fabsys config dump/storage_dirs`. By default, there is only one dir, which is
   /var/lib/fabs/fabs-dumps.

So, by default, you can backup the whole fabs system by backing up all files
inside /etc/fabs and /var/lib/fabs. Of course, this may change if you change
the paths in the fabs configuration, and future versions of fabs may add data
in other locations. In general, though, you should be able to always backup
everything needed for fabs by backing up two things:

 - The /etc/fabs directory.
 - All locations referenced by the configuration. You can see the entire
   configuration by running `fabsys config --dump-all`.

How to actually backup this data is up to you; fabs does not currently have a
mechanism for backing up itself.

Restoring (CLI)
===============

To restore a volume, first you must find which backup of it that you want to
restore. You can search for backups of a volume using the `fabsys dump-find`
command, which can find backup dumps by volume name or by path. For examine:

    $ fabsys dump-find --volume user.adeason --near 1438491600 --admin

That will search for backups of the volume `home.adeason` around the timestamp
1438491600 (unix time). The --admin flag indicates this is being performed by
an administrator, and so no access checks will be performed.

Searching by path looks like this:

    $ fabsys dump-find --path /afs/cell/user/adeason --near 1438491600 --admin

To search on behalf of a user (typically done via some scripting frontend or
web interface), you can run it like this:

    $ fabsys dump-find --path /afs/cell/user/adeason --near 1438491600 --user adeason

Which checks that `adeason` has permission to restore that volume. That
authorization check is done by checking if `adeason` can write to the root
directory of the volume (specifically, if they have rlidw rights).

In the output of those commands, they will show some info for each dump found,
including the volume dump id. That volume dump id is what you use to actually
restore the volume, with the `fabsys restore-start` command:

    $ fabsys restore-start --dump-id 6 --admin

Again, --admin bypasses authorization checks (and `--user <user>` says what
user to check authorization for).

To view status on a running restore, you can run:

    $ fabsys restore-status

Or to see restore requests from a specific user:

    $ fabsys restore-status --user adeason

When the data has been restored to a staging location, that location will be
shown in the `fabsys restore-status` output, and the
/etc/fabs/hooks/stage-notify script will be run. That script can, for example,
send an email to the user to let them know a restore has finished.

After a certain amount of time (configurable via `stage/lifetime`, defaults to
1 week), the staging data will be removed, and the restore request will be
marked as done.

Killing/Retrying
================

Backup runs and restore requests can be forcibly killed with the two commands:

    $ fabsys backup-kill <brun_id>
    $ fabsys restore-kill <req_id>

This will cause the relevant job to be marked as "failed", just as if it
encountered an error multiple times and gave up. Pass the --note option to
provide a reason as to why the job was killed (the note is free-form text,
stored in the database with the relevant job).

Later on, you can cause a failed job to be retried from the last known good
point by running:

    $ fabsys backup-retry <brun_id>
    $ fabsys restore-retry <req_id>

That can be done for jobs that have failed either because they were killed, or
because they failed too many times. Of course, it is recommended that you fix
whatever problem was causing the job to fail, before issuing that command.

Upgrades
========

If you are upgrading from a previous version of fabs where the database schema
was different, you will need to upgrade the database with the
`fabsys db-upgrade` command. This command can tell you if the db needs to be
upgraded:

    $ fabsys db-upgrade
    
    Database MUST be upgraded (version 3 -> 4)
    
    See the documentation for the --exec and --sql options to perform the
    upgrade.  Please make a backup copy of the database before upgrading!

Or if the db is already at the current version:

    $ fabsys db-upgrade
    
    Database already at latest version 4

To perform the upgrade, you should make a backup of the database, and then run
`fabsys db-upgrade --exec` or `fabsys db-upgrade --sql` to perform the upgrade.
See `fabsys_db-upgrade(1)` for details.
