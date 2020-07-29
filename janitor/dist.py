#!/usr/bin/python3
# Copyright (C) 2020 Jelmer Vernooij <jelmer@jelmer.uk>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

from breezy.export import export
from breezy.tree import Tree
from breezy.workingtree import WorkingTree
from debian.deb822 import Deb822
from janitor.fix_build import (
    DependencyContext,
    resolve_error,
    APT_FIXERS,
    )
from janitor.sbuild_log import (
    find_apt_get_failure,
    find_build_failure_description,
    Problem,
    MissingPerlModule,
    )
from janitor.schroot import Session
from janitor.trace import note, warning
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Optional, TextIO, List, Tuple, Callable, Type
from breezy.plugins.debian.repack_tarball import get_filetype


def run_apt(session: Session, args: List[str]) -> None:
    args = ['apt', '-y'] + args
    retcode, lines = run_with_tee(session, args, cwd='/', user='root')
    if retcode == 0:
        return
    offset, line, error = find_apt_get_failure(lines)
    if error is not None:
        raise DetailedDistCommandFailed(retcode, args, error)
    raise UnidentifiedError(retcode, args, lines)


def apt_install(session: Session, packages: List[str]) -> None:
    run_apt(session, ['install'] + packages)


def apt_satisfy(session: Session, deps: List[str]) -> None:
    run_apt(session, ['satisfy'] + deps)


def satisfy_build_deps(session: Session, tree):
    source = Deb822(tree.get_file('debian/control'))
    deps = []
    for name in ['Build-Depends', 'Build-Depends-Indep', 'Build-Depends-Arch']:
        try:
            deps.append(source[name].strip().strip(','))
        except KeyError:
            pass
    for name in ['Build-Conflicts', 'Build-Conflicts-Indeo',
                 'Build-Conflicts-Arch']:
        try:
            deps.append('Conflicts: ' + source[name])
        except KeyError:
            pass
    apt_satisfy(session, deps)


def run_with_tee(session: Session, args: List[str], **kwargs):
    p = session.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    contents = []
    while p.poll() is None:
        line = p.stdout.readline()
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        contents.append(line.decode('utf-8', 'surrogateescape'))
    return p.returncode, contents


class SchrootDependencyContext(DependencyContext):

    def __init__(self, session):
        self.session = session

    def add_dependency(self, package, minimum_version=None):
        # TODO(jelmer): Handle minimum_version
        apt_install(self.session, [package])
        return True


class DetailedDistCommandFailed(Exception):

    def __init__(self, retcode, argv, error):
        self.retcode = retcode
        self.argv = argv
        self.error = error


class UnidentifiedError(Exception):

    def __init__(self, retcode, argv, lines):
        self.retcode = retcode
        self.argv = argv
        self.lines = lines


def fix_perl_module_from_cpan(error, context):
    # TODO(jelmer): Specify -T to skip tests?
    context.session.check_call(
        ['cpan', '-i', error.module], user='root',
        env={'PERL_MM_USE_DEFAULT': '1'})
    return True


GENERIC_INSTALL_FIXERS: List[
        Tuple[Type[Problem], Callable[[Problem, DependencyContext], bool]]] = [
    (MissingPerlModule, fix_perl_module_from_cpan),
]


def run_with_build_fixer(session: Session, args: List[str]):
    fixed_errors = []
    while True:
        retcode, lines = run_with_tee(session, args)
        if retcode == 0:
            return
        offset, line, error = find_build_failure_description(lines)
        if error is None:
            warning('Build failed with unidentified error. Giving up.')
            if line is not None:
                raise UnidentifiedError(retcode, args, [line])
            raise UnidentifiedError(retcode, args, lines)

        note('Identifier error: %r', error)
        if error in fixed_errors:
            warning('Failed to resolve error %r, it persisted. Giving up.',
                    error)
            raise DetailedDistCommandFailed(retcode, args, error)
        if not resolve_error(
                error, SchrootDependencyContext(session),
                fixers=(APT_FIXERS + GENERIC_INSTALL_FIXERS)):
            warning('Failed to find resolution for error %r. Giving up.',
                    error)
            raise DetailedDistCommandFailed(retcode, args, error)
        fixed_errors.append(error)


class NoBuildToolsFound(Exception):
    """No supported build tools were found."""


