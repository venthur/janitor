#!/usr/bin/python3
# Copyright (C) 2018 Jelmer Vernooij <jelmer@jelmer.uk>
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

import argparse
from contextlib import contextmanager
from datetime import datetime
from debian.changelog import Changelog, Version, ChangelogCreateError
import distro_info
import json
import os
import subprocess
import sys
import traceback
from typing import Callable, Dict, List, Optional, Any, Type, Iterator, Tuple

import breezy
from breezy import osutils
from breezy.config import GlobalStack
from breezy.errors import (
    InvalidNormalization,
    NoRoundtrippingSupport,
    )
from breezy.transform import (
    MalformedTransform,
    )
from breezy.tree import Tree
from breezy.transport import Transport
from breezy.workingtree import WorkingTree

import silver_platter
from silver_platter.debian import (
    MissingUpstreamTarball,
    Workspace,
    pick_additional_colocated_branches,
)
from silver_platter.debian.lintian import (
    available_lintian_fixers,
    get_fixers,
    run_lintian_fixers,
    has_nontrivial_changes,
    DEFAULT_ADDON_FIXERS,
    DEFAULT_MINIMUM_CERTAINTY,
    calculate_value as lintian_brush_calculate_value,
)
from silver_platter.proposal import Hoster
from lintian_brush.config import Config as LintianBrushConfig
from debmutate.reformatting import GeneratedFile, FormattingUnpreservable
from silver_platter.debian.upstream import (
    import_upstream,
    merge_upstream,
    refresh_quilt_patches,
    InconsistentSourceFormatError,
    InvalidFormatUpstreamVersion,
    DistCommandFailed,
    NewUpstreamMissing,
    NewUpstreamTarballMissing,
    UnparseableChangelog,
    UpstreamAlreadyImported,
    UpstreamAlreadyMerged,
    UpstreamMergeConflicted,
    UpstreamBranchUnavailable,
    UpstreamBranchUnknown,
    PackageIsNative,
    PreviousVersionTagMissing,
    PristineTarError,
    QuiltError,
    UScanError,
    UpstreamVersionMissingInUpstreamBranch,
    UpstreamMetadataSyntaxError,
    MissingChangelogError,
    QuiltPatchPushFailure,
    update_packaging,
)

from silver_platter.utils import (
    run_pre_check,
    run_post_check,
    PreCheckFailed,
    PostCheckFailed,
    open_branch,
    BranchMissing,
    BranchUnavailable,
)

from .fix_build import build_incrementally
from .build import (
    build_once,
    MissingChangesFile,
    SbuildFailure,
)
from .trace import (
    note,
    warning,
)


# Whether to trust packages enough to run code from them,
# e.g. when guessing repo location.
TRUST_PACKAGE = False


DEFAULT_DIST_COMMAND = os.path.join(os.path.dirname(__file__), '..', 'dist.py')
DEFAULT_BUILD_COMMAND = 'sbuild -A -s -v'


class SubWorkerResult(object):

    def __init__(
            self, description: Optional[str], value: Optional[int],
            auxiliary_branches: Optional[List[str]] = None,
            tags: Optional[List[str]] = None):
        self.description = description
        self.value = value
        self.auxiliary_branches = auxiliary_branches
        self.tags = tags

    @classmethod
    def from_changer_result(cls, result):
        return cls(
            tags=result.tags,
            auxiliary_branches=result.auxiliary_branches,
            description=result.description,
            value=result.value)


class SubWorker(object):

    def __init__(self, command: List[str], env: Dict[str, str]) -> None:
        """Initialize a subworker.

        Args:
          command: List of command arguments
          env: Environment dictionary
        """

    def make_changes(self, local_tree: WorkingTree, subpath: str,
                     report_context: Callable[[str], None],
                     metadata, base_metadata) -> SubWorkerResult:
        """Make the actual changes to a tree.

        Args:
          local_tree: Tree to make changes to
          report_context: report context
          metadata: JSON Dictionary that can be used for storing results
          base_metadata: Optional JSON Dictionary with results of
            any previous runs this one is based on
          subpath: Path in the branch where the package resides
        Returns:
          SubWorkerResult
        """
        raise NotImplementedError(self.make_changes)


common_parser = argparse.ArgumentParser(add_help=False)
common_parser.add_argument(
    '--no-update-changelog', action="store_false", default=None,
    dest="update_changelog", help="do not update the changelog")
common_parser.add_argument(
    '--update-changelog', action="store_true", dest="update_changelog",
    help="force updating of the changelog", default=None)


