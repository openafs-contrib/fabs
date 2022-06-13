#!/usr/bin/perl

# Example "report/json/command" script for FABS.
# Look in fabsys_config(1) under "report/json/command" to see what this is for.
#
# As-is, this generates a plain-text report that is pretty much the same as the
# "report/txt/command" report, and writes the result to a temporary file.

use strict;
use warnings FATAL => qw(uninitialized);

use JSON;
use File::Temp;

# Read in the JSON data from stdin
my $data = JSON->new->utf8->decode(do { local $/; <STDIN> })
    or die("Cannot create JSON decoder");

die("Backup run not successful") if (!exists($data->{'success'}));
$data = $data->{'success'};

# Start the report with some simple general info
my $report = << "EOS";
FABS v$data->{'fabs_version'} Backup Report

Started on:  $data->{'start'}
Finished on: $data->{'end'}
Note: $data->{'note'}

Attempted to backup $data->{'nvols_total'} volume(s)
Successfully dumped $data->{'nvols_done'} volume(s)
Failed to dump $data->{'nvols_err'} volume(s)
Skipped $data->{'nvols_skipped'} volume(s)

Volumes per partition:
EOS

my @failed_total;
my @done_total;
my @error_total;
my %skipped_jobs;
my $n_skipped = 0;

# Provide a summary of how many volumes we processed for each server and partition
while (my ($server, $part_data) = each %{$data->{'jobs'}}) {
    while (my ($part, $joblist) = each %$part_data) {
        my @failed;
        my @done;
        my @errors;
        for my $job (@$joblist) {
            if ($job->{'failed'}) {
                push(@failed, $job);

            } elsif ($job->{'skip_reason'}) {
                my $reason = $job->{'skip_reason'};
                $n_skipped++;
                if (!exists($skipped_jobs{$reason})) {
                    $skipped_jobs{$reason} = [];
                }
                push(@{$skipped_jobs{$reason}}, $job);

            } else {
                if ($job->{'errors'} > 0) {
                    push(@errors, $job);
                }
                push(@done, $job);
            }
        }

        my $error_str = "";
        if (scalar(@errors) > 0) {
            $error_str = " (".scalar(@errors)." with errors)";
        }

        $report .= sprintf("  %s vicep%s: %s total, %s dumped%s, %s failed, %d skipped\n",
                           $server, $part, scalar(@$joblist), scalar(@done), $error_str,
                           scalar(@failed), $n_skipped);
    
        push @failed_total, @failed;
        push @done_total, @done;
        push @error_total, @errors;
    }
}

# Provide a list of failed volumes (if any)
if (@failed_total) {
    $report .= "\nVolumes that failed to dump:\n";

    # Sort jobs by volume name
    for my $job (sort { $a->{'name'} cmp $b->{'name'} } @failed_total) {
        $report .= "  $job->{'name'} (vl_id $job->{'vl_id'})\n";
    }
}

if (@error_total) {
    $report .= "\nThe following volumes encountered non-fatal errors.\n";
    $report .= "They were dumped, but encountered some problems along\n";
    $report .= "the way. Look for log messages mentioning the relevant\n";
    $report .= "volume name or FABS vl_id for more details.\n";
    $report .= "\n";
    $report .= "Volumes dumped with errors:\n";

    # Sort jobs by volume name
    for my $job (sort { $a->{'name'} cmp $b->{'name'} } @error_total) {
        $report .= "  $job->{'name'} (vl_id $job->{'vl_id'}, errors $job->{'errors'})\n";
    }
}

for my $skip_reason (sort keys %skipped_jobs) {
    my @jobs = sort { $a->{'name'} cmp $b->{'name'} } @{$skipped_jobs{$skip_reason}};
    my $descr;
    if ($skip_reason eq 'UNCHANGED') {
        $descr = 'because the volume was unchanged';
    } elsif ($skip_reason eq 'OFFLINE') {
        $descr = 'because the volume was offline';
    } else {
        $descr = 'for unrecognized reason "'.$skip_reason.'"';
    }

    $report .= "\nVolumes that were skipped $descr:\n";

    for my $job (@jobs) {
        $report .= "  $job->{'name'} (vl_id $job->{'vl_id'})\n";
    }
}

# Provide a list of successful volumes (if any)
if (@done_total) {
    $report .= "\nVolumes that were dumped successfully:\n";

    # Sort jobs by volume name
    for my $job (sort { $a->{'name'} cmp $b->{'name'} } @done_total) {
        $report .= "  $job->{'name'} (vl_id $job->{'vl_id'})\n";
    }
}

# Write the formatted report out somewhere
my $fh = File::Temp->new(TMPDIR => 1, UNLINK => 0,
                         TEMPLATE => 'fabs-report.XXXXXX', SUFFIX => '.txt');
print $fh $report;

print "Formatted report written to ".$fh->filename."\n";