def run_dist_in_chroot(session):
    apt_install(session, ['git'])

    if os.path.exists('package.xml'):
        apt_install(session, ['php-pear', 'php-horde-core'])
        note('Found package.xml, assuming pear package.')
        session.check_call(['pear', 'package'])
        return

    if os.path.exists('pyproject.toml'):
        import toml
        with open('pyproject.toml', 'r') as pf:
            pyproject = toml.load(pf)
        if 'poetry' in pyproject.get('tool', []):
            note('Found pyproject.toml with poetry section, '
                 'assuming poetry project.')
            apt_install(session, ['python3-venv', 'python3-pip'])
            session.check_call(['pip3', 'install', 'poetry'], user='root')
            session.check_call(['poetry', 'build', '-f', 'sdist'])
            return

    if os.path.exists('setup.py'):
        note('Found setup.py, assuming python project.')
        apt_install(session, ['python3', 'python3-pip'])
        with open('setup.py', 'r') as f:
            setup_py_contents = f.read()
        try:
            with open('setup.cfg', 'r') as f:
                setup_cfg_contents = f.read()
        except FileNotFoundError:
            setup_cfg_contents = ''
        if 'setuptools' in setup_py_contents:
            note('Reference to setuptools found, installing.')
            apt_install(session, ['python3-setuptools'])
        if ('setuptools_scm' in setup_py_contents or
                'setuptools_scm' in setup_cfg_contents):
            note('Reference to setuptools-scm found, installing.')
            apt_install(
                session, ['python3-setuptools-scm', 'git', 'mercurial'])

        # TODO(jelmer): Install setup_requires

        if (os.stat('setup.py').st_mode & stat.S_IEXEC and
                setup_py_contents.startswith('#!')):
            apt_install(session, ['python'])
            run_with_build_fixer(session, ['./setup.py', 'sdist'])
        else:
            run_with_build_fixer(session, ['python3', './setup.py', 'sdist'])
        return

    if os.path.exists('setup.cfg'):
        note('Found setup.cfg, assuming python project.')
        apt_install(session, ['python3-pep517', 'python3-pip'])
        session.check_call(['python3', '-m', 'pep517.build', '-s', '.'])
        return

    if os.path.exists('dist.ini') and not os.path.exists('Makefile.PL'):
        apt_install(session, ['libdist-inkt-perl'])
        with open('dist.ini', 'rb') as f:
            for line in f:
                if not line.startswith(b';;'):
                    continue
                try:
                    (key, value) = line[2:].split(b'=', 1)
                except ValueError:
                    continue
                if (key.strip() == b'class' and
                        value.strip().startswith(b"'Dist::Inkt")):
                    note('Found Dist::Inkt section in dist.ini, '
                         'assuming distinkt.')
                    # TODO(jelmer): install via apt if possible
                    session.check_call(
                        ['cpan', 'install', value.decode().strip("'")],
                        user='root')
                    run_with_build_fixer(session, ['distinkt-dist'])
                    return
        # Default to invoking Dist::Zilla
        note('Found dist.ini, assuming dist-zilla.')
        apt_install(session, ['libdist-zilla-perl'])
        run_with_build_fixer(session, ['dzil', 'build', '--in', '..'])
        return

    if os.path.exists('package.json'):
        apt_install(session, ['npm'])
        run_with_build_fixer(session, ['npm', 'pack'])
        return

    gemfiles = [name for name in os.listdir('.') if name.endswith('.gem')]
    if gemfiles:
        apt_install(session, ['gem2deb'])
        if len(gemfiles) > 1:
            warning('More than one gemfile. Trying the first?')
        run_with_build_fixer(session, ['gem2tgz', gemfiles[0]])
        return

    if os.path.exists('waf'):
        apt_install(session, ['python3'])
        run_with_build_fixer(session, ['./waf', 'dist'])
        return

    if os.path.exists('Makefile.PL') and not os.path.exists('Makefile'):
        apt_install(session, ['perl'])
        run_with_build_fixer(session, ['perl', 'Makefile.PL'])

    if not os.path.exists('Makefile') and not os.path.exists('configure'):
        if os.path.exists('autogen.sh'):
            run_with_build_fixer(session, ['./autogen.sh'])
        elif os.path.exists('configure.ac') or os.path.exists('configure.in'):
            apt_install(session, [
                'autoconf', 'automake', 'gettext', 'libtool', 'gnu-standards'])
            run_with_build_fixer(session, ['autoreconf', '-i'])

    if not os.path.exists('Makefile') and os.path.exists('configure'):
        session.check_call(['./configure'])

    if os.path.exists('Makefile'):
        apt_install(session, ['make'])
        try:
            run_with_build_fixer(session, ['make', 'dist'])
        except UnidentifiedError as e:
            if "make: *** No rule to make target 'dist'.  Stop.\n" in e.lines:
                pass
            elif ("make[1]: *** No rule to make target 'dist'. Stop.\n"
                    in e.lines):
                pass
            elif ("Gnulib not yet bootstrapped; run ./bootstrap instead.\n"
                  in e.lines):
                run_with_build_fixer(session, ["./bootstrap"])
                run_with_build_fixer(session, ['make', 'dist'])
            elif ("Reconfigure the source tree "
                    "(via './config' or 'perl Configure'), please.\n"
                  ) in e.lines:
                run_with_build_fixer(session, ['perl', 'configure'])
                run_with_build_fixer(session, ['make', 'dist'])
            else:
                raise
        else:
            return

    raise NoBuildToolsFound()