class MultiArchHintsWorker(SubWorker):

    def __init__(self, command, env):
        self.committer = env.get('COMMITTER')
        subparser = argparse.ArgumentParser(
            prog='multiarch-fix', parents=[common_parser])
        from silver_platter.debian.multiarch import MultiArchHintsChanger
        self.changer = MultiArchHintsChanger()
        self.changer.setup_parser(subparser)
        self.args = subparser.parse_args(command)

    def make_changes(self, local_tree, subpath, report_context, metadata,
                     base_metadata):
        """Make the actual changes to a tree.

        Args:
          local_tree: Tree to make changes to
          report_context: report context
          metadata: JSON Dictionary that can be used for storing results
          base_metadata: Optional JSON Dictionary with results of
            any previous runs this one is based on
          subpath: Path in the branch where the package resides
        """
        from lintian_brush import NoChanges
        update_changelog = self.args.update_changelog
        try:
            cfg = LintianBrushConfig.from_workingtree(local_tree, subpath)
        except FileNotFoundError:
            pass
        else:
            if update_changelog is None:
                update_changelog = cfg.update_changelog()
        if control_files_in_root(local_tree, subpath):
            raise WorkerFailure(
                'control-files-in-root',
                'control files live in root rather than debian/ '
                '(LarstIQ mode)')

        try:
            with local_tree.lock_write():
                result = self.changer.make_changes(
                    local_tree, subpath, update_changelog=update_changelog,
                    committer=self.committer)
        except NoChanges:
            raise WorkerFailure('nothing-to-do', 'no hints to apply')
        except FormattingUnpreservable:
            raise WorkerFailure(
                'formatting-unpreservable',
                'unable to preserve formatting while editing')
        except GeneratedFile as e:
            raise WorkerFailure(
                'generated-file',
                'unable to edit generated file: %r' % e)

        hint_names = []
        metadata['applied-hints'] = []
        for (binary, hint, description, certainty) in result.mutator.changes:
            entry = dict(hint.items())
            hint_names.append(entry['link'].split('#')[-1])
            entry['action'] = description
            entry['certainty'] = certainty
            metadata['applied-hints'].append(entry)
            note('%s: %s' % (binary['Package'], description))
        return SubWorkerResult.from_changer_result(result=result)


class OrphanWorker(SubWorker):

    def __init__(self, command, env):
        self.committer = env.get('COMMITTER')
        subparser = argparse.ArgumentParser(
            prog='orphan', parents=[common_parser])
        from silver_platter.debian.orphan import OrphanChanger
        self.changer = OrphanChanger(salsa_push=False)
        self.changer.setup_parser(subparser)
        self.args = subparser.parse_args(command)

    def make_changes(self, local_tree, subpath, report_context, metadata,
                     base_metadata):
        """Make the actual changes to a tree.

        Args:
          local_tree: Tree to make changes to
          report_context: report context
          metadata: JSON Dictionary that can be used for storing results
          base_metadata: Optional JSON Dictionary with results of
            any previous runs this one is based on
          subpath: Path in the branch where the package resides
        """
        update_changelog = self.args.update_changelog
        try:
            cfg = LintianBrushConfig.from_workingtree(local_tree, subpath)
        except FileNotFoundError:
            pass
        else:
            if update_changelog is None:
                update_changelog = cfg.update_changelog()
        try:
            result = self.changer.make_changes(
                local_tree, subpath=subpath, update_changelog=update_changelog,
                committer=self.committer)
        except FormattingUnpreservable:
            raise WorkerFailure(
                'formatting-unpreservable',
                'unable to preserve formatting while editing')
        except GeneratedFile as e:
            raise WorkerFailure(
                'generated-file',
                'unable to edit generated file: %r' % e)
        metadata['old_vcs_url'] = result.mutator.old_vcs_url
        metadata['new_vcs_url'] = result.mutator.new_vcs_url
        metadata['pushed'] = result.mutator.pushed
        return SubWorkerResult.from_changer_result(result=result)


class CMEWorker(SubWorker):

    def __init__(self, command, env):
        self.committer = env.get('COMMITTER')
        subparser = argparse.ArgumentParser(
            prog='cme-fix', parents=[common_parser])
        from silver_platter.debian.cme import CMEChanger
        self.changer = CMEChanger(salsa_push=False)
        self.changer.setup_parser(subparser)
        self.args = subparser.parse_args(command)

    def make_changes(self, local_tree, subpath, report_context, metadata,
                     base_metadata):
        """Make the actual changes to a tree.

        Args:
          local_tree: Tree to make changes to
          report_context: report context
          metadata: JSON Dictionary that can be used for storing results
          base_metadata: Optional JSON Dictionary with results of
            any previous runs this one is based on
          subpath: Path in the branch where the package resides
        """
        update_changelog = self.args.update_changelog
        try:
            cfg = LintianBrushConfig.from_workingtree(local_tree, subpath)
        except FileNotFoundError:
            pass
        else:
            if update_changelog is None:
                update_changelog = cfg.update_changelog()
        result = self.changer.make_changes(
            local_tree, subpath=subpath, update_changelog=update_changelog,
            committer=self.committer)
        return SubWorkerResult.from_changer_result(result=result)


