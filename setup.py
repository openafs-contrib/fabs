"""
FABS
-----

FABS (the "Flexible AFS Backup System") is a suite of tools used for backing
up AFS cells, to be used together with separate (possibly existing) "plain"
backup system.
"""

import glob
import json
import os.path
import pathlib
import subprocess
import sys

from setuptools import setup
from setuptools.command.build_py import build_py

with open('.version') as fh:
    __version__ = fh.readline().rstrip().replace('~', '')

# Set our "build-time" constants, which will be available in fabs.const when
# fabs is built.
_const_dict = {}
_const_dict['PREFIX'] = os.environ.get('PREFIX', '/opt/fabs')
_const_dict['SYSCONFDIR'] = os.environ.get('SYSCONFDIR',
                                           os.path.join(_const_dict['PREFIX'],
                                                        'etc'))
_const_dict['LOCALSTATEDIR'] = os.environ.get('LOCALSTATEDIR',
                                              os.path.join(_const_dict['PREFIX'],
                                                           'var'))
_const_dict['LOCKDIR'] = os.environ.get('LOCKDIR',
                                        os.path.join(_const_dict['LOCALSTATEDIR'],
                                                     'lock'))
_const_dict['VERSION'] = __version__
_const_dict['CONF_DIR'] = os.path.join(_const_dict['SYSCONFDIR'], 'fabs')
_const_dict['STORAGE_DIR'] = os.path.join(_const_dict['LOCALSTATEDIR'],
                                          'lib', 'fabs')

def _consts_code():
    code = [
        'consts = %r' % _const_dict,
        '',
        "# Set fabs.const.VERSION to consts['VERSION'], etc",
        'globals().update(consts)'
    ]
    return ''.join(line+'\n' for line in code)

def _gen_const_py():
    from distutils import log

    # Path to top-level 'fabs' dir.
    topdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fabs')

    # Generate const.py by appending our const dict to const.py.in. Write to
    # const.py.tmp, and rename() it to const.py when we're done.

    const_py = os.path.join(topdir, 'const.py')
    const_py_in = os.path.join(topdir, 'const.py.in')
    const_py_tmp = os.path.join(topdir, 'const.py.tmp')
    log.info("generating %s" % const_py)

    with open(const_py_in) as in_fh:
        in_data = in_fh.read()

    with open(const_py_tmp, 'w') as tmp_fh:
        tmp_fh.write(in_data)
        tmp_fh.write(_consts_code())

    os.rename(const_py_tmp, const_py)

def _run(argv):
    arg_str = ' '.join(argv)
    print("+ %s" % arg_str, file=sys.stderr)
    subprocess.run(argv, check=True)

# Subclass the 'build_py' command to run a few extra things at build time.
class fabs_build_py(build_py):
    def run(self):
        # Generate fabs/const.py
        _gen_const_py()

        # Generate manpages in doc/man1
        _run(['./doc/generate-man', 'doc'])

        super().run()

def get_data_files():
    man_paths = []

    for path in glob.iglob('doc/pod1/*.pod'):
        fname = os.path.basename(path)
        assert fname.endswith('.pod')

        page_name = fname[:-len('.pod')]

        man_paths.append('doc/man1/%s.1' % page_name)

    yield ('share/man/man1', man_paths)

topdir = pathlib.Path(__file__).parent
readme_path = topdir / "README.md"
readme_contents = readme_path.read_text()

setup(
    name='fabs',
    version=__version__,
    license='ISC',
    author='Sine Nomine Associates',
    author_email='sna-packager@sinenomine.net',
    url='https://github.com/openafs-contrib/fabs',
    description='A flexible backup suite for AFS cells',
    long_description=readme_contents,
    long_description_content_type='text/markdown',
    install_requires=[
        'PyYAML>=3.10',
        'SQLAlchemy>=0.8',
        'python_dateutil',
    ],
    extras_require={
        'Alembic': ['Alembic>=0.4'],
    },
    cmdclass={
        'build_py': fabs_build_py,
    },
    packages=['fabs', 'fabs.scripts'],
    entry_points={
        # Entry points for our CLI commands
        'console_scripts': [
            'fabsys = fabs.scripts.fabsys:main',
            'sbaf = fabs.scripts.sbaf:main',
        ],
    },
    # Include non-*.py files in our 'fabs' subdir (e.g. log_cli.conf)
    package_data={'fabs': [
        'log_cli.conf',
        'log_daemon.conf',
    ]},

    # Include files outside of our 'fabs' subdir (man pages, hook examples,
    # etc)
    data_files=list(get_data_files()),
)
