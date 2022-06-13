Name:    fabs
Version: %{fabsver}
Release: 1%{?dist}
Summary: Flexible AFS Backup System

License: ISC
URL:     http://www.sinenomine.net/
Source0: fabs-v%{fabsver}.tar.gz

BuildArch: noarch
BuildRequires: python3-devel
BuildRequires: fedora-rpm-macros
BuildRequires: /usr/bin/podchecker
BuildRequires: /usr/bin/pyflakes-3
BuildRequires: /usr/bin/pylint-3
BuildRequires: python3-pytest
BuildRequires: python3dist(pyyaml)
BuildRequires: python3dist(sqlalchemy)
BuildRequires: python3dist(python-dateutil)
BuildRequires: python3dist(alembic)

# alembic is an optional dep in setup.py, but declare it Requires so it gets
# pulled in automatically.
Requires: python3dist(alembic)

# Don't look in the doc directory when autoscanning for Requires or Provides;
# we have some example perl scripts in there, which would cause some perl deps
# to be added.
%define __requires_exclude_from %{_docdir}
%define __provides_exclude_from %{_docdir}

%description
The Flexible AFS Backup System (FABS). FABS is a suite of tools and daemons
used for backing up an AFS cell. FABS itself does not handle long-term storage
of any AFS data, but merely feeds data into an existing backup system. As such,
the primary duties of FABS involve orchestrating AFS backups and restores, and
storing a record of which path names map to which volumes.

%prep
%setup -q

%build
PREFIX=%{_prefix} LOCALSTATEDIR=%{_var} SYSCONFDIR=%{_sysconfdir} %py3_build

%install
PREFIX=%{_prefix} LOCALSTATEDIR=%{_var} SYSCONFDIR=%{_sysconfdir} %py3_install
./doc/generate-man %{buildroot}/%{_mandir}
mkdir -p %{buildroot}/%{_sysconfdir}/fabs/fabs.yaml.d
mkdir -p %{buildroot}/%{_var}/lib/fabs/fabs-dumps
mkdir -p %{buildroot}/%{_var}/lock

mkdir -p %{buildroot}/%{_sysconfdir}/fabs/hooks
install -m 755 etc/fabs/hooks/backend-request %{buildroot}/%{_sysconfdir}/fabs/hooks/backend-request
install -m 755 etc/fabs/hooks/dump-filter %{buildroot}/%{_sysconfdir}/fabs/hooks/dump-filter
install -m 755 etc/fabs/hooks/dump-report %{buildroot}/%{_sysconfdir}/fabs/hooks/dump-report
install -m 755 etc/fabs/hooks/stage-notify %{buildroot}/%{_sysconfdir}/fabs/hooks/stage-notify

%check
%pytest

%files
%{python3_sitelib}/fabs
%{python3_sitelib}/fabs-*.egg-info
%{_bindir}/fabsys
%{_bindir}/sbaf
%{_mandir}/man1/*.1*
%{_sysconfdir}/fabs/fabs.yaml.d
%{_var}/lib/fabs

%config(noreplace) %{_sysconfdir}/fabs/hooks/backend-request
%config(noreplace) %{_sysconfdir}/fabs/hooks/dump-filter
%config(noreplace) %{_sysconfdir}/fabs/hooks/dump-report
%config(noreplace) %{_sysconfdir}/fabs/hooks/stage-notify

%doc README
%doc doc/example_fabsreport.pl

%changelog
* Fri Jun 10 2022  Andrew Deason <adeason@sinenomine.net> 1.0-1
- First public release