class LintianBrushWorker(SubWorker):
    """Janitor-specific Lintian Fixer."""

    def __init__(self, command, env):
        from lintian_brush import (
            SUPPORTED_CERTAINTIES,
            )
        self.committer = env.get('COMMITTER')
        subparser = argparse.ArgumentParser(
            prog='lintian-brush', parents=[common_parser])
        subparser.add_argument("tags", nargs='*')
        subparser.add_argument(
            '--exclude', action='append', type=str,
            help="Exclude fixer.")
        subparser.add_argument(
            '--compat-release', type=str, default=None,
            help='Oldest Debian release to be compatible with.')
        subparser.add_argument(
            '--propose-addon-only',
            help='Fixers that should be considered add-on-only.',
            type=str, action='append', default=DEFAULT_ADDON_FIXERS)
        subparser.add_argument(
            '--allow-reformatting', default=None, action='store_true',
            help='Whether to allow reformatting.')
        subparser.add_argument(
            '--minimum-certainty',
            type=str,
            choices=SUPPORTED_CERTAINTIES,
            default=None,
            help=argparse.SUPPRESS)
        self.args = subparser.parse_args(command)

    def make_changes(self, local_tree, subpath, report_context, metadata,
                     base_metadata):
        from lintian_brush import (
            version_string as lintian_brush_version_string,
            )
        fixers = get_fixers(
            available_lintian_fixers(), tags=self.args.tags,
            exclude=self.args.exclude)

        compat_release = self.args.compat_release
        allow_reformatting = self.args.allow_reformatting
        minimum_certainty = self.args.minimum_certainty
        try:
            cfg = LintianBrushConfig.from_workingtree(local_tree, '')
        except FileNotFoundError:
            pass
        else:
            if compat_release is None:
                compat_release = cfg.compat_release()
            allow_reformatting = cfg.allow_reformatting()
            minimum_certainty = cfg.minimum_certainty()
        if compat_release is None:
            compat_release = debian_info.stable()
        if allow_reformatting is None:
            allow_reformatting = False
        if minimum_certainty is None:
            minimum_certainty = DEFAULT_MINIMUM_CERTAINTY

        with local_tree.lock_write():
            if control_files_in_root(local_tree, subpath):
                raise WorkerFailure(
                    'control-files-in-root',
                    'control files live in root rather than debian/ '
                    '(LarstIQ mode)')

            try:
                overall_result = run_lintian_fixers(
                        local_tree, fixers,
                        committer=self.committer,
                        update_changelog=self.args.update_changelog,
                        compat_release=compat_release,
                        minimum_certainty=minimum_certainty,
                        allow_reformatting=allow_reformatting,
                        trust_package=TRUST_PACKAGE,
                        net_access=True, subpath=(subpath or '.'),
                        opinionated=False,
                        diligence=10)
            except ChangelogCreateError as e:
                raise WorkerFailure(
                    'changelog-create-error',
                    'Error creating changelog entry: %s' % e)

        if overall_result.failed_fixers:
            for fixer_name, failure in overall_result.failed_fixers.items():
                note('Fixer %r failed to run:', fixer_name)
                sys.stderr.write(failure.errors)

        metadata['versions'] = {
            'lintian-brush': lintian_brush_version_string,
            'silver-platter': silver_platter.version_string,
            'breezy': breezy.version_string,
            }
        metadata['applied'] = []
        if base_metadata:
            metadata['applied'].extend(base_metadata['applied'])
        for result, summary in overall_result.success:
            metadata['applied'].append({
                'summary': summary,
                'description': result.description,
                'fixed_lintian_tags': result.fixed_lintian_tags,
                'revision_id': result.revision_id.decode('utf-8'),
                'certainty': result.certainty})
        metadata['failed'] = {
            name: e.errors
            for (name, e) in overall_result.failed_fixers.items()}
        metadata['add_on_only'] = not has_nontrivial_changes(
            overall_result.success, self.args.propose_addon_only)
        if base_metadata and not base_metadata['add_on_only']:
            metadata['add_on_only'] = False

        if not overall_result.success:
            raise WorkerFailure('nothing-to-do', 'no fixers to apply')

        tags = set()
        for entry in metadata['applied']:
            tags.update(entry['fixed_lintian_tags'])
        value = lintian_brush_calculate_value(tags)
        return SubWorkerResult(
            description='Applied fixes for %r' % tags,
            value=value, tags=[])


