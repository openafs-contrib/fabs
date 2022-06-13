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

# Our custom exceptions. The 'fabs_mid' field is used as by our logging as the
# MID to use when we log an exception.

class FabsError(Exception): pass
class InternalError(FabsError): fabs_mid = 'int_err'
class VLFsIdError(FabsError): fabs_mid = 'vlentry_noaddr_fail'
class ConfigError(FabsError): fabs_mid = 'cfg_err'
class DbNoUpdateError(FabsError): fabs_mid = 'db_noupdate'
class BadBridError(FabsError): fabs_mid = 'bad_brid'
class BadVlidError(FabsError): fabs_mid = 'bad_vlid'
class VersionError(FabsError): fabs_mid = 'db_version'
class ConfigCmdError(FabsError): fabs_mid = 'config_cmd_err'
class ArgumentError(FabsError): fabs_mid = 'arg_err'
class AccessError(FabsError): fabs_mid = 'no_access'
class StageDirError(FabsError): fabs_mid = 'no_stagedir'
class BadVoldumpIdError(FabsError): fabs_mid = 'bad_voldump_id'
class BadDumpError(FabsError): fabs_mid = 'bad_dump'
class VosRestoreError(FabsError): fabs_mid = 'vosrestore_err'
class VosDumpError(FabsError): fabs_mid = 'vosdump_err'
class DumpRaceError(FabsError): fabs_mid = 'dump_race'
class VolCleanError(FabsError): fabs_mid = 'vol_clean_err'
class MtptCleanError(FabsError): fabs_mid = 'mtpt_clean_err'
class RetryStateError(FabsError): fabs_mid = 'retry_state'
class PathError(FabsError): fabs_mid = 'path_err'
class StorageError(FabsError): fabs_mid = 'storage_error'
class DbUpgradeError(FabsError): fabs_mid = 'db_upgrade'
class DumpOfflineError(FabsError): fabs_mid = 'offline_err'
class VolDumpActiveError(FabsError): fabs_mid = 'voldump_active'
class BrunActiveError(FabsError): fabs_mid = 'brun_active'