def create_dist_schroot(
        tree: Tree, target_filename: str,
        chroot: str, packaging_tree: Optional[Tree] = None,
        include_controldir: bool = True,
        subdir: Optional[str] = None) -> bool:
    if subdir is None:
        subdir = 'package'
    with Session(chroot) as session:
        if packaging_tree is not None:
            satisfy_build_deps(session, packaging_tree)
        build_dir = os.path.join(session.location, 'build')

        directory = tempfile.mkdtemp(dir=build_dir)
        reldir = '/' + os.path.relpath(directory, session.location)

        export_directory = os.path.join(directory, subdir)
        if not include_controldir:
            export(tree, export_directory, 'dir', subdir)
        else:
            with tree.lock_read():
                if isinstance(tree, WorkingTree):
                    tree = tree.basis_tree()
            tree._repository.controldir.sprout(
                export_directory,
                create_tree_if_local=True,
                revision_id=tree.get_revision_id())

        existing_files = os.listdir(export_directory)

        oldcwd = os.getcwd()
        os.chdir(export_directory)
        try:
            session.chdir(os.path.join(reldir, subdir))
            run_dist_in_chroot(session)
        except NoBuildToolsFound:
            note('No build tools found, falling back to simple export.')
            return False
        finally:
            os.chdir(oldcwd)

        new_files = os.listdir(export_directory)
        diff_files = set(new_files) - set(existing_files)
        diff = set([n for n in diff_files if get_filetype(n) is not None])
        if len(diff) == 1:
            fn = diff.pop()
            note('Found tarball %s in package directory.', fn)
            shutil.copy(
                os.path.join(export_directory, fn),
                target_filename)
            return True
        if 'dist' in diff_files:
            for entry in os.scandir(os.path.join(export_directory, 'dist')):
                if get_filetype(entry.name) is not None:
                    note('Found tarball %s in dist directory.', entry.name)
                    shutil.copy(entry.path, target_filename)
                    return True
            note('No tarballs found in dist directory.')

        diff = set(os.listdir(directory)) - set([subdir])
        if len(diff) == 1:
            fn = diff.pop()
            note('Found tarball %s in parent directory.', fn)
            shutil.copy(
                os.path.join(directory, fn),
                target_filename)
            return True

        note('No tarball created :(')
        return False


if __name__ == '__main__':
    import argparse
    import breezy.bzr
    import breezy.git

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--chroot', default='unstable-amd64-sbuild', type=str,
        help='Name of chroot to use')
    parser.add_argument(
        'directory', default='.', type=str, nargs='?',
        help='Directory with upstream source.')
    parser.add_argument(
        '--packaging-directory', type=str,
        help='Path to packaging directory.')
    parser.add_argument(
        '--target-filename', type=str,
        help='Target filename')
    args = parser.parse_args()
    tree = WorkingTree.open(args.directory)
    if args.packaging_directory:
        packaging_tree = WorkingTree.open(args.packaging_directory)
        with packaging_tree.lock_read():
            source = Deb822(packaging_tree.get_file('debian/control'))
        package = source['Source']
        subdir = package
        target_filename = args.target_filename or ('%s.tar.gz' % package)
    else:
        packaging_tree = None
        target_filename = args.target_filename or 'dist.tar.gz'
        subdir = None

    ret = create_dist_schroot(
        tree, subdir=subdir, target_filename=os.path.abspath(target_filename),
        packaging_tree=packaging_tree,
        chroot=args.chroot)
    if ret:
        sys.exit(0)
    else:
        sys.exit(1)