class NewUpstreamWorker(SubWorker):

    def __init__(self, command, env):
        self.committer = env.get('COMMITTER')
        subparser = argparse.ArgumentParser(
            prog='new-upstream', parents=[common_parser])
        subparser.add_argument(
            '--chroot', type=str, help="Name of chroot",
            default=os.environ.get('CHROOT'))
        subparser.add_argument(
            '--snapshot',
            help='Merge a new upstream snapshot rather than a release',
            action='store_true')
        subparser.add_argument(
            '--import-only', action='store_true',
            help='Only import new version, do not merge into packaging.')
        self.args = subparser.parse_args(command)

    def make_changes(self, local_tree, subpath, report_context, metadata,
                     base_metadata):
        from janitor.dist import (
            create_dist_schroot,
            DetailedDistCommandFailed,
            UnidentifiedError,
            )
        with local_tree.lock_write():
            if control_files_in_root(local_tree, subpath):
                raise WorkerFailure(
                    'control-files-in-root',
                    'control files live in root rather than debian/ '
                    '(LarstIQ mode)')

            def create_dist(tree, package, version, target_filename):
                try:
                    return create_dist_schroot(
                        tree, subdir=package, target_filename=target_filename,
                        packaging_tree=local_tree, chroot=self.args.chroot)
                except DetailedDistCommandFailed:
                    raise
                except UnidentifiedError as e:
                    traceback.print_exc()
                    lines = [line for line in e.lines if line]
                    if len(lines) == 1:
                        raise DistCommandFailed(
                            'command %r failed: %s' % (e.argv, lines[0]))
                    else:
                        raise DistCommandFailed(
                            'command %r failed with unidentified error '
                            '(return code %d)' % (e.argv, e.retcode))
                except Exception as e:
                    traceback.print_exc()
                    raise DistCommandFailed(str(e))

            try:
                if self.args.import_only:
                    result = import_upstream(
                        tree=local_tree, subpath=(subpath or ''),
                        snapshot=self.args.snapshot, committer=self.committer,
                        trust_package=TRUST_PACKAGE,
                        create_dist=create_dist)
                else:
                    result = merge_upstream(
                        tree=local_tree, subpath=(subpath or ''),
                        snapshot=self.args.snapshot, committer=self.committer,
                        trust_package=TRUST_PACKAGE,
                        create_dist=create_dist)
            except UpstreamAlreadyImported as e:
                report_context(e.version)
                metadata['upstream_version'] = e.version
                error_description = (
                    "Upstream version %s already imported." % (e.version))
                raise WorkerFailure('nothing-to-do', error_description)
            except UpstreamAlreadyMerged as e:
                error_description = (
                    "Last upstream version %s already merged." % e.version)
                error_code = 'nothing-to-do'
                report_context(e.version)
                metadata['upstream_version'] = e.version
                raise WorkerFailure(error_code, error_description)
            except NewUpstreamMissing:
                error_description = "Unable to find new upstream source."
                error_code = 'new-upstream-missing'
                raise WorkerFailure(error_code, error_description)
            except NewUpstreamTarballMissing as e:
                error_code = 'new-upstream-tarball-missing'
                error_description = (
                    'New upstream version (%s/%s) found, but was missing '
                    'when retrieved as tarball from %r.' % (
                        e.package, e.version, e.upstream))
                report_context(e.version)
                metadata['upstream_version'] = e.version
                raise WorkerFailure(error_code, error_description)
            except UpstreamBranchUnavailable as e:
                error_description = (
                    "The upstream branch at %s was unavailable: %s" % (
                        e.location, e.error))
                error_code = 'upstream-branch-unavailable'
                if 'Fossil branches are not yet supported' in str(e.error):
                    error_code = 'upstream-unsupported-vcs-fossil'
                if 'Mercurial branches are not yet supported.' in str(e.error):
                    error_code = 'upstream-unsupported-vcs-hg'
                if 'Subversion branches are not yet supported.' in str(
                        e.error):
                    error_code = 'upstream-unsupported-vcs-svn'
                if 'Darcs branches are not yet supported' in str(e.error):
                    error_code = 'upstream-unsupported-vcs-darcs'
                if 'Unsupported protocol for url' in str(e.error):
                    if 'svn://' in str(e.error):
                        error_code = 'upstream-unsupported-vcs-svn'
                    elif 'cvs+pserver://' in str(e.error):
                        error_code = 'upstream-unsupported-vcs-cvs'
                    else:
                        error_code = 'upstream-unsupported-vcs'
                raise WorkerFailure(error_code, error_description)
            except UpstreamMergeConflicted as e:
                error_description = "Upstream version %s conflicted." % (
                    e.version)
                error_code = 'upstream-merged-conflicts'
                report_context(e.version)
                metadata['upstream_version'] = e.version
                metadata['conflicts'] = e.conflicts
                raise WorkerFailure(error_code, error_description)
            except PreviousVersionTagMissing as e:
                error_description = (
                     "Previous upstream version %s missing (tag: %s)" %
                     (e.version, e.tag_name))
                error_code = 'previous-upstream-missing'
                raise WorkerFailure(error_code, error_description)
            except PristineTarError as e:
                error_description = ('Error from pristine-tar: %s' % e)
                error_code = 'pristine-tar-error'
                raise WorkerFailure(error_code, error_description)
            except UpstreamBranchUnknown:
                error_description = (
                    'The location of the upstream branch is unknown.')
                error_code = 'upstream-branch-unknown'
                raise WorkerFailure(error_code, error_description)
            except PackageIsNative:
                error_description = (
                    'Package is native; unable to merge upstream.')
                error_code = 'native-package'
                raise WorkerFailure(error_code, error_description)
            except NoRoundtrippingSupport:
                error_description = (
                    'Unable to import upstream repository into '
                    'packaging repository.')
                error_code = 'roundtripping-error'
                raise WorkerFailure(error_code, error_description)
            except MalformedTransform:
                error_description = (
                    'Malformed tree transform during new upstream merge')
                error_code = 'malformed-transform'
                raise WorkerFailure(error_code, error_description)
            except InconsistentSourceFormatError as e:
                error_description = str(e)
                error_code = 'inconsistent-source-format'
                raise WorkerFailure(error_code, error_description)
            except InvalidFormatUpstreamVersion as e:
                error_description = (
                        'Invalid format upstream version: %r' %
                        e.version)
                error_code = 'invalid-upstream-version-format'
                raise WorkerFailure(error_code, error_description)
            except UnparseableChangelog as e:
                error_description = str(e)
                error_code = 'unparseable-changelog'
                raise WorkerFailure(error_code, error_description)
            except UScanError as e:
                error_description = str(e)
                if e.errors == 'OpenPGP signature did not verify.':
                    error_code = 'upstream-pgp-signature-verification-failed'
                else:
                    error_code = 'uscan-error'
                raise WorkerFailure(error_code, error_description)
            except UpstreamVersionMissingInUpstreamBranch as e:
                error_description = (
                    'Upstream version %s not in upstream branch %r' % (
                        e.version, e.branch))
                error_code = 'upstream-version-missing-in-upstream-branch'
                raise WorkerFailure(error_code, error_description)
            except UpstreamMetadataSyntaxError as e:
                error_description = 'Syntax error in upstream metadata: %s' % (
                        e.error)
                error_code = 'upstream-metadata-syntax-error'
                raise WorkerFailure(error_code, error_description)
            except DistCommandFailed as e:
                error_description = str(e)
                error_code = 'dist-command-failed'
                raise WorkerFailure(error_code, error_description)
            except DetailedDistCommandFailed as e:
                error_code = 'dist-' + e.error.kind
                error_description = str(e.error)
                raise WorkerFailure(error_code, error_description)
            except MissingChangelogError as e:
                error_description = str(e)
                error_code = 'missing-changelog'
                raise WorkerFailure(error_code, error_description)
            except MissingUpstreamTarball as e:
                error_description = str(e)
                error_code = 'missing-upstream-tarball'
                raise WorkerFailure(error_code, error_description)
            except InvalidNormalization as e:
                error_description = str(e)
                error_code = 'invalid-path-normalization'
                raise WorkerFailure(error_code, error_description)

            report_context(result.new_upstream_version)

            if not self.args.import_only:
                patch_series_path = 'debian/patches/series'
                if subpath not in (None, '', '.'):
                    patch_series_path = os.path.join(
                        subpath, patch_series_path)

                if local_tree.has_filename(patch_series_path):
                    try:
                        refresh_quilt_patches(
                            local_tree,
                            old_version=result.old_upstream_version,
                            new_version=result.new_upstream_version,
                            committer=self.committer,
                            subpath=subpath)
                    except QuiltError as e:
                        error_description = (
                            "An error (%d) occurred refreshing quilt patches: "
                            "%s%s" % (e.retcode, e.stderr, e.extra))
                        error_code = 'quilt-refresh-error'
                        raise WorkerFailure(error_code, error_description)
                    except QuiltPatchPushFailure as e:
                        error_description = (
                            "An error occurred refreshing quilt patch %s: %s"
                            % (e.patch_name, e.actual_error.extra))
                        error_code = 'quilt-refresh-error'
                        raise WorkerFailure(error_code, error_description)

                old_tree = local_tree.branch.repository.revision_tree(
                    result.old_revision)
                metadata['notes'] = update_packaging(local_tree, old_tree)

            metadata['old_upstream_version'] = result.old_upstream_version
            metadata['upstream_version'] = result.new_upstream_version
            if result.upstream_branch:
                metadata['upstream_branch_url'] = (
                    result.upstream_branch.user_url)
                metadata['upstream_branch_browse'] = (
                    result.upstream_branch_browse)
        if self.args.import_only:
            return SubWorkerResult(
                description="Imported new upstream version %s" % (
                    result.new_upstream_version),
                value=None,
                tags=['upstream/%s' % result.new_upstream_version])
        else:
            return SubWorkerResult(
                description="Merged new upstream version %s" % (
                    result.new_upstream_version),
                value=None,
                tags=['upstream/%s' % result.new_upstream_version])


