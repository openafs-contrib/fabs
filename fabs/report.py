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

from fabs.dumpjob import DumpJob

import fabs.server
import fabs.util as util
import fabs.err as err
import fabs.config as config
import fabs.const
import fabs.log
log = fabs.log.getLogger(__name__)

def report_success(brun):
    """
    Run the relevant report-generating commands for a successful backup run.

    Args:
        `fabs.brun.BackupRun`: The backup run that successfully ran.
    """

    jobs = DumpJob.describe(brun)
    job_data = {}
    jobs_done = 0
    jobs_err = 0
    jobs_skipped = 0
    for job in jobs:
        if job['state'] == 'DONE':
            job['failed'] = False
            if job['skip_reason']:
                jobs_skipped = jobs_skipped + 1
            else:
                jobs_done = jobs_done + 1
        else:
            job['failed'] = True
            jobs_err = jobs_err + 1

        srv = job_data.setdefault(job['server_addr'], {})
        part = srv.setdefault(job['partition'], [])
        part.append(job)

    data = dict(
        fabs_version=fabs.const.VERSION,
        id=brun.id,
        note=brun.note,
        cell=brun.cell,
        volume=brun.volume,
        nvols_total=len(jobs),
        nvols_done=jobs_done,
        nvols_err=jobs_err,
        nvols_skipped=jobs_skipped,
        jobs=job_data,
    )

    for attr in ('start', 'end'):
        val = getattr(brun, attr)
        log.d("Got attribute %s value %s" % (attr, val))
        data[attr] = util.time_unix2str(val)

    if data['volume'] is None:
        data['volume'] = '*'

    report_data = dict(
        success=data,
    )

    if config.get('report/only_on_error') and data['nvols_err'] == 0:
        log.d("Not sending success-only report")
        return

    cmd = config.get('report/txt/command')
    if cmd:
        report = _report_txt(report_data)
        util.run_config_cmd(cmd, stdin=report)

    cmd = config.get('report/json/command')
    if cmd:
        report = json.dumps(report_data)
        util.run_config_cmd(cmd, stdin=report)

def _report_txt(data):
    report = ""

    if 'success' in data:
        data = data['success']
        report += """FABS v%(fabs_version)s Backup Report

Backup run %(id)s for cell %(cell)s, volume %(volume)s
Started on:  %(start)s
Finished on: %(end)s
Note: %(note)s

Attempted to backup %(nvols_total)d volume(s)
Successfully dumped %(nvols_done)d volume(s)
Failed to dump %(nvols_err)d volume(s)
Skipped %(nvols_skipped)d volume(s)

""" % data

        report += "Volumes per partition:\n"

        failed_jobs = []
        done_jobs = []
        error_jobs = []
        skipped_jobs = {}

        for server, part_data in data['jobs'].items():
            for part, joblist in part_data.items():
                done = 0
                failed = 0
                skipped = 0
                errors = 0
                for job in joblist:
                    if job['failed']:
                        failed = failed + 1
                        failed_jobs.append(job)

                    elif job['skip_reason']:
                        skipped += 1
                        skipped_jobs.setdefault(job['skip_reason'], [])
                        skipped_jobs[job['skip_reason']].append(job)

                    else:
                        if job['errors'] > 0:
                            errors = errors + 1
                            error_jobs.append(job)

                        done = done + 1
                        done_jobs.append(job)

                error_str = ""
                if errors > 0:
                    error_str = " (%d with errors)" % errors

                report += "  %s vicep%s: %d total, %d dumped%s, %d failed, %d skipped\n" % (
                          server, part, len(joblist), done, error_str, failed, skipped)

        if failed_jobs:
            report += "\nVolumes that failed to dump:\n"

            # Sort jobs by volume name
            failed_jobs = sorted(failed_jobs, key=lambda job: job['name'])
            for job in failed_jobs:
                report += "  %s (vl_id %d)\n" % (job['name'], job['vl_id'])

        if error_jobs:
            report += "\nThe following volumes encountered non-fatal errors.\n"
            report += "They were dumped, but encountered some problems along\n"
            report += "the way. Look for log messages mentioning the relevant\n"
            report += "volume name or FABS vl_id for more details.\n"
            report += "\n"
            report += "Volumes dumped with errors:\n"

            # Sort jobs by volume name
            error_jobs = sorted(error_jobs, key=lambda job: job['name'])
            for job in error_jobs:
                report += "  %s (vl_id %d, errors %d)\n" % (job['name'],
                          job['vl_id'], job['errors'])

        for skip_reason in sorted(skipped_jobs.keys()):
            jobs = sorted(skipped_jobs[skip_reason], key=lambda job: job['name'])
            if skip_reason == 'UNCHANGED':
                skip_txt = 'because the volume was unchanged'
            elif skip_reason == 'OFFLINE':
                skip_txt = 'because the volume was offline'
            else:
                skip_txt = 'for unrecognized reason "%s"' % skip_reason

            report += "\nVolumes that were skipped %s:\n" % skip_txt

            for job in jobs:
                report += "  %s (vl_id %d)\n" % (job['name'], job['vl_id'])

        if done_jobs:
            report += "\nVolumes that were dumped successfully:\n"

            done_jobs = sorted(done_jobs, key=lambda job: job['name'])
            for job in done_jobs:
                report += "  %s (vl_id %d)\n" % (job['name'], job['vl_id'])

        return report

    if 'failed' in data:
        raise NotImplementedError

    raise err.InternalError("Cannot format report with keys %r" % data.keys())