class JustBuildWorker(SubWorker):

    def __init__(self, command, env):
        subparser = argparse.ArgumentParser(
            prog='just-build', parents=[common_parser])
        subparser.add_argument(
            '--revision', type=str,
            help='Specific revision to build.')
        self.args = subparser.parse_args(command)

    def make_changes(self, local_tree, subpath, report_context, metadata,
                     base_metadata):
        if self.args.revision:
            local_tree.update(revision=self.args.revision.encode('utf-8'))
        if control_files_in_root(local_tree, subpath):
            raise WorkerFailure(
                'control-files-in-root',
                'control files live in root rather than debian/ '
                '(LarstIQ mode)')
        return SubWorkerResult(None, None)


class UncommittedWorker(SubWorker):

    def __init__(self, command, env):
        self.committer = env.get('COMMITTER')
        from silver_platter.debian.uncommitted import UncommittedChanger
        self.changer = UncommittedChanger()
        subparser = argparse.ArgumentParser(
            prog='import-upload', parents=[common_parser])
        self.changer.setup_parser(subparser)
        self.args = subparser.parse_args(command)

    def make_changes(self, local_tree, subpath, report_context, metadata,
                     base_metadata):
        from silver_platter.debian.uncommitted import (
            NoMissingVersions,
            TreeUpstreamVersionMissing,
            TreeVersionNotInArchiveChangelog,
            )
        try:
            result = self.changer.make_changes(
                local_tree, subpath=subpath, committer=self.committer,
                update_changelog=False)
        except NoMissingVersions as e:
            raise WorkerFailure('nothing-to-do', str(e))
        except TreeUpstreamVersionMissing as e:
            raise WorkerFailure('tree-upstream-version-missing', str(e))
        except TreeVersionNotInArchiveChangelog as e:
            raise WorkerFailure(
                'tree-version-not-in-archive-changelog', str(e))

        metadata['tags'] = [
            (tag_name, str(version))
            for (tag_name, version) in result.mutator]
        return SubWorkerResult.from_changer_result(result=result)


class WorkerResult(object):

    def __init__(
            self, description: Optional[str],
            value: Optional[int],
            changes_filename: Optional[str] = None) -> None:
        self.description = description
        self.value = value
        self.changes_filename = changes_filename


class WorkerFailure(Exception):
    """Worker processing failed."""

    def __init__(self, code: str, description: str) -> None:
        self.code = code
        self.description = description


def tree_set_changelog_version(
        tree: WorkingTree, build_version: Version, subpath: str) -> None:
    cl_path = osutils.pathjoin(subpath, 'debian/changelog')
    with tree.get_file(cl_path) as f:
        cl = Changelog(f)
    if Version(str(cl.version) + '~') > build_version:
        return
    cl.set_version(build_version)
    with open(tree.abspath(cl_path), 'w') as f:
        cl.write_to_open_file(f)


debian_info = distro_info.DebianDistroInfo()


def control_files_in_root(tree: Tree, subpath: str) -> bool:
    debian_path = 'debian'
    if subpath not in (None, '', '.'):
        debian_path = os.path.join(subpath, 'debian')
    if tree.has_filename(debian_path):
        return False
    control_path = 'control'
    if subpath not in (None, '', '.'):
        control_path = os.path.join(subpath, control_path)
    if tree.has_filename(control_path):
        return True
    if tree.has_filename(control_path + '.in'):
        return True
    return False


def control_file_present(tree: Tree, subpath: str) -> bool:
    """Check whether there are any control files present in a tree.

    Args:
      tree: Tree to check
      subpath: subpath to check
    Returns:
      whether control file is present
    """
    for name in ['debian/control', 'debian/control.in', 'control',
                 'control.in']:
        if subpath not in ('', '.'):
            name = os.path.join(subpath, name)
        if tree.has_filename(name):
            return True
    return False


@contextmanager
def process_package(vcs_url: str, subpath: str, env: Dict[str, str],
                    command: List[str], output_directory: str,
                    metadata: Any, build_command: Optional[str] = None,
                    pre_check_command: Optional[str] = None,
                    post_check_command: Optional[str] = None,
                    possible_transports: Optional[List[Transport]] = None,
                    possible_hosters: Optional[List[Hoster]] = None,
                    resume_branch_url: Optional[str] = None,
                    cached_branch_url: Optional[str] = None,
                    last_build_version: Optional[Version] = None,
                    build_distribution: Optional[str] = None,
                    build_suffix: Optional[str] = None,
                    resume_subworker_result: Any = None
                    ) -> Iterator[Tuple[Workspace, WorkerResult]]:
    pkg = env['PACKAGE']

    metadata['command'] = command

    subworker_cls: Type[SubWorker]
    # TODO(jelmer): sort out this mess:
    if command[0] == 'lintian-brush':
        subworker_cls = LintianBrushWorker
    elif command[0] == 'new-upstream':
        subworker_cls = NewUpstreamWorker
    elif command[0] == 'just-build':
        subworker_cls = JustBuildWorker
    elif command[0] == 'apply-multiarch-hints':
        subworker_cls = MultiArchHintsWorker
    elif command[0] == 'orphan':
        subworker_cls = OrphanWorker
    elif command[0] == 'import-upload':
        subworker_cls = UncommittedWorker
    elif command[0] == 'cme-fix':
        subworker_cls = CMEWorker
    else:
        raise WorkerFailure(
            'unknown-subcommand',
            'unknown subcommand %s' % command[0])
    subworker = subworker_cls(command[1:], env)

    note('Opening branch at %s', vcs_url)
    try:
        main_branch = open_branch(
            vcs_url, possible_transports=possible_transports)
    except BranchUnavailable as e:
        if e.url in str(e):
            msg = str(e)
        else:
            msg = '%s: %s' % (str(e), e.url)
        raise WorkerFailure(
            'worker-branch-unavailable', msg)
    except BranchMissing as e:
        raise WorkerFailure('worker-branch-missing', str(e))

    if cached_branch_url:
        try:
            cached_branch = open_branch(
                cached_branch_url,
                possible_transports=possible_transports)
        except BranchMissing as e:
            note('Cached branch URL %s missing: %s', cached_branch_url, e)
            cached_branch = None
        except BranchUnavailable as e:
            warning('Cached branch URL %s unavailable: %s',
                    cached_branch_url, e)
            cached_branch = None
        else:
            note('Using cached branch %s', cached_branch.user_url)
    else:
        cached_branch = None

    if resume_branch_url:
        try:
            resume_branch = open_branch(
                resume_branch_url,
                possible_transports=possible_transports)
        except BranchUnavailable as e:
            raise WorkerFailure('worker-resume-branch-unavailable', str(e))
        except BranchMissing as e:
            raise WorkerFailure('worker-resume-branch-missing', str(e))
        else:
            note('Resuming from branch %s', resume_branch.user_url)
    else:
        resume_branch = None

    with Workspace(
            main_branch, resume_branch=resume_branch,
            cached_branch=cached_branch,
            path=os.path.join(output_directory, pkg),
            additional_colocated_branches=(
                pick_additional_colocated_branches(main_branch))) as ws:
        if ws.local_tree.has_changes():
            if list(ws.local_tree.iter_references()):
                raise WorkerFailure(
                    'requires-nested-tree-support',
                    'Missing support for nested trees in Breezy.')
            raise AssertionError

        metadata['revision'] = metadata['main_branch_revision'] = (
            ws.main_branch.last_revision().decode())

        if not control_file_present(ws.local_tree, subpath):
            raise WorkerFailure(
                'missing-control-file',
                'missing control file: debian/control')

        try:
            run_pre_check(ws.local_tree, pre_check_command)
        except PreCheckFailed as e:
            raise WorkerFailure('pre-check-failed', str(e))

        metadata['subworker'] = {}

        def provide_context(c):
            metadata['context'] = c

        if ws.resume_branch is None:
            # If the resume branch was discarded for whatever reason, then we
            # don't need to pass in the subworker result.
            resume_subworker_result = None

        try:
            subworker_result = subworker.make_changes(
                ws.local_tree, subpath, provide_context, metadata['subworker'],
                resume_subworker_result)
        except WorkerFailure as e:
            if (e.code == 'nothing-to-do' and
                    resume_subworker_result is not None):
                e = WorkerFailure('nothing-new-to-do', e.description)
                raise e
            else:
                raise
        finally:
            metadata['revision'] = (
                ws.local_tree.branch.last_revision().decode())

        if command[0] != 'just-build':
            if not ws.changes_since_main():
                raise WorkerFailure('nothing-to-do', 'Nothing to do.')

            if ws.resume_branch and not ws.changes_since_resume():
                raise WorkerFailure('nothing-to-do', 'Nothing new to do.')

        try:
            run_post_check(ws.local_tree, post_check_command, ws.orig_revid)
        except PostCheckFailed as e:
            raise WorkerFailure('post-check-failed', str(e))

        if build_command:
            if last_build_version:
                # Update the changelog entry with the previous build version;
                # This allows us to upload incremented versions for subsequent
                # runs.
                tree_set_changelog_version(
                    ws.local_tree, last_build_version, subpath)

            source_date_epoch = ws.local_tree.branch.repository.get_revision(
                ws.main_branch.last_revision()).timestamp
            try:
                if not build_suffix:
                    (changes_name, cl_version) = build_once(
                        ws.local_tree, build_distribution, output_directory,
                        build_command, subpath=subpath,
                        source_date_epoch=source_date_epoch)
                else:
                    (changes_name, cl_version) = build_incrementally(
                        ws.local_tree, '~' + build_suffix,
                        build_distribution, output_directory,
                        build_command, committer=env.get('COMMITTER'),
                        subpath=subpath, source_date_epoch=source_date_epoch)
            except MissingUpstreamTarball:
                raise WorkerFailure(
                    'build-missing-upstream-source',
                    'unable to find upstream source')
            except MissingChangesFile as e:
                raise WorkerFailure(
                    'build-missing-changes',
                    'Expected changes path %s does not exist.' % e.filename)
            except SbuildFailure as e:
                if e.error is not None:
                    if e.stage:
                        code = '%s-%s' % (e.stage, e.error.kind)
                    else:
                        code = e.error.kind
                elif e.stage is not None:
                    code = 'build-failed-stage-%s' % e.stage
                else:
                    code = 'build-failed'
                raise WorkerFailure(code, e.description)
            note('Built %s', changes_name)
        else:
            changes_name = None

        yield ws, WorkerResult(
            subworker_result.description, subworker_result.value,
            changes_filename=changes_name)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='janitor-worker',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--output-directory', type=str,
        help='Output directory', default='.')
    parser.add_argument(
        '--branch-url', type=str,
        help='URL of branch to build.')
    parser.add_argument(
        '--resume-branch-url', type=str,
        help='URL of resume branch to continue on (if any).')
    parser.add_argument(
        '--resume-result-path', type=str,
        help=('Path to a JSON file with the results for '
              'the last run on the resumed branch.'))
    parser.add_argument(
        '--last-build-version', type=str,
        help='Version of the last built Debian package.')
    parser.add_argument(
        '--cached-branch-url', type=str,
        help='URL of cached branch to start from.')
    parser.add_argument(
        '--pre-check',
        help='Command to run to check whether to process package.',
        type=str)
    parser.add_argument(
        '--post-check',
        help='Command to run to check package before pushing.',
        type=str, default=None)
    parser.add_argument(
        '--subpath', type=str,
        help='Path in the branch under which the package lives.',
        default='')
    parser.add_argument(
        '--build-command',
        help='Build package to verify it.', type=str,
        default=DEFAULT_BUILD_COMMAND)
    parser.add_argument(
        '--tgz-repo',
        help='Whether to create a tgz of the VCS repo.',
        action='store_true')
    parser.add_argument(
        '--build-distribution', type=str, help='Build distribution.')
    parser.add_argument('--build-suffix', type=str, help='Build suffix.')

    parser.add_argument('command', nargs=argparse.REMAINDER)

    args = parser.parse_args(argv)
    if args.branch_url is None:
        parser.print_usage()
        return 1

    output_directory = os.path.abspath(args.output_directory)

    global_config = GlobalStack()
    global_config.set('branch.fetch_tags', True)

    if args.resume_result_path:
        with open(args.resume_result_path, 'r') as f:
            resume_subworker_result = json.load(f)
    else:
        resume_subworker_result = None

    metadata = {}
    start_time = datetime.now()
    metadata['start_time'] = start_time.isoformat()
    try:
        with process_package(
                args.branch_url, args.subpath, os.environ,
                args.command, output_directory, metadata,
                build_command=args.build_command,
                pre_check_command=args.pre_check,
                post_check_command=args.post_check,
                resume_branch_url=args.resume_branch_url,
                cached_branch_url=args.cached_branch_url,
                build_distribution=args.build_distribution,
                build_suffix=args.build_suffix,
                last_build_version=args.last_build_version,
                resume_subworker_result=resume_subworker_result
                ) as (ws, result):
            if args.tgz_repo:
                subprocess.check_call(
                    ['tar', 'czf', os.environ['PACKAGE'] + '.tgz',
                     os.environ['PACKAGE']],
                    cwd=output_directory)
            else:
                ws.defer_destroy()
    except WorkerFailure as e:
        metadata['code'] = e.code
        metadata['description'] = e.description
        note('Worker failed (%s): %s', e.code, e.description)
        return 0
    except BaseException as e:
        metadata['code'] = 'worker-exception'
        metadata['description'] = str(e)
        raise
    else:
        metadata['code'] = None
        metadata['value'] = result.value
        metadata['description'] = result.description
        note('%s', result.description)
        if result.changes_filename is not None:
            note('Built %s.', result.changes_filename)
        return 0
    finally:
        finish_time = datetime.now()
        note('Elapsed time: %s', finish_time - start_time)
        with open(os.path.join(output_directory, 'result.json'), 'w') as f:
            json.dump(metadata, f, indent=2)


if __name__ == '__main__':
    sys.exit(main())
